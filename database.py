"""Async SQLite layer. Single-user app — one shared connection."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from config import settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    started_at DATETIME,
    ended_at DATETIME,
    status TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions(id),
    sender TEXT,
    content TEXT,
    emote_text TEXT,
    spoken_text TEXT,
    scene_context TEXT,
    mood TEXT,
    stoned_level_tim INTEGER DEFAULT 0,
    stoned_level_eli INTEGER DEFAULT 0,
    trivia TEXT,
    frame_path TEXT,
    latency_ms INTEGER,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS movies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions(id),
    plex_rating_key TEXT,
    title TEXT,
    year INTEGER,
    director TEXT,
    runtime_minutes INTEGER,
    briefing TEXT,
    started_at DATETIME,
    ended_at DATETIME
);

CREATE TABLE IF NOT EXISTS ingestion_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions(id),
    who TEXT,
    method TEXT,
    logged_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS daily_usage (
    date TEXT PRIMARY KEY,
    gemini_calls INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS gemini_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    call_type TEXT,        -- scene | briefing | trivia | reaction | condense
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    thinking_tokens INTEGER DEFAULT 0,
    grounded INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_movies_session ON movies(session_id);
CREATE INDEX IF NOT EXISTS idx_ingestion_session ON ingestion_events(session_id);
CREATE INDEX IF NOT EXISTS idx_gemini_calls_session ON gemini_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_gemini_calls_ts ON gemini_calls(timestamp);
"""

DEFAULT_SETTINGS = {
    "emotes_visible": "true",
    "trivia_visible": "true",
    "mood_gradient": "true",
    "capture_seconds": "30",
    "trivia_grounding": "true",
    "context_exchanges": "5",
    "auto_journal": "true",
    # Which family member (registry key) Movie Mode is currently talking to.
    "active_character": "eli",
    # Physical-setting context: where Tim is and who's in the room.
    "active_venue": "living_room",
    "present_people": "[]",            # JSON list of presence.PEOPLE keys
    "venue_descriptions": "{}",        # JSON map venue_key -> saved description
}


