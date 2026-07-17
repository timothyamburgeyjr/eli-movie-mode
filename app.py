"""Project Eli: Movie Mode — FastAPI app with WebSocket dashboard."""
import asyncio
import json
import logging
import os
import random
import re
import secrets
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import bcrypt
import httpx
from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

from config import settings
import affinity
import facts
from context_manager import build_quiet_stretch, build_session_history_for_gemini
import characters
import coordinator
import emotions
from database import db, row_to_dict, rows_to_list
from gemini_brain import GeminiError, gemini_brain
from kindroid_relay import KindroidError, build_payload, parse_reply, send_message
from plex_monitor import plex_monitor
import portraits
import presence
from session_manager import (
    build_session_stats,
    format_stats_hint,
    write_journal_entry,
)
from stoned_tracker import current_state as stoned_current_state
from stoned_tracker import ingestion_narration
from stoned_tracker import curve_phase
from stoned_tracker import eli_state_directive
from stoned_tracker import private_aside
from stoned_tracker import state_directive
from stoned_tracker import taking_narration
from stoned_tracker import narration_for as stoned_narration_for  # legacy — Tim-POV, no longer used
from stoned_tracker import reinforcement_narration
from smart_snap import (
    FFmpegError,
    FFmpegNotFound,
    build_stream_url,
    build_thumb_url,
    extract_clip,
    extract_frame,
    ffmpeg_available,
)

# NOTHING WAS CONFIGURING THE ROOT LOGGER, so Python fell back to WARNING and every
# log.info() in this app was silently dropped — including `mic order:`, which has
# been dead since the room was built. The only lines that ever reached `docker logs`
# were failures, which means the room's DECISIONS (who spoke, who stayed quiet, who
# Tim addressed, who leaned in and why) have been invisible this whole time.
#
# That is the wrong default for an app whose whole behaviour is a series of
# judgment calls you can't see from the outside.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)      # one line per HTTP call, useless
logging.getLogger("httpcore").setLevel(logging.WARNING)

