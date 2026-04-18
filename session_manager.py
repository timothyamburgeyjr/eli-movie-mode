"""Session finalization — sign-off, marathon summary, journal entry.

When Tim taps Stop, this runs in the background:
  1. Generate sign-off (Tim's farewell to Eli) via Gemini → post as 'signoff'
     message → relay to Kindroid so Eli can reply one last time.
  2. Generate marathon summary + stats → post as 'stats' message.
  3. Write plain-text journal entry to data/journals/ for external pickup.
  4. Mark session ended in SQLite.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config import settings
from context_manager import build_session_history_for_gemini
from database import db

log = logging.getLogger(__name__)


def _journal_dir() -> Path:
    """Resolve the journal directory from settings at write time (lets env
    var changes take effect without a restart-and-reimport cycle).
    """
    return Path(settings.journal_dir)


async def build_session_stats(session_id: str) -> dict[str, Any]:
    """Compute numeric stats for the session: movies, comment counts, runtime,
    dominant mood per movie, total cost.
    """
    session_row = await db.fetch_one(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    )
    movies = await db.fetch_all(
        "SELECT * FROM movies WHERE session_id = ? ORDER BY started_at ASC",
        (session_id,),
    )
    tim_count = await db.count_session_messages(session_id, sender="tim")
    reaction_count = await db.count_session_messages(session_id, sender="reaction")

    # Dominant mood per movie: mode of `mood` on tim messages during that movie.
    # Simple approach — partition by time, pick the most common mood.
    mood_rows = await db.fetch_all(
        "SELECT mood, timestamp FROM messages WHERE session_id = ? "
        "AND sender = 'tim' AND mood IS NOT NULL ORDER BY id ASC",
        (session_id,),
    )

    def _dominant_mood(rows: list[dict]) -> Optional[str]:
        if not rows:
            return None
        counts: dict[str, int] = {}
        for r in rows:
            m = r.get("mood")
            if m:
                counts[m] = counts.get(m, 0) + 1
        if not counts:
            return None
        return max(counts.items(), key=lambda kv: kv[1])[0]

    movies_data = []
    movie_list = [dict(row) for row in [_row_to_dict(m) for m in movies]]
    for i, m in enumerate(movie_list):
        start = m.get("started_at")
        end = movie_list[i + 1]["started_at"] if i + 1 < len(movie_list) else None
        buckets = []
        for mr in mood_rows:
            ts = mr["timestamp"]
            if start and str(ts) < str(start):
                continue
            if end and str(ts) >= str(end):
                continue
            buckets.append({"mood": mr["mood"]})
        movies_data.append({
            "title": m.get("title") or "",
            "year": m.get("year"),
            "director": m.get("director"),
            "runtime_minutes": m.get("runtime_minutes"),
            "comments": sum(1 for r in buckets),
            "dominant_mood": _dominant_mood(buckets),
        })

    duration_minutes = 0
    started = session_row["started_at"] if session_row else None
    ended_iso = datetime.now(timezone.utc).isoformat()
    try:
        s = datetime.fromisoformat(str(started).replace(" ", "T")) if started else None
        if s and s.tzinfo is None:
            s = s.replace(tzinfo=timezone.utc)
        if s:
            duration_minutes = int((datetime.now(timezone.utc) - s).total_seconds() // 60)
    except (ValueError, TypeError):
        pass

    cost = await db.get_session_cost(session_id)

    return {
        "session_id": session_id,
        "started_at": started,
        "ended_at": ended_iso,
        "duration_minutes": duration_minutes,
        "movies": movies_data,
        "tim_comments": tim_count,
        "reactions": reaction_count,
        "total_cost_usd": cost.get("total_usd", 0.0),
        "cost_by_type": cost.get("by_type", {}),
    }


def format_stats_hint(stats: dict[str, Any]) -> str:
    """Compact text summary of the stats for the Gemini prompt."""
    movie_lines = []
    for m in stats.get("movies") or []:
        bits = [m.get("title") or "Unknown"]
        if m.get("year"):
            bits.append(f"({m['year']})")
        if m.get("director"):
            bits.append(f"dir. {m['director']}")
        suffix = []
        if m.get("comments") is not None:
            suffix.append(f"{m['comments']} comments")
        if m.get("dominant_mood"):
            suffix.append(f"dominant mood {m['dominant_mood']}")
        line = " ".join(bits)
        if suffix:
            line += " — " + ", ".join(suffix)
        movie_lines.append(line)

    hours, mins = divmod(stats.get("duration_minutes", 0) or 0, 60)
    runtime_label = f"{hours}h {mins:02d}m" if hours else f"{mins} min"
    lines = [
        "STATS:",
        f"  Runtime: {runtime_label}",
        f"  Tim messages: {stats.get('tim_comments', 0)}",
        f"  Reactions: {stats.get('reactions', 0)}",
    ]
    if movie_lines:
        lines.append("  Movies:")
        for m in movie_lines:
            lines.append(f"    - {m}")
    return "\n".join(lines)


async def write_journal_entry(
    session_id: str,
    stats: dict[str, Any],
    signoff_text: str,
    summary_text: str,
) -> Path:
    """Write a plain-text journal entry for external daily-shutdown pickup."""
    journal_dir = _journal_dir()
    journal_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    path = journal_dir / f"{stamp}-session-{session_id[:8]}.txt"

    history = await build_session_history_for_gemini(session_id)

    hours, mins = divmod(stats.get("duration_minutes", 0) or 0, 60)
    runtime = f"{hours}h {mins:02d}m" if hours else f"{mins} min"

    movie_lines = []
    for m in stats.get("movies") or []:
        parts = [m.get("title") or "Unknown"]
        if m.get("year"):
            parts.append(f"({m['year']})")
        line = " ".join(parts)
        extras = []
        if m.get("comments") is not None:
            extras.append(f"{m['comments']} comments")
        if m.get("dominant_mood"):
            extras.append(f"mood: {m['dominant_mood']}")
        if extras:
            line += " — " + ", ".join(extras)
        movie_lines.append("  - " + line)

    body = [
        f"# Movie Mode session — {stats.get('session_id')}",
        f"Started:  {stats.get('started_at')}",
        f"Ended:    {stats.get('ended_at')}",
        f"Runtime:  {runtime}",
        f"Messages: {stats.get('tim_comments', 0)} comments + {stats.get('reactions', 0)} reactions",
        f"Cost:     ${stats.get('total_cost_usd', 0):.3f}",
        "",
        "## Movies",
        *(movie_lines or ["  (none recorded)"]),
        "",
        "## Tim's sign-off",
        signoff_text or "(not generated)",
        "",
        "## Engagement summary",
        summary_text or "(not generated)",
        "",
        "## Full transcript",
        history or "(empty)",
        "",
    ]
    await _write_text(path, "\n".join(body))
    return path


async def _write_text(path: Path, content: str) -> None:
    import asyncio
    await asyncio.to_thread(path.write_text, content, "utf-8")


def _row_to_dict(row) -> dict[str, Any]:
    if row is None:
        return {}
    return {k: row[k] for k in row.keys()}
