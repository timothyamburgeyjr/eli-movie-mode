"""Project Eli: Movie Mode — FastAPI app with WebSocket dashboard."""
import asyncio
import json
import logging
import random
import re
import secrets
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import bcrypt
import httpx
from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

from config import settings
from context_manager import build_session_history_for_gemini
import characters
from database import db, row_to_dict, rows_to_list
from gemini_brain import GeminiError, gemini_brain
from kindroid_relay import KindroidError, build_payload, parse_reply, send_message
from plex_monitor import plex_monitor
from session_manager import (
    build_session_stats,
    format_stats_hint,
    write_journal_entry,
)
from stoned_tracker import current_state as stoned_current_state
from stoned_tracker import ingestion_narration
from stoned_tracker import eli_state_directive
from stoned_tracker import narration_for as stoned_narration_for  # legacy — Tim-POV, no longer used
from stoned_tracker import reinforcement_narration
from smart_snap import (
    FFmpegError,
    FFmpegNotFound,
    build_stream_url,
    build_thumb_url,
    extract_clip,
    ffmpeg_available,
)

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# App lifecycle
# ──────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    Path(settings.frames_dir).mkdir(parents=True, exist_ok=True)

    plex_monitor.add_listener(_on_plex_event)
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
async def _generate_briefing_card(session_id: str, movie_id: int, plex_data: dict[str, Any]) -> None:
    """Kick off a Gemini briefing for a newly-detected movie, then relay it to
    Kindroid so Eli gets the context too.

    Idempotent: if a briefing has already been generated for this movie (the
    `movies.briefing` column is populated), skip — prevents duplicate sends
    when session-start and media_change both fire for the same rating_key.
    """
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

    briefing_text = result.get("briefing", "")
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
    if briefing_text.strip():
        await _send_to_kindroid_and_render(
            session_id,
            scene_narration=briefing_text,
            history_narrative="",
            stoned_line="",
            reaction_narration="",
            typed_dialogue="",
            mood=None,
            show_typing=False,
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
    prev = _mood_tick_state.get("last_mood")
    if new_mood == prev:
        return  # No change — keep the WS quiet, the UI doesn't need a re-render.
    _mood_tick_state["last_mood"] = new_mood
    await manager.broadcast({"type": "mood", "mood": new_mood})
    await _broadcast_cost(session_id)
    log.info("mood ticker: %s → %s (offset %.1fs)", prev, new_mood, view_offset_sec)


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
    """Increment the per-session counter and fire an Eli-state observation
    emote when we hit the 2-3 message threshold, if Eli is currently stoned.
    """
    if _pipeline_paused():
        return
    eli_level, eli_method, _ = await stoned_current_state("eli")
    if eli_level <= 0:
        # Reset the counter so we don't fire immediately when Eli starts later.
        _reinforcement_counters[session_id] = 0
        return
    count = _reinforcement_counters.get(session_id, 0) + 1
    threshold = random.randint(*_REINFORCEMENT_THRESHOLD_RANGE)
    if count < threshold:
        _reinforcement_counters[session_id] = count
        return
    _reinforcement_counters[session_id] = 0
    narration = reinforcement_narration(eli_level, eli_method)
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


async def _send_to_kindroid_and_render(
    session_id: str,
    *,
    scene_narration: str = "",
    history_narrative: str = "",
    stoned_line: str = "",
    reaction_narration: str = "",
    typed_dialogue: str = "",
    mood: Optional[str] = None,
    show_typing: bool = True,
    image_urls: Optional[list[str]] = None,
) -> None:
    # Respect any mid-flight standby/away flip.
    if _pipeline_paused():
        if show_typing:
            await manager.broadcast({"type": "eli_typing", "on": False})
        return
    """Assemble the Kindroid payload from emote sections, send, parse, persist
    Eli's reply, and broadcast it to the UI. Handles overflow with condense().
    """
    payload = build_payload(
        scene_narration=scene_narration,
        history_narrative=history_narrative,
        stoned_narration=stoned_line,
        reaction_narration=reaction_narration,
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
                typed_dialogue=typed_dialogue,
            )
        except Exception:
            log.exception("condense overflow-fix failed")

    if show_typing:
        await manager.broadcast({"type": "eli_typing", "on": True})
    log.info(
        "kindroid SEND session=%s payload_chars=%d image_urls=%d",
        session_id, len(payload), len(image_urls or []),
    )
    active = await _active_character()
    ai_id = active.ai_id if active else None
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
                "Eli's response came back empty — Kindroid returned nothing. "
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
            f"Eli couldn't respond — Kindroid error: {detail}",
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
    eli_eli_level, _, _ = await stoned_current_state("eli")
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
    tim_dialogue = (tim_msg_row["content"] if tim_msg_row else "") or ""
    dialogue_len = len(tim_dialogue)
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
            scene_result = await gemini_brain.analyze_scene(
                clip_path,
                movie_title=plex.get("display_title") or plex.get("title"),
                timestamp_label=_format_hms(plex.get("view_offset_ms") or 0),
                target_chars=scene_target,
                session_history=history_text,
                history_budget=history_budget,
                tim_message=tim_dialogue,
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
                    tim_message=tim_dialogue,
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
        tim_dialogue.strip() or scene_for_kindroid or stoned_line
    )
    if has_content:
        await _send_to_kindroid_and_render(
            session_id,
            scene_narration=scene_for_kindroid,
            history_narrative=history_for_kindroid,
            stoned_line=stoned_line,
            typed_dialogue=tim_dialogue,
            mood=mood_for_kindroid,
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
        await _send_to_kindroid_and_render(
            session_id,
            scene_narration=scene_desc,
            history_narrative=history_narrative_text,
            stoned_line=stoned_line,
            reaction_narration=oneliner,
            typed_dialogue="",
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
    extra_photos: list[str] = []
    if segments_raw:
        try:
            parsed = json.loads(segments_raw)
            # Legacy shape: a bare list of segment dicts.
            if isinstance(parsed, list):
                segments = parsed
            # New shape: dict with optional "segments" + "extra_photos".
            elif isinstance(parsed, dict):
                segs = parsed.get("segments")
                if isinstance(segs, list):
                    segments = segs
                extras = parsed.get("extra_photos")
                if isinstance(extras, list):
                    extra_photos = [u for u in extras if isinstance(u, str)]
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "id": msg_row.get("id"),
        "sender": msg_row.get("sender"),
        "content": msg_row.get("content"),
        "emote_text": msg_row.get("emote_text"),
        "spoken_text": msg_row.get("spoken_text"),
        "segments": segments,
        "scene_context": msg_row.get("scene_context"),
        "mood": msg_row.get("mood"),
        "stoned_level_tim": msg_row.get("stoned_level_tim", 0),
        "stoned_level_eli": msg_row.get("stoned_level_eli", 0),
        "trivia": msg_row.get("trivia"),
        "latency_ms": msg_row.get("latency_ms"),
        "timestamp": msg_row.get("timestamp"),
        "frame_path": msg_row.get("frame_path"),
        "extra_photos": extra_photos,
    }


async def _build_session_snapshot() -> dict[str, Any]:
    active = await db.get_active_session()
    last_ended = await db.get_last_ended_session()
    usage = await db.get_today_usage()
    all_settings = await db.get_all_settings()
    all_settings.pop("password_hash", None)

    today_cost = await db.get_today_cost()
    # Eli's current ingestion state — drives the quick-popover highlight on
    # the chat-side leaf indicator. Reads the latest event from the DB so
    # the UI is correct even on a fresh page load.
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
        "eli_ingestion": {"level": eli_level, "method": eli_method},
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
    if _verify_cookie(eli_session):
        return RedirectResponse(url="/dashboard", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


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


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, eli_session: Optional[str] = Cookie(default=None, alias=COOKIE_NAME)):
    if not _verify_cookie(eli_session):
        return RedirectResponse(url="/login", status_code=302)
    response = templates.TemplateResponse("dashboard.html", {"request": request})
    # Prevent browsers (especially mobile Safari) from caching the dashboard
    # HTML — the inline JS changes across deploys and stale cache causes
    # confusing "button/feature missing" reports.
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


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
async def api_session_stop(_auth: dict = Depends(require_auth)):
    # Idempotent — if there's no active session we return success with
    # already_stopped=true rather than 400. This tolerates double-taps
    # and avoids scary red errors in the console.
    active = await db.get_active_session()
    if not active:
        return JSONResponse({"ended": False, "already_stopped": True})

    session_id = active["id"]
    await db.add_message(session_id, "system", "Wrapping up the session…")
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
    asyncio.create_task(_finalize_session(session_id))
    return JSONResponse({"session_id": session_id, "ending": True})


async def _finalize_session(session_id: str) -> None:
    """Orchestrate sign-off, marathon summary, journal, and session end."""
    try:
        stats = await build_session_stats(session_id)
        stats_hint = format_stats_hint(stats)
        history = await build_session_history_for_gemini(session_id)

        # 1) Sign-off (Gemini → DB → Kindroid so Eli gets a last word).
        signoff_text = ""
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
            # Send the sign-off to Kindroid as plain dialogue so Eli can reply.
            # This is the spec's exception to the "everything wrapped in emotes"
            # rule — sign-off is Tim speaking DIRECTLY to Eli.
            await _send_to_kindroid_and_render(
                session_id,
                scene_narration="",
                history_narrative="",
                stoned_line="",
                reaction_narration="",
                typed_dialogue=signoff_text,
                mood=None,
                show_typing=True,
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


@app.get("/api/characters")
async def api_get_characters(_auth: dict = Depends(require_auth)):
    """Selectable family members for the Movie Mode character dropdown."""
    roster = [
        {"key": c.key, "name": c.name, "first_name": c.first_name}
        for c in characters.selectable()
    ]
    active = await db.get_setting("active_character") or characters.DEFAULT_KEY
    return JSONResponse({"characters": roster, "active": active})


@app.put("/api/settings/{key}")
async def api_put_setting(key: str, request: Request, _auth: dict = Depends(require_auth)):
    if key == "password_hash":
        raise HTTPException(status_code=403, detail="Use /api/password")
    body = await request.json()
    value = body.get("value")
    if value is None:
        raise HTTPException(status_code=400, detail="Missing value")
    await db.set_setting(key, str(value))
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
    # Tim no longer has a tracker (his body handles his own state); only
    # Eli's ingestion is logged now. Reject Tim requests but stay quiet for
    # any stale frontend that still sends them.
    if who not in ("eli",) or method not in ("smoke", "edible", "dab", "sober"):
        raise HTTPException(status_code=400, detail="Invalid ingestion payload")
    active = await db.get_active_session()
    session_id = active["id"] if active else None
    # Stacking behavior: each non-sober tap = current Eli level + 1 (cap 3).
    # Sober resets to 0. Timer always resets to "now."
    if method == "sober":
        new_peak: Optional[int] = 0
    else:
        from stoned_tracker import MAX_LEVEL
        cur_level, _, _ = await stoned_current_state("eli")
        new_peak = max(1, min(MAX_LEVEL, cur_level + 1))
    await db.log_ingestion(session_id, who, method, peak_level=new_peak)
    await manager.broadcast({
        "type": "ingestion", "who": who, "method": method, "peak_level": new_peak,
    })

    # Post a Tim-POV ingestion emote to chat (Tim passing it to Eli with her
    # reaction folded in) for Eli's non-sober taps. Sober taps reset the
    # tracker silently.
    if session_id and method != "sober":
        narration = ingestion_narration(method, who="eli")
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
async def api_plex_thumb(_auth: dict = Depends(require_auth)):
    """Proxy the current Plex poster thumb so the token never leaves the server."""
    state = plex_monitor.current_state()
    thumb_key = state.get("thumb")
    if not thumb_key or not settings.plex_url or not settings.plex_token:
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


@app.post("/api/reaction")
async def api_send_reaction(request: Request, _auth: dict = Depends(require_auth)):
    """Store a reaction and generate a contextual one-liner in the background."""
    active = await db.get_active_session()
    if not active:
        raise HTTPException(status_code=400, detail="No active session")
    body = await request.json()
    emoji = (body.get("emoji") or "").strip()
    label = (body.get("label") or "").strip()
    placeholder = f"{emoji} {label}".strip()
    msg_id = await db.add_message(active["id"], "reaction", placeholder)
    row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (msg_id,))
    payload = _message_to_payload(row_to_dict(row) or {})
    await manager.broadcast({"type": "message", "message": payload})
    asyncio.create_task(_process_reaction(msg_id, active["id"], emoji, label))
    return JSONResponse({"id": msg_id})


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
            stoned_line="",
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
            stoned_line="",
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