# PERSIST THE LOGS. `docker logs` lives inside the container and dies with it — a
# rebuild (or a plain `up --build`) wipes every line, so a failure Tim hits on a
# movie night is unrecoverable by the next morning. This mirrors the whole stream
# to a rotating file on the mounted data/ volume, which outlives the container. Full
# timestamps here (the console uses %H:%M:%S) so a line still tells you its DAY.
try:
    _log_dir = Path(os.getenv("LOG_DIR", "data/logs"))
    _log_dir.mkdir(parents=True, exist_ok=True)
    _file_handler = RotatingFileHandler(
        _log_dir / "movie-mode.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    _file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(_file_handler)
except OSError:
    logging.getLogger(__name__).warning("could not open log file — console only", exc_info=True)

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# App lifecycle
# ──────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    Path(settings.frames_dir).mkdir(parents=True, exist_ok=True)

    # Prime Gemini with the persisted active character's persona.
    await _refresh_active_companion()

    # Downscale any bust portraits the vault has picked up since last boot.
    # Never fatal — a kin with no art just renders as initials.
    try:
        await portraits.refresh_portraits()
    except Exception:
        log.exception("portrait refresh failed — falling back to initials")

    plex_monitor.add_listener(_on_plex_event)
    # A pin survives a restart. Otherwise a container rebuild mid-film quietly hands the
    # choice back to the guesswork he had already corrected.
    pinned = await db.get_setting("plex_pinned")
    if pinned:
        plex_monitor.pin(pinned)
    await plex_monitor.start()

    mood_task = asyncio.create_task(_mood_ticker_loop(), name="mood-ticker")

    try:
        yield
    finally:
        mood_task.cancel()
        try:
            await mood_task
        except asyncio.CancelledError:
            pass
        await plex_monitor.stop()
        await db.close()


app = FastAPI(title="Project Eli: Movie Mode", lifespan=lifespan)

templates = Jinja2Templates(directory="templates")
Path("static/frames").mkdir(parents=True, exist_ok=True)
Path(settings.live_photos_dir).mkdir(parents=True, exist_ok=True)
Path(settings.live_audio_dir).mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
# Public, unauthenticated — Kindroid fetches images server-side and won't carry
# a session cookie. The per-photo UUID path acts as the access secret. Files
# live under data/live_photos/{session_id}/{uuid}.{ext} and get wiped on
# session finalize. Audio uses the same disposable-storage pattern.
app.mount("/photos", StaticFiles(directory=settings.live_photos_dir), name="photos")
app.mount("/audio", StaticFiles(directory=settings.live_audio_dir), name="audio")


# ──────────────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────────────
COOKIE_NAME = "eli_session"
_serializer = URLSafeSerializer(settings.secret_key, salt="movie-mode-session")


def _sign_cookie(payload: dict[str, Any]) -> str:
    return _serializer.dumps(payload)


def _verify_cookie(raw: Optional[str]) -> Optional[dict[str, Any]]:
    if not raw:
        return None
    try:
        return _serializer.loads(raw)
    except BadSignature:
        return None


async def password_is_set() -> bool:
    return await db.get_setting("password_hash") is not None


async def verify_password(password: str) -> bool:
    stored = await db.get_setting("password_hash")
    if not stored:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
    except ValueError:
        return False


async def set_password(password: str) -> None:
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    await db.set_setting("password_hash", hashed)


async def require_auth(eli_session: Optional[str] = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    payload = _verify_cookie(eli_session)
    if not payload or not payload.get("authed"):
        raise HTTPException(status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers={"Location": "/login"})
    return payload


# ──────────────────────────────────────────────────────────────────────
# WebSocket connection manager
# ──────────────────────────────────────────────────────────────────────
class ConnectionManager:
    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self.connections.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self.connections.discard(ws)

    async def broadcast(self, message: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        async with self._lock:
            targets = list(self.connections)
        for ws in targets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self.connections.discard(ws)


manager = ConnectionManager()


# ──────────────────────────────────────────────────────────────────────
# Plex event handler
# ──────────────────────────────────────────────────────────────────────
async def _on_plex_event(event: str, data: dict[str, Any]) -> None:
    await manager.broadcast({"type": "plex", "event": event, "data": data})

    if event == "media_change" and data:
        active = await db.get_active_session()
        if active and data.get("rating_key"):
            existing = await db.fetch_one(
                "SELECT id FROM movies WHERE session_id = ? AND plex_rating_key = ?",
                (active["id"], data["rating_key"]),
            )
            if not existing:
                runtime_min = None
                if data.get("duration_ms"):
                    runtime_min = int(data["duration_ms"]) // 60000 or None
                movie_id = await db.add_movie(
                    session_id=active["id"],
                    plex_rating_key=data["rating_key"],
                    title=data.get("title") or "",
                    year=data.get("year"),
                    director=data.get("director"),
                    runtime_minutes=runtime_min,
                )
                title_label = data.get("title") or "Unknown"
                await db.add_message(
                    active["id"], "system", f"Switching to: {title_label}"
                )
                await manager.broadcast(await _build_session_snapshot())
                asyncio.create_task(
                    _generate_briefing_card(active["id"], movie_id, dict(data))
                )


# ──────────────────────────────────────────────────────────────────────
# Gemini pipeline
# ──────────────────────────────────────────────────────────────────────
def _mechanical_briefing(plex_data: dict[str, Any], is_continuation: bool) -> str:
    """A briefing built from what Plex already handed us. No model, so it cannot fail.

    THE BRIEFING IS NOT COMMENTARY. It is the thing that tells everyone in the room that the
    show is STARTING — the lights going down. When Gemini returns nothing (and it does), the
    old code hit `if briefing_text.strip():`, found an empty string, and quietly skipped the
    entire relay. No error, no system line, no retry: Tim got a blank card and a family who
    had never been told a film had begun. He only found out by opening their Kindroids.

    An empty briefing must never mean silence. Anything is better than nothing here, and Plex
    already gives us the title, the year, the director and a synopsis — which is most of what
    the briefing was going to say anyway.

    First person, because Kindroid renders the OUTGOING message as the SENDER's. This is Tim
    talking to the room.
    """
    title = (plex_data.get("title") or "").strip() or "something"
    year = plex_data.get("year")
    director = (plex_data.get("director") or "").strip()
    summary = (plex_data.get("summary") or "").strip()
    series = (plex_data.get("series_title") or "").strip()
    season = plex_data.get("season_number")
    episode = plex_data.get("episode_number")

    if series and season and episode:
        what = f"{series}, season {season}, episode {episode} — \u201c{title}\u201d"
    elif year:
        what = f"{title} ({year})"
    else:
        what = title

    opener = "Next up:" if is_continuation else "Putting on"
    bits = [f"{opener} {what}."]
    if director:
        bits.append(f"{director} directed it.")
    if summary:
        bits.append(summary[:400].rstrip())
    bits.append("Settling in now \u2014 talk to you as it goes.")
    return " ".join(bits)


async def _generate_briefing_card(session_id: str, movie_id: int, plex_data: dict[str, Any]) -> None:
    """Kick off a Gemini briefing for a newly-detected movie, then relay it to
    Kindroid so Eli gets the context too.

    Idempotent: if a briefing has already been generated for this movie (the
    `movies.briefing` column is populated), skip — prevents duplicate sends
    when session-start and media_change both fire for the same rating_key.
    """
    # Tim's call on whether he wants the intro at all. A briefing costs a Gemini
    # call plus one Kindroid call PER KIN — in a room of six that's six minutes
    # of everyone getting caught up before the film has started.
    #   always — brief every film
    #   first  — skip the opening film's, brief the ones that follow (you already
    #            know what you sat down to watch; it's the switch you want warned about)
    #   never  — no intros at all
    mode = (await db.get_setting("briefings") or "always").lower()
    if mode == "never":
        log.info("briefing skipped (briefings=never)")
        return
    if mode == "first":
        count = await db.fetch_one(
            "SELECT COUNT(*) AS n FROM movies WHERE session_id = ?", (session_id,)
        )
        if int(count["n"] if count else 0) <= 1:
            log.info("briefing skipped for the opening film (briefings=first)")
            return

    existing = await db.fetch_one(
        "SELECT briefing, mood FROM movies WHERE id = ?", (movie_id,)
    )
    if existing and (existing["briefing"] or "").strip():
        # Even though the briefing is cached, broadcast the stored mood so
        # the Movie Mode button themes correctly on this fresh session.
        cached_mood = (existing["mood"] or "").strip()
        if cached_mood:
            await manager.broadcast({"type": "mood", "mood": cached_mood})
            _mood_tick_state["last_mood"] = cached_mood
        log.debug("briefing already exists for movie %s, skipping generation", movie_id)
        return

    if _pipeline_paused():
        return

    # No typing indicator for the initial briefing — it's a system-generated
    # announcement, not a Tim-initiated exchange. User sees the briefing card
    # and Eli's reply arrives in the background without dots.

    # Continuation = this isn't the first movie of the session.
    movies_so_far = await db.fetch_all(
        "SELECT id FROM movies WHERE session_id = ? ORDER BY id ASC",
        (session_id,),
    )
    is_continuation = len(movies_so_far) > 1
    tod = _time_of_day_label()

    try:
        result = await gemini_brain.generate_briefing(
            title=plex_data.get("title") or "",
            year=plex_data.get("year"),
            director=plex_data.get("director"),
            runtime_minutes=(plex_data.get("duration_ms") or 0) // 60000 or None,
            summary=plex_data.get("summary") or "",
            time_of_day=tod,
            is_continuation=is_continuation,
            media_type=plex_data.get("type") or "movie",
            series_title=plex_data.get("series_title"),
            season_number=plex_data.get("season_number"),
            episode_number=plex_data.get("episode_number"),
        )
    except Exception:
        log.exception("briefing generation failed")
        await db.add_message(session_id, "system", "Briefing unavailable — Gemini error.")
        return

    briefing_text = (result.get("briefing") or "").strip()
    # GEMINI CAME BACK EMPTY. It happens. What must NOT happen is that the room hears nothing
    # and nobody is told — which is exactly what used to occur, because an empty string simply
    # failed the `if briefing_text.strip()` guard further down and the whole relay was skipped
    # in silence. Write one ourselves out of the Plex metadata, and say we did.
    if not briefing_text:
        log.warning("Gemini returned an empty briefing for %r — using the plain one",
                    plex_data.get("title"))
        briefing_text = _mechanical_briefing(plex_data, is_continuation)
        await db.add_message(
            session_id, "system",
            "Briefing came back empty \u2014 sent the plain one instead, so everyone knows "
            "the film has started.",
        )

    scores_payload = {
        "scores": result.get("scores") or {},
        "rating_key": plex_data.get("rating_key"),
        "title": plex_data.get("title"),
        "year": plex_data.get("year"),
        "director": plex_data.get("director"),
        "runtime_minutes": (plex_data.get("duration_ms") or 0) // 60000 or None,
        "thumb": plex_data.get("thumb"),
    }

    msg_id = await db.add_message(
        session_id,
        "briefing",
        briefing_text,
        scene_context=json.dumps(scores_payload),
        latency_ms=result.get("latency_ms"),
    )
    await db.execute(
        "UPDATE movies SET briefing = ?, mood = ? WHERE id = ?",
        (briefing_text, result.get("mood"), movie_id),
    )
    row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (msg_id,))
    await manager.broadcast(
        {"type": "message", "message": _message_to_payload(row_to_dict(row) or {})}
    )
    # Broadcast the initial mood from the briefing so the Movie Mode button
    # themes itself immediately, before any scene analysis has run.
    initial_mood = result.get("mood")
    if initial_mood:
        await manager.broadcast({"type": "mood", "mood": initial_mood})
        _mood_tick_state["last_mood"] = initial_mood
    await _broadcast_cost(session_id)

    # Actually relay the briefing to Kindroid so Eli has context.
    # The briefing goes in the scene-narration slot (top-of-mind context).
    # No history (fresh start for this movie), no reaction, no dialogue.
    # No typing indicator — briefing isn't a Tim-initiated conversation turn.
    # EVERYONE PRESENT GETS IT — the people Tim is talking to AND the people listening.
    # `_send_to_kindroid_and_render` fans out over the whole ROOM (not the addressed subset),
    # which is what marks the start of the show for all of them. Verified against a real
    # briefing: room was jeff/adam/thomas/bobby with only adam addressed, and all four
    # answered.
    #
    # No `if` guard here any more. There was one, and an empty briefing walked straight
    # through it into silence.
    room, addressed = await _room_and_addressed()
    log.info("briefing -> %d in the room: %s", len(room), ", ".join(c.key for c in room))
    if room:
        venue, people, descriptions = await _presence_state()
        setting_note = presence.briefing_note(
            venue, descriptions.get(venue or "", ""), people
        )
        await _send_to_kindroid_and_render(
            session_id,
            scene_narration=briefing_text,
            history_narrative="",
            reaction_narration="",
            typed_dialogue="",
            mood=None,
            show_typing=False,
            presence_override=setting_note,
        )


async def _count_tim_exchanges(session_id: str) -> int:
    return await db.count_session_messages(session_id, sender="tim")


# Per-session counter of user-initiated Tim messages since the last stoned
# reinforcement emote. When it hits the threshold and Eli is non-sober, we
# auto-post a Tim-POV observation of Eli's state to keep her altered
# condition active in Kindroid's context.
_reinforcement_counters: dict[str, int] = {}
_REINFORCEMENT_THRESHOLD_RANGE = (2, 3)

# Global single-user session-control flags. These are server-side truth — UI
# state is derived from broadcasts. Single-user app, so module-level is fine.
_standby_active: bool = False
_away_active: bool = False
_away_entered_at: Optional[datetime] = None
_away_plex_offset_ms: Optional[int] = None

# Mood ticker state — drives the passive 30s mood heartbeat. last_full_scene_at
# is set whenever a full Pro scene analysis runs (so the ticker doesn't
# re-classify mood right after a Tim message already did).
_mood_tick_state: dict[str, Any] = {
    "last_mood": None,
    "last_check_at": None,         # datetime aware
    "last_plex_offset_ms": None,   # int
    "last_full_scene_at": None,    # datetime aware
}
_MOOD_TICK_CADENCE_SEC = 30
_MOOD_TICK_SKIP_AFTER_SCENE_SEC = 45  # skip if Pro scene just classified mood
_MOOD_TICK_CLIP_SECONDS = 3


def _pipeline_paused() -> bool:
    """Return True when any session-control flag should block pipeline work.

    Standby hard-stops everything. Away blocks Eli-facing work but lets the
    WDIM catch-up flow complete when the user returns.
    """
    return _standby_active or _away_active


def _mark_full_scene_mood(mood: Optional[str]) -> None:
    """Record that a full Pro scene analysis just classified the mood, so
    the passive mood ticker knows to skip its next cycle. Called from both
    `_process_tim_message_body` and `_process_reaction_body`.
    """
    if mood:
        _mood_tick_state["last_mood"] = mood
    _mood_tick_state["last_full_scene_at"] = datetime.now(timezone.utc)


async def _maybe_tick_mood(*, force: bool = False) -> None:
    """Run a lightweight mood classification on the current scene IF the
    skip rules allow. Cheap Flash call (~$0.0007) with broadcast-on-change.

    Skip rules:
      • Pipeline paused (standby / away) — no work
      • No active session — no work
      • Plex not playing or no part_key — nothing to clip
      • Playhead hasn't advanced since last check — same frame on screen
      • Full Pro scene analysis ran in last 45s — mood is already fresh
      • Last mood check ran less than cadence seconds ago — dedup

    `force=True` bypasses ONLY the cadence dedup (e.g., when a photo/audio/
    trivia event wants an immediate refresh). Other skip rules still apply.
    """
    if _pipeline_paused():
        return
    active = await db.get_active_session()
    if not active:
        return
    session_id = active["id"]

    now = datetime.now(timezone.utc)

    last_full = _mood_tick_state.get("last_full_scene_at")
    if last_full and (now - last_full).total_seconds() < _MOOD_TICK_SKIP_AFTER_SCENE_SEC:
        return

    last_check = _mood_tick_state.get("last_check_at")
    if not force and last_check and (now - last_check).total_seconds() < _MOOD_TICK_CADENCE_SEC:
        return

    plex = plex_monitor.current_state()
    if not plex or not plex.get("part_key"):
        return
    if (plex.get("state") or "").lower() != "playing":
        return

    current_offset = int(plex.get("view_offset_ms") or 0)
    last_offset = _mood_tick_state.get("last_plex_offset_ms")
    if last_offset is not None and current_offset == last_offset:
        return  # paused / stuck — nothing to re-classify

    _mood_tick_state["last_check_at"] = now
    _mood_tick_state["last_plex_offset_ms"] = current_offset

    clip_path: Optional[Path] = None
    try:
        view_offset_sec = current_offset / 1000.0
        stream_url = build_stream_url(plex["part_key"])
        clip_path = await extract_clip(stream_url, view_offset_sec, _MOOD_TICK_CLIP_SECONDS)
    except Exception:
        log.debug("mood ticker: clip extraction failed", exc_info=True)
        return

    # THE LIVE FRAME, FOR FREE. This clip is already downloaded and is about to be
    # deleted — cutting one keyframe out of it first costs 0.08s of local ffmpeg
    # (measured) and not one penny of API. The desktop's "on screen now" panel has
    # a fresh picture every 30 seconds and nobody paid for it.
    try:
        await _refresh_live_frame(clip_path)
    except Exception:
        log.debug("live frame refresh failed", exc_info=True)

    try:
        result = await gemini_brain.classify_mood(clip_path)
    except Exception:
        log.debug("mood ticker: Flash classify failed", exc_info=True)
        return
    finally:
        if clip_path and clip_path.exists():
            try:
                clip_path.unlink()
            except OSError:
                pass

    new_mood = result.get("mood")
    if not new_mood:
        return

    # Record the mood against the PLAYHEAD, not the clock — the arc is the film's
    # emotional shape, so it has to be drawn along the film's timeline. (Pausing for
    # twenty minutes must not stretch the ribbon.) The rating key is what stops one
    # film's arc bleeding into the next one's on a marathon night.
    await db.add_mood_event(
        session_id, current_offset, new_mood, rating_key=plex.get("rating_key")
    )

    prev = _mood_tick_state.get("last_mood")
    if new_mood == prev:
        return  # No change — keep the WS quiet, the UI doesn't need a re-render.
    _mood_tick_state["last_mood"] = new_mood
    await manager.broadcast({"type": "mood", "mood": new_mood})
    await _broadcast_cost(session_id)
    log.info("mood ticker: %s → %s (offset %.1fs)", prev, new_mood, view_offset_sec)


# ─── The live frame ───────────────────────────────────────────────────
#
# What's on screen RIGHT NOW, refreshed every mood tick. Free, because the ticker
# already pulls a 3-second clip every 30 seconds and throws it away — we just cut
# one frame out of it on the way past.
#
# NOTE what this is NOT: it is not "what Gemini sees". Gemini only reads the scene
# when Tim prompts it. The picture is live; the DESCRIPTION beside it can be twenty
# minutes stale, and the UI has to say so. See `_live_scene_state`.
LIVE_FRAME_PATH = Path(settings.frames_dir) / "live.jpg"
_live_frame_at: Optional[datetime] = None


async def _refresh_live_frame(clip_path: Path) -> None:
    """Cut a keyframe from the ticker's clip before it's binned."""
    global _live_frame_at
    if not clip_path or not clip_path.exists():
        return
    LIVE_FRAME_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = await extract_frame(str(clip_path), 0.5, out_dir=LIVE_FRAME_PATH.parent)
    try:
        await asyncio.to_thread(shutil.move, str(tmp), str(LIVE_FRAME_PATH))
        _live_frame_at = datetime.now(timezone.utc)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise


@app.get("/api/live-scene")
async def api_live_scene(_auth: dict = Depends(require_auth)):
    """What's on screen now, and what Gemini last made of it.

    TWO DIFFERENT CLOCKS, and the UI must not blur them:
      • `frame` is LIVE (~30s old at worst — the ticker's cadence).
      • `description` is whenever Tim last prompted. Could be twenty minutes ago.
        `read_seconds_ago` is how the UI tells the truth about that.
    """
    active = await db.get_active_session()
    if not active:
        return JSONResponse({"frame": None, "description": "", "mood": None})

    row = await db.fetch_one(
        "SELECT scene_context, mood, timestamp FROM messages "
        "WHERE session_id = ? AND scene_context IS NOT NULL AND scene_context != '' "
        "ORDER BY id DESC LIMIT 1",
        (active["id"],),
    )
    read_ago: Optional[int] = None
    if row and row["timestamp"]:
        try:
            ts = datetime.fromisoformat(str(row["timestamp"]).replace(" ", "T"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            read_ago = max(0, int((datetime.now(timezone.utc) - ts).total_seconds()))
        except ValueError:
            pass

    return JSONResponse({
        # Cache-bust on the frame's own timestamp so the browser actually refetches.
        "frame": (
            f"/static/frames/live.jpg?t={int(_live_frame_at.timestamp())}"
            if _live_frame_at and LIVE_FRAME_PATH.exists() else None
        ),
        "frame_seconds_ago": (
            int((datetime.now(timezone.utc) - _live_frame_at).total_seconds())
            if _live_frame_at else None
        ),
        "description": (row["scene_context"] if row else "") or "",
        "mood": (row["mood"] if row else None) or _mood_tick_state.get("last_mood"),
        "read_seconds_ago": read_ago,
    })


@app.get("/api/mood-arc")
async def api_mood_arc(_auth: dict = Depends(require_auth)):
    """The film's emotional shape, as a ribbon along the scrubber.

    Keyed on the PLAYHEAD, not wall-clock, so the arc maps onto the film's timeline
    rather than onto how long Tim sat there.
    """
    active = await db.get_active_session()
    if not active:
        return JSONResponse({"events": [], "duration_ms": 0})
    plex = plex_monitor.current_state() or {}
    rating_key = plex.get("rating_key")
    return JSONResponse({
        # Scoped to THIS film. A marathon session holds several arcs; without the
        # filter the ribbon would splice The Iron Giant onto the end of 1408.
        "events": await db.get_mood_events(active["id"], rating_key=rating_key),
        "duration_ms": int(plex.get("duration_ms") or 0),
        "rating_key": rating_key,
    })


async def _mood_ticker_loop() -> None:
    """Background heartbeat. Wakes every cadence interval and asks
    `_maybe_tick_mood` to do work — that helper handles all skip rules.
    """
    log.info("mood ticker started (cadence=%ds)", _MOOD_TICK_CADENCE_SEC)
    while True:
        try:
            await asyncio.sleep(_MOOD_TICK_CADENCE_SEC)
            await _maybe_tick_mood()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("mood ticker iteration crashed — continuing")


async def _fire_stoned_emote(session_id: str, narration: str) -> None:
    """Post a first-person emote as a Tim message and run it through the
    normal pipeline so Eli reacts to it.
    """
    content = f"_(*{narration}*)_"
    msg_id = await db.add_message(session_id, "tim", content)
    _, _, segments = parse_reply(content)
    if segments:
        await db.execute(
            "UPDATE messages SET segments_json = ? WHERE id = ?",
            (json.dumps(segments), msg_id),
        )
    row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (msg_id,))
    await manager.broadcast(
        {"type": "message", "message": _message_to_payload(row_to_dict(row) or {})}
    )
    # Spawn as non-user-initiated so we don't trigger another reinforcement.
    asyncio.create_task(
        _process_tim_message(msg_id, session_id, user_initiated=False)
    )


async def _maybe_fire_reinforcement(session_id: str) -> None:
    """Every 2-3 messages, note how the stoned members of the room are doing.

    Keeps their altered state alive in the Kindroid context so it doesn't drift
    back to sober. With several people high, one of them is picked at random —
    firing an observation about every one of them each time would read like a
    roll call, not a room.
    """
    if _pipeline_paused():
        return

    room, _ = await _room_and_addressed()
    stoned: list[tuple["characters.Character", int, Optional[str]]] = []
    for kin in room:
        level, method, _ = await stoned_current_state(kin.key)
        if level > 0:
            stoned.append((kin, level, method))

    if not stoned:
        # Reset, so nobody gets an instant observation the moment they light up.
        _reinforcement_counters[session_id] = 0
        return

    count = _reinforcement_counters.get(session_id, 0) + 1
    threshold = random.randint(*_REINFORCEMENT_THRESHOLD_RANGE)
    if count < threshold:
        _reinforcement_counters[session_id] = count
        return
    _reinforcement_counters[session_id] = 0

    kin, level, method = random.choice(stoned)
    # The second-person lines ("I love you like this") are written for Tim's
    # partner. Everyone else gets the named, third-person set — and in a room
    # even Eli must be named, or the other kins can't tell who "you" is.
    solo = len(room) <= 1
    narration = reinforcement_narration(
        level, method,
        name=None if (solo and kin.romantic) else kin.first_name,
        intimate=kin.romantic,
    )
    if narration:
        await _fire_stoned_emote(session_id, narration)


async def _broadcast_cost(session_id: Optional[str] = None) -> None:
    if session_id is None:
        active = await db.get_active_session()
        session_id = active["id"] if active else None
    payload = {
        "type": "cost",
        "session": (
            await db.get_session_cost(session_id)
            if session_id
            else {"total_usd": 0.0, "calls": 0, "by_type": {}}
        ),
        "today_usd": await db.get_today_cost(),
        "calls_today": await db.get_today_usage(),
    }
    await manager.broadcast(payload)


async def _latest_scene_description(session_id: str) -> str:
    row = await db.fetch_one(
        "SELECT scene_context FROM messages "
        "WHERE session_id = ? AND sender = 'tim' AND scene_context IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (session_id,),
    )
    return (row["scene_context"] or "") if row else ""


async def _active_character() -> Optional["characters.Character"]:
    """The family member Movie Mode is currently talking to (per settings)."""
    key = await db.get_setting("active_character")
    return characters.resolve_or_default(key)


# Where a venue's photo lives once Tim has taken one. Served straight out of
# /static, so the launcher can just point an <img> at it.
VENUES_DIR = Path("static/venues")


def _venue_image_url(venue_key: str) -> Optional[str]:
    """The venue's own photo, if Tim has ever given it one."""
    for p in sorted(VENUES_DIR.glob(f"{venue_key}.*")):
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            return f"/static/venues/{p.name}"
    return None


async def _presence_state() -> tuple[Optional[str], list[dict], dict]:
    """Read current venue key, present-people dicts, and venue descriptions."""
    venue = await db.get_setting("active_venue")
    try:
        people_keys = json.loads(await db.get_setting("present_people") or "[]")
    except (ValueError, TypeError):
        people_keys = []
    try:
        descriptions = json.loads(await db.get_setting("venue_descriptions") or "{}")
    except (ValueError, TypeError):
        descriptions = {}
    return venue, presence.present_people(people_keys), descriptions


async def _presence_standing_line() -> str:
    """Short per-message presence emote (where + who else is in the room)."""
    venue, people, _ = await _presence_state()
    return presence.standing_line(venue, people)


async def _refresh_active_companion() -> Optional["characters.Character"]:
    """Resolve the active character and push their persona into Gemini.

    Called at startup and whenever the dropdown changes, so every Gemini
    prompt is framed for whoever is selected without threading persona
    through each pipeline method.
    """
    char = await _active_character()
    if char:
        preamble = characters.companion_preamble(characters.build_persona(char))
        gemini_brain.set_companion(preamble)
    else:
        gemini_brain.set_companion("")
    return char


async def _send_to_kindroid_and_render(
    session_id: str,
    *,
    scene_narration: str = "",
    history_narrative: str = "",
    reaction_narration: str = "",
    typed_dialogue: str = "",
    mood: Optional[str] = None,
    show_typing: bool = True,
    include_presence: bool = True,
    presence_override: Optional[str] = None,
    image_urls: Optional[list[str]] = None,
    character: Optional["characters.Character"] = None,
) -> None:
    """Send one message to EVERYONE in the room, concurrently.

    Used by the paths that aren't a mic-passing turn — the movie briefing, the
    sign-off, a live photo, a voice note. There's no relay here because there's
    no floor to hold: it's one thing addressed to the whole room, and each kin
    answers it in their own voice.

    Every kin in the room gets it. If no room is picked, this falls back to the
    single `active_character`, exactly as before.

    Pass `character` to pin a single recipient and skip the fan-out entirely.

    NOTE: this used to send only to `active_character` regardless of the room,
    which meant a briefing went to Eli even when the room was Adam and Jeff —
    and Eli would answer from a room he wasn't in.
    """
    if character is not None:
        targets = [character]
    else:
        room, _ = await _room_and_addressed()
        targets = room

    targets = [t for t in targets if t]
    if not targets:
        return

    if len(targets) == 1:
        await _send_one_kin(
            session_id, targets[0],
            scene_narration=scene_narration, history_narrative=history_narrative,
            reaction_narration=reaction_narration,
            typed_dialogue=typed_dialogue, mood=mood, show_typing=show_typing,
            include_presence=include_presence, presence_override=presence_override,
            image_urls=image_urls,
        )
        return

    # Nobody's waiting on anybody else here, so don't make them queue.
    results = await asyncio.gather(
        *(
            _send_one_kin(
                session_id, kin,
                scene_narration=scene_narration, history_narrative=history_narrative,
                reaction_narration=reaction_narration,
                typed_dialogue=typed_dialogue, mood=mood, show_typing=show_typing,
                include_presence=include_presence, presence_override=presence_override,
                image_urls=image_urls,
            )
            for kin in targets
        ),
        return_exceptions=True,
    )

    # A KIN WHO MISSED IT MUST NOT MISS IT SILENTLY.
    #
    # `return_exceptions=True` collects the failures instead of raising them — and then this
    # code threw the list away without looking at it. A briefing went out to a room of four;
    # Bobby's call failed; jeff, thomas and adam answered and Bobby simply wasn't there. The
    # card cheerfully said the briefing had been sent, and the only way to discover otherwise
    # was to open his Kindroid and find nothing in it.
    #
    # Half a room being briefed is worse than none, because nobody can see the difference.
    missed = [
        (kin, err) for kin, err in zip(targets, results) if isinstance(err, BaseException)
    ]
    for kin, err in missed:
        log.error("kindroid send FAILED for %s: %s", kin.key, err, exc_info=err)
    if missed:
        names = _join_names([k.first_name for k, _ in missed])
        await db.add_message(
            session_id, "system",
            f"{names} didn't get that — Kindroid wouldn't take it.",
        )
        await manager.broadcast({"type": "refresh"})


async def _send_one_kin(
    session_id: str,
    character: "characters.Character",
    *,
    scene_narration: str = "",
    history_narrative: str = "",
    reaction_narration: str = "",
    typed_dialogue: str = "",
    mood: Optional[str] = None,
    show_typing: bool = True,
    include_presence: bool = True,
    presence_override: Optional[str] = None,
    image_urls: Optional[list[str]] = None,
) -> None:
    """Assemble the Kindroid payload from emote sections, send, parse, persist
    the kin's reply, and broadcast it to the UI. Handles overflow with condense().
    """
    # Respect any mid-flight standby/away flip.
    if _pipeline_paused():
        if show_typing:
            await manager.broadcast({"type": "eli_typing", "on": False})
        return
    # Standing presence cue (where Tim is + who else is in the room) goes on
    # every message so the kin keeps it in mind and modulates tone. A caller
    # may supply a richer one-off note (e.g. the briefing's full setting).
    if presence_override is not None:
        presence_line = presence_override
    else:
        presence_line = await _presence_standing_line() if include_presence else ""

    # The private channel — see _relay_to_kin. This kin's own state, plus (for a
    # partner) an aside nobody else in the room hears.
    kin_level, kin_method, _ = await stoned_current_state(character.key)
    stoned_line = state_directive(kin_level, kin_method, kin_key=character.key)
    aside = private_aside(kin_level, intimate=character.romantic, kin_key=character.key)
    if aside:
        stoned_line = f"{stoned_line}\n\n{aside}" if stoned_line else aside

    payload = build_payload(
        scene_narration=scene_narration,
        history_narrative=history_narrative,
        stoned_narration=stoned_line,
        reaction_narration=reaction_narration,
        presence_narration=presence_line,
        typed_dialogue=typed_dialogue,
    )

    # Overflow guard — if we still blew past the cap, condense the longest
    # emote section (almost always the scene) and reassemble.
    if len(payload) > settings.kindroid_char_limit and scene_narration:
        overflow = len(payload) - settings.kindroid_char_limit
        target_scene_len = max(300, len(scene_narration) - overflow - 50)
        try:
            scene_narration = await gemini_brain.condense(
                scene_narration, max_chars=target_scene_len
            )
            payload = build_payload(
                scene_narration=scene_narration,
                history_narrative=history_narrative,
                stoned_narration=stoned_line,
                reaction_narration=reaction_narration,
                presence_narration=presence_line,
                typed_dialogue=typed_dialogue,
            )
        except Exception:
            log.exception("condense overflow-fix failed")

    active = character
    ai_id = active.ai_id
    who = active.first_name
    if show_typing:
        await manager.broadcast(
            {"type": "eli_typing", "on": True, "character_key": active.key, "name": who}
        )
    log.info(
        "kindroid SEND session=%s to=%s payload_chars=%d image_urls=%d",
        session_id, active.key, len(payload), len(image_urls or []),
    )
    try:
        reply = await send_message(payload, ai_id=ai_id, image_urls=image_urls)
        raw_text = (reply.get("raw") or "")
        log.info(
            "kindroid REPLY session=%s raw_chars=%d stripped_chars=%d segments=%d emote_chars=%d spoken_chars=%d",
            session_id,
            len(raw_text),
            len(raw_text.strip()),
            len(reply.get("segments") or []),
            len(reply.get("emote_text") or ""),
            len(reply.get("spoken_text") or ""),
        )
        raw_preview = raw_text[:200].replace("\n", " ⏎ ")
        log.info("kindroid REPLY preview: %r", raw_preview)
        # Safety net: Kindroid sometimes returns whitespace-only bodies for
        # reasons we don't yet fully understand (possibly API config mismatch,
        # rate-limit silent failure, or content moderation). Surface it as a
        # system message so the user knows something went wrong rather than
        # seeing a silent empty Eli bubble.
        if not raw_text.strip():
            log.warning("kindroid returned empty body — surfacing system error to user")
            await db.add_message(
                session_id, "system",
                f"{who}'s response came back empty — Kindroid returned nothing. "
                "Check API credentials, AI ID, or whether the message tripped a filter.",
            )
            snap_row = await db.fetch_one(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            )
            if snap_row:
                await manager.broadcast(
                    {"type": "message", "message": _message_to_payload(row_to_dict(snap_row) or {})}
                )
            return
    except KindroidError as e:
        log.exception("kindroid relay failed")
        detail = str(e)[:200] or "no detail"
        await db.add_message(
            session_id,
            "system",
            f"{who} couldn't respond — Kindroid error: {detail}",
        )
        snap_row = await db.fetch_one(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        if snap_row:
            await manager.broadcast(
                {"type": "message", "message": _message_to_payload(row_to_dict(snap_row) or {})}
            )
        return
    finally:
        if show_typing:
            await manager.broadcast({"type": "eli_typing", "on": False})

    # Tim's leaf level is inferred by Gemini at scene-analysis time and
    # stamped on his most recent message. Inherit that here so Eli's reply
    # shows the same leaf count (visual continuity per exchange).
    latest_tim_row = await db.fetch_one(
        "SELECT stoned_level_tim FROM messages WHERE session_id = ? "
        "AND sender = 'tim' ORDER BY id DESC LIMIT 1",
        (session_id,),
    )
    eli_tim_level = int(latest_tim_row["stoned_level_tim"] or 0) if latest_tim_row else 0
    eli_eli_level = kin_level  # this kin's own level, computed above
    eli_msg_id = await db.add_message(
        session_id,
        "eli",
        content=reply.get("raw", ""),
        emote_text=reply.get("emote_text") or None,
        spoken_text=reply.get("spoken_text") or None,
        # Don't store scene_context on Eli's row — it's already shown on
        # Tim's message above hers, and rendering it again would duplicate
        # the "Scene context" collapsible block in the UI.
        scene_context=None,
        mood=mood,
        stoned_level_tim=eli_tim_level,
        stoned_level_eli=eli_eli_level,
        latency_ms=reply.get("latency_ms"),
        # WHO said it. 'eli' remains the lane (companion vs tim vs system);
        # this is the identity, and it's what lets a room of kins share a
        # transcript without the UI relabelling everyone on a dropdown change.
        character_key=active.key if active else None,
    )
    segments = reply.get("segments") or []
    if segments:
        await db.execute(
            "UPDATE messages SET segments_json = ? WHERE id = ?",
            (json.dumps(segments), eli_msg_id),
        )
    row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (eli_msg_id,))
    if row:
        await manager.broadcast(
            {"type": "message", "message": _message_to_payload(row_to_dict(row) or {})}
        )


def _join_names(names: list[str]) -> str:
    """'Bobby', 'Bobby and Eli', 'Bobby, Eli, and Tommy'."""
    if not names:
        return "the room"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


async def _stage(text: str) -> None:
    """Tell the UI what the pipeline is doing right now.

    A turn is 30+ seconds per kin. Without this the screen just sits there and
    Tim can't tell the difference between "Gemini is watching the clip" and
    "the whole thing has hung".
    """
    await manager.broadcast({"type": "stage", "text": text})


async def _room_and_addressed() -> tuple[list["characters.Character"], list["characters.Character"]]:
    """(room, addressed) as resolved Characters.

    An empty room means Tim hasn't picked one — fall back to the legacy
    single-kin behaviour on `active_character` so nothing regresses. An empty
    `addressed` inside a non-empty room means "everyone".
    """
    room_keys = await _kin_list("room_kins")
    if not room_keys:
        active = await _active_character()
        return ([active], [active]) if active else ([], [])

    room = [c for c in (characters.get_character(k) for k in room_keys) if c]
    addressed_keys = await _kin_list("addressed_kins")
    addressed = [c for c in room if c.key in addressed_keys]
    return room, (addressed or list(room))


def _named_in(
    message: str, room: list["characters.Character"]
) -> list["characters.Character"]:
    """Everyone Tim called out by name, in the order he named them.

    TIM ALWAYS OUTRANKS THE COORDINATOR. If he says "Bobby, what do you think?"
    then Bobby answers — whether or not Bobby was tapped, and whatever the
    affinity sheets have to say about it.

    Pass the WHOLE ROOM here, not just the addressed set. Handing it only the
    people Tim had already selected made it structurally unable to do the one job
    it exists for: it could reorder the mic among his taps, but it could never pull
    anyone in. That was survivable while every kin replied to everything. Once
    silence became the default it meant Tim could ask Bobby a direct question and
    get nothing back at all.

    Matches the registry aliases (which already exist) and the display first name,
    on word boundaries so "Ellen" doesn't fire on "excellent".
    """
    text = (message or "").lower()
    if not text:
        return []
    hits: list[tuple[int, "characters.Character"]] = []
    for char in room:
        names = {char.first_name.lower(), char.key, *char.aliases}
        best: Optional[int] = None
        for name in names:
            if not name:
                continue
            m = re.search(rf"\b{re.escape(name)}\b", text)
            if m and (best is None or m.start() < best):
                best = m.start()
        if best is not None:
            hits.append((best, char))
    # In the order he said them: "Bobby and Daisy, what do you two make of this?"
    return [c for _, c in sorted(hits, key=lambda h: h[0])]


async def _turns_since_spoke(session_id: str, room: list["characters.Character"]) -> dict[str, int]:
    """How many of Tim's turns ago each kin last held the mic.

    Feeds the coordinator's fairness nudge, so a kin who's been quiet gets
    pulled forward over one who just spoke. A kin who has never spoken this
    session reads as maximally overdue.
    """
    total = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM messages WHERE session_id = ? AND sender = 'tim'",
        (session_id,),
    )
    turns = int(total["n"]) if total else 0
    out: dict[str, int] = {}
    for char in room:
        row = await db.fetch_one(
            """SELECT (SELECT COUNT(*) FROM messages t
                       WHERE t.session_id = m.session_id AND t.sender = 'tim' AND t.id > m.id) AS since
               FROM messages m
               WHERE m.session_id = ? AND m.character_key = ?
               ORDER BY m.id DESC LIMIT 1""",
            (session_id, char.key),
        )
        out[char.key] = int(row["since"]) if row else max(turns, 99)
    return out


# ─── How loud the room is ─────────────────────────────────────────────
#
# Silence is the default. Only Tim's picks speak, plus whoever the coordinator
# says genuinely has something, plus anyone who's been quiet too long.
_MAX_BARGE_INS = 2        # how many the coordinator may promote per turn
_MAX_FLOOR_PULLS = 2      # how many long-quiet kins get pulled in per turn
_QUIET_FLOOR_TURNS = 6    # ...after this many turns without speaking.
_CATCHUP_AFTER_TURNS = 2  # missed this many of Tim's turns -> catch them up on return.
#                           Was 3. Two is the point at which the film has genuinely
#                           moved on and someone may well have said your name.
_LEAN_WINDOW = 5          # how many of Tim's turns the coordinator sees when pacing itself

# HOW NOISY THE ROOM HAS BEEN, per session: one lean-in count per turn of Tim's,
# oldest first. Fed back to the coordinator so it can pace itself against what it
# ACTUALLY did rather than against an adjective in a prompt.
#
# In memory on purpose. It is a pacing signal with a five-turn horizon, not a fact
# about the evening — losing it on a container restart costs nothing, and it does
# not belong in a table anyone has to migrate.
_LEAN_LOG: dict[str, list[int]] = {}
#
# The floor exists because affinity alone leaves people out in the cold: a kin
# whose sheet never matches a horror film could sit mute for two hours and read
# as BROKEN rather than as quiet. Six turns is roughly "you've been quiet a
# while" in a room where one or two people speak per turn — tune it if the room
# feels either chatty or dead.


# How much of the 4,000-char Kindroid cap each part of the payload may claim.
# These are floors, not targets: the scene gets whatever is left over, because it
# is the only part that can be shortened without losing something structural.
_MAX_PRIOR_KINS = 3          # how many previous speakers a kin actually hears
_MAX_PRIOR_CHARS = 240       # per speaker, per field (emote / spoken)
_MIN_SCENE_CHARS = 320       # below this the scene stops being worth sending
# Format directive (~80) + one `_(* *)_` wrapper per paragraph + slack. Measured
# worst case (room of 8, everyone verbose) lands at ~3,990/4,000 with 400 here —
# which fits, but with 13 characters to spare. Blowing the cap is the exact thing
# this budget exists to prevent, so buy the margin.
_PAYLOAD_RESERVE = 520
_DIRECTIVE_CHARS = 80        # the format-directive emote, on every payload

# THE CATCH-UP TRACKS, per track (film / room). Elastic, not a flat cap.
#
# Measured against 3,142 real kin messages: 83% of them go FIRST in the circle, so
# they carry NO prior block at all — the scene fits whole and roughly 700 characters
# go unspent. A flat 300 was leaving that on the floor and asking Gemini for a thinner
# catch-up than the budget could easily afford.
#
# Only 7% of messages carry a full 3-speaker prior (~1,500 chars). Those are the ones
# that genuinely have to scrimp, and the floor is what protects them: a kin who has
# been quiet for ten turns must be told SOMETHING, even standing fourth in a queue.
_QUIET_TRACK_MIN = 240
_QUIET_TRACK_MAX = 700


def _trim_prior(
    prior: list[dict[str, str]], *, kin_count_hint: int = 0
) -> list[dict[str, str]]:
    """Keep the room hearable without letting it eat the payload.

    A kin hears the LAST few speakers, not all of them — which is also how a real
    room works: you respond to what was just said, not to everything said since
    the film started. Keeping all eight is what pushed Ellen's packet to 7,773
    characters and cost her the scene entirely.
    """
    kept = prior[-_MAX_PRIOR_KINS:]
    if len(prior) > len(kept):
        log.info(
            "trimming prior for a room of %d: %d speakers -> the last %d",
            kin_count_hint, len(prior), len(kept),
        )
    out: list[dict[str, str]] = []
    for p in kept:
        emote = (p.get("emote") or "")[:_MAX_PRIOR_CHARS]
        spoken = (p.get("spoken") or "")[:_MAX_PRIOR_CHARS]
        out.append({"name": p.get("name", ""), "emote": emote, "spoken": spoken})
    return out


def _fit_scene(scene: str, *, overhead: int) -> str:
    """Give the scene whatever room is left, and say so when it gets squeezed.

    The scene is the only elastic part — everything else (Tim's words, the other
    kins' replies, how this one is feeling) is structural and can't be cut without
    breaking something. So it absorbs the pressure, and we cut it HERE, on a
    sentence boundary, rather than letting the packet blow the cap and get thrown
    away downstream.
    """
    budget = settings.kindroid_char_limit - overhead - _PAYLOAD_RESERVE
    if len(scene) <= budget:
        return scene
    if budget < _MIN_SCENE_CHARS:
        log.warning(
            "payload overhead is %d chars — no room left for the scene at all", overhead
        )
        return ""
    cut = scene[:budget]
    # Prefer a sentence end, so the kin isn't handed half a sentence.
    for stop in (". ", "! ", "? "):
        i = cut.rfind(stop)
        if i > budget * 0.6:
            cut = cut[: i + 1]
            break
    log.info("scene trimmed %d -> %d chars to fit the payload", len(scene), len(cut))
    return cut.strip()


async def _relay_to_kin(
    session_id: str,
    kin: "characters.Character",
    *,
    scene: str,
    history: str,
    presence: str,
    dialogue: str,
    reaction: str,
    prior: list[dict[str, str]],
    mood: Optional[str],
    react_only: bool = False,
    addressed_names: Optional[list[str]] = None,
    barged_in: str = "",
) -> Optional[dict[str, str]]:
    """One hop of the relay: package → send → normalize → persist → broadcast.

    `barged_in` is set when the coordinator promoted this kin — someone Tim did
    NOT tap, who leaned in anyway. It's the reason, shown to Tim on the ❗ badge.

    Returns {name, emote, spoken} so the caller can hand this kin's reaction to
    the next one in the circle. None when the kin didn't answer.
    """
    if _pipeline_paused():
        return None

    # THE PRIVATE CHANNEL. Each kin gets their own Kindroid message, so anything
    # put here is heard by them and nobody else.
    #
    #   • How they FEEL — second person, safe precisely because the audience is
    #     one. Computed per kin, or a room would tell Bobby he's high because
    #     Eli is.
    #   • What Tim murmurs to his partner — never broadcast. Passing the joint is
    #     public (the room watches him do it); what he says close to Eli's ear
    #     while the others watch the film is not.
    kin_level, kin_method, _ = await stoned_current_state(kin.key)
    stoned = state_directive(kin_level, kin_method, kin_key=kin.key)
    aside = private_aside(kin_level, intimate=kin.romantic, kin_key=kin.key)
    if aside:
        stoned = f"{stoned}\n\n{aside}" if stoned else aside

    # ─── WHAT THEY'VE BEEN SITTING WITH ───────────────────────────────
    #
    # Silence is cheap because a silent kin gets no Kindroid message at all — but
    # that leaves a HOLE in their thread. They don't know what Tim said, what the
    # others said, or (worse) what has happened in the FILM. Come back after six
    # turns and the movie has moved on without you.
    #
    # So when someone returns from a stretch of quiet, they get caught up. The
    # framing is the important part and it's in the prompt: THEY WERE NOT AWAY.
    # They sat right there, watching, listening, not speaking. This isn't a
    # briefing on what they missed — it's what they've been sitting with. Get that
    # wrong and Bobby comes back like a man who stepped out for a cigarette; get it
    # right and he comes back like a man who's been listening to you bang on about
    # a Nintendo for ten minutes.
    #
    # One cheap text call, and only when someone actually returns. Most turns pay
    # nothing. If it fails, the turn goes out without it.
    # TWO TRACKS, not one paragraph. Every kin carries their own pair, measured from
    # THEIR last message — so they're different for everyone in the room:
    #
    #   film — what's happened ON SCREEN since they last spoke. Gives them something
    #          to react to. A kin who's been quiet twenty minutes has watched twenty
    #          minutes of movie.
    #   room — what's happened BETWEEN THE PEOPLE since they last spoke. Tells them
    #          where they STAND: who's had the floor, and whether anyone said their
    #          name. That last part is the whole reason these are separate — blended
    #          into one paragraph, "Tim asked you a direct question and you let it go
    #          by" reads exactly like "the others were chatting".
    # THE PRIOR IS TRIMMED FIRST, because how deep this kin sits in the circle is
    # what decides whether anyone has room to breathe. Speaking first (83% of all kin
    # messages, measured) means no prior block at all and ~700 spare characters;
    # speaking fourth means carrying 1,500 characters of other people's replies.
    prior = _trim_prior(prior, kin_count_hint=len(addressed_names or []) + len(prior))
    prior_chars = sum(
        len(p.get("emote", "")) + len(p.get("spoken", "")) + 30 for p in prior
    )

    quiet_film = quiet_room = ""
    stretch = await build_quiet_stretch(session_id, kin.key)
    if stretch and stretch["tim_turns"] >= _CATCHUP_AFTER_TURNS:
        plex_now = plex_monitor.current_state()
        await _stage(f"Catching {kin.first_name} up…")
        # SIZE THE TRACKS TO WHAT'S ACTUALLY FREE, don't guess at a flat cap.
        #
        # A flat 300 each was leaving ~700 characters unspent on the vast majority of
        # messages — the scene fits whole with room over, and we were asking Gemini
        # for a thinner catch-up than the budget could afford. Measure, then ask.
        #
        # The scene keeps first claim on the space (it's what everyone is reacting to
        # RIGHT NOW), but never at the cost of the tracks' floor: a kin who's been
        # quiet ten turns must be told SOMETHING, even in a crowded circle.
        free = settings.kindroid_char_limit - _PAYLOAD_RESERVE - (
            _DIRECTIVE_CHARS + len(stoned) + len(presence) + len(history)
            + len(reaction) + len(dialogue) + prior_chars
        )
        scene_claim = min(len(scene), max(0, free - 2 * _QUIET_TRACK_MIN))
        per_track = max(_QUIET_TRACK_MIN,
                        min(_QUIET_TRACK_MAX, (free - scene_claim) // 2))
        tracks = await gemini_brain.quiet_stretch_narration(
            kin_name=kin.first_name,
            said=stretch["said"],
            scenes=stretch["scenes"],
            movie_title=(plex_now.get("display_title") or plex_now.get("title") or "")
            if plex_now else "",
            max_chars=per_track,
        )
        quiet_film, quiet_room = tracks["film"], tracks["room"]
        if quiet_film or quiet_room:
            log.info(
                "catching %s up on %d quiet turns — %d free, %d/track "
                "-> film:%dch room:%dch",
                kin.key, stretch["tim_turns"], free, per_track,
                len(quiet_film), len(quiet_room),
            )

    # BUDGET THE INPUT, DON'T JUST REJECT THE OUTPUT.
    #
    # The relay hands each kin every previous kin's reply as narration — that's
    # the entire point, it's what lets Bobby hear Eli. But it means the payload
    # GROWS DOWN THE CIRCLE, and the last kin carries the fattest one. In a room
    # of eight, Ellen's packet was arriving at 7,773 characters against a 4,000
    # cap, so `_verify_packet` rejected it, the mechanical fallback fired, and
    # THAT was over too — so it dropped the scene. Six of eight kins were
    # replying to a film they'd been told nothing about.
    #
    # The handoff predicted exactly this ("the last kin in the circle carries the
    # fattest payload"); the room just hadn't been big enough to hit it before.
    #
    # Asking Claude to fit an impossible budget doesn't work — it can't summarise
    # its way out of 7,773 chars of required content without paraphrasing Tim,
    # which is the one thing it must never do. So trim BEFORE the call.
    # (`prior` was trimmed further up — the catch-up budget depends on it.)
    scene = _fit_scene(
        scene,
        overhead=_DIRECTIVE_CHARS + len(stoned) + len(presence) + len(history)
        + len(reaction) + len(dialogue) + len(quiet_film) + len(quiet_room)
        + prior_chars,
    )

    # Claude writes the body — including rendering the others' reactions as
    # third-person narration. If it produces something that breaks the format
    # contract, fall back to the mechanical builder rather than sending junk.
    body: Optional[str] = None
    try:
        await _stage(f"Claude is writing {kin.first_name}'s message…")
        body = await coordinator.package_turn(
            kin=kin,
            pronouns=characters.build_persona(kin).get("pronouns", "they/them"),
            scene=scene,
            history=history,
            stoned=stoned,
            presence=presence,
            quiet_film=quiet_film,
            quiet_room=quiet_room,
            dialogue=dialogue,
            reaction=reaction,
            prior=prior,
            react_only=react_only,
            addressed_names=addressed_names,
            session_id=session_id,
        )
    except coordinator.CoordinatorError as e:
        log.warning("coordinator unavailable for %s (%s) — mechanical payload", kin.key, e)

    if body is None:
        # THE FALLBACK MUST NOT UNDO THE FIX. `typed_dialogue` goes out as RAW
        # text, which Kindroid renders as Tim speaking straight at the recipient.
        # For a kin who only OVERHEARD the remark, that's the whole "Bobby thinks
        # he's been called Kiddo" bug walking back in through the back door. So
        # when the packet is for someone Tim wasn't addressing, his words become
        # narration here too — named, quoted, inside an emote.
        spoken_to = (not addressed_names) or (kin.first_name in addressed_names)
        fb_dialogue = dialogue if spoken_to else ""
        fb_reaction = reaction
        if dialogue and not spoken_to:
            who = _join_names(addressed_names or [])
            overheard = f'Tim turns to {who} and says, "{dialogue.strip()}"'
            fb_reaction = f"{reaction}\n\n{overheard}" if reaction else overheard

        body = build_payload(
            scene_narration=scene,
            history_narrative=history,
            stoned_narration=stoned,
            presence_narration=presence,
            quiet_film=quiet_film,
            quiet_room=quiet_room,
            reaction_narration=fb_reaction,
            typed_dialogue=fb_dialogue,
        )
        if len(body) > settings.kindroid_char_limit:
            # Drop the SCENE, never the catch-up: the scene is one moment, the
            # catch-up is everything they'd otherwise have no memory of at all.
            log.warning("fallback payload for %s is over the cap — dropping scene", kin.key)
            body = build_payload(
                scene_narration="",
                history_narrative=history,
                stoned_narration=stoned,
                presence_narration=presence,
                quiet_film=quiet_film,
                quiet_room=quiet_room,
                reaction_narration=fb_reaction,
                typed_dialogue=fb_dialogue,
            )

    await manager.broadcast(
        {"type": "eli_typing", "on": True, "character_key": kin.key, "name": kin.first_name}
    )
    await _stage(f"{kin.first_name} is answering…")
    try:
        reply = await send_message(body, ai_id=kin.ai_id)
    except KindroidError as e:
        log.exception("relay to %s failed", kin.key)
        await _system_message(session_id, f"{kin.first_name} couldn't respond — {str(e)[:160]}")
        return None
    finally:
        await manager.broadcast({"type": "eli_typing", "on": False, "character_key": kin.key})

    raw = (reply.get("raw") or "").strip()
    if not raw:
        await _system_message(session_id, f"{kin.first_name}'s response came back empty.")
        return None

    # Claude re-labels the reply into canonical segments, fixing whatever markup
    # dialect Kindroid used this time. Falls back to the regex parser internally.
    segments = await coordinator.normalize_reply(
        raw, kin_name=kin.first_name, session_id=session_id
    )
    emote_text, spoken_text = coordinator.segments_to_texts(segments)

    latest_tim = await db.fetch_one(
        "SELECT stoned_level_tim FROM messages WHERE session_id = ? "
        "AND sender = 'tim' ORDER BY id DESC LIMIT 1",
        (session_id,),
    )
    msg_id = await db.add_message(
        session_id,
        "eli",
        content=raw,
        emote_text=emote_text or None,
        spoken_text=spoken_text or None,
        scene_context=None,
        mood=mood,
        stoned_level_tim=int(latest_tim["stoned_level_tim"] or 0) if latest_tim else 0,
        stoned_level_eli=kin_level,
        latency_ms=reply.get("latency_ms"),
        character_key=kin.key,
    )
    # A barge-in has to be VISIBLE. The system just overrode Tim's choice of who to
    # talk to; a room that silently changes who's speaking to you is creepy, one
    # that shows its hand is a room-mate leaning in. The reason rides along so he
    # can see WHY it happened — and it forces the bubble open even for a muted kin,
    # which is coherent: mute here isn't silence, it's "collapse their bubbles". A
    # barge-in is a notification breaking through Do Not Disturb.
    if segments or barged_in:
        blob: dict[str, Any] = {}
        if segments:
            blob["segments"] = segments
        if barged_in:
            blob["barged_in"] = barged_in
        await db.execute(
            "UPDATE messages SET segments_json = ? WHERE id = ?",
            (json.dumps(blob), msg_id),
        )
    row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (msg_id,))
    if row:
        await manager.broadcast(
            {"type": "message", "message": _message_to_payload(row_to_dict(row) or {})}
        )
    await _broadcast_cost(session_id)

    return {"name": kin.first_name, "emote": emote_text, "spoken": spoken_text}


async def _system_message(session_id: str, text: str) -> None:
    """Post a system line and push it to the UI."""
    await db.add_message(session_id, "system", text)
    row = await db.fetch_one(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
        (session_id,),
    )
    if row:
        await manager.broadcast(
            {"type": "message", "message": _message_to_payload(row_to_dict(row) or {})}
        )


async def _room_note(
    session_id: str, *, turn_id: int, sat_out: list[dict]
) -> None:
    """"🤐 Jeff and Adam sat this one out" — and each name is a tap target.

    THIS LINE EXISTS SO THERE IS SOMETHING TO CLICK.

    A kin who stays quiet produces no Kindroid call and therefore no message row —
    that is the whole design of the cheap room. But it means that when the room gets
    it wrong by leaving someone OUT, there is nothing on screen to point at and no way
    to tell us so. The report button on a message can only ever catch the opposite
    mistake.

    It's a real `messages` row, so it survives a reload and scrolls with the
    conversation. The turn id rides in `segments_json`, so the report knows exactly
    which decision Tim is complaining about — no reconstruction, no guessing.
    """
    if not sat_out:
        return
    names = [s["name"] for s in sat_out]
    if len(names) == 1:
        text = f"{names[0]} sat this one out"
    elif len(names) == 2:
        text = f"{names[0]} and {names[1]} sat this one out"
    else:
        text = f"{', '.join(names[:-1])} and {names[-1]} sat this one out"

    msg_id = await db.add_message(session_id, "room_note", text)
    await db.execute(
        "UPDATE messages SET segments_json = ? WHERE id = ?",
        (json.dumps({"turn_id": turn_id, "sat_out": sat_out}), msg_id),
    )
    row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (msg_id,))
    if row:
        await manager.broadcast(
            {"type": "message", "message": _message_to_payload(row_to_dict(row) or {})}
        )


async def _run_relay(
    session_id: str,
    *,
    scene: str,
    history: str,
    dialogue: str = "",
    reaction: str = "",
    mood: Optional[str] = None,
    tim_situation: str = "",
    scene_situation: str = "",
    tim_message_id: Optional[int] = None,
) -> None:
    """Pass the mic around the room.

    The addressed kins speak in sequence, each one hearing what the last said.
    Everyone else in the room then gets the whole packet at once, told to react
    rather than hold forth — those fire concurrently, since they don't depend on
    each other.

    `tim_message_id` is the TURN'S ANCHOR. Every decision this function makes — who
    spoke, who passed, who leaned in, and why — used to be broadcast over a WebSocket
    and then thrown away. It is now frozen against that message id, along with every
    fact that was true at the time, so Tim can come back an hour later and tell us we
    got it wrong.
    """
    room, addressed = await _room_and_addressed()
    if not room:
        await _system_message(session_id, "No one's in the room — pick who's watching with you.")
        return

    # NOTE — there was briefly a "first-person guard" here, rewriting `reaction`
    # into the third person on the theory that Kindroid renders an emote as the
    # RECIPIENT's action, so "I snort" would reach Bobby as *Bobby* snorting.
    #
    # THAT IS WRONG, and it broke a working feature for one evening. Kindroid
    # renders the OUTGOING message as the SENDER's — and the sender is Tim. So
    # `_(* I snort *)_` means TIM snorts, to every recipient. First person is the
    # convention for Tim's own actions, and it always was:
    #
    #   HANDOFF-multi-kin-room.md:71, the canonical PUBLIC emote —
    #       _(* I hold out the bag and Bobby reads the label first… *)_
    #   stoned_tracker.py — "Tim's POV of passing the substance… first person"
    #   REACTION_SYSTEM_PROMPT — "FIRST PERSON (Tim's voice)… do NOT refer to
    #                             Tim in third person"
    #
    # The real landmine in the handoff is a different one: SECOND person ("you")
    # broadcast to a room, and RAW TEXT aimed at one kin being overheard by
    # another. Both are handled in `_relay_to_kin` / the coordinator. Don't
    # "fix" first person again.

    presence = await _presence_standing_line()

    # ═══ FREEZE THE FACTS ═══════════════════════════════════════════════
    #
    # Every fact about right now — mood, venue, who's in the room, who's REALLY in
    # the room, how stoned everyone is, what we're watching, how far into it, whether
    # it's a rewatch, what time it is. ~20 values, all read from the thing that
    # actually knows them. No model, no guessing, no cost.
    #
    # It's frozen HERE, before anyone decides anything, for two reasons: the rules are
    # evaluated against it, and Tim can report this turn an hour from now — by which
    # time Daisy will have gone home and the mood will have changed. The rule he
    # writes must be scoped to what was true WHEN THE ROOM GOT IT WRONG.
    all_settings = await db.get_all_settings()
    quiet_all = await _turns_since_spoke(session_id, room)
    try:
        ctx = await facts.build_context(
            session_id=session_id,
            plex=plex_monitor.current_state(),
            mood=mood,
            tim_situation=tim_situation,
            scene_situation=scene_situation,
            room_keys=[c.key for c in room],
            addressed_keys=[c.key for c in addressed],
            settings_map=all_settings,
            quiet_turns=quiet_all,
            lean_history=_LEAN_LOG.get(session_id, [])[-_LEAN_WINDOW:],
        )
    except Exception:
        # A missing fact must never kill a turn — it just means no rule can fire.
        log.exception("could not read the facts for this turn — rules are off for it")
        ctx = facts.TurnContext()

    # ═══ THE RULES ══════════════════════════════════════════════════════
    #
    # Evaluated in PYTHON, against the exact values above. A rule either applies or
    # it doesn't — no judgment, no classifier, nothing to get wrong.
    #
    #   always / never   -> a LAW.  Enforced below, BEFORE the coordinator is called.
    #                              A kin a law excludes is not discouraged in the
    #                              prompt; they are not IN the prompt.
    #   usually / rarely -> a LEAN. Rides into the prompt as guidance.
    rule_splits: dict[str, facts.RuleSplit] = {}
    fired_rule_ids: list[int] = []
    for c in room:
        try:
            kin_rules = await db.get_kin_rules(c.key)
        except Exception:
            log.exception("could not load %s's rules — treating them as none", c.key)
            kin_rules = []
        split = facts.split_rules(kin_rules, ctx, c.key)
        rule_splits[c.key] = split
        for r in ([split.law] if split.law else []) + split.leans:
            if r.get("id"):
                fired_rule_ids.append(int(r["id"]))
    if fired_rule_ids:
        await db.bump_rule_hits(fired_rule_ids)

    # TIM NAMING SOMEONE OUTRANKS EVERYTHING. Search the whole ROOM, not just who
    # he tapped — "Bobby, what do you think?" has to reach Bobby even if Bobby
    # wasn't selected. He is then ADDRESSED for this turn: he speaks, he goes to
    # the front of the mic, and he gets Tim's words as RAW TEXT rather than as
    # overheard narration, because they were said to him.
    named = _named_in(dialogue, room)
    addressed_keys = {c.key for c in addressed}
    pulled_in = [c for c in named if c.key not in addressed_keys]
    if pulled_in:
        log.info("named by Tim, pulled into the mic: %s", ", ".join(c.key for c in pulled_in))
        addressed = named + [c for c in addressed if c.key not in {n.key for n in named}]

    # A TAP IS AN INVITATION, NOT A SUMMONS.
    #
    # It used to be law: every tapped kin answered every message. So four people left
    # tapped meant four replies to "God, I hate this episode" — and the whole point of
    # a room is that most of it is people watching a film. The coordinator can now let a
    # tapped kin PASS when the remark plainly wasn't for them.
    #
    # Guarded in coordinator.decide_mic_order: anyone Tim NAMED can never be passed, and
    # `order` is never empty. Someone always answers him.
    order_keys = [c.key for c in addressed]
    passed: list["characters.Character"] = []
    pass_reasons: dict[str, str] = {}

    # ═══ THE LAWS EXECUTE HERE — BEFORE ANY MODEL IS CALLED ═════════════
    #
    # This is what makes a law a law. A kin a `never` rule excludes is not merely
    # DISCOURAGED in the prompt — they are not IN the prompt. A model cannot politely
    # disagree with a rule it never sees, and "he NEVER heckles in a public theatre"
    # is not a suggestion.
    #
    # The one thing that outranks a law: TIM NAMING SOMEONE. He asked them a direct
    # question. Not answering it is the worst thing this room can do, and no rule he
    # ever wrote was meant to make that happen.
    named_keys = {c.key for c in named}
    law_silenced: list["characters.Character"] = []
    law_forced: list["characters.Character"] = []
    law_reasons: dict[str, str] = {}

    for c in room:
        split = rule_splits.get(c.key)
        if not split or not split.law:
            continue
        if c.key in named_keys:
            log.info("law for %s is overridden — Tim named him", c.key)
            continue
        if split.forces_silence:
            law_silenced.append(c)
            law_reasons[c.key] = split.law["rule_text"]
        elif split.forces_speech:
            law_forced.append(c)
            law_reasons[c.key] = split.law["rule_text"]

    if law_silenced:
        silenced_keys = {c.key for c in law_silenced}
        addressed = [c for c in addressed if c.key not in silenced_keys]
        log.info("LAW — silent: %s", ", ".join(f"{c.key} ({law_reasons[c.key]})"
                                               for c in law_silenced))
    if law_forced:
        have = {c.key for c in addressed}
        for c in law_forced:
            if c.key not in have:
                addressed.append(c)
        log.info("LAW — speaks: %s", ", ".join(f"{c.key} ({law_reasons[c.key]})"
                                               for c in law_forced))

    # A room silenced entirely BY TIM'S OWN RULES is obeyed — "nobody talks over the
    # credits" is a real rule and he meant it. But it is never silent about it. A room
    # that goes quiet for reasons he cannot see is the one failure we refuse to ship.
    if not addressed and law_silenced:
        await _system_message(
            session_id,
            f"Nobody answered — {len(law_silenced)} rule"
            f"{'s' if len(law_silenced) != 1 else ''} you wrote said they shouldn't.",
        )
        return

    order_keys = [c.key for c in addressed]
    mic_rationale = ""
    if len(addressed) > 1:
        try:
            await _stage("Claude is deciding who speaks…")
            decision = await coordinator.decide_mic_order(
                addressed=addressed,
                scene=scene,
                mood=mood or "",
                message=dialogue,
                turns_since_spoke=await _turns_since_spoke(session_id, addressed),
                named=[c.key for c in named],
                # A kin a LAW already forced in is not up for debate. The coordinator
                # orders them; it does not get to reconsider whether they speak.
                locked_in=[c.key for c in law_forced],
                rules={c.key: facts.for_prompt(rule_splits[c.key], c.first_name)
                       for c in addressed if c.key in rule_splits},
                session_id=session_id,
            )
            order_keys = decision.order
            by_addr = {c.key: c for c in addressed}
            passed = [by_addr[p.key] for p in decision.passing if p.key in by_addr]
            pass_reasons = {p.key: p.reason for p in decision.passing}
            mic_rationale = decision.rationale or ""
            log.info("mic order: %s — %s", order_keys, decision.rationale)
        except coordinator.CoordinatorError as e:
            log.warning("mic order unavailable (%s) — everyone tapped speaks", e)
    if named:
        # Whoever he addressed by name goes first, in the order he said them.
        head = [c.key for c in named]
        order_keys = head + [k for k in order_keys if k not in set(head)]
        log.info("mic forced to %s — Tim named them", ", ".join(head))

    by_key = {c.key: c for c in room}
    speakers = [by_key[k] for k in order_keys if k in by_key]
    others = [c for c in room if c.key not in {k.key for k in speakers}]

    # WHO Tim was actually talking to. Everyone else in the room only OVERHEARD
    # it, and must be told so — Kindroid renders raw text as Tim speaking straight
    # at the recipient, so without this a remark aimed at Tommy lands on Bobby and
    # Daisy as though it had been said to them.
    addressed_names = [c.first_name for c in speakers]

    # ─── WHO ACTUALLY SPEAKS ──────────────────────────────────────────
    #
    # SILENCE IS THE DEFAULT. This used to be the opposite: every kin in the room
    # replied to every single message — the addressed ones fully, everyone else as
    # "react-only". Nobody was ever quiet. Say "I wanted a Nintendo" to a room of
    # eight and you got eight replies and waited five minutes for them.
    #
    # That made a barge-in meaningless: it can't make someone APPEAR when nobody
    # is ever absent. Promoting Bobby only made his reply longer.
    #
    # So now:
    #   • Tim's taps       → speak. LAW. Nothing here can touch that.
    #   • the coordinator  → may RAISE A HAND for up to 2 others whose sheets say
    #                        this moment is squarely theirs (❗ in the UI).
    #   • the long-quiet   → get pulled in so nobody sits mute all night.
    #   • everyone else    → SILENT. No Kindroid call at all.
    #
    # The coordinator may only ever ADD a voice, never remove one — that asymmetry
    # is the safety property. Tim can always see who was added, and why.
    #
    # This is also the fix for what the handoff calls the unsolved wall ("a room of
    # N is roughly N × 30 seconds"). Eight Kindroid calls become two or three.
    leaners: list["characters.Character"] = []
    barge_reasons: dict[str, str] = {}
    floor: list["characters.Character"] = []
    # WHO TIM TAPPED AND WHO STAYED QUIET ANYWAY, with the reason. Two ways in:
    #   • the coordinator PASSED them — the remark plainly wasn't theirs
    #   • a PRIVATE MOMENT — he was talking to one person and the room stood down
    # Both are the coordinator taking a voice away, so both are shown to Tim.
    held_back: list["characters.Character"] = list(passed)
    held_reasons: dict[str, str] = dict(pass_reasons)

    # A kin who just passed does NOT get to lean in, and is NOT pulled off the fairness
    # floor. "This wasn't for you" and "…but here he is anyway" is not a room reading a
    # moment, it's a bug with a badge on it.
    #
    # The same, harder, for anyone a LAW silenced: a law that could be undone by a
    # lean-in or by being overdue for a turn is not a law at all. They are out of the
    # pool entirely — and they carry their reason, so Tim sees his own rule at work.
    law_silent_keys = {c.key for c in law_silenced}
    for c in law_silenced:
        if c not in held_back:
            held_back.append(c)
            held_reasons[c.key] = f"your rule: {law_reasons.get(c.key, '')}"
    barge_pool = [
        c for c in others
        if c.key not in {p.key for p in passed} and c.key not in law_silent_keys
    ]

    if barge_pool and speakers:
        quiet = await _turns_since_spoke(session_id, barge_pool)
        try:
            await _stage("Anyone else want in?")
            called, barge, call = await coordinator.decide_barge_ins(
                candidates=barge_pool,
                speaking=speakers,
                scene=scene,
                mood=mood or "",
                message=dialogue or reaction,
                turns_since_spoke=quiet,
                recent_lean_ins=_LEAN_LOG.get(session_id, [])[-_LEAN_WINDOW:],
                limit=_MAX_BARGE_INS,
                session_id=session_id,
            )
            # HE SPOKE TO THEM. THEY ANSWER — first, and with his words as RAW TEXT,
            # because they were said TO them. Not a barge-in, not a judgment call,
            # not subject to the silence rule.
            #
            # This is the safety net for the fact that a regex over the alias list
            # cannot keep up with how Tim actually types, especially high. He wrote
            # "gram", her alias says "gran", and she sat silent while two other
            # people answered a question he had asked his grandmother.
            if called:
                log.info("Tim addressed by name: %s", ", ".join(c.key for c in called))
                speakers = called + [c for c in speakers if c.key not in {n.key for n in called}]
                addressed_names = [c.first_name for c in speakers]
                others = [c for c in others if c.key not in {n.key for n in called}]
                barge_pool = [c for c in barge_pool if c.key not in {n.key for n in called}]
                # If he named someone the coordinator had just passed, the name wins.
                held_back = [c for c in held_back if c.key not in {n.key for n in called}]

            by_other = {c.key: c for c in barge_pool}
            leaners = [by_other[b.key] for b in barge if b.key in by_other]
            barge_reasons = {b.key: b.reason for b in barge}

            # ─── HE'S TALKING TO HIS FAMILY ───────────────────────────────
            #
            #   "That's all the Supernatural I can do today — I've got a meeting with
            #    my boss in 30 minutes. Want to do this again tomorrow?"
            #
            # A goodbye, a piece of his life, and a question, all in one. EVERY PERSON
            # IN THE ROOM ANSWERS THAT — the ones he tapped and the ones just watching.
            # You do not sit in silence while someone says goodbye to you.
            #
            # This has to be forced, because the affinity sheets will confidently
            # conclude that nobody "lights up on scheduling" and mute the entire room.
            # The sheets tell you who cares about a plot hole. They tell you NOTHING
            # about who answers when the man asks his family a question.
            if call.to_the_room:
                # ...EXCEPT anyone one of Tim's own LAWS has silenced.
                #
                # `to_the_room` is a judgment the coordinator made. A law is a rule TIM
                # wrote. The precedence stack is absolute and this is where it would
                # quietly invert: without this filter, a goodbye would resurrect the kin
                # whose law says "never speaks in a public theatre" — and the law would
                # be worth nothing. If he wants an answer from that person, he can say
                # their name; THAT beats a law, and nothing else does.
                extra = [
                    c for c in others
                    if c.key not in {s.key for s in speakers}
                    and c.key not in law_silent_keys
                ]
                speakers = speakers + extra
                addressed_names = [c.first_name for c in speakers]
                others = [c for c in law_silenced]
                barge_pool = []
                # Nobody PASSES on a goodbye — but a law still holds, and Tim still
                # sees it hold.
                held_back = [c for c in law_silenced]
                held_reasons = {c.key: f"your rule: {law_reasons.get(c.key, '')}"
                                for c in law_silenced}
                leaners, barge_reasons = [], {}   # you don't "lean in" to a question asked of you
                log.info(
                    "TO THE ROOM — all %d answer: %s%s",
                    len(speakers), ", ".join(c.key for c in speakers),
                    f" (except {', '.join(c.key for c in law_silenced)} — your rules)"
                    if law_silenced else "",
                )

            private = call.private

            # ─── THE ONE TIME THE ROOM STANDS DOWN ────────────────────────
            #
            # "Babe, I love you" is not a cue for a film observation. Everywhere else
            # in this file the coordinator may only ADD a voice, never remove one —
            # that asymmetry is the safety property, because a coordinator that can
            # silence people will eventually silence someone Tim wanted and he'll
            # never know why.
            #
            # This is the deliberate exception, and it's narrow on purpose:
            #   • it only fires when Tim is speaking intimately to specific people
            #   • somebody ALWAYS still speaks — the ones it's between
            #   • Tim SEES it happen, with a reason, on the room panel
            #
            # That last point is what keeps the safety property intact. The failure
            # mode we're avoiding is a SILENT demotion. This one announces itself.
            if private:
                keep = {k for k in private.keys}
                stood_down = [c for c in speakers if c.key not in keep]
                if stood_down and len(stood_down) < len(speakers):
                    held_back = held_back + stood_down
                    for c in stood_down:
                        held_reasons[c.key] = private.reason
                    speakers = [c for c in speakers if c.key in keep]
                    addressed_names = [c.first_name for c in speakers]
                    others = others + stood_down
                    log.info(
                        "private moment — %s hold back (%s)",
                        ", ".join(c.key for c in stood_down),
                        private.reason,
                    )
                # Nobody leans in over a moment like this, and nobody gets pulled in
                # off the fairness floor either — being overdue for a turn is not a
                # reason to talk across "I love you".
                leaners, barge_reasons = [], {}
        except Exception:
            # A barge-in is a bonus, never a dependency. If it breaks, the turn is
            # simply Tim's chosen kins and nobody else.
            log.exception("barge-in failed — nobody leans in")

        # THE FAIRNESS FLOOR. Affinity alone will leave someone out in the cold —
        # a kin whose sheet never matches a horror film could sit silent for two
        # hours and read as broken rather than as quiet. So anyone who hasn't
        # spoken in a while gets pulled in regardless, react-only and concurrent,
        # which costs no wall-clock.
        #
        # Suppressed entirely when anyone was held back — a passed kin, or a private
        # moment. "This wasn't for you, but you're overdue, so here you are" is not a
        # room reading a moment.
        chosen = {c.key for c in speakers} | {c.key for c in leaners}
        held_keys = {c.key for c in held_back}
        floor = [] if held_back else [
            c for c in others
            if c.key not in chosen and c.key not in held_keys
            and quiet.get(c.key, 99) >= _QUIET_FLOOR_TURNS
        ][:_MAX_FLOOR_PULLS]

    # Log how loud this turn came out, so the coordinator can pace the NEXT one
    # against what it actually did rather than against an adjective.
    _LEAN_LOG.setdefault(session_id, []).append(len(leaners))
    del _LEAN_LOG[session_id][:-_LEAN_WINDOW]

    held_keys = {c.key for c in held_back}
    silent = [
        c for c in others
        if c.key not in ({k.key for k in leaners} | {k.key for k in floor} | held_keys)
    ]
    if silent or held_back:
        log.info(
            "speaking: %s%s%s | passed: %s | silent: %s",
            ", ".join(c.key for c in speakers) or "(nobody)",
            "".join(f" +❗{c.key}" for c in leaners),
            "".join(f" +floor:{c.key}" for c in floor),
            ", ".join(c.key for c in held_back) or "-",
            ", ".join(c.key for c in silent) or "-",
        )

    # PUT THE ROOM ON SCREEN. Every one of these decisions — who Tim addressed, who
    # the coordinator gave the floor to and why, who's been quiet and for how long —
    # has existed ONLY as a log line. The app has been making genuinely interesting
    # judgments all evening and showing none of them.
    #
    # Nothing new is computed here. It's all already sitting in local variables.
    quiet_now = await _turns_since_spoke(session_id, room)
    # WHO ACTUALLY GOT THE MIC. This is the list, and it is not `speaking`.
    #
    # `speaking` is the coordinator's PLAN — the mic order it handed out. But people also
    # join by BARGING IN (`leaned_in`) and by being pulled in off the floor, and the relay
    # sends for all of them (see `speaking_order` below). So `speaking` is a plan, and this
    # is what happened.
    #
    # Reading `spoke` off `speaking` meant every barge-in was recorded as having stayed
    # QUIET. Bobby leaned in, spoke, Tim tapped 👍 to reinforce it — and the dialog asked him
    # to explain why Bobby was right to say nothing. A 👍 there would have written a rule
    # teaching the exact opposite of what he meant, and it would have looked correct the
    # whole way through.
    spoke_keys = (
        [c.key for c in speakers]
        + [c.key for c in leaners]
        + [c.key for c in floor]
        + [c.key for c in law_forced]
    )
    room_decision = {
        "spoke": list(dict.fromkeys(spoke_keys)),   # THE record of who talked
        "speaking": [c.key for c in speakers],      # the coordinator's mic order (the plan)
        "leaned_in": [
            {"key": c.key, "reason": barge_reasons.get(c.key, "")} for c in leaners
        ],
        "floor": [c.key for c in floor],   # pulled in because they'd gone quiet too long
        # Selected by Tim, but the moment was between him and someone else. The ONLY
        # place the coordinator takes a voice away — so it says so, out loud, with a
        # reason. A silent demotion would be the bug; this is the feature.
        "held_back": [
            {"key": c.key, "reason": held_reasons.get(c.key, "")} for c in held_back
        ],
        "passed": [
            {"key": c.key, "reason": pass_reasons.get(c.key, "")} for c in passed
        ],
        "law_silenced": [
            {"key": c.key, "reason": law_reasons.get(c.key, "")} for c in law_silenced
        ],
        "law_forced": [
            {"key": c.key, "reason": law_reasons.get(c.key, "")} for c in law_forced
        ],
        "silent": [c.key for c in silent if c.key not in held_keys],
        "quiet_turns": {c.key: quiet_now.get(c.key, 0) for c in room},
        # WHAT WAS IN FORCE, per person. This is what turns a 👍/👎 from an opinion into
        # a VERDICT ON A SPECIFIC RULE — the one thing statistics can honestly do here.
        # Recorded once, for everyone, so it works identically whether they spoke or not.
        "rules_in_force": {
            c.key: [
                int(r["id"])
                for r in (
                    ([rule_splits[c.key].law] if rule_splits[c.key].law else [])
                    + rule_splits[c.key].leans
                )
                if r.get("id")
            ]
            for c in room if c.key in rule_splits
        },
        # How stoned each of them was. Tim can't judge a silence without it.
        "stoned": {c.key: (ctx.get("self_stoned", c.key) or 0) for c in room},
    }
    await manager.broadcast({"type": "room_state", **room_decision})

    # ═══ FREEZE IT ══════════════════════════════════════════════════════
    #
    # Every judgment above — who spoke, who passed, who leaned in, whose own rule
    # silenced them, and WHY in each case — has until now lived for exactly as long as
    # a WebSocket frame and then ceased to exist. The app was making genuinely
    # interesting decisions all evening and keeping none of them.
    #
    # It goes to disk with the FACTS THAT WERE TRUE AT THE TIME. That pairing is the
    # whole point: an hour from now Daisy will have gone home and the mood will have
    # moved on, but when Tim says "Bobby shouldn't have said that", the rule he writes
    # has to be scoped to the room as it was WHEN THE ROOM GOT IT WRONG.
    turn_id: Optional[int] = None
    if tim_message_id is not None:
        try:
            turn_id = await db.add_turn_decision(
                session_id=session_id,
                tim_message_id=tim_message_id,
                facts_json=ctx.to_json(),
                decision_json=json.dumps(room_decision),
                rationale=mic_rationale,
            )
        except Exception:
            # Losing the record must never cost Tim his turn. He just can't report it.
            log.exception("could not freeze the turn decision — this turn can't be reported")

    # EVERY PERSON IN THE ROOM GETS A BUBBLE, INCLUDING THE ONES WHO SAID NOTHING.
    #
    # A kin who stays quiet makes no Kindroid call and therefore no message — that is the
    # whole design of the cheap room. But it means half the room has nothing on screen to
    # judge, and the feedback loop can only ever catch the room speaking too MUCH.
    #
    # THEIR STONED LEVEL RIDES ALONG, and it is not decoration: Tommy sitting out BLASTED
    # is a completely different event from Tommy sitting out SOBER, and Tim cannot judge
    # which one he's looking at without it. It's already in the frozen facts; it was just
    # never surfaced.
    quiet_ones = [c for c in held_back] + [c for c in silent if c.key not in held_keys]
    if turn_id and quiet_ones:
        await _room_note(
            session_id,
            turn_id=turn_id,
            sat_out=[
                {"key": c.key,
                 "name": c.first_name,
                 "reason": held_reasons.get(c.key, "") or law_reasons.get(c.key, ""),
                 "stoned": ctx.get("self_stoned", c.key) or 0,
                 # What was in force when he stayed quiet. A 👍 or 👎 here is a verdict
                 # on THESE rules, which is what makes the tap evidence rather than mood.
                 "rules": [
                     int(r["id"]) for r in (
                         ([rule_splits[c.key].law] if rule_splits.get(c.key) and rule_splits[c.key].law else [])
                         + (rule_splits[c.key].leans if rule_splits.get(c.key) else [])
                     ) if r.get("id")
                 ]}
                for c in quiet_ones
            ],
        )

    # Tell the UI the queue up front so it can show who's still to answer and lock
    # the composer. ONLY the people who will actually speak — listing a silent kin
    # here would leave them "pending" forever.
    speaking_order = [c.key for c in speakers] + [c.key for c in leaners] + [c.key for c in floor]

    async def _pending(keys: list[str]) -> None:
        await manager.broadcast(
            {"type": "relay", "pending": keys, "order": speaking_order}
        )

    outstanding = list(speaking_order)
    await _pending(outstanding)

    try:
        prior: list[dict[str, str]] = []

        # Tim's picks, then whoever leaned in — serially, so each one hears the
        # room as it actually stands by the time they open their mouth.
        for kin in speakers + leaners:
            if _pipeline_paused():
                return
            result = await _relay_to_kin(
                session_id, kin,
                scene=scene, history=history, presence=presence,
                dialogue=dialogue, reaction=reaction, prior=list(prior), mood=mood,
                addressed_names=addressed_names,
                barged_in=barge_reasons.get(kin.key, ""),
            )
            if result:
                prior.append(result)
            outstanding = [k for k in outstanding if k != kin.key]
            await _pending(outstanding)

        # The fairness pulls heard all of that. They react, they don't hold forth,
        # and they fire concurrently so they cost nothing in wall-clock.
        rest = floor
        if not rest or _pipeline_paused():
            return

        async def _react(kin):
            try:
                return await _relay_to_kin(
                    session_id, kin,
                    scene=scene, history=history, presence=presence,
                    dialogue=dialogue, reaction=reaction, prior=list(prior), mood=mood,
                    react_only=True, addressed_names=addressed_names,
                )
            finally:
                # They fire concurrently, so tick each one off as it lands rather
                # than waiting for the slowest.
                nonlocal outstanding
                outstanding = [k for k in outstanding if k != kin.key]
                await _pending(outstanding)

        await asyncio.gather(*(_react(kin) for kin in rest), return_exceptions=True)
    finally:
        # Whatever happened — crash, standby, a dead kin — the composer must
        # come back. A locked input box with no way out is the worst outcome here.
        await manager.broadcast({"type": "relay", "pending": [], "order": []})
        await _stage("")


async def _process_tim_message(
    msg_id: int, session_id: str, *, user_initiated: bool = True
) -> None:
    """Background pipeline after Tim sends a message.

    Extract clip from current Plex playhead, run Gemini scene analysis,
    optionally fetch trivia, persist on the message row, relay to Kindroid,
    and broadcast each step.

    `user_initiated=False` is set when we're processing an auto-generated
    emote (ingestion, reinforcement) so we don't cascade more auto-fires.
    """
    # Session-control hard-stop: standby/away skip the whole pipeline.
    if _pipeline_paused():
        return
    # Show the typing indicator for the full pipeline, not just Kindroid —
    # user wants continuous "something's happening" feedback.
    await manager.broadcast({"type": "eli_typing", "on": True})
    kindroid_called = False
    try:
        await _process_tim_message_body(msg_id, session_id, user_initiated)
    except Exception:
        log.exception("tim-message pipeline crashed")
        await db.add_message(session_id, "system", "Pipeline error — see server logs.")
        snap_row = await db.fetch_one(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        if snap_row:
            await manager.broadcast(
                {"type": "message", "message": _message_to_payload(row_to_dict(snap_row) or {})}
            )
    finally:
        # Guarantee the typing indicator always turns off, no matter what
        # branch the pipeline took. Idempotent if already off.
        await manager.broadcast({"type": "eli_typing", "on": False})


async def _process_tim_message_body(
    msg_id: int, session_id: str, user_initiated: bool
) -> None:
    """Inner pipeline body — split out so the outer wrapper can guarantee
    cleanup (typing indicator off, error surfacing) regardless of outcome.
    """
    plex = plex_monitor.current_state()
    if not plex or not plex.get("part_key"):
        await db.add_message(
            session_id,
            "system",
            "Scene capture skipped — no Plex stream active.",
        )
        await manager.broadcast(await _build_session_snapshot())
        return

    settings_map = await db.get_all_settings()
    try:
        capture_seconds = int(settings_map.get("capture_seconds") or settings.default_capture_seconds)
    except ValueError:
        capture_seconds = settings.default_capture_seconds
    view_offset_sec = (plex.get("view_offset_ms") or 0) / 1000.0
    stream_url = build_stream_url(plex["part_key"])

    clip_path: Optional[Path] = None
    scene_result: Optional[dict[str, Any]] = None

    # Compute dynamic emote budget for Kindroid payload:
    # total emote content = 2000 (cap) - dialogue - wrapper overhead (~30)
    # Reserve slots for history (~250) and stoned (~120) so scene doesn't
    # hog the whole budget when those apply.
    tim_msg_row = await db.fetch_one("SELECT content FROM messages WHERE id = ?", (msg_id,))
    tim_content = (tim_msg_row["content"] if tim_msg_row else "") or ""

    # SPLIT Tim's message into what he SAID and what he DID.
    #
    # Not everything on Tim's row is dialogue. The auto-fired emotes — handing
    # someone a gummy, noticing that Bobby has gone quiet — are stored as Tim
    # messages whose entire content is `_(*...*)_`. That's narration, not speech.
    #
    # Passing it to Claude as `dialogue` (which the format contract says must be
    # reproduced VERBATIM as raw text) asked it to print emote markup as if Tim
    # had said it out loud. Claude rightly refused, the verifier caught the
    # mismatch, and EVERY packet fell back to the mechanical payload — the whole
    # Claude packaging layer silently disabled, on every ingestion, all night.
    _, _, tim_segments = parse_reply(tim_content)
    tim_dialogue = "\n\n".join(
        s["text"] for s in tim_segments if s.get("type") == "spoken"
    ).strip()
    tim_narration = "\n\n".join(
        s["text"] for s in tim_segments if s.get("type") == "emote"
    ).strip()

    dialogue_len = len(tim_content)
    total_emote_budget = max(500, settings.kindroid_char_limit - dialogue_len - 40)

    history_text = await build_session_history_for_gemini(session_id)
    history_budget = 250 if history_text.strip() else 0

    # Tim's tracker retired — his body handles his own state; his message
    # tone communicates it to Eli. We only inject Eli's directive emote.
    tim_level = 0
    eli_level, eli_method, _ = await stoned_current_state("eli")
    stoned_line = eli_state_directive(eli_level, eli_method)
    stoned_budget = len(stoned_line) + 10 if stoned_line else 0

    scene_target = max(500, total_emote_budget - history_budget - stoned_budget)
    scene_target = min(1800, scene_target)

    scene_error_msg: Optional[str] = None
    try:
        await _stage("Grabbing the clip…")
        clip_path = await extract_clip(stream_url, view_offset_sec, capture_seconds)
    except FFmpegNotFound:
        scene_error_msg = "ffmpeg unavailable — commentary running without scene capture."
    except FFmpegError as e:
        log.exception("clip extraction failed")
        scene_error_msg = f"Scene capture failed ({type(e).__name__}) — continuing without it."

    # Scene analysis with model fallback: try Pro, then Flash, then give up
    # gracefully (text-only to Kindroid — per spec).
    if clip_path and not scene_error_msg:
        try:
            await _stage("Gemini is watching the scene…")
            scene_result = await gemini_brain.analyze_scene(
                clip_path,
                movie_title=plex.get("display_title") or plex.get("title"),
                timestamp_label=_format_hms(plex.get("view_offset_ms") or 0),
                target_chars=scene_target,
                session_history=history_text,
                history_budget=history_budget,
                tim_message=tim_content,
            )
        except GeminiError as primary_err:
            log.warning("Pro scene analysis failed, falling back to Flash: %s", primary_err)
            try:
                scene_result = await gemini_brain.analyze_scene(
                    clip_path,
                    movie_title=plex.get("display_title") or plex.get("title"),
                    timestamp_label=_format_hms(plex.get("view_offset_ms") or 0),
                    target_chars=scene_target,
                    session_history=history_text,
                    history_budget=history_budget,
                    model_override="gemini-2.5-flash",
                    tim_message=tim_content,
                )
                scene_error_msg = "Commentary running on Flash fallback — Pro unavailable."
            except GeminiError as fallback_err:
                log.exception("Flash fallback also failed")
                scene_error_msg = (
                    f"Scene analysis unavailable — {type(fallback_err).__name__}. "
                    "Commentary running without scene context."
                )
            except Exception as fallback_err:
                log.exception("Flash fallback crashed unexpectedly")
                scene_error_msg = (
                    f"Scene analysis unavailable — {type(fallback_err).__name__}. "
                    "Commentary running without scene context."
                )
        except Exception as e:
            log.exception("scene analysis crashed unexpectedly")
            scene_error_msg = (
                f"Scene analysis paused — {type(e).__name__}: {str(e)[:120]}"
            )

    if clip_path and clip_path.exists():
        try:
            clip_path.unlink()
        except OSError:
            pass

    if scene_error_msg:
        await db.add_message(session_id, "system", scene_error_msg)
        err_row = await db.fetch_one(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        if err_row:
            await manager.broadcast(
                {"type": "message", "message": _message_to_payload(row_to_dict(err_row) or {})}
            )

    # Auto-trivia retired — trivia is now exclusively user-triggered via the
    # poster menu. The `trivia` column on this row stays NULL.

    # Tim's leaf level is inferred by Gemini from his message tone (added to
    # the scene_result JSON). Fall back to 0 when scene analysis didn't run.
    if scene_result and scene_result.get("tim_stoned_level") is not None:
        tim_level = int(scene_result.get("tim_stoned_level") or 0)

    # Persist analysis fields on Tim's message row.
    await db.execute(
        """UPDATE messages
           SET scene_context = ?, mood = ?, trivia = ?, latency_ms = ?,
               stoned_level_tim = ?, stoned_level_eli = ?
           WHERE id = ?""",
        (
            scene_result.get("scene_description") if scene_result else None,
            scene_result.get("mood") if scene_result else None,
            None,
            scene_result.get("latency_ms") if scene_result else None,
            tim_level,
            eli_level,
            msg_id,
        ),
    )

    updated = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (msg_id,))
    if updated:
        await manager.broadcast(
            {"type": "message_updated", "message": _message_to_payload(row_to_dict(updated) or {})}
        )
    if scene_result and scene_result.get("mood"):
        await manager.broadcast({"type": "mood", "mood": scene_result["mood"]})
        _mark_full_scene_mood(scene_result["mood"])
    await _broadcast_cost(session_id)

    # Relay to Kindroid for Eli's reply. Per spec, we ALWAYS send to Kindroid
    # when there's something to say (dialogue or stoned line) — even when
    # scene analysis fully failed. Eli's reply is more important than
    # perfect scene context; graceful degradation beats stuck chat.
    scene_for_kindroid = scene_result.get("scene_description", "") if scene_result else ""
    history_for_kindroid = scene_result.get("history_narrative", "") if scene_result else ""
    mood_for_kindroid = scene_result.get("mood") if scene_result else None
    has_content = bool(
        tim_dialogue or tim_narration or scene_for_kindroid or stoned_line
    )
    if has_content:
        # Pass the mic around the room. With no room picked, this resolves to a
        # single kin and behaves exactly as it did before.
        await _run_relay(
            session_id,
            scene=scene_for_kindroid,
            history=history_for_kindroid,
            dialogue=tim_dialogue,
            reaction=tim_narration,
            mood=mood_for_kindroid,
            # BOTH triggers — what Tim did, and what the screen did. Free: they ride
            # in the scene analysis that already ran.
            tim_situation=(scene_result or {}).get("tim_situation", ""),
            scene_situation=(scene_result or {}).get("scene_situation", ""),
            tim_message_id=msg_id,
        )

    # Only user-initiated messages tick the reinforcement counter — we don't
    # want ingestion taps and reinforcement emotes to themselves trigger
    # more reinforcements.
    if user_initiated:
        await _maybe_fire_reinforcement(session_id)


async def _process_reaction(msg_id: int, session_id: str, emoji: str, label: str) -> None:
    """Background: run fresh scene analysis, expand the emoji into a
    first-person narration tied to THAT scene, relay to Kindroid."""
    if _pipeline_paused():
        return
    await manager.broadcast({"type": "eli_typing", "on": True})
    try:
        await _process_reaction_body(msg_id, session_id, emoji, label)
    except Exception:
        log.exception("reaction pipeline crashed")
        await db.add_message(session_id, "system", "Reaction pipeline error — see server logs.")
        snap_row = await db.fetch_one(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        if snap_row:
            await manager.broadcast(
                {"type": "message", "message": _message_to_payload(row_to_dict(snap_row) or {})}
            )
    finally:
        await manager.broadcast({"type": "eli_typing", "on": False})


async def _process_reaction_body(
    msg_id: int, session_id: str, emoji: str, label: str
) -> None:
    """Fresh clip → scene analysis (Pro→Flash fallback) → reaction narration
    tied to that scene → Kindroid relay. Mirrors _process_tim_message_body's
    failure-recovery model.
    """
    plex = plex_monitor.current_state()
    settings_map = await db.get_all_settings()
    try:
        capture_seconds = int(settings_map.get("capture_seconds") or settings.default_capture_seconds)
    except ValueError:
        capture_seconds = settings.default_capture_seconds

    history_text = await build_session_history_for_gemini(session_id)
    history_budget = 250 if history_text.strip() else 0
    # Reactions carry no typed dialogue, but the reaction emote itself costs
    # ~400 chars; keep the scene budget sensible so total stays ≤2000.
    total_emote_budget = max(500, settings.kindroid_char_limit - 40 - 400)
    scene_target = min(1800, max(500, total_emote_budget - history_budget))

    clip_path: Optional[Path] = None
    scene_result: Optional[dict[str, Any]] = None
    scene_error_msg: Optional[str] = None

    if plex and plex.get("part_key"):
        view_offset_sec = (plex.get("view_offset_ms") or 0) / 1000.0
        stream_url = build_stream_url(plex["part_key"])
        try:
            clip_path = await extract_clip(stream_url, view_offset_sec, capture_seconds)
        except FFmpegNotFound:
            scene_error_msg = "ffmpeg unavailable — reaction without scene context."
        except FFmpegError as e:
            log.exception("reaction clip extraction failed")
            scene_error_msg = f"Scene capture failed ({type(e).__name__}) — reaction without context."

        if clip_path and not scene_error_msg:
            try:
                scene_result = await gemini_brain.analyze_scene(
                    clip_path,
                    movie_title=plex.get("title"),
                    timestamp_label=_format_hms(plex.get("view_offset_ms") or 0),
                    target_chars=scene_target,
                    session_history=history_text,
                    history_budget=history_budget,
                )
            except GeminiError as primary_err:
                log.warning("reaction scene Pro failed, falling back: %s", primary_err)
                try:
                    scene_result = await gemini_brain.analyze_scene(
                        clip_path,
                        movie_title=plex.get("title"),
                        timestamp_label=_format_hms(plex.get("view_offset_ms") or 0),
                        target_chars=scene_target,
                        session_history=history_text,
                        history_budget=history_budget,
                        model_override="gemini-2.5-flash",
                    )
                except Exception:
                    log.exception("reaction scene fallback failed")
            except Exception:
                log.exception("reaction scene analysis crashed")

        if clip_path and clip_path.exists():
            try:
                clip_path.unlink()
            except OSError:
                pass

    if scene_error_msg:
        await db.add_message(session_id, "system", scene_error_msg)

    scene_desc = scene_result.get("scene_description", "") if scene_result else ""
    history_narrative_text = scene_result.get("history_narrative", "") if scene_result else ""
    mood = scene_result.get("mood") if scene_result else None

    # Generate the first-person reaction narration using the FRESH scene.
    try:
        result = await gemini_brain.reaction_oneliner(
            emoji=emoji,
            label=label,
            scene_description=scene_desc,
            movie_title=((plex.get("display_title") or plex.get("title") or "") if plex else ""),
        )
    except Exception:
        log.exception("reaction one-liner failed")
        await db.add_message(session_id, "system", "Reaction narration failed.")
        return

    oneliner = (result.get("text") or "").strip()
    new_content = f"{emoji} {oneliner}".strip()

    # Reactions don't have typed text, so we can't infer Tim's level fresh
    # — inherit the latest inferred value from his most recent message row.
    latest_tim = await db.fetch_one(
        "SELECT stoned_level_tim FROM messages WHERE session_id = ? "
        "AND sender = 'tim' ORDER BY id DESC LIMIT 1",
        (session_id,),
    )
    tim_level = int(latest_tim["stoned_level_tim"] or 0) if latest_tim else 0
    eli_level, eli_method, _ = await stoned_current_state("eli")
    stoned_line = eli_state_directive(eli_level, eli_method)

    await db.execute(
        """UPDATE messages
           SET content = ?, scene_context = ?, mood = ?, latency_ms = ?,
               stoned_level_tim = ?, stoned_level_eli = ?
           WHERE id = ?""",
        (
            new_content,
            scene_desc or None,
            mood,
            result.get("latency_ms"),
            tim_level,
            eli_level,
            msg_id,
        ),
    )
    updated = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (msg_id,))
    if updated:
        await manager.broadcast(
            {"type": "message_updated", "message": _message_to_payload(row_to_dict(updated) or {})}
        )
    if mood:
        await manager.broadcast({"type": "mood", "mood": mood})
        _mark_full_scene_mood(mood)
    await _broadcast_cost(session_id)

    if scene_desc or oneliner or stoned_line:
        # Reactions go round the room too — tapping 😱 with four kins watching
        # should reach all four, not just whoever happens to be `active_character`.
        # There's no typed dialogue, so the emoji one-liner IS the narration.
        await _run_relay(
            session_id,
            scene=scene_desc,
            history=history_narrative_text,
            reaction=oneliner,
            mood=mood,
        )


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────
def _format_duration_ms(start_iso: str, end_iso: str) -> str:
    try:
        s = datetime.fromisoformat(start_iso)
        e = datetime.fromisoformat(end_iso)
        total_min = int((e - s).total_seconds() // 60)
        hours, mins = divmod(total_min, 60)
        return f"{hours}h {mins:02d}m" if hours else f"{mins}m"
    except (ValueError, TypeError):
        return "—"


def _format_hms(ms: int) -> str:
    total_s = max(0, int(ms) // 1000)
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _time_of_day_label(dt: Optional[datetime] = None) -> str:
    hour = (dt or datetime.now()).hour
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "late night"


def _format_short_date(iso_ts: str) -> str:
    try:
        d = datetime.fromisoformat(iso_ts)
        return f"{d.strftime('%b')} {d.day}"
    except (ValueError, TypeError):
        return "—"


def _message_to_payload(msg_row: dict[str, Any]) -> dict[str, Any]:
    segments_raw = msg_row.get("segments_json")
    segments: list[dict[str, str]] = []
    turn_id: Optional[int] = None
    sat_out: list[dict] = []
    extra_photos: list[str] = []
    barged_in: str = ""
    if segments_raw:
        try:
            parsed = json.loads(segments_raw)
            # Legacy shape: a bare list of segment dicts.
            if isinstance(parsed, list):
                segments = parsed
            # New shape: dict with optional "segments" + "extra_photos" + "barged_in".
            elif isinstance(parsed, dict):
                segs = parsed.get("segments")
                if isinstance(segs, list):
                    segments = segs
                extras = parsed.get("extra_photos")
                if isinstance(extras, list):
                    extra_photos = [u for u in extras if isinstance(u, str)]
                if isinstance(parsed.get("barged_in"), str):
                    barged_in = parsed["barged_in"]
                # A `room_note` carries the turn it belongs to and who sat it out, so
                # tapping a name knows exactly which decision Tim is complaining about.
                # It survives a reload because it's a real row, not a WebSocket frame.
                if isinstance(parsed.get("turn_id"), int):
                    turn_id = parsed["turn_id"]
                if isinstance(parsed.get("sat_out"), list):
                    sat_out = parsed["sat_out"]
        except (json.JSONDecodeError, TypeError):
            pass
    # Who actually spoke. Rows written before the roster existed have no
    # character_key; they were all Eli, and the migration backfilled them.
    char_key = msg_row.get("character_key")
    char = characters.get_character(char_key) if char_key else None
    return {
        "id": msg_row.get("id"),
        "sender": msg_row.get("sender"),
        "content": msg_row.get("content"),
        "emote_text": msg_row.get("emote_text"),
        "spoken_text": msg_row.get("spoken_text"),
        "segments": segments,
        "turn_id": turn_id,      # for the ⚑ report button
        "sat_out": sat_out,      # for the 🤐 room-note line
        "scene_context": msg_row.get("scene_context"),
        "mood": msg_row.get("mood"),
        "stoned_level_tim": msg_row.get("stoned_level_tim", 0),
        "stoned_level_eli": msg_row.get("stoned_level_eli", 0),
        "trivia": msg_row.get("trivia"),
        "latency_ms": msg_row.get("latency_ms"),
        "timestamp": msg_row.get("timestamp"),
        "frame_path": msg_row.get("frame_path"),
        "extra_photos": extra_photos,
        # Set when the coordinator promoted this kin — Tim didn't tap them, they
        # leaned in anyway. The string IS the reason, shown on the ❗ badge. It also
        # forces the bubble open even if the kin is muted.
        "barged_in": barged_in,
        # Baked onto the message, not looked up at render time — this is what
        # stops the whole transcript relabelling itself when the room changes.
        "character_key": char_key,
        "character_name": char.first_name if char else None,
        "portrait_url": portraits.portrait_url(char_key) if char_key else None,
    }


async def _build_session_snapshot() -> dict[str, Any]:
    active = await db.get_active_session()
    last_ended = await db.get_last_ended_session()
    usage = await db.get_today_usage()
    all_settings = await db.get_all_settings()
    all_settings.pop("password_hash", None)

    today_cost = await db.get_today_cost()
    # Everyone's current level, keyed by registry key. Drives the per-kin
    # ingestion rows and the leaf indicators. Read fresh from the DB so a page
    # reload shows the real state, not a stale client guess.
    ingestion: dict[str, dict[str, Any]] = {}
    for kin in characters.selectable():
        lvl, mth, mins = await stoned_current_state(kin.key)
        phase = curve_phase(mth, mins)
        # LEVEL 0 IS NOT THE SAME AS SOBER. An edible sits at zero for its first thirty
        # minutes, so this used to filter out anyone who was COMING UP — Tim would hand
        # Tommy a gummy, watch the screen say "sober", and have no idea it was on its
        # way. Anyone still on a curve belongs here, even at level 0.
        if lvl > 0 or phase.get("phase") == "onset":
            ingestion[kin.key] = {
                "level": lvl,
                "method": mth,
                # Where they REALLY are on the curve — the panel used to claim
                # everyone was "coming down" the instant they got high.
                **phase,
            }
    eli_level, eli_method, _ = await stoned_current_state("eli")
    snapshot: dict[str, Any] = {
        "type": "snapshot",
        "session": None,
        "messages": [],
        "movies": [],
        "last_session": None,
        "gemini_usage": usage,
        "gemini_budget": settings.gemini_daily_budget,
        "today_cost_usd": today_cost,
        "session_cost": {"total_usd": 0.0, "calls": 0, "by_type": {}},
        "settings": all_settings,
        "plex": plex_monitor.current_state(),
        "plex_unreachable": plex_monitor.is_unreachable(),
        "standby": _standby_active,
        "away": _away_active,
        # Legacy single-kin field — kept so nothing that still reads it breaks.
        "eli_ingestion": {"level": eli_level, "method": eli_method},
        # Everyone's level, keyed by registry key. Only non-sober kins appear.
        "ingestion": ingestion,
        # The room: who's watching tonight, and who the next message is aimed
        # at. Both settable from the portrait picker; empty room falls back to
        # the legacy single-kin `active_character` path.
        "room": await _kin_list("room_kins"),
        "addressed": await _kin_list("addressed_kins"),
        # Muted kins still reply — their bubbles just arrive collapsed.
        "muted": await _kin_list("muted_kins"),
    }

    if active:
        session_id = active["id"]
        messages = await db.get_session_messages(session_id)
        movies = await db.get_session_movies(session_id)
        snapshot["session"] = row_to_dict(active)
        snapshot["messages"] = [_message_to_payload(m) for m in rows_to_list(messages)]
        snapshot["movies"] = rows_to_list(movies)
        snapshot["session_cost"] = await db.get_session_cost(session_id)

    if last_ended:
        movies = await db.get_session_movies(last_ended["id"])
        msg_count = await db.count_session_messages(last_ended["id"], sender="tim")
        started = last_ended["started_at"]
        ended = last_ended["ended_at"]
        title = " + ".join(m["title"] for m in movies if m["title"]) if movies else "Untitled session"
        duration = _format_duration_ms(started, ended) if started and ended else "—"
        date_label = _format_short_date(started) if started else "—"
        snapshot["last_session"] = {
            "id": last_ended["id"],
            "title": title,
            "date": date_label,
            "exchanges": msg_count,
            "runtime": duration,
        }

    return snapshot


# ──────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root(request: Request, eli_session: Optional[str] = Cookie(default=None, alias=COOKIE_NAME)):
    """Land wherever Tim last chose to be.

    The two views are equals, not a default and an alternative — on a phone you
    want the room on its own, at the desk you want it beside the film — so the
    launcher just remembers which one he picked last and takes him there.
    """
    if not _verify_cookie(eli_session):
        return RedirectResponse(url="/login", status_code=302)
    settings_map = await db.get_all_settings()
    # ONE setting now, not two. `view_mode` (normal|theater) and `layout`
    # (auto|compact|desktop|theater) were two knobs for one decision, and they could
    # disagree — which is exactly why picking a mode never quite did what it said.
    #
    # "auto" can't be resolved here: it depends on the window width, which the server
    # does not know. So everything but an explicit theatre goes to /dashboard, and the
    # client picks the shape (and rewrites the URL to match).
    lay = (settings_map.get("layout") or "auto").lower()
    return RedirectResponse(
        url="/theater" if lay == "theater" else "/dashboard", status_code=302
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    first_run = not await password_is_set()
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "first_run": first_run, "error": None},
    )


@app.post("/login")
async def login_submit(
    request: Request,
    password: str = Form(...),
    confirm: Optional[str] = Form(default=None),
):
    first_run = not await password_is_set()

    if first_run:
        if not password or len(password) < 6:
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "first_run": True, "error": "Password must be at least 6 characters."},
                status_code=400,
            )
        if password != confirm:
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "first_run": True, "error": "Passwords do not match."},
                status_code=400,
            )
        await set_password(password)
    else:
        if not await verify_password(password):
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "first_run": False, "error": "Incorrect password."},
                status_code=401,
            )

    token = _sign_cookie({"authed": True, "nonce": secrets.token_hex(8)})
    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response