class Database:
    def __init__(self) -> None:
        self.conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        db_path = Path(settings.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(db_path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        # Lightweight migrations for columns added after initial schema.
        await self._ensure_column("messages", "segments_json", "TEXT")
        await self._ensure_column("movies", "mood", "TEXT")
        await self._ensure_column("ingestion_events", "peak_level", "INTEGER")
        for key, value in DEFAULT_SETTINGS.items():
            await self.conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        await self.conn.commit()

    async def _ensure_column(self, table: str, column: str, coltype: str) -> None:
        assert self.conn is not None
        async with self.conn.execute(f"PRAGMA table_info({table})") as cur:
            cols = [row[1] for row in await cur.fetchall()]
        if column not in cols:
            await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        assert self.conn is not None
        async with self._lock:
            cursor = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cursor

    async def fetch_one(self, sql: str, params: tuple = ()) -> Optional[aiosqlite.Row]:
        assert self.conn is not None
        async with self.conn.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def fetch_all(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        assert self.conn is not None
        async with self.conn.execute(sql, params) as cursor:
            return await cursor.fetchall()

    # ─── Settings ───
    async def get_setting(self, key: str) -> Optional[str]:
        row = await self.fetch_one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def get_all_settings(self) -> dict[str, str]:
        rows = await self.fetch_all("SELECT key, value FROM settings")
        return {r["key"]: r["value"] for r in rows}

    # ─── Sessions ───
    async def create_session(self, session_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.execute(
            "INSERT INTO sessions (id, started_at, status) VALUES (?, ?, 'active')",
            (session_id, now),
        )

    async def end_session(self, session_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.execute(
            "UPDATE sessions SET ended_at = ?, status = 'ended' WHERE id = ?",
            (now, session_id),
        )

    async def get_active_session(self) -> Optional[aiosqlite.Row]:
        return await self.fetch_one(
            "SELECT * FROM sessions WHERE status = 'active' "
            "ORDER BY started_at DESC LIMIT 1"
        )

    async def get_last_ended_session(self) -> Optional[aiosqlite.Row]:
        return await self.fetch_one(
            "SELECT * FROM sessions WHERE status = 'ended' "
            "ORDER BY ended_at DESC LIMIT 1"
        )

    # ─── Messages ───
    async def add_message(
        self,
        session_id: str,
        sender: str,
        content: str = "",
        *,
        emote_text: Optional[str] = None,
        spoken_text: Optional[str] = None,
        scene_context: Optional[str] = None,
        mood: Optional[str] = None,
        stoned_level_tim: int = 0,
        stoned_level_eli: int = 0,
        trivia: Optional[str] = None,
        frame_path: Optional[str] = None,
        latency_ms: Optional[int] = None,
    ) -> int:
        cursor = await self.execute(
            """INSERT INTO messages
            (session_id, sender, content, emote_text, spoken_text, scene_context,
             mood, stoned_level_tim, stoned_level_eli, trivia, frame_path, latency_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id, sender, content, emote_text, spoken_text, scene_context,
                mood, stoned_level_tim, stoned_level_eli, trivia, frame_path, latency_ms,
            ),
        )
        return cursor.lastrowid or 0

    async def get_session_messages(self, session_id: str) -> list[aiosqlite.Row]:
        return await self.fetch_all(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        )

    async def count_session_messages(self, session_id: str, sender: Optional[str] = None) -> int:
        if sender:
            row = await self.fetch_one(
                "SELECT COUNT(*) AS n FROM messages WHERE session_id = ? AND sender = ?",
                (session_id, sender),
            )
        else:
            row = await self.fetch_one(
                "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?",
                (session_id,),
            )
        return row["n"] if row else 0

    # ─── Movies ───
    async def add_movie(
        self,
        session_id: str,
        plex_rating_key: str,
        title: str,
        year: Optional[int] = None,
        director: Optional[str] = None,
        runtime_minutes: Optional[int] = None,
        briefing: Optional[str] = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self.execute(
            """INSERT INTO movies
            (session_id, plex_rating_key, title, year, director, runtime_minutes,
             briefing, started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, plex_rating_key, title, year, director, runtime_minutes, briefing, now),
        )
        return cursor.lastrowid or 0

    async def get_session_movies(self, session_id: str) -> list[aiosqlite.Row]:
        return await self.fetch_all(
            "SELECT * FROM movies WHERE session_id = ? ORDER BY started_at ASC",
            (session_id,),
        )

    # ─── Ingestion ───
    async def log_ingestion(
        self,
        session_id: Optional[str],
        who: str,
        method: str,
        peak_level: Optional[int] = None,
    ) -> None:
        await self.execute(
            "INSERT INTO ingestion_events (session_id, who, method, peak_level) VALUES (?, ?, ?, ?)",
            (session_id, who, method, peak_level),
        )

    async def get_latest_ingestion(self, who: str) -> Optional[aiosqlite.Row]:
        return await self.fetch_one(
            "SELECT * FROM ingestion_events WHERE who = ? ORDER BY logged_at DESC LIMIT 1",
            (who,),
        )

    # ─── Daily usage ───
    async def increment_gemini_usage(self) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        await self.execute(
            "INSERT INTO daily_usage (date, gemini_calls) VALUES (?, 1) "
            "ON CONFLICT(date) DO UPDATE SET gemini_calls = gemini_calls + 1",
            (today,),
        )
        row = await self.fetch_one(
            "SELECT gemini_calls FROM daily_usage WHERE date = ?", (today,)
        )
        return row["gemini_calls"] if row else 0

    async def get_today_usage(self) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = await self.fetch_one(
            "SELECT gemini_calls FROM daily_usage WHERE date = ?", (today,)
        )
        return row["gemini_calls"] if row else 0

    # ─── Gemini call cost tracking ──────────────────────────────
    async def log_gemini_call(
        self,
        *,
        session_id: Optional[str],
        call_type: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        thinking_tokens: int = 0,
        grounded: bool = False,
        cost_usd: float = 0.0,
    ) -> None:
        await self.execute(
            """INSERT INTO gemini_calls
            (session_id, call_type, model, input_tokens, output_tokens,
             thinking_tokens, grounded, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id, call_type, model, int(input_tokens or 0),
                int(output_tokens or 0), int(thinking_tokens or 0),
                1 if grounded else 0, float(cost_usd or 0.0),
            ),
        )

    async def get_session_cost(self, session_id: str) -> dict[str, Any]:
        row = await self.fetch_one(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total, COUNT(*) AS calls "
            "FROM gemini_calls WHERE session_id = ?",
            (session_id,),
        )
        breakdown_rows = await self.fetch_all(
            "SELECT call_type, COUNT(*) AS calls, "
            "COALESCE(SUM(cost_usd), 0) AS cost "
            "FROM gemini_calls WHERE session_id = ? "
            "GROUP BY call_type",
            (session_id,),
        )
        return {
            "total_usd": float(row["total"]) if row else 0.0,
            "calls": int(row["calls"]) if row else 0,
            "by_type": {
                r["call_type"]: {
                    "calls": int(r["calls"]),
                    "cost_usd": float(r["cost"]),
                }
                for r in breakdown_rows
            },
        }

    async def get_today_cost(self) -> float:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = await self.fetch_one(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM gemini_calls "
            "WHERE DATE(timestamp) = ?",
            (today,),
        )
        return float(row["total"]) if row else 0.0


db = Database()


def row_to_dict(row: Optional[aiosqlite.Row]) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def rows_to_list(rows: list[aiosqlite.Row]) -> list[dict[str, Any]]:
    return [{k: r[k] for k in r.keys()} for r in rows]
