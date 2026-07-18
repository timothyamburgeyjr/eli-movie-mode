"""Plex sessions API poller with transition detection.

Polls `/status/sessions` every N seconds, normalizes playback state, and emits
events to registered listeners:

    Events:
        play          — playback active (first detection or resumed)
        pause         — playback paused
        progress      — same media, same state, offset advanced
        media_change  — different ratingKey than previous state
        stop          — playback ended or session gone
        unreachable   — Plex API fetch failed
        reachable     — Plex API recovered after an unreachable interval

Each payload is a dict with normalized keys; see `_parse_meta`.
"""
import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

import httpx

from config import settings

log = logging.getLogger(__name__)

Listener = Callable[[str, dict], Awaitable[None]]

UNREACHABLE_SLEEP = 30.0

# A marathon is ~4 films; 8 is generous and bounded.
META_CACHE_MAX = 8
# Plex returns 30+ roles. Billing order means the tail is spear-carriers who are never
# on screen long enough to react to, and every name is prompt tokens.
CAST_LIMIT = 12


class PlexMonitor:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._listeners: list[Listener] = []
        self._last: dict[str, Any] = {}
        self._unreachable = False
        # WHICH PLAYER ARE WE FOLLOWING. Sticky, so a second person starting something in
        # another room can't yank the app off Tim's film mid-scene.
        self._player: Optional[str] = None
        # Tim's explicit choice, when he's made one. Outranks every heuristic below —
        # the whole point of asking him is that his answer is final.
        self._pinned: Optional[str] = None
        # Everything genuinely on offer (one per player, ghosts already dropped). The UI
        # asks him to choose when this has more than one thing in it.
        self._candidates: list[dict[str, Any]] = []
        # /library/metadata responses, keyed by ratingKey. THE SESSION PAYLOAD IS THIN:
        # no Part.key, and no Role[]. This endpoint has both — and we used to fetch it,
        # take the single field we wanted, and throw the ground-truth cast on the floor.
        # Memoized because it's immutable per film and we poll every few seconds.
        self._meta_cache: dict[str, dict] = {}

    # ─── Listener API ─────────────────────────────────────────────
    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def current_state(self) -> dict[str, Any]:
        return dict(self._last)

    def candidates(self) -> list[dict[str, Any]]:
        """Everything Plex is offering, one per player, ghosts removed.

        More than one means we are GUESSING, and Tim should be asked instead.
        """
        return [dict(c) for c in self._candidates]

    def pinned(self) -> Optional[str]:
        return self._pinned

    def pin(self, rating_key: Optional[str]) -> None:
        """Tim has told us what he is watching. That settles it.

        Passing None un-pins and hands the choice back to the heuristics.
        """
        self._pinned = str(rating_key) if rating_key else None
        log.info("plex: pinned to rating_key=%r", self._pinned)

    def is_unreachable(self) -> bool:
        return self._unreachable

    # ─── Lifecycle ────────────────────────────────────────────────
    async def start(self) -> None:
        if self._task is not None:
            return
        self._client = httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            verify=settings.plex_verify_ssl,
        )
        self._task = asyncio.create_task(self._loop(), name="plex-monitor")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ─── Internals ────────────────────────────────────────────────
    async def _loop(self) -> None:
        while True:
            try:
                data = await self._fetch_sessions()
                if data is None:
                    await self._mark_unreachable()
                    await asyncio.sleep(UNREACHABLE_SLEEP)
                    continue
                if self._unreachable:
                    self._unreachable = False
                    await self._emit("reachable", {})
                self._candidates = _candidates(data)
                active = _extract_active(
                    data, prefer_player=self._player, pinned=self._pinned
                )
                if active:
                    self._player = active.get("player_id") or self._player
                await self._handle(active)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("plex monitor loop error")
            await asyncio.sleep(settings.plex_poll_interval_seconds)

    async def _fetch_sessions(self) -> Optional[dict]:
        if self._client is None:
            return None
        if not settings.plex_url or not settings.plex_token:
            return None
        url = f"{settings.plex_url.rstrip('/')}/status/sessions"
        headers = {"Accept": "application/json", "X-Plex-Token": settings.plex_token}
        try:
            r = await self._client.get(url, headers=headers)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            log.debug("plex poll failed: %s", e)
            return None
        except ValueError as e:
            log.debug("plex returned non-JSON: %s", e)
            return None

    async def _fetch_library_metadata(self, rating_key: str) -> Optional[dict]:
        """One GET of /library/metadata/{key}, memoized. Returns Metadata[0] or None.

        Both the Part.key and the cast come from here, so the fetch is shared rather
        than made twice. FAILURES ARE NOT CACHED: if Plex happens to be restarting the
        second we first see a film, that film must not be permanently cast-less — we
        return None and try again on the next poll.
        """
        if self._client is None or not rating_key:
            return None
        key = str(rating_key)
        cached = self._meta_cache.get(key)
        if cached is not None:
            return cached
        url = f"{settings.plex_url.rstrip('/')}/library/metadata/{key}"
        headers = {"Accept": "application/json", "X-Plex-Token": settings.plex_token}
        try:
            r = await self._client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.debug("library metadata fetch failed for %s: %s", key, e)
            return None
        metas = (data.get("MediaContainer") or {}).get("Metadata") or []
        if not metas or not isinstance(metas[0], dict):
            return None
        if len(self._meta_cache) >= META_CACHE_MAX:
            self._meta_cache.pop(next(iter(self._meta_cache)))   # oldest insert evicted
        self._meta_cache[key] = metas[0]
        return metas[0]

    async def _fetch_library_part_key(self, rating_key: str) -> Optional[str]:
        """Plex /status/sessions omits Part.key — read it from library metadata."""
        meta = await self._fetch_library_metadata(rating_key)
        if not meta:
            return None
        media = meta.get("Media") or []
        if not media:
            return None
        parts = media[0].get("Part") or []
        if not parts:
            return None
        return parts[0].get("key")

    async def _fetch_library_cast(
        self, rating_key: str, show_rating_key: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """The billed cast for this item. Empty list when Plex has none, or is down.

        TV: which level carries Role[] depends on the agent. The newer Plex agents put
        the SERIES regulars on the show and only guest stars on the episode; older ones
        leave the episode bare. So try the episode first (the more specific answer when
        it's there), then fall back to the show. Without the fallback, TV gets no cast.
        """
        cast = _parse_roles(await self._fetch_library_metadata(rating_key))
        if not cast and show_rating_key:
            cast = _parse_roles(await self._fetch_library_metadata(show_rating_key))
        return cast

    async def _mark_unreachable(self) -> None:
        if not self._unreachable:
            self._unreachable = True
            await self._emit("unreachable", {})

    async def _handle(self, active: Optional[dict]) -> None:
        last = self._last

        if active is None:
            if last:
                self._last = {}
                await self._emit("stop", last)
            return

        rating_key = active["rating_key"]
        state = active["state"]

        # Session responses omit Part.key AND Role[]; both come from library metadata.
        # Reuse the previous values if we're still on the same media.
        same_media = last.get("rating_key") == rating_key
        if not active.get("part_key"):
            if same_media and last.get("part_key"):
                active["part_key"] = last["part_key"]
            else:
                active["part_key"] = await self._fetch_library_part_key(rating_key)

        # THE CAST IS FETCHED INDEPENDENTLY OF part_key. Direct-play sessions sometimes
        # carry Part.key already, which short-circuits the branch above — and the cast
        # would then never be fetched for exactly those films. Cheap either way: the
        # metadata is memoized, so a cast-less film costs dict lookups, not HTTP.
        if same_media and last.get("cast"):
            active["cast"] = last["cast"]
        else:
            active["cast"] = await self._fetch_library_cast(
                rating_key, active.get("grandparent_rating_key")
            )
            # The likeliest failure here is an empty cast everywhere that nobody
            # notices, because every consumer degrades silently to its old behaviour.
            log.info("plex: cast for %s -> %d names", rating_key, len(active["cast"]))

        if not last:
            self._last = active
            await self._emit("media_change", active)
            await self._emit(state, active)
        elif last.get("rating_key") != rating_key:
            self._last = active
            await self._emit("media_change", active)
            await self._emit(state, active)
        else:
            prev_state = last.get("state")
            self._last = active
            if prev_state != state:
                await self._emit(state, active)
            else:
                await self._emit("progress", active)

    async def _emit(self, event: str, data: dict) -> None:
        for fn in list(self._listeners):
            try:
                await fn(event, data)
            except Exception:
                log.exception("plex listener %s failed on %s", fn, event)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────
def _recency(m: dict) -> int:
    """When was this session last actually touched."""
    for k in ("lastViewedAt", "updatedAt", "addedAt"):
        try:
            v = int(m.get(k) or 0)
        except (TypeError, ValueError):
            v = 0
        if v:
            return v
    return 0


def _player_id(m: dict) -> str:
    p = m.get("Player") or {}
    return str(p.get("machineIdentifier") or p.get("device") or p.get("title") or "?")


def _is_playing(m: dict) -> bool:
    return ((m.get("Player") or {}).get("state") or "").lower() == "playing"


def _candidates(data: dict) -> list[dict]:
    """Everything genuinely on offer — one per player, most recent, ghosts dropped."""
    mc = data.get("MediaContainer", {}) if isinstance(data, dict) else {}
    metas = [m for m in (mc.get("Metadata") or []) if isinstance(m, dict)]
    newest: dict[str, dict] = {}
    for m in metas:
        pid = _player_id(m)
        if pid not in newest or _recency(m) > _recency(newest[pid]):
            newest[pid] = m
    out = sorted(newest.values(), key=lambda m: (0 if _is_playing(m) else 1, -_recency(m)))
    return [_parse_meta(m) for m in out]


def _extract_active(
    data: dict,
    prefer_player: Optional[str] = None,
    pinned: Optional[str] = None,
) -> Optional[dict]:
    """Which of Plex's sessions is the one Tim is actually watching.

    THIS USED TO BE `metas[0]` — whichever Plex happened to list first.

    Plex Web leaves GHOST SESSIONS behind. Switch episodes and the old one lingers in
    /status/sessions with the same machineIdentifier, the same user, the same device — and
    frequently listed FIRST. So the app latched onto the episode Tim had already left,
    never saw the rating_key change, and never fired `media_change`. He switched to Hook Man;
    the app sat on Skin; no briefing, no scene analysis, nothing. Silently, for as long as he
    kept watching. (Verified against the live server: two sessions, same player, Skin at
    lastViewedAt 1757273811 and Hook Man at 1757276292.)

    The old "prefer type == movie" rule never helped here, because both were EPISODES — it
    fell straight through to metas[0].

    So:
      1. A PLAYER CAN ONLY PLAY ONE THING. Where several sessions share a machineIdentifier,
         only the most recently touched one is real; the rest are ghosts.
      2. Stick with the player we were already following, if it's still there. Otherwise a
         second person starting something in another room would yank the app away mid-film.
      3. Prefer a session that is actually PLAYING over one that is paused.
      4. Then the most recently touched.
    """
    cands = _candidates(data)
    if not cands:
        return None

    # 0. TIM SAID SO. Nothing below gets a vote.
    if pinned:
        for c in cands:
            if str(c.get("rating_key")) == str(pinned):
                return c

    # 1. Stay with the player we were already following.
    if prefer_player:
        mine = [c for c in cands if c.get("player_id") == prefer_player]
        if mine:
            return mine[0]

    # 2. Already sorted: playing beats paused, then most recently touched.
    return cands[0]


def _parse_roles(meta: Optional[dict]) -> list[dict[str, Any]]:
    """Plex `Role[]` -> [{actor, character, thumb}]. The cast, as the library knows it.

    Kept in the order Plex returns it, which is BILLING ORDER — that ordering is itself
    information (the first names are the film's leads), so it is never sorted. Truncating
    from the front therefore always keeps the people most likely to be on screen.
    """
    if not isinstance(meta, dict):
        return []
    roles = meta.get("Role")
    if not isinstance(roles, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in roles:
        if not isinstance(r, dict):
            continue
        actor = str(r.get("tag") or "").strip()
        if not actor:
            continue
        dedupe = actor.casefold()
        if dedupe in seen:          # Plex credits an actor twice when they play two parts
            continue
        seen.add(dedupe)
        out.append({
            "actor": actor,
            "character": str(r.get("role") or "").strip() or None,
            "thumb": str(r.get("thumb") or "").strip() or None,
        })
        if len(out) >= CAST_LIMIT:
            break
    return out


def format_cast(
    cast: Optional[list[dict[str, Any]]],
    *,
    limit: int = 8,
    header: str = "Cast",
) -> str:
    """The cast as one prompt line, or "" when there's nothing to say.

    Returning "" for an empty cast means every call site is `if s: parts.append(s)` and
    no dangling "Cast:" label with nothing after it ever reaches a prompt. Truncation is
    from the front, which is billing order, which is the leads.
    """
    if not cast:
        return ""
    bits: list[str] = []
    for c in cast[:limit]:
        actor = str((c or {}).get("actor") or "").strip()
        if not actor:
            continue
        character = str((c or {}).get("character") or "").strip()
        bits.append(f"{actor} as {character}" if character else actor)
    if not bits:
        return ""
    return f"{header} (from the Plex library, billing order): " + "; ".join(bits)


def _parse_meta(m: dict) -> dict:
    player = m.get("Player", {}) or {}
    part_key: Optional[str] = None
    container: Optional[str] = None
    media = m.get("Media") or []
    if media:
        container = media[0].get("container")
        parts = media[0].get("Part") or []
        if parts:
            part_key = parts[0].get("key")
    directors = [d.get("tag") for d in (m.get("Director") or []) if d.get("tag")]
    state = (player.get("state") or "playing").lower()
    player_id = _player_id(m)
    player_name = player.get("title") or player.get("device") or player.get("product") or ""
    last_seen = _recency(m)
    media_type = m.get("type")
    title = m.get("title") or ""
    year = m.get("year")

    # TV episode fields — Plex puts the show on grandparentTitle, the season
    # on parentIndex/parentTitle, and the episode number on index. The
    # `title` field itself only carries the episode name.
    series_title = m.get("grandparentTitle") or None
    season_title = m.get("parentTitle") or None
    season_number = m.get("parentIndex")
    episode_number = m.get("index")
    try:
        season_number = int(season_number) if season_number is not None else None
    except (ValueError, TypeError):
        season_number = None
    try:
        episode_number = int(episode_number) if episode_number is not None else None
    except (ValueError, TypeError):
        episode_number = None

    # Build a unified display label that works for both movies and TV.
    if media_type == "episode" and series_title:
        season_ep = ""
        if season_number is not None:
            season_ep += f"S{season_number:02d}"
        if episode_number is not None:
            season_ep += f"E{episode_number:02d}"
        bits = [series_title]
        if season_ep:
            bits.append(season_ep)
        if title:
            bits.append(title)
        display_title = " · ".join(bits)
    else:
        display_title = f"{title} ({year})" if year else title

    return {
        "rating_key": str(m.get("ratingKey") or ""),
        "title": title,                       # episode name OR movie title
        "year": year,
        "type": media_type,
        "director": directors[0] if directors else None,
        "duration_ms": int(m.get("duration") or 0),
        "view_offset_ms": int(m.get("viewOffset") or 0),
        "state": state,
        "part_key": part_key,
        "container": container,
        "thumb": m.get("thumb"),
        "art": m.get("art"),
        "summary": m.get("summary") or "",
        # New TV fields — None for movies.
        "series_title": series_title,
        # The SHOW's ratingKey. Needed because Role[] frequently lives on the series
        # rather than the episode — without it, TV gets no cast at all.
        "grandparent_rating_key": str(m.get("grandparentRatingKey") or "") or None,
        # Filled in by _handle from library metadata; the session payload has no Role[].
        "cast": [],
        "season_title": season_title,
        "season_number": season_number,
        "episode_number": episode_number,
        "display_title": display_title,
        # WHO IS ACTUALLY PLAYING THIS. Until now `Player` was opened, `state` was
        # read out of it, and every other field was thrown on the floor — so the app
        # could not answer "which client is Tim watching on?", which is the first
        # question you need for anything to do with playback control.
        #
        # (The answer, for the record, turned out to be "Plex Web", which cannot be
        # remote-controlled at all — but we could only establish that by looking.)
        "player_product": player.get("product"),          # "Plex Web" / "Plex for Android" …
        "player_device": player.get("device"),
        "player_title": player.get("title"),              # "Tim's iPhone"
        # Use the SAME id the picker keys on — machineIdentifier, falling back to device
        # or title. If these two ever disagreed, the sticky-player rule would silently
        # stop sticking and the app would drift back onto whatever Plex listed first.
        "player_id": player_id,
        "player_name": player_name,
        "player_local": bool(player.get("local")),
        # When this session was last actually touched. This is what tells a live session
        # from the GHOST that Plex Web leaves behind when you change episode.
        "last_seen": last_seen,
    }


plex_monitor = PlexMonitor()