# ─── The browser-safe Plex URL ────────────────────────────────────────
#
# The backend talks to Plex with `plex_verify_ssl=False`, because the seedbox
# presents a cert for `*.<hash>.plex.direct` while we reach it at
# `pixel-direct.usbx.me`. ffmpeg and httpx are happy to be told to ignore that.
#
# A BROWSER IS NOT. Point an iframe at the same URL and Firefox throws
# SSL_ERROR_BAD_CERT_DOMAIN across the whole pane, and there is no flag we can
# pass from our side to make it stop.
#
# But Plex issues those certs on purpose, and there IS a hostname that matches:
#
#     45-86-221-67.<hash>.plex.direct   ->   45.86.221.67
#
# `<ip-with-dashes>.<hash>.plex.direct` is a real DNS record resolving straight
# back to the same box, and the cert is valid for it. So we read the hash out of
# the server's OWN certificate, resolve the host, and build the URL it will
# actually trust. Nothing is hardcoded — if the seedbox moves, or Plex rotates
# the hash, this re-derives it on the next boot.
_plex_web_url_cache: Optional[str] = None


def _derive_plex_web_url() -> str:
    """The Plex Web URL a BROWSER will accept. Falls back to the raw one."""
    global _plex_web_url_cache
    if _plex_web_url_cache:
        return _plex_web_url_cache

    raw = settings.plex_url.rstrip("/")
    fallback = f"{raw}/web/index.html"

    if settings.plex_web_url:                       # explicit override wins
        _plex_web_url_cache = settings.plex_web_url
        return _plex_web_url_cache
    if not raw.startswith("https://"):              # http needs no cert dance
        _plex_web_url_cache = fallback
        return _plex_web_url_cache

    try:
        import socket
        import ssl
        from urllib.parse import urlparse

        parsed = urlparse(raw)
        host, port = parsed.hostname, parsed.port or 443

        # Pull the cert WITHOUT verifying it — we're reading it, not trusting it.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=6) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)

        # The SAN we want looks like *.404bba…plex.direct. getpeercert() returns
        # nothing useful under CERT_NONE, so parse the DER for the literal.
        m = re.search(rb"\*\.([0-9a-f]{32})\.plex\.direct", der)
        if not m:
            log.info("plex cert has no *.plex.direct SAN — iframe will use the raw URL")
            _plex_web_url_cache = fallback
            return _plex_web_url_cache

        plex_hash = m.group(1).decode()
        ip = socket.gethostbyname(host)
        safe_host = f"{ip.replace('.', '-')}.{plex_hash}.plex.direct"
        _plex_web_url_cache = f"https://{safe_host}:{port}/web/index.html"
        log.info("plex web (browser-trusted): %s", _plex_web_url_cache)
    except Exception:
        log.exception("couldn't derive a browser-trusted Plex URL — using the raw one")
        _plex_web_url_cache = fallback
    return _plex_web_url_cache


