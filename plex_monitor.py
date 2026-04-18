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


class PlexMonitor:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._listeners: list[Listener] = []
        self._last: dict[str, Any] = {}
        self._unreachable = False

    # ─── Listener API ─────────────────────────────────────────────
    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def current_state(self) -> dict[str, Any]:
        return dict(self._last)

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
                active = _extract_active(data)
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

    async def _fetch_library_part_key(self, rating_key: str) -> Optional[str]:
        """Plex /status/sessions omits Part.key — fetch it from library metadata."""
        if self._client is None or not rating_key:
            return None
        url = f"{settings.plex_url.rstrip('/')}/library/metadata/{rating_key}"
        headers = {"Accept": "application/json", "X-Plex-Token": settings.plex_token}
        try:
            r = await self._client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.debug("library metadata fetch failed: %s", e)
            return None
        metas = (data.get("MediaContainer") or {}).get("Metadata") or []
        if not metas:
            return None
        media = metas[0].get("Media") or []
        if not media:
            return None
        parts = media[0].get("Part") or []
        if not parts:
            return None
        return parts[0].get("key")

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

        # Session responses omit Part.key; fetch it from library metadata.
        # Reuse the previous part_key if we're still on the same media.
        if not active.get("part_key"):
            if last.get("rating_key") == rating_key and last.get("part_key"):
                active["part_key"] = last["part_key"]
            else:
                active["part_key"] = await self._fetch_library_part_key(rating_key)

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
def _extract_active(data: dict) -> Optional[dict]:
    mc = data.get("MediaContainer", {}) if isinstance(data, dict) else {}
    metas = mc.get("Metadata") or []
    if not metas:
        return None
    # Prefer "movie" type, fall back to first playing item.
    for m in metas:
        if m.get("type") == "movie":
            return _parse_meta(m)
    return _parse_meta(metas[0])


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
    return {
        "rating_key": str(m.get("ratingKey") or ""),
        "title": m.get("title") or "",
        "year": m.get("year"),
        "type": m.get("type"),
        "director": directors[0] if directors else None,
        "duration_ms": int(m.get("duration") or 0),
        "view_offset_ms": int(m.get("viewOffset") or 0),
        "state": state,
        "part_key": part_key,
        "container": container,
        "thumb": m.get("thumb"),
        "art": m.get("art"),
        "summary": m.get("summary") or "",
    }


plex_monitor = PlexMonitor()
