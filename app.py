"""Project Eli: Movie Mode — FastAPI app with WebSocket dashboard."""
import asyncio
import json
import logging
import random
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import bcrypt
import httpx
from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

from config import settings
from context_manager import build_session_history_for_gemini
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
from stoned_tracker import narration_for as stoned_narration_for
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

    try:
        yield
    finally:
        await plex_monitor.stop()
        await db.close()


app = FastAPI(title="Project Eli: Movie Mode", lifespan=lifespan)

templates = Jinja2Templates(directory="templates")
Path("static/frames").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


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
        "SELECT briefing FROM movies WHERE id = ?", (movie_id,)
    )
    if existing and (existing["briefing"] or "").strip():
        log.debug("briefing already exists for movie %s, skipping", movie_id)
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
        "UPDATE movies SET briefing = ? WHERE id = ?",
        (briefing_text, movie_id),
    )
    row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (msg_id,))
    await manager.broadcast(
        {"type": "message", "message": _message_to_payload(row_to_dict(row) or {})}
    )
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


def _pipeline_paused() -> bool:
    """Return True when any session-control flag should block pipeline work.

    Standby hard-stops everything. Away blocks Eli-facing work but lets the
    WDIM catch-up flow complete when the user returns.
    """
    return _standby_active or _away_active


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
    try:
        reply = await send_message(payload)
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

    # Stamp current stoned levels on Eli's row for per-message leaf rendering.
    eli_tim_level, _, _ = await stoned_current_state("tim")
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

    tim_level, tim_method, _ = await stoned_current_state("tim")
    eli_level, _, _ = await stoned_current_state("eli")
    stoned_line = stoned_narration_for(tim_level, tim_method)
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
                movie_title=plex.get("title"),
                timestamp_label=_format_hms(plex.get("view_offset_ms") or 0),
                target_chars=scene_target,
                session_history=history_text,
                history_budget=history_budget,
            )
        except GeminiError as primary_err:
            log.warning("Pro scene analysis failed, falling back to Flash: %s", primary_err)
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

    trivia_text: Optional[str] = None
    if scene_result and str(settings_map.get("trivia_grounding", "true")) == "true":
        exchange_count = await _count_tim_exchanges(session_id)
        if exchange_count % 3 == 0:
            try:
                trivia = await gemini_brain.generate_trivia(
                    movie_title=plex.get("title") or "",
                    scene_description=scene_result.get("scene_description", ""),
                )
                trivia_text = trivia.get("trivia") or None
            except Exception:
                log.exception("trivia generation failed")

    # Persist analysis fields on Tim's message row.
    await db.execute(
        """UPDATE messages
           SET scene_context = ?, mood = ?, trivia = ?, latency_ms = ?,
               stoned_level_tim = ?, stoned_level_eli = ?
           WHERE id = ?""",
        (
            scene_result.get("scene_description") if scene_result else None,
            scene_result.get("mood") if scene_result else None,
            trivia_text,
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
            movie_title=(plex.get("title") or "") if plex else "",
        )
    except Exception:
        log.exception("reaction one-liner failed")
        await db.add_message(session_id, "system", "Reaction narration failed.")
        return

    oneliner = (result.get("text") or "").strip()
    new_content = f"{emoji} {oneliner}".strip()

    tim_level, tim_method, _ = await stoned_current_state("tim")
    eli_level, _, _ = await stoned_current_state("eli")
    stoned_line = stoned_narration_for(tim_level, tim_method)

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
    if segments_raw:
        try:
            parsed = json.loads(segments_raw)
            if isinstance(parsed, list):
                segments = parsed
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
    }


async def _build_session_snapshot() -> dict[str, Any]:
    active = await db.get_active_session()
    last_ended = await db.get_last_ended_session()
    usage = await db.get_today_usage()
    all_settings = await db.get_all_settings()
    all_settings.pop("password_hash", None)

    today_cost = await db.get_today_cost()
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
        await manager.broadcast(await _build_session_snapshot())


@app.get("/api/settings")
async def api_get_settings(_auth: dict = Depends(require_auth)):
    all_settings = await db.get_all_settings()
    all_settings.pop("password_hash", None)
    return JSONResponse(all_settings)


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
            await db.add_message(active["id"], "system", "⏸ Away · " + _away_entered_at.astimezone().strftime("%-I:%M %p") if hasattr(_away_entered_at, "astimezone") else "⏸ Away")
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
    if who not in ("tim", "eli") or method not in ("smoke", "edible", "dab", "sober"):
        raise HTTPException(status_code=400, detail="Invalid ingestion payload")
    active = await db.get_active_session()
    session_id = active["id"] if active else None
    await db.log_ingestion(session_id, who, method)
    await manager.broadcast({"type": "ingestion", "who": who, "method": method})

    # Post a first-person ingestion emote to chat (and let Eli react) for
    # either Tim's or Eli's ingestion, during an active session, excluding
    # "sober" reset taps. The narration is always from Tim's POV — his own
    # action for Tim's tap, Tim passing it to Eli for Eli's tap.
    if session_id and method != "sober":
        narration = ingestion_narration(method, who=who)
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

    return JSONResponse({"logged": True})


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