def _render_dashboard(request: Request, *, layout: Optional[str] = None) -> Response:
    """THE ONE PAGE. Theatre is a LAYOUT of it, not a separate document.

    It used to be the other way round: `/theater` was a shell that iframed the
    dashboard into a 420px sidebar. That made every break-out panel physically
    impossible — an iframe is a hard clipping boundary, and nothing inside one can
    paint outside its own rectangle. The react wizard could never span the window,
    the people picker could never break out, and you cannot make an iframe
    transparent in patches (pointer-events on it is all-or-nothing).

    So we inverted it. Now the dashboard renders PLEX in an iframe, and everything
    else is ours to place anywhere on the screen.

    The Plex URL is derived, never `settings.plex_url` — a browser will not accept
    that certificate. See `_derive_plex_web_url()`.
    """
    response = templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "forced_layout": layout or "",
            "plex_web_url": _derive_plex_web_url(),
            # THE COLOUR SYSTEM, FROM ONE SOURCE. The JS used to carry a hand-copy of
            # emotions.py's tables, under a comment claiming they "can never drift apart".
            # They could: add a mood in Python, forget the JS, and it falls through to
            # no-colour in silence. Now there is nothing to keep in step.
            "mood_palette": json.dumps(emotions.palette()),
        },
    )
    # Prevent browsers (especially mobile Safari) from caching the dashboard HTML —
    # the inline JS changes across deploys, and a stale cache reads as "the button
    # is missing".
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/theater", response_class=HTMLResponse)
async def theater(request: Request, eli_session: Optional[str] = Cookie(default=None, alias=COOKIE_NAME)):
    """The same page, in the theatre layout. Kept as a route so bookmarks survive."""
    if not _verify_cookie(eli_session):
        return RedirectResponse(url="/login", status_code=302)
    return _render_dashboard(request, layout="theater")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, eli_session: Optional[str] = Cookie(default=None, alias=COOKIE_NAME)):
    if not _verify_cookie(eli_session):
        return RedirectResponse(url="/login", status_code=302)
    return _render_dashboard(request)


# ─── JSON API ───────────────────────────────────────────────────────
@app.get("/api/session")
async def api_session(_auth: dict = Depends(require_auth)):
    return JSONResponse(await _build_session_snapshot())


@app.post("/api/session/start")
async def api_session_start(_auth: dict = Depends(require_auth)):
    existing = await db.get_active_session()
    if existing:
        return JSONResponse({"session_id": existing["id"], "already_active": True})

    session_id = uuid.uuid4().hex
    await db.create_session(session_id)
    await db.add_message(session_id, "system", "Movie Mode activated")
    _reinforcement_counters[session_id] = 0

    # If a movie is already playing when the session starts, register it as
    # the first-watched movie and kick off briefing generation.
    plex = plex_monitor.current_state()
    if plex and plex.get("rating_key"):
        runtime_min = None
        if plex.get("duration_ms"):
            runtime_min = int(plex["duration_ms"]) // 60000 or None
        movie_id = await db.add_movie(
            session_id=session_id,
            plex_rating_key=plex["rating_key"],
            title=plex.get("title") or "",
            year=plex.get("year"),
            director=plex.get("director"),
            runtime_minutes=runtime_min,
        )
        asyncio.create_task(_generate_briefing_card(session_id, movie_id, dict(plex)))

    snapshot = await _build_session_snapshot()
    await manager.broadcast(snapshot)
    return JSONResponse({"session_id": session_id, "already_active": False})


@app.post("/api/session/stop")
async def api_session_stop(request: Request, _auth: dict = Depends(require_auth)):
    # Idempotent — if there's no active session we return success with
    # already_stopped=true rather than 400. This tolerates double-taps
    # and avoids scary red errors in the console.
    active = await db.get_active_session()
    if not active:
        return JSONResponse({"ended": False, "already_stopped": True})

    session_id = active["id"]

    # DOES THE FAMILY GET A DEBRIEF, OR DOES IT JUST STOP?
    #
    # The debrief is a real thing that happens TO them: Gemini writes a wrap-up in Tim's
    # voice and it is RELAYED TO EVERY KIN — one Kindroid call each, minutes of waiting,
    # and a permanent entry in each of their memories.
    #
    # That is exactly right on a proper movie night. It is exactly wrong when he's cut a
    # film short, or is testing, or just wants the thing to stop — and it was happening
    # every single time, with no way to decline. Everything else in the wrap-up (the
    # summary, the consolidation, the rule scoring) is FOR TIM and costs the family
    # nothing, so it always runs.
    try:
        body = await request.json()
    except Exception:
        body = {}
    debrief = bool(body.get("debrief", True))

    await db.add_message(
        session_id, "system",
        "Wrapping up the session…" if debrief else "Wrapping up — no debrief tonight.",
    )
    row = await db.fetch_one(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
        (session_id,),
    )
    if row:
        await manager.broadcast(
            {"type": "message", "message": _message_to_payload(row_to_dict(row) or {})}
        )

    # Run sign-off / marathon summary / journal in the background so the
    # HTTP response returns immediately. Session is marked 'ended' only
    # after finalization completes.
    asyncio.create_task(_finalize_session(session_id, debrief=debrief))
    return JSONResponse({"session_id": session_id, "ending": True, "debrief": debrief})


async def _consolidate_session_rules(session_id: str) -> list[str]:
    """WHAT DID TONIGHT TEACH US? Distill the evening's rules, per person.

    The rules Tim writes mid-film are RAW: he's stoned, he taps four times, and what
    comes out overlaps, sometimes contradicts, and is usually narrower than he meant.
    This is the step that makes the system COMPOUND rather than silt up — the reports
    are raw material, the consolidated rules are the product.

    THE ONE THING THAT MUST NOT HAPPEN IS WIDENING. "Stays out of grief when Daisy's
    here" + "stays out of romance" must never become "stays out of emotional scenes" —
    that is broader than either parent, it would mute him in rooms Tim never asked
    about, and Tim would never learn why. So it is checked HERE, in code, not merely
    requested in the prompt.

    Returns the lines for the summary card. Nothing here may ever break the wrap-up.
    """
    notes: list[str] = []
    try:
        fresh_all = await db.get_session_rules(session_id)
    except Exception:
        log.exception("could not read tonight's rules")
        return notes
    if not fresh_all:
        return notes

    by_kin: dict[str, list[dict]] = {}
    for r in fresh_all:
        by_kin.setdefault(r["kin_key"], []).append(r)

    for kin_key, fresh in by_kin.items():
        kin = characters.get_character(kin_key)
        if not kin or len(fresh) < 2:
            # One rule can't be consolidated with itself. Leave it be.
            continue
        for r in fresh:
            r["conditions_label"] = facts.describe_conditions(r)
        existing = [r for r in await db.get_kin_rules(kin_key)
                    if r["id"] not in {f["id"] for f in fresh}]
        for r in existing:
            r["conditions_label"] = facts.describe_conditions(r)

        try:
            result = await coordinator.consolidate_rules(
                kin=kin,
                profile_block=affinity.profile_for_prompt(kin_key),
                fresh=fresh,
                existing=existing,
                session_id=session_id,
            )
        except Exception:
            log.exception("consolidation failed for %s — leaving his rules as they are", kin_key)
            continue

        fresh_by_id = {r["id"]: r for r in fresh}
        merged_any = False
        covered: set[int] = set()

        for c in result.rules:
            replaces = [i for i in c.replaces if i in fresh_by_id]
            covered.update(replaces)
            if len(replaces) < 2:
                continue  # nothing was actually merged

            # ── THE GUARD. No widening. Ever. ──
            #
            # Every parent's conditions must SURVIVE into the merged rule. Not "at
            # least as many conditions" — that check looks right and is wrong, because
            # a universal parent has zero of them and would wave any merge through
            # while quietly stripping the scope off the other one. See facts.would_widen.
            new_conds = [{"fact": x.fact, "op": x.op, "value": x.value} for x in c.conditions]
            parents = [fresh_by_id[i] for i in replaces]
            widened = facts.would_widen(parents, new_conds)
            if widened:
                log.warning("consolidation tried to WIDEN a rule for %s — refused: %s",
                            kin_key, widened)
                continue
            # And a law may never be conjured out of leans, or vice versa.
            if any((p["verdict"] in facts.LAWS) != (c.verdict in facts.LAWS) for p in parents):
                log.warning("consolidation tried to change law/lean for %s — refused", kin_key)
                continue

            new_id = await db.add_kin_rule(
                kin_key=kin_key,
                rule_text=c.rule_text,
                rule_why=c.rule_why,
                evidence=f"consolidated from {len(replaces)} rules you wrote",
                verdict=c.verdict,
                when=new_conds,
                session_id=session_id,
            )
            await db.merge_rules_into(replaces, new_id)
            merged_any = True
            log.info("consolidated %d of %s's rules into #%s", len(replaces), kin_key, new_id)

        # NOTHING MAY VANISH. Tim approved every one of these; a rule that silently
        # disappears is a promise broken. Anything the model failed to account for
        # simply stays exactly as it was.
        orphaned = [r["id"] for r in fresh if r["id"] not in covered]
        if orphaned:
            log.warning("consolidation didn't account for %s's rules %s — leaving them alone",
                        kin_key, orphaned)

        if merged_any and result.lesson:
            notes.append(f"{kin.first_name}: {result.lesson}")
        elif result.lesson:
            notes.append(f"{kin.first_name}: {result.lesson}")

    return notes


async def _score_rules(session_id: str) -> list[str]:
    """WHICH OF TIM'S RULES EARNED THEIR KEEP, AND WHICH KEPT MISFIRING?

    Every 👍/👎 is a verdict on the rules that were actually IN FORCE at that moment. So a
    rule that fires and produces an outcome Tim rejects, over and over, is a rule quietly
    making his room worse — and he would never catch it by hand, because each individual
    misfire looks like a one-off.

    This is the honest use of the taps. We do NOT mine them for patterns: his own data
    killed that idea, because eight of the fourteen facts never change inside one evening,
    so a miner would confidently report that Bobby goes quiet ON MONDAYS. A verdict on a
    specific rule is direct evidence. A search for coincidences is not.

    NEVER AUTO-RETIRES. A rule Tim didn't kill himself is one he'll wonder about later.
    """
    notes: list[str] = []
    try:
        scores = await db.score_rules(session_id)
    except Exception:
        log.exception("could not score the rules")
        return notes

    for s in scores:
        wrong, kept = s["wrong"], s["kept"]
        if wrong >= 3 and wrong > kept:
            kin = characters.get_character(s["kin_key"])
            who = kin.first_name if kin else s["kin_key"]
            notes.append(
                f"⚠ Your rule for {who} — “{s['rule_text']}” — was wrong {wrong} time"
                f"{'s' if wrong != 1 else ''} tonight and right {kept}. "
                f"Open his rules to retire it."
            )
            log.info("rule %s misfired %d/%d — surfacing to Tim", s["rule_id"], wrong, wrong + kept)
    return notes


async def _finalize_session(session_id: str, *, debrief: bool = True) -> None:
    """Orchestrate sign-off, marathon summary, journal, and session end.

    `debrief` decides whether THE FAMILY hears about it. When false we skip the sign-off
    entirely: no Gemini call, and — the part that actually matters — no Kindroid relay to
    every kin in the room. That relay is minutes of waiting and a permanent entry in each
    of their memories, which is right for a proper movie night and wrong when Tim has cut
    a film short or is just testing.

    Everything else here (the summary, the consolidation, the rule scoring) is FOR TIM,
    costs the family nothing, and always runs.
    """
    try:
        # WHAT DID WE LEARN TONIGHT? Before the wrap-up, so the lessons can go in it.
        try:
            lessons = await _consolidate_session_rules(session_id)
        except Exception:
            log.exception("consolidation failed — the wrap-up carries on regardless")
            lessons = []
        if lessons:
            await _system_message(
                session_id,
                "📜 What the room learned tonight — " + " · ".join(lessons),
            )

        # AND WHICH OF HIS OWN RULES LET HIM DOWN. He can't spot this by hand — each
        # misfire looks like a one-off.
        try:
            for note in await _score_rules(session_id):
                await _system_message(session_id, note)
        except Exception:
            log.exception("rule scoring failed — the wrap-up carries on regardless")

        stats = await build_session_stats(session_id)
        stats_hint = format_stats_hint(stats)
        history = await build_session_history_for_gemini(session_id)

        # 1) THE DEBRIEF — and only if Tim said yes.
        #
        #    This is the one part of the wrap-up that happens TO THE FAMILY: Gemini
        #    writes it in Tim's voice, and it is RELAYED TO EVERY KIN — a Kindroid call
        #    each, minutes of waiting, and a permanent entry in each of their memories.
        #
        #    Deliberately NOT a farewell — nobody's leaving, they live in the same house.
        #    It marks the end of the FILM and recaps what the room just went through
        #    together. (The `signoff` sender is legacy naming; the semantics moved.)
        signoff_text = ""
        if not debrief:
            log.info("session %s: no debrief — the family won't be told", session_id[:8])
        else:
            try:
                result = await gemini_brain.generate_signoff(
                    session_context=history, stats_hint=stats_hint,
                )
                signoff_text = (result.get("signoff") or "").strip()
            except Exception:
                log.exception("sign-off generation failed")

        if signoff_text:
            msg_id = await db.add_message(
                session_id,
                "signoff",
                signoff_text,
                latency_ms=None,
            )
            row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (msg_id,))
            if row:
                await manager.broadcast(
                    {"type": "message", "message": _message_to_payload(row_to_dict(row) or {})}
                )
            # Goes out as plain dialogue, not an emote — this is the exception to
            # the "everything that isn't Tim is narration" rule, because it IS
            # Tim, speaking directly to the room.
            await _send_to_kindroid_and_render(
                session_id,
                scene_narration="",
                history_narrative="",
                reaction_narration="",
                typed_dialogue=signoff_text,
                mood=None,
                show_typing=True,
                include_presence=False,
            )

        # 2) Marathon summary (Gemini → DB 'stats' message).
        summary_text = ""
        try:
            result = await gemini_brain.generate_marathon_summary(
                session_context=history, stats_hint=stats_hint,
            )
            summary_text = (result.get("summary") or "").strip()
        except Exception:
            log.exception("marathon summary generation failed")

        stats_payload = dict(stats)
        stats_payload["summary"] = summary_text
        msg_id = await db.add_message(
            session_id,
            "stats",
            summary_text or "Session summary unavailable.",
            scene_context=json.dumps(stats_payload, default=str),
        )
        row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (msg_id,))
        if row:
            await manager.broadcast(
                {"type": "message", "message": _message_to_payload(row_to_dict(row) or {})}
            )

        # 3) Journal entry (plain-text file for external pickup).
        journal_on = (await db.get_setting("auto_journal") or "true").lower() == "true"
        if journal_on:
            try:
                path = await write_journal_entry(session_id, stats, signoff_text, summary_text)
                await db.add_message(
                    session_id, "system", f"Journal saved → {path.name}"
                )
                last = await db.fetch_one(
                    "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                    (session_id,),
                )
                if last:
                    await manager.broadcast(
                        {"type": "message", "message": _message_to_payload(row_to_dict(last) or {})}
                    )
            except Exception:
                log.exception("journal write failed")
    finally:
        await db.end_session(session_id)
        _reinforcement_counters.pop(session_id, None)
        # Wipe any live-photo / live-audio uploads from this session — disposable.
        for media_dir in (settings.live_photos_dir, settings.live_audio_dir):
            try:
                session_media_dir = Path(media_dir) / session_id
                if session_media_dir.exists():
                    await asyncio.to_thread(shutil.rmtree, session_media_dir, True)
            except Exception:
                log.exception("live-media cleanup failed for %s in %s", session_id, media_dir)
        await manager.broadcast(await _build_session_snapshot())


@app.get("/api/settings")
async def api_get_settings(_auth: dict = Depends(require_auth)):
    all_settings = await db.get_all_settings()
    all_settings.pop("password_hash", None)
    return JSONResponse(all_settings)


async def _kin_list(key: str) -> list[str]:
    """Read a JSON-list setting (`room_kins` / `addressed_kins`) as roster keys.

    Filters to keys that still resolve — a kin removed from the registry
    shouldn't strand the room in a state the UI can't render.
    """
    try:
        raw = json.loads(await db.get_setting(key) or "[]")
    except (ValueError, TypeError):
        return []
    valid = {c.key for c in characters.selectable()}
    return [str(k).lower() for k in raw if str(k).lower() in valid]


@app.get("/api/characters")
async def api_get_characters(_auth: dict = Depends(require_auth)):
    """The roster, plus who's in the room and who the next message is aimed at."""
    roster = [
        {
            "key": c.key,
            "name": c.name,
            "first_name": c.first_name,
            # `romantic` gates the cannabis/vibe-check UI (Eli only).
            "romantic": c.romantic,
            # None when the kin has no bust in the vault — UI falls back to initials.
            "portrait_url": portraits.portrait_url(c.key),
        }
        for c in characters.selectable()
    ]
    active = await db.get_setting("active_character") or characters.DEFAULT_KEY
    return JSONResponse(
        {
            "characters": roster,
            "active": active,
            "room": await _kin_list("room_kins"),
            "addressed": await _kin_list("addressed_kins"),
            "muted": await _kin_list("muted_kins"),
        }
    )


@app.get("/api/presence")
async def api_get_presence(_auth: dict = Depends(require_auth)):
    """Venue + present-people presets and current selections for the UI."""
    venue, people, descriptions = await _presence_state()
    return JSONResponse({
        # Each venue carries its own photo if Tim has taken one — so the launcher
        # can show him his actual living room instead of the words "Living room".
        "venues": [{**v, "image": _venue_image_url(v["key"])} for v in presence.VENUES],
        "people": presence.PEOPLE,
        "active_venue": venue or "living_room",
        "present_people": [p["key"] for p in people],
        "venue_descriptions": descriptions,
    })


@app.post("/api/venue-describe")
async def api_venue_describe(
    venue: str = Form(...),
    note: str = Form(""),
    photo: UploadFile = File(None),
    _auth: dict = Depends(require_auth),
):
    """Seed a venue's saved description from a photo and/or a typed note.

    Gemini turns the photo/note into a short room description that we store
    in `venue_descriptions[venue]` and reuse in future briefings. One-time
    per venue — not per session.
    """
    if not presence.venue_label(venue):
        raise HTTPException(status_code=400, detail="Unknown venue")
    if photo is None and not note.strip():
        raise HTTPException(status_code=400, detail="Provide a photo or a note")

    tmp_path: Optional[Path] = None
    mime = "image/jpeg"
    try:
        if photo is not None:
            mime = (photo.content_type or "image/jpeg").lower()
            if not mime.startswith("image/"):
                raise HTTPException(status_code=415, detail=f"Unsupported media type: {mime}")
            ext = _PHOTO_EXT_BY_MIME.get(mime, "jpg")
            tmp_dir = Path(settings.live_photos_dir) / "_venue_tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = tmp_dir / f"{uuid.uuid4().hex}.{ext}"
            await asyncio.to_thread(tmp_path.write_bytes, await photo.read())

        try:
            result = await gemini_brain.describe_location(
                photo_path=tmp_path, mime_type=mime, note=note,
                venue_label=presence.venue_label(venue),
            )
        except GeminiError as e:
            raise HTTPException(status_code=502, detail=f"Gemini error: {e}")

        description = (result.get("description") or "").strip()
        _, _, descriptions = await _presence_state()
        descriptions[venue] = description
        await db.set_setting("venue_descriptions", json.dumps(descriptions))

        # KEEP THE PHOTO. It was being read by Gemini and then binned — but it's a
        # picture of the actual room, which is worth more than any placeholder art
        # I could invent. It becomes the venue's card on the launcher, so choosing
        # where you are is looking at your own living room rather than reading a
        # word in a dropdown.
        if tmp_path is not None and tmp_path.exists():
            try:
                VENUES_DIR.mkdir(parents=True, exist_ok=True)
                dest = VENUES_DIR / f"{venue}{tmp_path.suffix}"
                for old in VENUES_DIR.glob(f"{venue}.*"):
                    old.unlink(missing_ok=True)
                await asyncio.to_thread(shutil.copyfile, tmp_path, dest)
                log.info("venue image saved: %s", dest)
            except OSError:
                log.exception("couldn't keep the venue photo (continuing)")

        await manager.broadcast({
            "type": "setting_updated", "key": "venue_descriptions",
            "value": json.dumps(descriptions),
        })
        active = await db.get_active_session()
        if active:
            await _broadcast_cost(active["id"])
        return JSONResponse({
            "venue": venue,
            "description": description,
            "image": _venue_image_url(venue),
        })
    finally:
        if tmp_path is not None:
            try:
                await asyncio.to_thread(tmp_path.unlink)
            except OSError:
                pass


# ═══════════════════════════════════════════════════════════════════
#  Rules — the room learns
# ═══════════════════════════════════════════════════════════════════

def _registry_for_prompt(ctx: facts.TurnContext) -> str:
    """The fact registry, as the model sees it when proposing a scope.

    Only the facts that actually HAVE a value this turn. Offering to scope a rule to
    a venue we never recorded is offering him a rule that can never fire.
    """
    lines = []
    for f in facts.FACTS.values():
        have = ctx.values.get(f.key)
        if f.per_kin:
            have = "(varies by person)"
        elif have is None or have == [] :
            continue
        lines.append(f"  {f.key} ({f.kind}) — {f.blurb}\n      right now: {have!r}")
    return "\n".join(lines)


def _registry_all_for_prompt() -> str:
    """The WHOLE registry — every fact and its allowed values, with no turn to read from.

    `_registry_for_prompt` above lists only the facts that have a value *this turn*, which is
    exactly right when Tim is judging a moment. But when he writes a rule from the editor with
    no film running there IS no turn, so that function returns nothing and the model has
    nothing to scope to — every rule would come out universal, which is the very gap this
    feature exists to close.

    So this one lists the vocabulary instead of the readings: what a fact IS, and what values
    it can take.
    """
    lines = []
    for f in facts.FACTS.values():
        line = f"  {f.key} ({f.kind}) — {f.blurb}"
        if f.options:
            vals = [str(o["value"]) for o in f.options]
            shown = ", ".join(vals[:14]) + (" …" if len(vals) > 14 else "")
            line += f"\n      values: {shown}"
        elif f.kind == "level":
            line += "\n      values: 0-3  (sober, lifted, baked, blasted)"
        elif f.kind == "flag":
            line += "\n      values: true, false"
        elif f.kind == "ladder":
            line += "\n      values: whatever is playing — a show, a season, an episode "
            line += "(hierarchical: scope to the SHOW unless he means the one episode)"
        lines.append(line)
    return "\n".join(lines)


def _facts_for_prompt(ctx: facts.TurnContext, kin_key: str) -> str:
    """What was true at the time, in words."""
    lines = []
    for f in facts.FACTS.values():
        v = ctx.get(f.key, kin_key)
        if v in (None, [], ""):
            continue
        lines.append(f"  {f.label}: {v}")
    return "\n".join(lines) or "  (nothing recorded)"


def _decision_for_prompt(decision: dict, kin_key: str, first_name: str) -> str:
    """What the room did to THIS person, in one line."""
    def _reason(bucket):
        for e in decision.get(bucket) or []:
            if e.get("key") == kin_key:
                return e.get("reason") or ""
        return None

    if kin_key in (decision.get("speaking") or []):
        r = _reason("leaned_in")
        if r:
            return f"{first_name} SPOKE, and he leaned in uninvited — “{r}”"
        return f"{first_name} SPOKE."
    r = _reason("held_back")
    if r is not None:
        return f"{first_name} STAYED QUIET — “{r}”"
    r = _reason("passed")
    if r is not None:
        return f"{first_name} was tapped but PASSED — “{r}”"
    if kin_key in (decision.get("silent") or []):
        return f"{first_name} STAYED QUIET (nobody asked him and he didn't lean in)."
    return f"{first_name} stayed quiet."


@app.get("/api/facts")
async def api_facts(_auth: dict = Depends(require_auth)):
    """The registry, for the dialog's chips.

    The UI renders itself from this — so adding a dial to `facts.py` makes it appear
    in the rule dialog with no front-end work at all. That is the entire reason the
    registry exists rather than a hardcoded list of columns.
    """
    plex = plex_monitor.current_state() or {}
    room, _ = await _room_and_addressed()
    out = []
    for f in facts.FACTS.values():
        opts = list(f.options)
        if f.key == "watching":
            opts = facts.watching_options(plex)
        elif f.key in ("kins_here", "kins_talking"):
            opts = [{"value": c.key, "label": c.first_name, "emoji": "👤"} for c in room]
        out.append({
            "key": f.key, "label": f.label, "group": f.group, "kind": f.kind,
            "per_kin": f.per_kin, "blurb": f.blurb, "options": opts,
        })
    return JSONResponse({"facts": out, "groups": facts.FACT_GROUPS,
                         "verdicts": facts.VERDICTS, "laws": sorted(facts.LAWS)})


def _who_spoke(decision: dict) -> set[str]:
    """Everyone who got the mic on that turn.

    NOT `speaking` — that is the coordinator's plan. People also barge in (`leaned_in`) and
    get pulled in off the floor, and the relay sends for all of them. Newer turns record
    this directly as `spoke`; older ones are reconstructed from the same buckets.

    KEEP THIS ABOVE THE ROUTE DECORATOR. It once sat directly BENEATH `@app.post`, which
    bound the route to this helper instead of the handler below — and because this returns a
    set, the route answered 200 with `[]`. It failed OPEN: no error in the browser, the dialog
    still opened, and every 👍/👎 was silently discarded. test_routes.py now asserts the
    binding, because nothing else can see this.
    """
    if decision.get("spoke") is not None:
        return set(decision["spoke"])
    out: set[str] = set(decision.get("speaking") or [])
    out |= set(decision.get("floor") or [])
    for bucket in ("leaned_in", "law_forced"):
        out |= {x["key"] for x in (decision.get(bucket) or []) if isinstance(x, dict)}
    return out


@app.post("/api/feedback")
async def api_feedback(request: Request, _auth: dict = Depends(require_auth)):
    """👍 / 👎 on one person, on one turn. Banked instantly. NEVER blocks on a model.

    The tap is the part Tim actually does, twenty times an evening, stoned, mid-film. If
    it doesn't feel free he won't do it, and the whole loop dies of friction. So this
    route does nothing but write a row.

    IT BANKS EVEN WHEN HE MAKES NO RULE. That is the entire point. Without it the room
    learns only from its failures — and a missing reply irritates while a surplus one
    merely bores, so it would drift straight back to everyone talking.

    And it is a VERDICT ON THE RULES THAT WERE IN FORCE (`rules_in_force`), not a floating
    opinion. That's what lets us tell him at session end: "this rule of yours has misfired
    five times."
    """
    body = await request.json()
    turn_id = body.get("turn_id")
    kin_key = (body.get("kin_key") or "").strip()
    approved = bool(body.get("approved"))
    if not turn_id or not characters.get_character(kin_key):
        raise HTTPException(status_code=400, detail="Need a turn and a person")

    turn = await db.get_turn_decision(int(turn_id))
    if not turn:
        raise HTTPException(status_code=404, detail="That turn wasn't recorded")
    try:
        decision = json.loads(turn.get("decision_json") or "{}")
    except (ValueError, TypeError):
        decision = {}

    # Did they speak? Read it from the FROZEN record, never from what the client claims —
    # the client could be looking at a stale bubble, and getting this backwards would teach
    # the room the exact opposite of what he meant.
    #
    # `spoke` is the list of everyone who got the mic. `speaking` is only the coordinator's
    # PLAN, and reading this off `speaking` missed every barge-in — so a kin who leaned in
    # and talked was recorded as having stayed quiet, and a 👍 on him wrote a rule telling
    # him to shut up. Turns frozen before `spoke` existed are reconstructed from the same
    # buckets the relay actually sends to.
    spoke = kin_key in _who_spoke(decision)
    in_force = (decision.get("rules_in_force") or {}).get(kin_key) or []

    fid = await db.set_turn_feedback(
        turn_id=int(turn_id),
        session_id=turn.get("session_id"),
        kin_key=kin_key,
        spoke=spoke,
        approved=approved,
        rules_in_force=in_force,
    )
    log.info("feedback: %s %s on turn %s (they %s)",
             kin_key, "👍" if approved else "👎", turn_id,
             "spoke" if spoke else "stayed quiet")

    # THE FOUR COMBINATIONS, AND THEY COLLAPSE TO ONE LINE.
    #
    #   spoke   + 👍  ->  right to jump in       ->  speaks
    #   spoke   + 👎  ->  shouldn't have         ->  stays quiet
    #   skipped + 👍  ->  right to stay out      ->  stays quiet
    #   skipped + 👎  ->  should have spoken     ->  speaks
    #
    # i.e. the rule says SPEAKS exactly when spoke == approved.
    wants_speech = (spoke == approved)
    direction = (
        "was_right_to_speak"      if spoke and approved else
        "shouldnt_have_spoken"    if spoke else
        "was_right_to_stay_quiet" if approved else
        "should_have_spoken"
    )
    return JSONResponse({
        "feedback_id": fid,
        "spoke": spoke,
        "approved": approved,
        "direction": direction,
        "wants_speech": wants_speech,
        "rules_in_force": in_force,
    })


@app.get("/api/turn-for-message/{msg_id}")
async def api_turn_for_message(msg_id: int, _auth: dict = Depends(require_auth)):
    """Which turn does this message belong to?

    A kin's reply doesn't carry the turn id, but it belongs to exactly one — the last
    thing Tim said before it. Walk back to that message and look up its decision.
    Turns from before this table existed simply have none, and the dialog says so
    rather than pretending.
    """
    row = await db.fetch_one(
        "SELECT session_id, sender FROM messages WHERE id = ?", (msg_id,)
    )
    if not row:
        raise HTTPException(status_code=404, detail="No such message")
    tim_row = await db.fetch_one(
        "SELECT id FROM messages WHERE session_id = ? AND id <= ? "
        "AND sender IN ('tim', 'reaction') ORDER BY id DESC LIMIT 1",
        (row["session_id"], msg_id),
    )
    if not tim_row:
        return JSONResponse({"turn_id": None})
    turn = await db.get_turn_by_message(int(tim_row["id"]))
    return JSONResponse({"turn_id": turn["id"] if turn else None})


@app.get("/api/rules")
async def api_list_rules(kin: str = "", _auth: dict = Depends(require_auth)):
    """One kin's rules, for the editor.

    `hits` rides along, because a rule that has NEVER FIRED across fifty sessions is
    either wrong or scoped into oblivion — and Tim should be told that plainly rather
    than left wondering why nothing changed.
    """
    keys = [kin] if kin else [c.key for c in characters.selectable()]
    out = []
    for k in keys:
        for r in await db.get_kin_rules(k):
            r["conditions_label"] = facts.describe_conditions(r)
            r["is_law"] = facts.is_law(r)
            out.append(r)
    return JSONResponse({"rules": out})


@app.post("/api/rules/propose")
async def api_propose_rules(request: Request, _auth: dict = Depends(require_auth)):
    """Tim tapped the flag. Ask Claude what rules THIS PERSON's behaviour suggests.

    He is stoned and mid-episode. He will never type out a well-formed rule with
    conditions attached — but he will happily read four and tap the one that's right.
    The model articulates; he judges. That division is why this can work at all.
    """
    body = await request.json()
    kin_key = (body.get("kin_key") or "").strip()
    turn_id = body.get("turn_id")
    direction = (body.get("direction") or "").strip()

    kin = characters.get_character(kin_key)
    if not kin:
        raise HTTPException(status_code=400, detail=f"No such person: {kin_key}")
    turn = await db.get_turn_decision(int(turn_id)) if turn_id else None
    if not turn:
        raise HTTPException(status_code=404, detail="That turn wasn't recorded")

    ctx = facts.TurnContext.from_json(turn.get("facts_json"))
    try:
        decision = json.loads(turn.get("decision_json") or "{}")
    except (ValueError, TypeError):
        decision = {}

    tim_row = await db.fetch_one(
        "SELECT content, scene_context FROM messages WHERE id = ?",
        (turn.get("tim_message_id"),),
    )
    tim_said = (tim_row["content"] if tim_row else "") or ""
    scene = (tim_row["scene_context"] if tim_row else "") or ""

    # THE FOUR VERDICTS. Two correct the room, two confirm it — and they are genuinely
    # different questions to ask a model, not the same question with a sign flipped.
    #
    # The `wants` line is the one that matters: it tells the model which way the rule
    # should point. Get it backwards and every rule Tim writes teaches the room the exact
    # OPPOSITE of what he meant, and it would look right the entire time.
    n = kin.first_name
    complaint, wants = {
        "shouldnt_have_spoken": (
            f"{n} SPOKE, and he SHOULD NOT HAVE. Correct it.",
            "The rules you propose should make him STAY QUIET in moments like this.",
        ),
        "should_have_spoken": (
            f"{n} STAYED QUIET, and he SHOULD HAVE SPOKEN. Correct it.",
            "The rules you propose should make him SPEAK in moments like this.",
        ),
        "was_right_to_speak": (
            f"{n} SPOKE, and Tim says he was RIGHT to. Confirm it.",
            "The rules you propose should make sure he KEEPS SPEAKING in moments like "
            "this — but only if there is something here his profile does not already know.",
        ),
        "was_right_to_stay_quiet": (
            f"{n} STAYED QUIET, and Tim says he was RIGHT to. Confirm it.",
            "The rules you propose should make sure he KEEPS STAYING QUIET in moments "
            "like this — but only if there is something here his profile does not "
            "already know.",
        ),
    }.get(direction, (f"Tim judged {n}'s behaviour on this turn.", ""))
    complaint = f"{complaint}\n\n{wants}" if wants else complaint

    sheet = affinity.load_sheet(kin.key)
    existing = await db.get_kin_rules(kin.key)
    for r in existing:
        r["conditions_label"] = facts.describe_conditions(r)

    history = await build_session_history_for_gemini(
        turn["session_id"], max_exchanges=5
    )

    try:
        proposed = await coordinator.propose_rules(
            kin=kin,
            sheet_block=affinity.for_prompt(sheet) if sheet else "",
            profile_block=affinity.profile_for_prompt(kin.key),
            existing=existing,
            message=tim_said,
            scene=scene,
            decision=_decision_for_prompt(decision, kin.key, kin.first_name),
            complaint=complaint,
            facts_block=_facts_for_prompt(ctx, kin.key),
            registry_block=_registry_for_prompt(ctx),
            history=history,
            session_id=turn["session_id"],
        )
    except coordinator.CoordinatorError as e:
        raise HTTPException(status_code=502, detail=f"Couldn't think of any rules: {e}")

    await db.mark_turn_reported(int(turn_id))

    return JSONResponse({
        "kin": {"key": kin.key, "first_name": kin.first_name},
        "turn_id": int(turn_id),
        "you_said": tim_said,
        "what_happened": _decision_for_prompt(decision, kin.key, kin.first_name),
        "rules": [
            {
                "rule_text": p.rule_text,
                "rule_why": p.rule_why,
                "verdict": p.verdict,
                "is_law": p.verdict in facts.LAWS,
                "universal": p.universal,
                "conditions": [] if p.universal else [
                    {"fact": c.fact, "op": c.op, "value": c.value} for c in p.conditions
                ],
                "conflicts_with": p.conflicts_with,
            }
            for p in proposed
        ],
    })


# The strength words Tim actually types, and what they mean. Ordered longest-first so
# "not always" is seen before "always".
_STRENGTH_WORDS = [
    ("never", "never"), ("always", "always"),
    ("usually", "usually"), ("often", "usually"), ("tends to", "usually"),
    ("rarely", "rarely"), ("seldom", "rarely"), ("hardly ever", "rarely"),
]
_NEGATORS = ("not", "never", "doesn't", "does not", "don't", "do not",
             "isn't", "is not", "won't", "will not")


def _stated_strength(text: str) -> Optional[str]:
    """The verdict TIM ACTUALLY WROTE, if he wrote one.

    ENFORCED, NOT REQUESTED. He typed "bobby ALWAYS jumps in", and the model handed back
    `usually` — quietly overruling the one thing he was explicit about. He would not have
    found out until the room failed to do what he told it to.

    Asking a model harder is not a fix for that; the whole system already knows this, which is
    why laws execute in Python before a model is ever called. So: if he named a strength, it
    wins, and the code makes it win.

    A negated word means the opposite of itself and is NOT a strength claim — "he doesn't
    always jump in" is emphatically not `always`. When in doubt, return None and let the model
    (and its lean default) decide.
    """
    low = f" {(text or '').lower().strip()} "
    for word, verdict in _STRENGTH_WORDS:
        i = low.find(f" {word} ")
        if i < 0:
            continue
        before = low[max(0, i - 14):i]
        if any(n in before for n in _NEGATORS):
            return None          # "doesn't always" — he means the opposite. Don't guess.
        return verdict
    return None


@app.post("/api/rules/suggest")
async def api_suggest_rules(request: Request, _auth: dict = Depends(require_auth)):
    """What rules is this person MISSING? Read off who they are — no moment needed.

    The rule dialog used to open on a blank textarea whenever there was no turn to reason
    from. So the one path where Tim least knows what to write — "I've opened Bobby's rules,
    now what?" — was the one where the app said nothing at all. The premise of this whole
    feature is that the model articulates and he judges. A blank page inverts it.

    Wire-identical to `/api/rules/propose`, so the dialog renders these with no changes.
    """
    body = await request.json()
    kin_key = (body.get("kin_key") or "").strip()
    kin = characters.get_character(kin_key)
    if not kin:
        raise HTTPException(status_code=400, detail=f"No such person: {kin_key}")

    sheet = affinity.load_sheet(kin.key)
    existing = await db.get_kin_rules(kin.key)
    for r in existing:
        r["conditions_label"] = facts.describe_conditions(r)

    active = await db.get_active_session()
    try:
        proposed = await coordinator.suggest_from_profile(
            kin=kin,
            sheet_block=affinity.for_prompt(sheet) if sheet else "",
            profile_block=affinity.profile_for_prompt(kin.key),
            existing=existing,
            registry_block=_registry_all_for_prompt(),
            session_id=active["id"] if active else None,
        )
    except coordinator.CoordinatorError as e:
        raise HTTPException(status_code=502, detail=f"Couldn't think of anything: {e}")

    # A GUESS NEVER BECOMES A LAW.
    #
    # These are read off a disposition, not off anything Tim said — he hasn't even typed yet.
    # A LAW is enforced in Python before the room is asked, forever, without appeal, and there
    # is no way to soften a verdict on a proposal card: it is tick-or-leave. So a machine's
    # guess about a man's character must not be able to hand him one.
    #
    # The prompt asks for leans and the model mostly obliges — it returned a `never` on the
    # first real run. Asking is not enough for something this asymmetric.
    _SOFTEN = {"always": "usually", "never": "rarely"}
    for pr in proposed:
        if pr.verdict in _SOFTEN:
            pr.verdict = _SOFTEN[pr.verdict]

    return JSONResponse({
        "kin": {"key": kin.key, "first_name": kin.first_name},
        "rules": [
            {
                "rule_text": p.rule_text,
                "rule_why": p.rule_why,
                "verdict": p.verdict,
                "is_law": p.verdict in facts.LAWS,
                "universal": p.universal,
                "conditions": [] if p.universal else [
                    {"fact": c.fact, "op": c.op, "value": c.value} for c in p.conditions
                ],
                "conflicts_with": p.conflicts_with,
                "scope_note": getattr(p, "scope_note", "") or "",
            }
            for p in proposed
        ],
    })


@app.post("/api/rules/interpret")
async def api_interpret_rule(request: Request, _auth: dict = Depends(require_auth)):
    """Tim wrote a rule in his own words. Work out what he means — including the TRIGGERS.

    The hand-written path used to save whatever he typed with `conditions: []`, so every rule
    he wrote himself was universal. It never asked what the rule was ABOUT. This does.

    The response is deliberately wire-identical to `/api/rules/propose`, so the dialog that
    already renders proposal cards, ticks, and the "Only when…" chips renders these unchanged.
    His sentence just becomes a proposal — and because the model GUESSED those triggers, the
    part that matters is that he can see them and fix them before saving.
    """
    body = await request.json()
    kin_key = (body.get("kin_key") or "").strip()
    text = (body.get("text") or "").strip()
    turn_id = body.get("turn_id")
    kin = characters.get_character(kin_key)
    if not kin:
        raise HTTPException(status_code=400, detail=f"No such person: {kin_key}")
    if not text:
        raise HTTPException(status_code=400, detail="Write the rule first")

    sheet = affinity.load_sheet(kin.key)
    existing = await db.get_kin_rules(kin.key)
    for r in existing:
        r["conditions_label"] = facts.describe_conditions(r)

    # GROUND IT AS HARD AS WE CAN. Three tiers, and the gap between them is the gap between
    # a rule that fires and one that never does:
    #
    #   A TURN    — he is writing from the thumbs popup, pointing at one moment. The model
    #               gets what he SAID, what was ON SCREEN, what the ROOM DID, and the facts
    #               exactly as they were FROZEN at that instant. "He should have jumped in
    #               THERE" resolves, because there is a `there`.
    #   A SESSION — the editor, mid-film. Recent turns and the last frozen facts.
    #   NOTHING   — the editor, cold. Say so plainly; scope from his words alone.
    active = await db.get_active_session()
    context_block = ""
    ctx = None
    session_for_call = active["id"] if active else None

    turn = await db.get_turn_decision(int(turn_id)) if turn_id else None
    if turn:
        session_for_call = turn["session_id"] or session_for_call
        ctx = facts.TurnContext.from_json(turn.get("facts_json"))
        try:
            decision = json.loads(turn.get("decision_json") or "{}")
        except (ValueError, TypeError):
            decision = {}
        row = await db.fetch_one(
            "SELECT content, scene_context FROM messages WHERE id = ?",
            (turn.get("tim_message_id"),),
        )
        tim_said = (row["content"] if row else "") or ""
        scene = (row["scene_context"] if row else "") or ""
        history = await build_session_history_for_gemini(turn["session_id"], max_exchanges=5)
        bits = [
            "THE MOMENT HE IS POINTING AT:",
            "  TIM SAID: " + (tim_said or "(he typed nothing - he reacted)"),
            "  ON SCREEN: " + (scene or "(no scene analysis)"),
            "  THE ROOM DID: " + _decision_for_prompt(decision, kin.key, kin.first_name),
            "",
            "WHAT WAS TRUE AT THAT EXACT MOMENT:",
            _facts_for_prompt(ctx, kin.key),
        ]
        if history:
            bits += ["", "RECENTLY, IN THE ROOM:", history]
        context_block = "\n".join(bits)
    elif active:
        history = await build_session_history_for_gemini(active["id"], max_exchanges=5)
        bits = []
        if history:
            bits.append("RECENTLY, IN THE ROOM:\n" + history)
        last = await db.fetch_one(
            "SELECT facts_json FROM turn_decisions WHERE session_id = ? "
            "ORDER BY id DESC LIMIT 1", (active["id"],)
        )
        if last and last["facts_json"]:
            ctx = facts.TurnContext.from_json(last["facts_json"])
            bits.append("TRUE AS OF THE LAST TURN:\n" + _facts_for_prompt(ctx, kin.key))
        context_block = "\n\n".join(bits)

    try:
        proposed = await coordinator.interpret_rule(
            kin=kin,
            tim_text=text,
            sheet_block=affinity.for_prompt(sheet) if sheet else "",
            profile_block=affinity.profile_for_prompt(kin.key),
            existing=existing,
            # With a turn, show what each fact ACTUALLY READ at that instant, so the model
            # scopes to a real value instead of guessing from the vocabulary in the abstract.
            # Without one, the whole registry, because there is no reading to show.
            registry_block=(
                _registry_for_prompt(ctx) + "\n\n" + _registry_all_for_prompt()
                if ctx else _registry_all_for_prompt()
            ),
            context_block=context_block,
            session_id=session_for_call,
        )
    except coordinator.CoordinatorError as e:
        raise HTTPException(status_code=502, detail=f"Couldn't make sense of that: {e}")

    # HIS WORD WINS, AND CODE MAKES IT WIN. The prompt asks for this; the model does not
    # reliably comply (it gave `usually` for a sentence that plainly said ALWAYS). Overruling
    # him silently on the one thing he was explicit about is the worst failure this feature
    # has, so it is not left to persuasion.
    #
    # BUT NOT AT THE COST OF A MUTE BUTTON. Forcing `never` onto a reading with NO conditions
    # manufactures exactly the rule the schema validator exists to forbid: a law that applies
    # on every turn, forever, silencing him in every scene of every film. So a LAW is only
    # forced onto a reading that can actually carry one — a scoped one. If the model produced
    # no scoped reading at all, his word does not get to build a mute button; the leans stand,
    # and he can scope one himself with the chips.
    # THE MOMENT HE POINTED AT IS THE TRIGGER — AND THE CODE MAKES IT SO.
    #
    # He writes "he should've jumped in there" from the thumbs popup. "There" is not vague: it
    # is the turn in front of us, and its facts are frozen. The prompt says this at length,
    # three different ways. The model understood the moment perfectly — every rule it wrote was
    # ABOUT the craft scene — and then returned three UNIVERSAL readings anyway, which is not a
    # choice at all, it is the same rule said three times.
    #
    # So if it gave us nothing scoped, pin the first reading to the moment ourselves, and SAY
    # we did. This is safe in a way the reverse is not: over-scoping is one tap to undo (the
    # trigger is a chip he can remove), while a universal rule he BELIEVES is scoped is a rule
    # that fires on everything, and he finds out weeks later.
    # The test is whether the MOMENT'S OWN FACT is pinned anywhere — not whether any condition
    # exists at all. The model happily scoped reading 3 to `rewatch` while leaving the reading
    # he would actually pick universal, and the cruder check waved that through.
    if turn and ctx and proposed:
        pin = next(
            (f for f in ("scene_situation", "tim_situation")
             if ctx.get(f) and ctx.get(f) != "none"),
            None,
        )
        already = pin and any(
            c.fact == pin for p in proposed for c in p.conditions
        )
        if pin and not already and not proposed[0].conditions:
            value = str(ctx.get(pin))
            proposed[0].conditions = [
                coordinator.RuleCondition(fact=pin, op="is", value=[value])
            ]
            proposed[0].universal = False
            label = facts.FACTS[pin].label
            proposed[0].scope_note = (
                f"I pinned this to the moment you pointed at ({label}: {value}). "
                f"Remove the chip if you meant it more broadly."
            )

    # NO MOMENT TO POINT AT ⇒ A GENERAL RULE.
    #
    # From the rules editor he isn't pointing at anything — he's writing down something he
    # believes about the man. "He talks about cars" means he talks about cars, not "he talks
    # about cars WHEN THE MOOD IS TENSE AND WE'RE REWATCHING", which is what a model handed
    # the whole fact registry will happily produce. A rule pinned to five facts of one evening
    # never fires again as long as he lives, and it looks like precision while you do it.
    #
    # LEANS ONLY. Stripping the trigger off a LAW would manufacture exactly the mute button
    # the schema validator exists to forbid: an unscoped `never` does not mean "never during a
    # jump scare", it means he never speaks again, in any scene, in any film. If he typed
    # "always" or "never" cold, the law keeps its trigger.
    if not turn and proposed and proposed[0].conditions \
            and proposed[0].verdict in facts.LEANS:
        proposed[0].conditions = []
        proposed[0].universal = True
        proposed[0].scope_note = (
            "You weren't pointing at a moment, so I've made this a general rule. "
            "Add a trigger below if you meant something narrower."
        )

    strength = _stated_strength(text)
    if strength and proposed:
        if strength in facts.LEANS:
            proposed[0].verdict = strength                 # a lean is safe anywhere
        else:
            scoped = next((i for i, p in enumerate(proposed) if p.conditions), None)
            if scoped is not None:
                proposed[scoped].verdict = strength
                proposed[scoped].universal = False
                if scoped:                                  # bring his reading to the front
                    proposed.insert(0, proposed.pop(scoped))
            # else: no scoped reading exists. Do NOT forge an unscoped law out of his word.

    return JSONResponse({
        "kin": {"key": kin.key, "first_name": kin.first_name},
        "turn_id": None,
        "you_said": text,
        "what_happened": "",
        "grounded": bool(context_block),
        "grounded_on": "turn" if turn else ("session" if active else ""),
        "stated_strength": strength or "",
        "rules": [
            {
                "rule_text": p.rule_text,
                "rule_why": p.rule_why,
                "verdict": p.verdict,
                "is_law": p.verdict in facts.LAWS,
                "universal": p.universal,
                "conditions": [] if p.universal else [
                    {"fact": c.fact, "op": c.op, "value": c.value} for c in p.conditions
                ],
                "conflicts_with": p.conflicts_with,
                "scope_note": getattr(p, "scope_note", "") or "",
            }
            for p in proposed
        ],
    })


def _check_conditions(conds: list) -> None:
    """A condition that names a fact we don't have can NEVER fire.

    `_match_one` returns False for an unknown key and logs a warning nobody reads. So the rule
    saves cleanly, sits in his list looking authoritative, and does nothing — forever. That was
    survivable while conditions came from a menu of real dials. It is not survivable now that a
    model invents them from a sentence. Refuse it at the door.

    Values are checked too, where the fact has a fixed set of them. `watching includes
    ["western"]` names a real fact with a value it can never hold — `watching` is the title
    that is playing, not a genre — and it fails exactly like a bad key: silently, forever.
    """
    for c in conds or []:
        fact = (c or {}).get("fact")
        op = (c or {}).get("op")
        if fact not in facts.FACTS:
            raise HTTPException(status_code=400, detail=f"No such fact to scope to: {fact!r}")
        if op not in facts.OPS:
            raise HTTPException(status_code=400, detail=f"Not a condition I understand: {op!r}")
        f = facts.FACTS[fact]
        allowed = {str(o["value"]) for o in f.options} if f.options else None
        # `kins_here` / `kins_talking` have NO static options — they're built from the live
        # roster — so nothing checked them, and the model happily produced
        # `kins_talking = ['tim']`. Tim is not a kin. That condition could never match, and
        # the rule would have sat in the list looking authoritative and doing nothing.
        if f.kind == "set" and fact.startswith("kins_"):
            allowed = {c.key for c in characters.selectable()}
        if allowed is not None:
            for v in (c.get("value") or []):
                if str(v) not in allowed:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{fact} can't be {v!r}. It can be: "
                               f"{', '.join(sorted(allowed))}",
                    )


def _guard_unscoped_law(verdict: str, conds: list, first_name: str, confirmed: bool) -> None:
    """A LAW with no conditions applies on EVERY turn. Forever.

    Tim typed "bobby never talks during a jump scare" and the model handed back `never` with no
    conditions. Verified against the real matcher: that rule fires in a comedy scene, is
    enforced in Python before any model is called, and sets `forces_silence`. Bobby would never
    have spoken again — and nothing on screen would have told him why. `always` is the mirror:
    dragged into every turn, forever.

    The trigger he needed (`scene_situation: jump_scare`) was in the registry the model was
    given. It simply didn't use it. So this is not left to persuasion.

    He CAN still do it — "he never heckles, full stop" is a thing a person might mean — but he
    has to say so on purpose. An accident here is silent and permanent.
    """
    if verdict not in facts.LAWS or conds:
        return
    if confirmed:
        return
    doing = "never speak again — in ANY scene, in any film" if verdict == "never" \
        else "be pulled into EVERY turn — in any scene, in any film"
    raise HTTPException(
        status_code=409,
        detail=(
            f"That's a LAW with no trigger, so it applies on every single turn: "
            f"{first_name} would {doing}. "
            f"Add a trigger (\u201conly when\u2026\u201d), or soften it to \u201cusually\u201d / "
            f"\u201crarely\u201d — or confirm you really mean it."
        ),
    )


@app.post("/api/rules")
async def api_create_rules(request: Request, _auth: dict = Depends(require_auth)):
    """Save the rules Tim tapped."""
    body = await request.json()
    kin_key = (body.get("kin_key") or "").strip()
    rules = body.get("rules") or []
    turn_id = body.get("turn_id")
    kin = characters.get_character(kin_key)
    if not kin:
        raise HTTPException(status_code=400, detail=f"No such person: {kin_key}")

    active = await db.get_active_session()
    evidence = ""
    if turn_id:
        turn = await db.get_turn_decision(int(turn_id))
        if turn:
            row = await db.fetch_one(
                "SELECT content FROM messages WHERE id = ?", (turn.get("tim_message_id"),)
            )
            said = (row["content"] if row else "") or ""
            if said:
                evidence = f'you said "{said[:120]}"'

    made = []
    for r in rules:
        verdict = (r.get("verdict") or "").strip()
        if verdict not in facts.VERDICTS:
            raise HTTPException(status_code=400, detail=f"Bad verdict: {verdict!r}")
        text = (r.get("rule_text") or "").strip()
        if not text:
            continue
        conds = r.get("conditions") or []
        _check_conditions(conds)
        _guard_unscoped_law(verdict, conds, kin.first_name, bool(r.get("confirm_universal_law")))
        rule_id = await db.add_kin_rule(
            kin_key=kin_key,
            rule_text=text,
            rule_why=(r.get("rule_why") or "").strip(),
            evidence=evidence,
            verdict=verdict,
            when=r.get("conditions") or [],
            from_turn_id=int(turn_id) if turn_id else None,
            session_id=active["id"] if active else None,
        )
        made.append(rule_id)

    # Link the rule back to the tap that produced it, so we can tell later which taps
    # became rules and which were just "yes, that was right" — the latter are the
    # counterweight that stops the room drifting noisy.
    fid = body.get("feedback_id")
    if fid and made:
        try:
            await db.link_feedback_rule(int(fid), made[0])
        except Exception:
            log.exception("could not link the rule to its tap (harmless)")

    log.info("Tim wrote %d new rule(s) for %s", len(made), kin_key)
    await manager.broadcast({"type": "rules_changed", "kin": kin_key})
    return JSONResponse({"created": made})


@app.put("/api/rules/{rule_id}")
async def api_edit_rule(rule_id: int, request: Request, _auth: dict = Depends(require_auth)):
    """Edit a rule in place. A rule you can't fix is one you'll end up deleting."""
    body = await request.json()
    row = await db.fetch_one("SELECT * FROM kin_rules WHERE id = ?", (rule_id,))
    if not row:
        raise HTTPException(status_code=404, detail="No such rule")

    verdict = (body.get("verdict") or row["verdict"]).strip()
    if verdict not in facts.VERDICTS:
        raise HTTPException(status_code=400, detail=f"Bad verdict: {verdict!r}")
    if body.get("conditions") is not None:
        _check_conditions(body["conditions"])
    # He can also make an unscoped law by DELETING the last chip off a scoped one. Same
    # permanence, same silence.
    _edit_kin = characters.get_character(row["kin_key"])
    _guard_unscoped_law(
        verdict,
        body["conditions"] if body.get("conditions") is not None
        else json.loads(row["when_json"] or "[]"),
        _edit_kin.first_name if _edit_kin else row["kin_key"],
        bool(body.get("confirm_universal_law")),
    )

    await db.execute(
        "UPDATE kin_rules SET rule_text = ?, rule_why = ?, verdict = ?, when_json = ? "
        "WHERE id = ?",
        (
            (body.get("rule_text") or row["rule_text"]).strip(),
            (body.get("rule_why") if body.get("rule_why") is not None else row["rule_why"]),
            verdict,
            json.dumps(body.get("conditions")) if body.get("conditions") is not None
            else row["when_json"],
            rule_id,
        ),
    )
    await manager.broadcast({"type": "rules_changed", "kin": row["kin_key"]})
    return JSONResponse({"ok": True})


@app.delete("/api/rules/{rule_id}")
async def api_delete_rule(rule_id: int, _auth: dict = Depends(require_auth)):
    """Retire a rule that isn't working.

    Deactivated, not deleted — the rule's `hits` and its provenance are the only
    record of an experiment Tim ran on his own room, and they're worth keeping.
    """
    row = await db.fetch_one("SELECT kin_key FROM kin_rules WHERE id = ?", (rule_id,))
    if not row:
        raise HTTPException(status_code=404, detail="No such rule")
    await db.deactivate_rule(rule_id)
    log.info("rule %s retired", rule_id)
    await manager.broadcast({"type": "rules_changed", "kin": row["kin_key"]})
    return JSONResponse({"ok": True})


@app.put("/api/settings/{key}")
async def api_put_setting(key: str, request: Request, _auth: dict = Depends(require_auth)):
    if key == "password_hash":
        raise HTTPException(status_code=403, detail="Use /api/password")
    body = await request.json()
    value = body.get("value")
    if value is None:
        raise HTTPException(status_code=400, detail="Missing value")
    await db.set_setting(key, str(value))
    # Switching the active family member re-frames every Gemini prompt.
    if key == "active_character":
        await _refresh_active_companion()
    await manager.broadcast({"type": "setting_updated", "key": key, "value": str(value)})
    return JSONResponse({"key": key, "value": str(value)})


@app.post("/api/password")
async def api_update_password(request: Request, _auth: dict = Depends(require_auth)):
    body = await request.json()
    current = body.get("current", "")
    new_password = body.get("new", "")
    confirm = body.get("confirm", "")
    if not await verify_password(current):
        raise HTTPException(status_code=401, detail="Current password incorrect")
    if not new_password or len(new_password) < 6:
        raise HTTPException(status_code=400, detail="New password too short")
    if new_password != confirm:
        raise HTTPException(status_code=400, detail="Passwords do not match")
    await set_password(new_password)
    return JSONResponse({"updated": True})


async def _process_catchup(session_id: str, gap_start_ms: int, gap_end_ms: int) -> None:
    """Extract a sample clip from the middle of the missed span and run
    Gemini's WDIM prompt. Stored as a 'catchup' sender message — NEVER sent
    to Kindroid (per spec — this is purely for Tim).
    """
    plex = plex_monitor.current_state()
    if not plex or not plex.get("part_key"):
        await db.add_message(session_id, "system", "Catch-up skipped — no Plex stream.")
        await manager.broadcast(await _build_session_snapshot())
        return

    settings_map = await db.get_all_settings()
    try:
        capture_seconds = int(settings_map.get("capture_seconds") or settings.default_capture_seconds)
    except ValueError:
        capture_seconds = settings.default_capture_seconds

    # Sample clip from the middle of the gap — one clip the size of a normal
    # scene-analysis window, centered on the midpoint. Plenty of signal for
    # Gemini to reconstruct what happened without pulling a huge video.
    mid_ms = (int(gap_start_ms) + int(gap_end_ms)) // 2
    end_sec = (mid_ms + capture_seconds * 1000 // 2) / 1000.0
    stream_url = build_stream_url(plex["part_key"])

    clip_path: Optional[Path] = None
    try:
        clip_path = await extract_clip(stream_url, end_sec, capture_seconds)
        result = await gemini_brain.generate_catchup(
            clip_path,
            movie_title=plex.get("title"),
            gap_start_label=_format_hms(gap_start_ms),
            gap_end_label=_format_hms(gap_end_ms),
            gap_duration_label=_humanize_gap_ms(gap_end_ms - gap_start_ms),
        )
    except (FFmpegError, FFmpegNotFound):
        log.exception("catchup clip extraction failed")
        await db.add_message(session_id, "system", "Catch-up failed — clip extraction error.")
        await manager.broadcast(await _build_session_snapshot())
        return
    except Exception:
        log.exception("catchup generation failed")
        await db.add_message(session_id, "system", "Catch-up unavailable — Gemini error.")
        await manager.broadcast(await _build_session_snapshot())
        return
    finally:
        if clip_path and clip_path.exists():
            try:
                clip_path.unlink()
            except OSError:
                pass

    summary = (result.get("summary") or "").strip()
    if not summary:
        return

    scene_context_payload = json.dumps({
        "gap_start_ms": int(gap_start_ms),
        "gap_end_ms": int(gap_end_ms),
        "duration_label": _humanize_gap_ms(gap_end_ms - gap_start_ms),
        "gap_start_label": _format_hms(gap_start_ms),
        "gap_end_label": _format_hms(gap_end_ms),
    })
    msg_id = await db.add_message(
        session_id,
        "catchup",
        summary,
        scene_context=scene_context_payload,
        latency_ms=result.get("latency_ms"),
    )
    row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (msg_id,))
    if row:
        await manager.broadcast(
            {"type": "message", "message": _message_to_payload(row_to_dict(row) or {})}
        )
    await _broadcast_cost(session_id)


def _humanize_gap_ms(ms: int) -> str:
    total_sec = max(0, int(ms) // 1000)
    mins, secs = divmod(total_sec, 60)
    if mins <= 0:
        return f"{secs}s"
    return f"{mins} min {secs}s" if secs else f"{mins} min"


@app.post("/api/away")
async def api_away(request: Request, _auth: dict = Depends(require_auth)):
    """Flip away-mode. On enter, record the Plex view offset. On exit,
    compute the gap span and spawn the WDIM catch-up pipeline (no Kindroid).
    """
    global _away_active, _away_entered_at, _away_plex_offset_ms
    body = await request.json()
    desired = bool(body.get("active"))
    active = await db.get_active_session()

    if desired and not _away_active:
        _away_active = True
        _away_entered_at = datetime.now(timezone.utc)
        plex = plex_monitor.current_state()
        _away_plex_offset_ms = int(plex.get("view_offset_ms") or 0) if plex else 0
        if active:
            await db.add_message(active["id"], "system", "⏸ Away")
            row = await db.fetch_one(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (active["id"],),
            )
            if row:
                await manager.broadcast(
                    {"type": "message", "message": _message_to_payload(row_to_dict(row) or {})}
                )
        await manager.broadcast({"type": "away", "active": True})
        return JSONResponse({"away_active": True})

    if not desired and _away_active:
        # Exit away mode — compute gap, spawn catch-up if we have useful data.
        _away_active = False
        gap_start_ms = _away_plex_offset_ms or 0
        plex_now = plex_monitor.current_state()
        gap_end_ms = int(plex_now.get("view_offset_ms") or 0) if plex_now else gap_start_ms
        _away_entered_at = None
        _away_plex_offset_ms = None

        if active:
            await db.add_message(active["id"], "system", "▶ Back")
            row = await db.fetch_one(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (active["id"],),
            )
            if row:
                await manager.broadcast(
                    {"type": "message", "message": _message_to_payload(row_to_dict(row) or {})}
                )
        await manager.broadcast({"type": "away", "active": False})

        # Only spawn catch-up when the gap is meaningful and Plex advanced.
        if active and gap_end_ms > gap_start_ms + 10_000:
            asyncio.create_task(_process_catchup(active["id"], gap_start_ms, gap_end_ms))

        return JSONResponse({"away_active": False, "gap_ms": gap_end_ms - gap_start_ms})

    # No-op — already in requested state.
    return JSONResponse({"away_active": _away_active})


@app.post("/api/standby")
async def api_standby(request: Request, _auth: dict = Depends(require_auth)):
    """Flip session-control standby on/off. Server-side enforcement: pipelines
    no-op while standby is active. UI gets broadcast confirmation."""
    global _standby_active
    body = await request.json()
    desired = bool(body.get("active"))
    _standby_active = desired
    active = await db.get_active_session()
    if active:
        await db.add_message(
            active["id"],
            "system",
            "🛡 Standby mode — commentary paused" if desired else "▶ Resumed · commentary live",
        )
        row = await db.fetch_one(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (active["id"],),
        )
        if row:
            await manager.broadcast(
                {"type": "message", "message": _message_to_payload(row_to_dict(row) or {})}
            )
    await manager.broadcast({"type": "standby", "active": desired})
    return JSONResponse({"standby_active": desired})


@app.post("/api/ingestion")
async def api_log_ingestion(request: Request, _auth: dict = Depends(require_auth)):
    body = await request.json()
    who = body.get("who")
    method = body.get("method")
    if method not in ("smoke", "edible", "dab", "sober"):
        raise HTTPException(status_code=400, detail="Invalid method")
    # Tim has no tracker of his own — his body handles that, and Gemini infers
    # his level from how he types. Anyone on the roster can be handed something.
    kin = characters.get_character(who)
    if not kin or kin.is_placeholder:
        raise HTTPException(status_code=400, detail=f"Unknown kin: {who!r}")
    who = kin.key

    active = await db.get_active_session()
    session_id = active["id"] if active else None
    # Stacking: each non-sober tap is that kin's current level + 1, capped.
    # Sober resets them to 0. The timer always restarts from "now".
    if method == "sober":
        new_peak: Optional[int] = 0
    else:
        from stoned_tracker import MAX_LEVEL
        cur_level, _, _ = await stoned_current_state(who)
        new_peak = max(1, min(MAX_LEVEL, cur_level + 1))
    await db.log_ingestion(session_id, who, method, peak_level=new_peak)
    await manager.broadcast({
        "type": "ingestion", "who": who, "method": method, "peak_level": new_peak,
    })

    # Post a Tim-POV emote of handing it over. NAMING the recipient is essential:
    # this emote is relayed to the whole room, so "I pass you the pipe" would
    # have every kin in the room believe they'd just been handed a joint.
    if session_id and method != "sober":
        narration = taking_narration(method, kin_key=kin.key, name=kin.first_name)
        if narration:
            content = f"_(*{narration}*)_"
            msg_id = await db.add_message(session_id, "tim", content)
            _, _, segments = parse_reply(content)
            if segments:
                await db.execute(
                    "UPDATE messages SET segments_json = ? WHERE id = ?",
                    (json.dumps(segments), msg_id),
                )
            row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (msg_id,))
            await manager.broadcast(
                {"type": "message", "message": _message_to_payload(row_to_dict(row) or {})}
            )
            # Auto-fired emote — don't count toward the reinforcement cadence.
            asyncio.create_task(
                _process_tim_message(msg_id, session_id, user_initiated=False)
            )

    return JSONResponse({"logged": True, "peak_level": new_peak})


@app.post("/api/message")
async def api_send_message(request: Request, _auth: dict = Depends(require_auth)):
    """Store Tim's message and spawn the scene-analysis pipeline."""
    active = await db.get_active_session()
    if not active:
        raise HTTPException(status_code=400, detail="No active session")
    body = await request.json()
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Empty message")
    # Kindroid budget cap — see memory/project_kindroid_message_format.md.
    if len(content) > 1000:
        raise HTTPException(
            status_code=400,
            detail=f"Message too long ({len(content)} chars). Keep it under 1000.",
        )

    msg_id = await db.add_message(active["id"], "tim", content)
    # If Tim's message contains _(*emote*)_ blocks (he's using the Kindroid
    # emote convention in his own typing), parse them so the UI renders
    # interleaved emote/dialogue segments just like Eli's replies.
    _, _, tim_segments = parse_reply(content)
    if tim_segments and any(s["type"] == "emote" for s in tim_segments):
        await db.execute(
            "UPDATE messages SET segments_json = ? WHERE id = ?",
            (json.dumps(tim_segments), msg_id),
        )
    row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (msg_id,))
    payload = _message_to_payload(row_to_dict(row) or {})
    await manager.broadcast({"type": "message", "message": payload})
    asyncio.create_task(_process_tim_message(msg_id, active["id"]))
    return JSONResponse({"id": msg_id})


@app.get("/api/plex/thumb")
async def api_plex_thumb(k: str = "", _auth: dict = Depends(require_auth)):
    """Proxy a Plex poster so the token never leaves the server.

    `k` is a rating key, and IT HAS TO BE HONOURED. The UI has been sending
    `?k=<rating_key>` all along, but this handler had no `k` parameter — FastAPI
    silently dropped it and every request served whatever was playing RIGHT NOW.
    Two consequences, both visible:

      • Scroll back to an earlier film's briefing card and it shows the poster of
        the film currently on screen.
      • Stop playback and `current_state()` goes empty, so EVERY poster on the
        page 404s at once.

    Plex serves `/library/metadata/<ratingKey>/thumb` for any film in the library,
    playing or not (verified), so a rating key is all we need.
    """
    if not settings.plex_url or not settings.plex_token:
        raise HTTPException(status_code=404, detail="Plex not configured")

    rating_key = (k or "").strip()
    if rating_key:
        thumb_key = f"/library/metadata/{quote(rating_key, safe='')}/thumb"
    else:
        # No key — fall back to whatever's on now (the movie bar's own poster).
        thumb_key = plex_monitor.current_state().get("thumb")
    if not thumb_key:
        raise HTTPException(status_code=404, detail="No thumbnail available")

    url = build_thumb_url(thumb_key)
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, verify=settings.plex_verify_ssl) as c:
            r = await c.get(url)
            r.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Plex thumb fetch failed")
    return Response(
        content=r.content,
        media_type=r.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/api/cost")
async def api_cost(_auth: dict = Depends(require_auth)):
    """Return running cost totals for the active session + today."""
    active = await db.get_active_session()
    today = await db.get_today_cost()
    if not active:
        return JSONResponse({
            "session": {"total_usd": 0.0, "calls": 0, "by_type": {}},
            "today_usd": today,
        })
    return JSONResponse({
        "session": await db.get_session_cost(active["id"]),
        "today_usd": today,
    })


@app.get("/api/health")
async def api_health(_auth: dict = Depends(require_auth)):
    return JSONResponse(
        {
            "plex_unreachable": plex_monitor.is_unreachable(),
            "ffmpeg_available": await ffmpeg_available(),
            "plex_state": plex_monitor.current_state(),
        }
    )


@app.get("/api/movie-trivia/categories")
async def api_movie_trivia_categories(_auth: dict = Depends(require_auth)):
    """Static metadata for the X-Ray menu — category list with display
    properties. Subject lists and counts come from /api/movie-trivia/topics.
    """
    from gemini_brain import MOVIE_TRIVIA_CATEGORIES, AWARDS_CEREMONIES
    return JSONResponse({
        "categories": [
            {
                "id": k,
                "label": v["label"],
                "icon": v["icon"],
                "has_subpage": bool(v.get("has_subpage")),
                "movie_wide_only": bool(v.get("movie_wide_only")),
            }
            for k, v in MOVIE_TRIVIA_CATEGORIES.items()
        ],
        "awards_ceremonies": list(AWARDS_CEREMONIES),
    })


@app.post("/api/movie-trivia/topics")
async def api_movie_trivia_topics(request: Request, _auth: dict = Depends(require_auth)):
    """Populate the X-Ray sub-page subject lists. Always uses the current
    scene clip so the menu reflects what's on screen right now. Categories
    flagged `movie_wide_only` (Awards, Connections) ignore the clip internally.
    """
    active = await db.get_active_session()
    if not active:
        raise HTTPException(status_code=400, detail="No active session")

    plex = plex_monitor.current_state()
    title = (plex.get("title") or "").strip() if plex else ""
    year = plex.get("year") if plex else None
    if not title:
        raise HTTPException(status_code=400, detail="No movie currently detected on Plex")

    clip_path: Optional[Path] = None
    timestamp_label = ""
    if plex.get("part_key"):
        view_offset_ms = int(plex.get("view_offset_ms") or 0)
        view_offset_sec = view_offset_ms / 1000.0
        timestamp_label = _format_hms(view_offset_ms)
        stream_url = build_stream_url(plex["part_key"])
        try:
            clip_path = await extract_clip(stream_url, view_offset_sec, 12)
        except FFmpegNotFound:
            log.warning("ffmpeg unavailable for X-Ray topics — falling back to movie-wide")
        except FFmpegError as e:
            log.warning("X-Ray topics clip extraction failed (%s) — falling back", type(e).__name__)

    try:
        result = await gemini_brain.generate_scene_topics(
            movie_title=title, year=year,
            scene_clip=clip_path, timestamp_label=timestamp_label,
        )
    except Exception:
        log.exception("topics generation failed")
        raise HTTPException(status_code=500, detail="Topic extraction failed")
    finally:
        if clip_path and clip_path.exists():
            try:
                clip_path.unlink()
            except OSError:
                pass

    await _broadcast_cost(active["id"])
    return JSONResponse({
        "topics": result.get("topics", {}),
        "timestamp": timestamp_label or None,
    })


@app.post("/api/movie-trivia")
async def api_movie_trivia(request: Request, _auth: dict = Depends(require_auth)):
    """X-Ray trivia card fetch. Always uses the current scene context UNLESS
    the category is flagged `movie_wide_only` (Awards, Connections), in
    which case the clip is skipped.

    Body: {
      category: str,                    # required, one of MOVIE_TRIVIA_CATEGORIES
      subject?: str,                    # optional — for sub-page picks
      subject_subtitle?: str,           # optional — paren text like "Captain Sheldon"
    }
    """
    active = await db.get_active_session()
    if not active:
        raise HTTPException(status_code=400, detail="No active session")
    session_id = active["id"]
    body = await request.json()
    category = (body.get("category") or "").strip()
    subject = (body.get("subject") or "").strip() or None
    subject_subtitle = (body.get("subject_subtitle") or "").strip() or None
    from gemini_brain import MOVIE_TRIVIA_CATEGORIES
    if category not in MOVIE_TRIVIA_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Unknown category: {category}")
    meta = MOVIE_TRIVIA_CATEGORIES[category]
    # Movie-wide-only categories skip the clip entirely.
    use_scene_clip = not meta.get("movie_wide_only")

    plex = plex_monitor.current_state()
    title = (plex.get("title") or "").strip() if plex else ""
    year = plex.get("year") if plex else None
    if not title:
        raise HTTPException(status_code=400, detail="No movie currently detected on Plex")

    recent_rows = await db.fetch_all(
        "SELECT trivia FROM messages WHERE session_id = ? "
        "AND trivia IS NOT NULL AND trivia != '' "
        "ORDER BY id DESC LIMIT 12",
        (session_id,),
    )
    recent_trivia = [r["trivia"] for r in recent_rows if r["trivia"]]

    clip_path: Optional[Path] = None
    timestamp_label = ""
    if use_scene_clip and plex.get("part_key"):
        view_offset_ms = int(plex.get("view_offset_ms") or 0)
        view_offset_sec = view_offset_ms / 1000.0
        timestamp_label = _format_hms(view_offset_ms)
        stream_url = build_stream_url(plex["part_key"])
        try:
            clip_path = await extract_clip(stream_url, view_offset_sec, 12)
        except FFmpegNotFound:
            log.warning("ffmpeg unavailable for X-Ray card — falling back to text-only")
        except FFmpegError as e:
            log.warning("X-Ray card clip extraction failed (%s) — falling back", type(e).__name__)

    try:
        result = await gemini_brain.generate_category_trivia(
            movie_title=title, year=year, category=category,
            subject=subject, subject_subtitle=subject_subtitle,
            recent_trivia=recent_trivia,
            scene_clip=clip_path,
            timestamp_label=timestamp_label,
        )
    except Exception:
        log.exception("category trivia generation failed")
        raise HTTPException(status_code=500, detail="Trivia generation failed")
    finally:
        if clip_path and clip_path.exists():
            try:
                clip_path.unlink()
            except OSError:
                pass

    trivia_text = (result.get("trivia") or "").strip()
    if not trivia_text:
        raise HTTPException(status_code=502, detail="Trivia came back empty")
    chosen_label = result.get("label") or meta["label"]

    # Card label combines category with the picked subject when present.
    if subject:
        display_label = f"{chosen_label} · {subject}"
    else:
        display_label = chosen_label

    msg_id = await db.add_message(
        session_id,
        "category_trivia",
        display_label,
        trivia=trivia_text,
        scene_context=json.dumps({
            "category": category,
            "label": chosen_label,
            "subject": subject,
            "subject_subtitle": subject_subtitle,
            "scene_scoped": bool(result.get("scene_scoped")),
            "timestamp": timestamp_label or None,
        }),
        latency_ms=result.get("latency_ms"),
    )
    row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (msg_id,))
    if row:
        await manager.broadcast(
            {"type": "message", "message": _message_to_payload(row_to_dict(row) or {})}
        )
    await _broadcast_cost(session_id)
    # Trivia interaction — give the mood ticker a chance to refresh now,
    # bypassing its cadence dedup so Tim sees up-to-date theming.
    asyncio.create_task(_maybe_tick_mood(force=True))
    return JSONResponse({"id": msg_id, "trivia": trivia_text})


# ──────────────────────────────────────────────────────────────────────
# The reaction drawer
# ──────────────────────────────────────────────────────────────────────
#
# MEASURED against the live seedbox (2026-07-12, 1408 @ 1080p/10Mbps):
#
#     clip pull (45s)          5.9s   <- 4.5s of this is IRREDUCIBLE network:
#                                        TLS + a remote seek into a big mkv.
#                                        probesize/analyzeduration/nobuffer
#                                        together save 0.08s. Don't bother.
#     6 thumbs from the CLIP   0.08s  <- from the REMOTE stream it's 4.96s. 61x.
#     Gemini draft (LOW)       7.2s
#                             ------
#     drawer opens in          ~13s
#
# Two things that measurement killed, recorded so nobody re-derives them:
#   - Shortening the window saves nothing. The pull is ~4.8s FIXED + 0.025s per
#     second of video: a 15s clip costs 5.2s, a 60s clip costs 6.3s. Reach is
#     nearly free, so we take 45s.
#   - Warming a clip in the background WOULD halve the wait, and we deliberately
#     don't. A cached clip is stale by up to a tick, which means the newest
#     stretch of film is missing from the strip — i.e. the drawer can't show you
#     the thing that made you reach for the phone. That is the exact failure the
#     moment-picker exists to fix. Freshness beats latency here.

_REACTION_DRAFTS: dict[str, dict[str, Any]] = {}
_DRAFT_TTL_SECONDS = 600


def _sweep_reaction_drafts() -> None:
    """Drop expired drafts and their media. Called on every new draft — no timer."""
    now = datetime.now(timezone.utc)
    for did in [
        d for d, v in _REACTION_DRAFTS.items()
        if (now - v["created_at"]).total_seconds() > _DRAFT_TTL_SECONDS
    ]:
        _discard_draft(did, keep_frame=None)


def _discard_draft(draft_id: str, *, keep_frame: Optional[str]) -> None:
    """Delete a draft's clip and its unchosen thumbnails.

    `keep_frame` survives: it becomes the message row's frame_path, and is the
    first time a reaction has ever had a picture attached to it.
    """
    draft = _REACTION_DRAFTS.pop(draft_id, None)
    if not draft:
        return
    for m in draft.get("moments", []):
        fp = m.get("frame_path")
        if fp and fp != keep_frame:
            try:
                Path(fp).unlink(missing_ok=True)
            except OSError:
                pass
    clip = draft.get("clip_path")
    if clip:
        try:
            Path(clip).unlink(missing_ok=True)
        except OSError:
            pass


@app.post("/api/reaction/draft")
async def api_reaction_draft(request: Request, _auth: dict = Depends(require_auth)):
    """Open the drawer: pull ONE clip, index it, cut the strip from it.

    Strictly serial — we cannot thumbnail a beat before Gemini has named it. That
    buys one honest front-loaded wait, after which the wizard's first three steps
    run with no network at all.

    An optional `text` in the body is Tim SEARCHING: "what I'm reacting to". It rides
    into the same Gemini call as the clip, and comes back pointing at the one moment he
    meant plus the emotions he's likely feeling. No text = the browse path, unchanged.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    reacting_to = (body.get("text") or "").strip()[:300]

    active = await db.get_active_session()
    if not active:
        raise HTTPException(status_code=400, detail="No active session")
    if _pipeline_paused():
        raise HTTPException(status_code=409, detail="Paused")

    plex = plex_monitor.current_state()
    if not plex or not plex.get("part_key"):
        raise HTTPException(status_code=409, detail="Nothing is playing on Plex")

    # FREEZE THE PLAYHEAD. Every offset below is relative to this instant. Read it
    # live and the movie drifts under him while he's reading the strip — every
    # thumbnail would then point at the wrong moment, silently.
    playhead_sec = (plex.get("view_offset_ms") or 0) / 1000.0
    lookback = max(10, int(settings.reaction_lookback_seconds))
    stream_url = build_stream_url(plex["part_key"])

    settings_map = await db.get_all_settings()
    media_res = (settings_map.get("media_resolution") or "low").lower()

    try:
        clip_path = await extract_clip(stream_url, playhead_sec, lookback)
    except FFmpegNotFound:
        raise HTTPException(status_code=503, detail="ffmpeg unavailable")
    except FFmpegError as e:
        log.exception("reaction draft clip extraction failed")
        raise HTTPException(status_code=502, detail=f"Scene capture failed: {e}")

    try:
        draft = await gemini_brain.reaction_draft(
            clip_path,
            clip_seconds=lookback,
            movie_title=plex.get("display_title") or plex.get("title") or "",
            movie_context=(plex.get("summary") or "")[:800],
            # A 45-second clip CANNOT see a callback. Without this, the `callback`
            # and `theme` facets come back empty forever and nobody knows why.
            session_history=await build_session_history_for_gemini(active["id"]),
            media_resolution=media_res,
            reacting_to=reacting_to,
        )
    except GeminiError as e:
        clip_path.unlink(missing_ok=True)
        log.exception("reaction draft failed")
        raise HTTPException(status_code=502, detail=f"Couldn't read the scene: {e}")

    if not draft["moments"]:
        clip_path.unlink(missing_ok=True)
        raise HTTPException(status_code=502, detail="Nothing reactable found in the last "
                                                    f"{lookback} seconds")

    # Cut the strip FROM THE DOWNLOADED CLIP. Measured 61x faster than re-seeking
    # the remote stream, and it hits the seedbox zero further times.
    frames_dir = Path(settings.frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)
    results = await asyncio.gather(
        *(
            extract_frame(str(clip_path), m["offset_ms"] / 1000.0, out_dir=frames_dir)
            for m in draft["moments"]
        ),
        return_exceptions=True,
    )
    for m, res in zip(draft["moments"], results):
        if isinstance(res, Exception):
            log.warning("thumbnail failed for %s: %s", m["key"], res)
            m["frame_path"] = None
            m["frame_url"] = None
        else:
            m["frame_path"] = str(res)
            m["frame_url"] = f"/static/frames/{Path(res).name}"

    _sweep_reaction_drafts()
    draft_id = uuid.uuid4().hex[:12]
    _REACTION_DRAFTS[draft_id] = {
        **draft,
        "created_at": datetime.now(timezone.utc),
        "session_id": active["id"],
        "clip_path": str(clip_path),
        "clip_seconds": lookback,
        "playhead_sec": playhead_sec,
        "movie_title": plex.get("display_title") or plex.get("title") or "",
        "query": reacting_to,
    }

    # The mood came from the FILM, not from Tim's reaction — Decision #12 holds.
    # The drawer just learns it earlier than the old path did, so the glow shifts
    # on open rather than on send.
    if draft["mood"]:
        await manager.broadcast({"type": "mood", "mood": draft["mood"]})
        _mark_full_scene_mood(draft["mood"])
    await _broadcast_cost(active["id"])

    return JSONResponse(_reaction_draft_payload(draft_id, _REACTION_DRAFTS[draft_id]))


def _reaction_draft_payload(draft_id: str, draft: dict) -> dict[str, Any]:
    """The drawer's view of a draft. Shared by /draft and /refine so a refined draft
    is byte-for-byte the shape the wizard already renders."""
    lookback = draft.get("clip_seconds") or 45
    return {
        "draft_id": draft_id,
        "clip_seconds": lookback,
        "mood": draft["mood"],
        # Echo the search, and where it landed. `matched` is null on the browse path or
        # when his words matched nothing in the clip.
        "query": draft.get("query", ""),
        "matched": draft.get("matched"),
        "moments": [
            {
                "key": m["key"],
                "offset_ms": m["offset_ms"],
                "caption": m["caption"],
                # The fuller description, shown under the caption. Six frames from a
                # dim scene, on a phone, in the dark, are six identical rectangles —
                # the words are what make the strip usable, not the picture.
                "description": m["why"],
                "frame_url": m.get("frame_url"),
                # Seconds BEFORE the tap — what the strip is actually labelled with.
                "seconds_ago": round(lookback - m["offset_ms"] / 1000.0, 1),
                "targets": [
                    {
                        "key": t["key"],
                        "label": t["label"],
                        "facet": t["facet"],
                        "emotions": t["emotions"],
                    }
                    for t in m["targets"]
                ],
            }
            for m in draft["moments"]
        ],
        "reads": draft["reads"],
        # Quote tab: spoken lines heard in the clip. Each carries the key of its
        # injected `writing` target (on `moment_key`), so picking one rides the same
        # /sentences rails as any reaction. `moment_caption` = "which scene it was in".
        "quotes": [
            {
                "text": q["text"],
                "speaker": q["speaker"],
                "moment_key": q["moment_key"],
                "target_key": q["target_key"],
                "moment_caption": _caption_for(draft, q["moment_key"]),
            }
            for q in draft.get("quotes", [])
        ],
        # Song tab: whether music is playing (+ the heard cue). The identified track,
        # once looked up, is cached on the draft under "song".
        "music": draft.get("music", {"playing": False, "cue": ""}),
        "song": draft.get("song"),
        "emotions": _emotion_catalogue(),
    }


def _caption_for(draft: dict, moment_key: str) -> str:
    m = next((x for x in draft.get("moments", []) if x["key"] == moment_key), None)
    return (m or {}).get("caption", "")


def _inject_quotes(draft: dict, found: list[dict]) -> int:
    """Attach searched lines to the draft as `writing` targets, exactly like the
    quotes reaction_draft finds on its own — so picking one rides the same rails
    (the tsearch trick) and `_reaction_line_with_quote` ships it to the room.

    Each new line hangs on its nearest existing beat, deduped against lines already
    on the draft (case-insensitive) so re-searching doesn't pile up copies. Matches
    are prepended to `draft["quotes"]` so they surface at the top of the tab. The
    key CONTAINS "quote" on purpose — that's how `isForeignTarget` keeps it out of
    the Reaction tab's category lists. Returns how many new lines were added.
    """
    moments = draft.get("moments") or []
    if not moments:
        return 0
    seen = {(q.get("text") or "").strip().lower() for q in draft.get("quotes", [])}
    added: list[dict] = []
    for q in found:
        text = (q.get("text") or "").strip()
        if not text or text.lower() in seen:
            continue
        want = q.get("offset_ms") or 0
        beat = min(moments, key=lambda m: abs(m["offset_ms"] - want))
        speaker = (q.get("speaker") or "").strip()
        who = speaker or "someone off screen"
        # len(targets) as the suffix keeps the key unique on this beat across repeat
        # searches (targets only ever grow), and "quote" in it flags it as foreign.
        tkey = f"{beat['key']}quotefind{len(beat['targets'])}"
        beat["targets"].append({
            "key": tkey,
            "label": f'"{text}"'[:90],
            "facet": "writing",
            "note": f'Spoken by {who}: "{text}"'[:180],
            "emotions": [],
        })
        added.append({
            "text": text[:240],
            "speaker": speaker[:60],
            "moment_key": beat["key"],
            "target_key": tkey,
        })
        seen.add(text.lower())
    if added:
        draft["quotes"] = added + list(draft.get("quotes", []))
    return len(added)


def _reaction_line_with_quote(draft: dict, target: dict, first_person: str) -> str:
    """Ride the exact spoken words into the narration when a reaction is on a QUOTE.

    A quote target is injected onto its beat as a `writing` target and paired with
    an entry in `draft["quotes"]` by `target_key`. Match it back and prepend the
    line + speaker, so the room reacts to WHAT froze him, not just that he froze.
    Any non-quote target falls straight through to his sentence untouched.
    """
    quote = next(
        (q for q in draft.get("quotes", []) if q.get("target_key") == target.get("key")),
        None,
    )
    line = (quote or {}).get("text", "").strip()
    if not line:
        return first_person
    who = (quote.get("speaker") or "").strip()
    attribution = f"the line {who} just delivered" if who else "the line just delivered"
    return f'{first_person} — reacting to {attribution}: "{line}"'


def _reaction_line_with_song(draft: dict, target: dict, first_person: str) -> str:
    """Ride the TRACK into the narration when a reaction is on the identified song.

    Without this the room hears "god I love this" and has no idea what music moved
    him — it can't name the song back, talk about it, or catch the lyric. So when
    the target is the injected `song` target, hand the kins the whole card: the
    title + artist, one line about the music itself, and a memorable lyric when the
    lookup found one. Non-song targets fall straight through.
    """
    if target.get("key") != "song":
        return first_person
    card = (draft.get("song") or {}).get("card") or {}
    title = (card.get("title") or "").strip()
    if not title:
        return first_person
    artist = (card.get("artist") or "").strip()
    bits = [f'the track playing — "{title}"' + (f" by {artist}" if artist else "")]
    note = (card.get("note") or "").strip()
    if note:
        bits.append(note)
    lyric = (card.get("lyric") or "").strip()
    if lyric:
        bits.append(f'a line from it: "{lyric}"')
    return f'{first_person} — reacting to {"; ".join(bits)}'


def _emotion_catalogue() -> dict[str, Any]:
    """The whole thesaurus, once, so the client can render any key the draft ranks
    and also browse "all emotions" without another round trip."""
    return {
        "generics": [
            {"key": e.key, "emoji": e.emoji, "label": e.label,
             "color": emotions.color_for(e)}
            for e in emotions.GENERICS
        ],
        "by_mood": [
            {
                "mood": mood or "none",
                "color": emotions.MOOD_COLORS.get(mood or "", emotions.NULL_MOOD_COLOR),
                "emotions": [
                    {"key": e.key, "emoji": e.emoji, "label": e.label} for e in emos
                ],
            }
            for mood, emos in emotions.by_mood().items()
        ],
    }


def _refresh_frames_for(draft: dict, clip_path: Path) -> None:
    """After a refine re-indexes the SAME clip, the old thumbnails describe beats that no
    longer exist. Drop them; the new frames are cut by the caller."""
    for m in draft.get("moments", []):
        fp = m.get("frame_path")
        if fp:
            try:
                Path(fp).unlink(missing_ok=True)
            except OSError:
                pass


@app.post("/api/reaction/refine")
async def api_reaction_refine(request: Request, _auth: dict = Depends(require_auth)):
    """The MOMENT refine: reword and re-search the SAME cached clip.

    Distinct from "search again", which pulls a fresh 45s — by which point the beat he
    wanted has scrolled out of the window. This re-reads the clip already on disk, so the
    seedbox is never touched and his moment stays in reach.
    """
    body = await request.json()
    draft_id = (body.get("draft_id") or "").strip()
    draft = _REACTION_DRAFTS.get(draft_id)
    if not draft:
        raise HTTPException(status_code=410, detail="That draft expired — reopen the drawer")
    text = (body.get("text") or "").strip()[:300]
    clip_path = Path(draft["clip_path"])
    if not clip_path.exists():
        raise HTTPException(status_code=410, detail="The clip is gone — reopen the drawer")

    plex = plex_monitor.current_state() or {}
    try:
        fresh = await gemini_brain.reaction_draft(
            clip_path,
            clip_seconds=draft["clip_seconds"],
            movie_title=draft.get("movie_title", ""),
            movie_context=(plex.get("summary") or "")[:800],
            session_history=await build_session_history_for_gemini(draft["session_id"]),
            media_resolution=(await db.get_setting("media_resolution") or "low").lower(),
            reacting_to=text,
        )
    except GeminiError as e:
        log.exception("reaction refine failed")
        raise HTTPException(status_code=502, detail=f"Couldn't re-read the scene: {e}")
    if not fresh["moments"]:
        raise HTTPException(status_code=502, detail="Nothing reactable found for that")

    _refresh_frames_for(draft, clip_path)      # bin the old thumbnails
    frames_dir = Path(settings.frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)
    results = await asyncio.gather(
        *(extract_frame(str(clip_path), m["offset_ms"] / 1000.0, out_dir=frames_dir)
          for m in fresh["moments"]),
        return_exceptions=True,
    )
    for m, res in zip(fresh["moments"], results):
        if isinstance(res, Exception):
            m["frame_path"] = m["frame_url"] = None
        else:
            m["frame_path"] = str(res)
            m["frame_url"] = f"/static/frames/{Path(res).name}"

    # Update IN PLACE — same draft_id, same clip_path — so /sentences and /reaction hold.
    draft.update({
        "scene_description": fresh["scene_description"],
        "mood": fresh["mood"],
        "moments": fresh["moments"],
        "reads": fresh["reads"],
        "quotes": fresh.get("quotes", []),
        "music": fresh.get("music", {"playing": False, "cue": ""}),
        "matched": fresh.get("matched"),
    })
    draft.pop("song", None)   # re-indexed clip → any prior track-ID is stale
    await _broadcast_cost(draft["session_id"])
    return JSONResponse(_reaction_draft_payload(draft_id, draft))


@app.post("/api/reaction/song")
async def api_reaction_song(request: Request, _auth: dict = Depends(require_auth)):
    """The Song tab: name the track reaction_draft heard, and inject it as a
    `sound` target so 'React to this' rides the normal /sentences rails.

    Lazy — fired only when the tab opens — and cached on the draft, so re-opening
    it costs nothing. One grounded call (+~$0.035, +1 against the daily budget).
    """
    body = await request.json()
    draft_id = (body.get("draft_id") or "").strip()
    draft = _REACTION_DRAFTS.get(draft_id)
    if not draft:
        raise HTTPException(status_code=410, detail="That draft expired — reopen the drawer")

    # Idempotent: a prior lookup on this draft is returned as-is.
    if draft.get("song"):
        return JSONResponse(draft["song"])

    if not draft.get("moments"):
        raise HTTPException(status_code=400, detail="Nothing to anchor a song reaction to")

    # The clip is the real evidence — identify_song LISTENS to it first, so we no
    # longer gate on the draft's `music.playing` flag (a visuals-first read that
    # routinely files a score cue under dialogue as "sound design" and misses it).
    # We try whenever there's a clip to hear, or at least a cue already described.
    music = draft.get("music") or {}
    clip_ref = draft.get("clip_path")
    clip_path = Path(clip_ref) if clip_ref else None
    have_clip = bool(clip_path) and clip_path.exists()
    if not have_clip and not (music.get("playing") and music.get("cue")):
        raise HTTPException(status_code=400, detail="No music to identify in this clip")

    plex = plex_monitor.current_state() or {}
    playhead = draft.get("playhead_sec") or 0
    ts_label = f"{int(playhead // 60)}:{int(playhead % 60):02d}"
    try:
        card = await gemini_brain.identify_song(
            clip_path=clip_path if have_clip else None,
            movie_title=draft.get("movie_title", ""),
            movie_context=(plex.get("summary") or "")[:800],
            cue=music.get("cue", ""),
            timestamp_label=ts_label,
        )
    except GeminiError as e:
        log.exception("identify_song failed")
        raise HTTPException(status_code=502, detail=f"Couldn't identify the track: {e}")
    await _broadcast_cost(draft["session_id"])

    result: dict[str, Any] = {"found": bool(card.get("found"))}
    if card.get("found"):
        # Anchor on the most recent beat (closest to the tap); moments are sorted
        # ascending by offset, so that's the last one. Inject a `sound` target with
        # the fixed key "song" (replacing any stale one) — the drawer's emotion step
        # then renders it with no new code, exactly like a search match.
        beat = draft["moments"][-1]
        beat["targets"] = [t for t in beat["targets"] if t["key"] != "song"]
        title = card["title"]
        artist = card.get("artist", "")
        label = f"{title} — {artist}" if artist else title
        note_bits = [f'The song "{title}"' + (f" by {artist}" if artist else "")]
        if card.get("note"):
            note_bits.append(card["note"])
        # The lyric rides in the note too, so the sentence writer can lean on the
        # actual words — not just the title. (Capped hard; the note feeds a prompt.)
        if card.get("lyric"):
            note_bits.append(f'a line: "{card["lyric"]}"')
        song_target = {
            "key": "song",
            "label": label[:90],
            "facet": "sound",
            "note": " · ".join(note_bits)[:280],
            "emotions": card.get("emotions", []),
        }
        beat["targets"].append(song_target)
        result.update({
            "card": {k: card[k] for k in
                     ("title", "artist", "album", "year", "note", "source", "lyric")},
            "moment_key": beat["key"],
            "target_key": "song",
            # The client renders the drawer from its own draft snapshot (pulled before
            # this lookup), so hand it the injected target to seed the emotion step.
            "target": {k: song_target[k] for k in ("key", "label", "facet", "emotions")},
        })
    draft["song"] = result
    return JSONResponse(result)


@app.post("/api/reaction/quote-search")
async def api_reaction_quote_search(request: Request, _auth: dict = Depends(require_auth)):
    """The Quote tab's search: find a specific spoken line in the cached clip.

    reaction_draft only surfaces the few most notable quotes; this hunts the exact
    line Tim half-remembers ("he said something about jesus") by re-hearing the SAME
    clip on disk — the seedbox is never touched — and injects any matches as quote
    targets so they slot straight into the Quote list and the reaction flow.
    """
    body = await request.json()
    draft_id = (body.get("draft_id") or "").strip()
    draft = _REACTION_DRAFTS.get(draft_id)
    if not draft:
        raise HTTPException(status_code=410, detail="That draft expired — reopen the drawer")
    text = (body.get("text") or "").strip()[:300]
    if not text:
        raise HTTPException(status_code=400, detail="Type what was said")
    clip_ref = draft.get("clip_path")
    clip_path = Path(clip_ref) if clip_ref else None
    if not (clip_path and clip_path.exists()):
        raise HTTPException(status_code=410, detail="The clip is gone — reopen the drawer")
    if not draft.get("moments"):
        raise HTTPException(status_code=400, detail="Nothing to anchor a quote to")

    plex = plex_monitor.current_state() or {}
    try:
        found = await gemini_brain.find_quote(
            clip_path,
            query=text,
            clip_seconds=draft["clip_seconds"],
            movie_title=draft.get("movie_title", ""),
            movie_context=(plex.get("summary") or "")[:800],
        )
    except GeminiError as e:
        log.exception("find_quote failed")
        raise HTTPException(status_code=502, detail=f"Couldn't search the clip: {e}")
    await _broadcast_cost(draft["session_id"])

    matched = _inject_quotes(draft, found)
    payload = _reaction_draft_payload(draft_id, draft)
    payload["quote_matched"] = matched
    payload["quote_query"] = text
    return JSONResponse(payload)


@app.post("/api/reaction/retarget")
async def api_reaction_retarget(request: Request, _auth: dict = Depends(require_auth)):
    """The TARGET refine: 'no — his acting' → re-derive the things in this beat."""
    body = await request.json()
    draft = _REACTION_DRAFTS.get((body.get("draft_id") or "").strip())
    if not draft:
        raise HTTPException(status_code=410, detail="That draft expired — reopen the drawer")
    moment = next((m for m in draft["moments"] if m["key"] == body.get("moment")), None)
    if not moment:
        raise HTTPException(status_code=400, detail="Unknown moment")
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Say what you're reacting to")

    try:
        fresh = await gemini_brain.reaction_retarget(
            scene_description=draft["scene_description"],
            moment_caption=moment["caption"],
            moment_why=moment["why"],
            steer=text,
            movie_title=draft.get("movie_title", ""),
        )
    except GeminiError as e:
        log.exception("reaction retarget failed")
        raise HTTPException(status_code=502, detail=f"Couldn't re-read the beat: {e}")

    # New keys, keyed off the moment; the catch-all is always re-appended so he is never
    # stuck when none of the named targets is the thing he meant.
    targets = []
    for j, t in enumerate(fresh):
        targets.append({**t, "key": f"{moment['key']}rt{j}"})
    targets.append({"key": moment["key"] + "tall", "label": "just… all of it",
                    "facet": "story", "note": "The whole beat.", "emotions": []})
    moment["targets"] = targets
    await _broadcast_cost(draft["session_id"])
    return JSONResponse({"targets": [
        {"key": t["key"], "label": t["label"], "facet": t["facet"], "emotions": t["emotions"]}
        for t in targets
    ]})


@app.post("/api/reaction/reemote")
async def api_reaction_reemote(request: Request, _auth: dict = Depends(require_auth)):
    """The EMOJI refine: 'more nervous than scared' → re-rank the feelings for this target."""
    body = await request.json()
    draft = _REACTION_DRAFTS.get((body.get("draft_id") or "").strip())
    if not draft:
        raise HTTPException(status_code=410, detail="That draft expired — reopen the drawer")
    moment = next((m for m in draft["moments"] if m["key"] == body.get("moment")), None)
    target = (next((t for t in moment["targets"] if t["key"] == body.get("target")), None)
              if moment else None)
    if not target:
        raise HTTPException(status_code=400, detail="Unknown target")
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Say how it felt")

    try:
        keys = await gemini_brain.reaction_reemote(
            scene_description=draft["scene_description"],
            moment_caption=moment["caption"],
            target_label=target["label"],
            target_facet=target["facet"],
            steer=text,
        )
    except GeminiError as e:
        log.exception("reaction reemote failed")
        raise HTTPException(status_code=502, detail=f"Couldn't re-read the feeling: {e}")

    target["emotions"] = keys      # mutate in place so /sentences resolves them
    await _broadcast_cost(draft["session_id"])
    return JSONResponse({"emotions": keys})


@app.post("/api/reaction/sentences")
async def api_reaction_sentences(request: Request, _auth: dict = Depends(require_auth)):
    """The one mid-wizard call: the finished sentences for the path he walked."""
    body = await request.json()
    draft = _REACTION_DRAFTS.get((body.get("draft_id") or "").strip())
    if not draft:
        raise HTTPException(status_code=410, detail="That draft expired — reopen the drawer")

    moment = next((m for m in draft["moments"] if m["key"] == body.get("moment")), None)
    if not moment:
        raise HTTPException(status_code=400, detail="Unknown moment")
    target = next((t for t in moment["targets"] if t["key"] == body.get("target")), None)
    if not target:
        raise HTTPException(status_code=400, detail="Unknown target")
    emotion_key = (body.get("emotion") or "").strip()
    if emotion_key not in emotions.BY_KEY:
        raise HTTPException(status_code=400, detail="Unknown emotion")

    try:
        result = await gemini_brain.reaction_sentences(
            scene_description=draft["scene_description"],
            moment_caption=moment["caption"],
            moment_why=moment["why"],
            target_label=target["label"],
            target_note=target["note"],
            target_facet=target["facet"],
            emotion_key=emotion_key,
            movie_title=draft["movie_title"],
            intensity=int(body.get("intensity") or 2),
            shade=(body.get("shade") or "").strip(),
            steer=(body.get("steer") or "").strip(),
        )
    except GeminiError as e:
        log.exception("reaction sentences failed")
        raise HTTPException(status_code=502, detail=f"Couldn't write the lines: {e}")

    await _broadcast_cost(draft["session_id"])
    return JSONResponse({"options": result["options"]})


@app.post("/api/reaction")
async def api_send_reaction(request: Request, _auth: dict = Depends(require_auth)):
    """Commit the reaction Tim actually chose, and send it round the room."""
    active = await db.get_active_session()
    if not active:
        raise HTTPException(status_code=400, detail="No active session")
    body = await request.json()

    draft_id = (body.get("draft_id") or "").strip()
    draft = _REACTION_DRAFTS.get(draft_id)
    if not draft:
        raise HTTPException(status_code=410, detail="That draft expired — reopen the drawer")

    moment = next((m for m in draft["moments"] if m["key"] == body.get("moment")), None)
    target = (
        next((t for t in moment["targets"] if t["key"] == body.get("target")), None)
        if moment else None
    )
    emotion = emotions.get((body.get("emotion") or "").strip())
    if not (moment and target and emotion):
        raise HTTPException(status_code=400, detail="Incomplete reaction")

    first_person = (body.get("first_person") or "").strip()
    if not first_person:
        raise HTTPException(status_code=400, detail="No reaction text")

    intensity_level = emotions.intensity(body.get("intensity") or 2)["level"]

    # The line Tim READ AND CHOSE is the line that goes to the room, verbatim.
    # First person, because that is the convention for Tim's own actions in an
    # outgoing Kindroid payload — "I" is the sender, and the sender is Tim. (The
    # sentences also carry a third_person twin; it is vestigial. See the note in
    # `_run_relay` about the guard that briefly, wrongly, rewrote these.)
    aim_at = (body.get("aim_at") or "").strip() or None

    # If he's reacting to a QUOTED line or an identified SONG, the kins need the
    # thing itself — the same way a rating fine-tune hands them what's being judged.
    # Otherwise they hear "I froze" / "I love this" and have no idea what to. These
    # are mutually exclusive targets, so chaining is safe: each is a no-op off-target.
    reaction_line = _reaction_line_with_quote(draft, target, first_person)
    reaction_line = _reaction_line_with_song(draft, target, reaction_line)

    content = f"{emotion.emoji} {first_person}"
    msg_id = await db.add_message(active["id"], "reaction", content)
    await db.execute(
        """UPDATE messages
           SET scene_context = ?, mood = ?, frame_path = ?, emote_text = ?,
               spoken_text = ?
           WHERE id = ?""",
        (
            draft["scene_description"] or None,
            draft["mood"],
            # The frame he actually picked — the first time a reaction has ever
            # carried a picture. Stored as a URL, matching the live-photo rows the
            # renderer already knows how to draw.
            moment.get("frame_url"),
            # facet:label:intensity — the level rides along so the row remembers how hard
            # it hit, parsed leniently on read (older rows have no third segment).
            f"{target['facet']}:{target['label']}:{intensity_level}",
            emotion.key,
            msg_id,
        ),
    )
    row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (msg_id,))
    await manager.broadcast(
        {"type": "message", "message": _message_to_payload(row_to_dict(row) or {})}
    )

    # Keep the chosen frame; bin the clip and the thumbnails he didn't pick.
    scene = draft["scene_description"]
    mood = draft["mood"]
    _discard_draft(draft_id, keep_frame=moment.get("frame_path"))

    asyncio.create_task(
        _process_chosen_reaction(active["id"], scene, mood, reaction_line, aim_at)
    )
    return JSONResponse({"id": msg_id})


@app.post("/api/reaction/film")
async def api_film_verdict(request: Request, _auth: dict = Depends(require_auth)):
    """"The movie so far" — a verdict on the FILM, not on a moment.

    A genuinely different act from a moment reaction, which is why it was confusing
    sitting in the same bar pretending to be one. It has no beat, no target and no
    clip: it needs only the scene we already know about, so it keeps the old
    one-liner path (the only thing that still uses `reaction_oneliner`).
    """
    active = await db.get_active_session()
    if not active:
        raise HTTPException(status_code=400, detail="No active session")
    body = await request.json()
    emoji = (body.get("emoji") or "").strip()
    label = (body.get("label") or "").strip()
    if not emoji:
        raise HTTPException(status_code=400, detail="No verdict")

    msg_id = await db.add_message(active["id"], "reaction", f"{emoji} {label}")
    row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (msg_id,))
    await manager.broadcast(
        {"type": "message", "message": _message_to_payload(row_to_dict(row) or {})}
    )
    asyncio.create_task(_process_reaction(msg_id, active["id"], emoji, label))
    return JSONResponse({"id": msg_id})


async def _process_chosen_reaction(
    session_id: str,
    scene: str,
    mood: Optional[str],
    reaction_line: str,
    aim_at: Optional[str],
) -> None:
    """Relay the sentence Tim CHOSE. Not one Gemini invented for him.

    `aim_at` closes a real gap: `_named_in` forces a named kin to the mic, but it
    reads the TYPED dialogue — and a reaction has none. So until now a reaction
    always hit the whole room and you could never nudge one kin about the thing
    they'd care about.
    """
    if _pipeline_paused():
        return
    previous: Optional[str] = None
    try:
        if aim_at:
            previous = (await db.get_all_settings()).get("addressed_kins") or "[]"
            await db.set_setting("addressed_kins", json.dumps([aim_at]))
        await _run_relay(session_id, scene=scene, history="", reaction=reaction_line, mood=mood)
    except Exception:
        log.exception("reaction relay crashed")
        await _system_message(session_id, "Reaction pipeline error — see server logs.")
    finally:
        if previous is not None:
            await db.set_setting("addressed_kins", previous)


_PHOTO_EXT_BY_MIME = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/heic": "heic",
    "image/heif": "heif",
}


@app.post("/api/live-photo")
async def api_live_photo(
    photos: list[UploadFile] = File(...),
    caption: str = Form(""),
    _auth: dict = Depends(require_auth),
):
    """Tim shares one or more photos with Eli, optionally with a caption.

    Each file is saved to data/live_photos/{session_id}/{uuid}.{ext}, then a
    background pipeline analyzes them all together with Gemini Pro and relays
    the dual-focus narration + photo URLs to Kindroid. Files are wiped on
    session finalize — they're disposable.
    """
    active = await db.get_active_session()
    if not active:
        raise HTTPException(status_code=400, detail="No active session")
    if not photos:
        raise HTTPException(status_code=400, detail="No photos uploaded")
    session_id = active["id"]

    photo_dir = Path(settings.live_photos_dir) / session_id
    photo_dir.mkdir(parents=True, exist_ok=True)

    saved: list[tuple[Path, str, str]] = []  # (path, mime, public_url)
    for photo in photos:
        mime = (photo.content_type or "image/jpeg").lower()
        if not mime.startswith("image/"):
            raise HTTPException(status_code=415, detail=f"Unsupported media type: {mime}")
        ext = _PHOTO_EXT_BY_MIME.get(mime, "jpg")
        filename = f"{uuid.uuid4().hex}.{ext}"
        file_path = photo_dir / filename
        contents = await photo.read()
        await asyncio.to_thread(file_path.write_bytes, contents)
        url = f"{settings.public_base_url.rstrip('/')}/photos/{session_id}/{filename}"
        saved.append((file_path, mime, url))

    # The placeholder bubble shows the first photo as a thumbnail; the rest
    # will appear when the pipeline finishes (we update the bubble's
    # attachment list via segments_json metadata).
    first_url = saved[0][2]
    placeholder = "📸 (sharing a photo…)" if len(saved) == 1 else f"📸 (sharing {len(saved)} photos…)"
    msg_id = await db.add_message(
        session_id, "tim", placeholder, frame_path=first_url,
    )
    # Store the full attachment list in segments_json so it survives reloads.
    # Frontend renders one <img> per URL; the wrapped narration text will be
    # added by the background processor once Gemini returns.
    extra_urls = [u for (_, _, u) in saved[1:]]
    if extra_urls:
        await db.execute(
            "UPDATE messages SET segments_json = ? WHERE id = ?",
            (json.dumps({"extra_photos": extra_urls}), msg_id),
        )
    row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (msg_id,))
    if row:
        await manager.broadcast(
            {"type": "message", "message": _message_to_payload(row_to_dict(row) or {})}
        )

    asyncio.create_task(
        _process_live_photo(msg_id, session_id, saved, caption)
    )
    # Photo interaction — refresh the mood now (Tim engaged with the app).
    asyncio.create_task(_maybe_tick_mood(force=True))
    return JSONResponse({"id": msg_id, "urls": [u for (_, _, u) in saved]})


async def _build_background_movie_context(session_id: str) -> str:
    """Lightweight 'what's playing' summary for live photo/audio pipelines —
    title + the most recent scene description we already have on file. No
    fresh Plex clip extraction (this is meant to be cheap and background-only).
    """
    plex = plex_monitor.current_state()
    parts: list[str] = []
    title = (plex.get("display_title") or plex.get("title") or "").strip() if plex else ""
    if title:
        year = plex.get("year") if plex else None
        if year and "(" not in title:
            parts.append(f"Currently playing: {title} ({year})")
        else:
            parts.append(f"Currently playing: {title}")
        if plex.get("view_offset_ms"):
            parts.append(f"Playhead: {_format_hms(plex['view_offset_ms'])}")
    last_scene = await db.fetch_one(
        "SELECT scene_context, mood FROM messages "
        "WHERE session_id = ? AND scene_context IS NOT NULL AND scene_context != '' "
        "ORDER BY id DESC LIMIT 1",
        (session_id,),
    )
    if last_scene:
        sc = (last_scene["scene_context"] or "").strip()
        if sc:
            # Keep it short — this is background, not foreground.
            if len(sc) > 600:
                sc = sc[:600].rstrip() + "…"
            parts.append(f"Most recent scene: {sc}")
        mood = (last_scene["mood"] or "").strip()
        if mood:
            parts.append(f"Mood: {mood}")
    return "\n".join(parts)


_AUDIO_EXT_BY_MIME = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "m4a",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
}


@app.post("/api/live-audio")
async def api_live_audio(
    audio: UploadFile = File(...),
    caption: str = Form(""),
    _auth: dict = Depends(require_auth),
):
    """Tim records a voice note while watching and sends it to Eli.
    Optional `caption` is Tim's typed text accompanying the recording;
    it goes to Kindroid as his spoken dialog after the audio emotes.

    Same disposable-storage model as photos: saved to
    data/live_audio/{session_id}/{uuid}.{ext}, Gemini Pro transcribes,
    transcript + caption together go to Kindroid as Tim's spoken dialogue,
    files wiped on session finalize.
    """
    active = await db.get_active_session()
    if not active:
        raise HTTPException(status_code=400, detail="No active session")
    session_id = active["id"]

    mime = (audio.content_type or "audio/webm").lower().split(";")[0].strip()
    ext = _AUDIO_EXT_BY_MIME.get(mime, "webm")
    if not mime.startswith("audio/"):
        raise HTTPException(status_code=415, detail=f"Unsupported media type: {mime}")

    audio_dir = Path(settings.live_audio_dir) / session_id
    audio_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    file_path = audio_dir / filename
    contents = await audio.read()
    await asyncio.to_thread(file_path.write_bytes, contents)

    audio_url = f"{settings.public_base_url.rstrip('/')}/audio/{session_id}/{filename}"

    placeholder = "🎙 (transcribing voice note…)"
    msg_id = await db.add_message(
        session_id, "tim", placeholder, frame_path=audio_url,
    )
    row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (msg_id,))
    if row:
        await manager.broadcast(
            {"type": "message", "message": _message_to_payload(row_to_dict(row) or {})}
        )

    asyncio.create_task(
        _process_live_audio(msg_id, session_id, file_path, audio_url, mime, caption)
    )
    # Audio interaction — refresh the mood now (Tim engaged with the app).
    asyncio.create_task(_maybe_tick_mood(force=True))
    return JSONResponse({"id": msg_id, "url": audio_url})


async def _process_live_audio(
    msg_id: int,
    session_id: str,
    file_path: Path,
    audio_url: str,
    mime: str,
    caption: str = "",
) -> None:
    """Background: Gemini Pro transcribes audio, transcript → Kindroid as dialogue."""
    log.info("live-audio pipeline START msg_id=%s session=%s mime=%s",
             msg_id, session_id, mime)
    if _pipeline_paused():
        log.warning("live-audio pipeline ABORTED — pipeline paused")
        return
    await manager.broadcast({"type": "eli_typing", "on": True})
    try:
        try:
            movie_context = await _build_background_movie_context(session_id)
        except Exception:
            log.exception("live-audio movie_context build failed — proceeding without it")
            movie_context = ""
        try:
            log.info("live-audio calling Gemini analyze_live_audio…")
            result = await gemini_brain.analyze_live_audio(
                file_path, mime_type=mime, movie_context=movie_context,
            )
            log.info("live-audio Gemini OK — transcript=%d chars", len((result.get("transcript") or "")))
        except Exception:
            log.exception("live-audio Gemini analyze_live_audio CRASHED")
            await db.add_message(session_id, "system", "Voice note transcription failed — see server logs.")
            return

        tim_speech = (result.get("tim_speech") or "").strip()
        ambient_emote = (result.get("ambient_emote") or "").strip()
        # Defensive strip — Gemini may include wrappers despite the prompt.
        if ambient_emote.startswith("_(*") and ambient_emote.endswith("*)_"):
            ambient_emote = ambient_emote[3:-3].strip()
        if not tim_speech and not ambient_emote:
            log.warning("live-audio Gemini returned empty in both fields")
            await db.add_message(session_id, "system", "Voice note analysis returned nothing.")
            return

        # Build the bubble: emote(s) first, then any spoken text. Tim's
        # transcribed voice and his typed caption both render as spoken
        # segments after the ambient emote.
        caption_text = (caption or "").strip()
        segments_list: list[dict[str, str]] = []
        content_parts: list[str] = []
        if ambient_emote:
            segments_list.append({"type": "emote", "text": ambient_emote})
            content_parts.append(f"_(*{ambient_emote}*)_")
        if tim_speech:
            segments_list.append({"type": "spoken", "text": tim_speech})
            content_parts.append(tim_speech)
        if caption_text:
            segments_list.append({"type": "spoken", "text": caption_text})
            content_parts.append(caption_text)
        content_combined = "\n\n".join(content_parts)
        segments_payload = {"segments": segments_list}

        await db.execute(
            "UPDATE messages SET content = ?, segments_json = ?, latency_ms = ? WHERE id = ?",
            (content_combined, json.dumps(segments_payload), result.get("latency_ms"), msg_id),
        )
        updated = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (msg_id,))
        if updated:
            await manager.broadcast(
                {"type": "message_updated", "message": _message_to_payload(row_to_dict(updated) or {})}
            )
        # Combine transcript + caption into one typed_dialogue chunk for
        # Kindroid — they're both Tim's spoken contribution.
        dialogue_parts = [t for t in (tim_speech, caption_text) if t]
        kindroid_dialogue = "\n\n".join(dialogue_parts)
        log.info(
            "live-audio relaying to Kindroid — ambient=%d chars, dialogue=%d chars (transcript=%d + caption=%d)",
            len(ambient_emote), len(kindroid_dialogue), len(tim_speech), len(caption_text),
        )
        await _send_to_kindroid_and_render(
            session_id,
            scene_narration="",
            history_narrative="",
            reaction_narration=ambient_emote,
            typed_dialogue=kindroid_dialogue,
            mood=None,
        )
        log.info("live-audio pipeline DONE msg_id=%s", msg_id)
    except Exception:
        log.exception("live-audio pipeline CRASHED (outer)")
        try:
            await db.add_message(session_id, "system", "Audio pipeline crashed — see server logs.")
        except Exception:
            pass
    finally:
        await manager.broadcast({"type": "eli_typing", "on": False})


async def _process_live_photo(
    msg_id: int,
    session_id: str,
    saved: list[tuple[Path, str, str]],
    caption: str,
) -> None:
    """Background: Gemini Pro vision analyzes one or more photos + caption,
    produces a dual-focus narration (photo + movie background), then relays
    to Kindroid with all photo URLs attached.

    `saved` is a list of (file_path, mime_type, public_url) tuples.
    """
    photo_urls = [u for (_, _, u) in saved]
    log.info("live-photo pipeline START msg_id=%s session=%s photos=%d caption=%d chars",
             msg_id, session_id, len(saved), len(caption))
    if _pipeline_paused():
        log.warning("live-photo pipeline ABORTED — pipeline paused (standby=%s away=%s)",
                    _standby_active, _away_active)
        return
    await manager.broadcast({"type": "eli_typing", "on": True})
    try:
        try:
            movie_context = await _build_background_movie_context(session_id)
            log.info("live-photo movie_context built (%d chars)", len(movie_context))
        except Exception:
            log.exception("live-photo movie_context build failed — proceeding without it")
            movie_context = ""

        try:
            log.info("live-photo calling Gemini analyze_live_photo for %d image(s)…", len(saved))
            result = await gemini_brain.analyze_live_photo(
                [(p, m) for (p, m, _) in saved],
                caption=caption,
                movie_context=movie_context,
            )
            log.info("live-photo Gemini OK — narration=%d chars, latency=%sms",
                     len((result.get("narration") or "")), result.get("latency_ms"))
        except Exception:
            log.exception("live-photo Gemini analyze_live_photo CRASHED")
            await db.add_message(session_id, "system", "Photo analysis failed — see server logs.")
            return

        narration = (result.get("narration") or "").strip()
        # Defensive: strip outer wrappers if Gemini included them despite
        # instructions. The wrapper-add step below assumes raw inner text.
        if narration.startswith("_(*") and narration.endswith("*)_"):
            narration = narration[3:-3].strip()
        if not narration:
            log.warning("live-photo Gemini returned empty narration")
            await db.add_message(session_id, "system", "Photo analysis returned nothing.")
            return

        # Split the dual-focus narration into separate emote blocks. Gemini
        # was told to use a single blank line as the paragraph separator.
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", narration) if p.strip()]
        # Append the user's typed caption as a spoken segment so it shows
        # in the bubble below the photo emotes (and goes to Kindroid as
        # typed_dialogue rather than getting woven into the emote text).
        caption_text = (caption or "").strip()
        segments_list = [{"type": "emote", "text": p} for p in paragraphs]
        wrapped_parts = [f"_(*{p}*)_" for p in paragraphs]
        if caption_text:
            segments_list.append({"type": "spoken", "text": caption_text})
            wrapped_parts.append(caption_text)
        wrapped = "\n".join(wrapped_parts)
        # segments_json holds the parsed segments PLUS the extra-photo URL
        # list so the bubble can render all attachments on snapshot reload.
        extras_meta = {"extra_photos": photo_urls[1:]} if len(photo_urls) > 1 else {}
        segments_payload = {
            "segments": segments_list,
            **extras_meta,
        }
        await db.execute(
            "UPDATE messages SET content = ?, segments_json = ?, latency_ms = ? WHERE id = ?",
            (wrapped, json.dumps(segments_payload), result.get("latency_ms"), msg_id),
        )
        updated = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (msg_id,))
        if updated:
            await manager.broadcast(
                {"type": "message_updated", "message": _message_to_payload(row_to_dict(updated) or {})}
            )
        log.info(
            "live-photo relaying to Kindroid — %d image_url(s), caption=%d chars",
            len(photo_urls), len(caption_text),
        )
        # Photo emotes + photo URLs go in the emote slot; caption is Tim's
        # spoken dialog after the emotes.
        await _send_to_kindroid_and_render(
            session_id,
            scene_narration="",
            history_narrative="",
            reaction_narration=narration,
            typed_dialogue=caption_text,
            mood=None,
            image_urls=photo_urls,
        )
        log.info("live-photo pipeline DONE msg_id=%s", msg_id)
    except Exception:
        log.exception("live-photo pipeline CRASHED (outer)")
        try:
            await db.add_message(session_id, "system", "Photo pipeline crashed — see server logs.")
        except Exception:
            pass
    finally:
        await manager.broadcast({"type": "eli_typing", "on": False})


# ─── WebSocket ───────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    raw = websocket.cookies.get(COOKIE_NAME)
    if not _verify_cookie(raw):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect(websocket)
    try:
        snapshot = await _build_session_snapshot()
        await websocket.send_json(snapshot)

        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
            elif msg.get("type") == "resync":
                await websocket.send_json(await _build_session_snapshot())
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(websocket)


# --- Custom mood art -----------------------------------------------------------------
# Drop art at static/mood/<family>.png and it appears on the Movie Mode button. Nothing to
# register, no rebuild. Two modes:
#
#   <family>.png        MASK  — a transparent PNG; only the ALPHA is used. The room paints
#                              the shape in the mood's colour, so the art can NEVER come out
#                              a different colour from the border, the scrubber and the badge.
#   <family>.full.png   IMAGE — a transparent PNG shown as-is, keeping its own colours. Use
#                              this only for art generated IN that mood's palette; otherwise
#                              it will fight the rest of the room.
#
# Families: fear, tension, kinetic, wonder, levity, warmth, sorrow — plus `none` (no film).
_ART_DIR = Path("static/mood")



@app.get("/api/plex/sessions")
async def api_plex_sessions(_auth: dict = Depends(require_auth)):
    """Everything Plex is offering, and which one we're following.

    More than one candidate means WE ARE GUESSING. Tim gets asked instead of finding out an
    hour later that the app was watching a different episode than he was.
    """
    cands = plex_monitor.candidates()
    now = plex_monitor.current_state() or {}
    return JSONResponse({
        "sessions": [
            {
                "rating_key": c.get("rating_key"),
                "title": c.get("display_title") or c.get("title"),
                "state": c.get("state"),
                "player": c.get("player_name") or c.get("player_product") or "",
                "thumb": c.get("thumb"),
                "view_offset_ms": c.get("view_offset_ms"),
                "duration_ms": c.get("duration_ms"),
                "current": c.get("rating_key") == now.get("rating_key"),
            }
            for c in cands
        ],
        "pinned": plex_monitor.pinned(),
        "ambiguous": len(cands) > 1,
    })


@app.post("/api/plex/pick")
async def api_plex_pick(request: Request, _auth: dict = Depends(require_auth)):
    """Tim says WHICH ONE. That settles it — no heuristic gets a vote after this.

    `rating_key: null` un-pins and hands the choice back to the guesswork.
    """
    body = await request.json()
    key = body.get("rating_key")
    plex_monitor.pin(str(key) if key else None)
    await db.set_setting("plex_pinned", str(key) if key else "")
    await manager.broadcast({"type": "refresh"})
    return JSONResponse({"ok": True, "pinned": plex_monitor.pinned()})

@app.get("/api/mood-art")
async def api_mood_art():
    """What art actually exists on disk. Missing families fall back to the drawn vectors."""
    out: dict[str, dict] = {}
    if not _ART_DIR.is_dir():
        return out
    # Every MOOD (each gets its own face — `foreboding` and `horror` are not the same
    # picture) plus the button's own states. "no film" is worth drawing: it is the state
    # the phone sits in most of the time.
    families = set(emotions.MOODS) | {
        "none", "paused", "nofilm", "standby", "idle", "away", "ended",
    }
    for f in sorted(_ART_DIR.iterdir()):
        if f.suffix.lower() not in (".png", ".webp", ".svg"):
            continue
        stem = f.name[: -len(f.suffix)]
        full = stem.endswith(".full")
        if full:
            stem = stem[:-5]
        # Two independent slots per family: the icon, and the WORDMARK — the app's own name,
        # drawn in that mood. `fear.word.png` can trail cobwebs off the letters.
        suffix = ""
        for tag in (".face", ".lock", ".word", ".sub"):
            if stem.endswith(tag):
                suffix, stem = tag, stem[: -len(tag)]
                break
        fam = stem
        if fam not in families:
            continue
        key = f"{fam}{suffix}"
        # A .full file wins over a plain one for the same slot — it's the deliberate choice.
        if key in out and not full:
            continue
        # CACHE-BUST ON CONTENT. The whole point of this directory is that you iterate: drop a
        # better version of the art in under the SAME name and reload. But the URL doesn't
        # change, so the browser happily keeps serving the bytes it already has, and the new
        # art is invisible. Stamping the mtime makes each version its own URL.
        try:
            v = int(f.stat().st_mtime)
        except OSError:
            v = 0
        out[key] = {
            "src": f"/static/mood/{f.name}?v={v}",
            "mode": "image" if full else "mask",
        }
    return out
