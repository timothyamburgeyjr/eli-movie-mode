"""Async SQLite layer. Single-user app — one shared connection."""
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from config import settings

log = logging.getLogger(__name__)


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

-- The film's emotional shape, for the mood arc under the scrubber.
--
-- Keyed on the PLAYHEAD (`at_ms`), never on wall-clock. The arc is the shape of
-- the FILM, so it has to be drawn along the film's timeline — pausing for twenty
-- minutes must not stretch the ribbon, and rewinding must not run it backwards.
--
-- The mood ticker already classifies every 30s; it just never kept the answer.
CREATE TABLE IF NOT EXISTS mood_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions(id),
    rating_key TEXT,
    at_ms INTEGER,          -- the PLAYHEAD, not the clock
    mood TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- WHAT THE ROOM DECIDED, and every fact that was true when it decided.
--
-- Until this table existed, the room's decision — who spoke, who stayed quiet, who
-- leaned in and WHY — was broadcast over the WebSocket and then thrown away. It
-- lived nowhere. So Tim could not report a bad call after a refresh, and there was
-- nothing to learn from. The app was making genuinely interesting judgments all
-- evening and keeping none of them.
--
-- Keyed on Tim's `messages.id` — that row already carries `scene_context` and
-- `mood`, so it is the natural anchor for a turn.
--
-- FREEZING `facts_json` IS WHAT MAKES REPORTING HONEST. A week later Daisy has gone
-- home and the mood has changed, but the rule Tim writes must be scoped to what was
-- true WHEN THE ROOM GOT IT WRONG — not to whatever happens to be true when he
-- finally gets round to reporting it.
CREATE TABLE IF NOT EXISTS turn_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions(id),
    tim_message_id INTEGER REFERENCES messages(id),
    facts_json TEXT,        -- the whole TurnContext, frozen
    decision_json TEXT,     -- speaking / passing / leaned_in / floor / silent / laws
    rationale TEXT,         -- the coordinator's own one-liner
    reported INTEGER DEFAULT 0,   -- Tim flagged this turn as wrong
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- RULES TIM APPROVED. Rule + conditions.
--
-- A rule with no conditions is universal. `verdict` decides whether it is a LAW
-- (always/never — enforced in Python before the model is ever called) or a LEAN
-- (usually/rarely — guidance in the prompt). See facts.py.
--
-- `hits` is the pruning signal: a rule that has never once fired across fifty
-- sessions is either wrong or scoped into oblivion, and Tim should be TOLD that
-- rather than left to wonder why nothing changed.
--
-- WHY THERE IS A `why`. A rule without one is a bald instruction, and a model obeys
-- it mechanically and stupidly at the edges:
--
--     "Bobby stays out of grief."
--         -> obeyed blindly. Says nothing when Tim asks him directly.
--
--     "Bobby stays out of grief — he's a doer, not a talker. He'd refill your
--      glass, not explain the curse."
--         -> now the model knows the SPIRIT, and can act on a moment the
--            conditions never anticipated.
--
-- `evidence` grounds the rule in the actual moment that produced it, which is worth
-- far more to a language model than an abstraction ever is — and it lets the rules
-- list tell Tim "you made this the night Bobby explained the family curse over
-- 'god, poor Sam'", which is how he'll actually remember it.
CREATE TABLE IF NOT EXISTS kin_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kin_key TEXT NOT NULL,
    rule_text TEXT NOT NULL,
    rule_why TEXT DEFAULT '',     -- why this is true of THIS person, from their vault
    evidence TEXT DEFAULT '',     -- the moment that produced it, in one line
    verdict TEXT NOT NULL,        -- always | usually | rarely | never
    when_json TEXT NOT NULL,      -- [] = universal; else [{fact, op, value}]
    from_turn_id INTEGER,         -- provenance: the turn that produced it
    hits INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- EVERY TAP COUNTS, even the ones that never become a rule.
--
-- `spoke` + `approved` is the whole semantic:
--
--     spoke   👍  ->  he was right to jump in        ->  rule says SPEAKS
--     spoke   👎  ->  he shouldn't have              ->  rule says STAYS QUIET
--     skipped 👍  ->  he was right to stay out       ->  rule says STAYS QUIET
--     skipped 👎  ->  he should have spoken          ->  rule says SPEAKS
--
--     i.e.  the rule says "speaks"  <=>  spoke == approved
--
-- THESE TAPS DO NOT MINE RULES. That was tried and abandoned, and Tim's own data killed
-- it: across 21 real frozen turns, EIGHT of the fourteen facts never changed at all —
-- venue, who was in the room, who was really in the room, how stoned he was, what was
-- playing, the day of the week. So a miner looking for "what do his approvals have in
-- common" would confidently report that Bobby goes quiet ON MONDAYS and IN THE LIVING
-- ROOM. Demanding more agreeing taps makes it WORSE: they all share the same constants.
--
-- What the taps do instead is the one thing statistics CAN do honestly here: they GRADE
-- the rules that were actually in force. That is a direct verdict on a specific rule, not
-- a hunt for coincidences among facts that cannot vary inside one evening.
CREATE TABLE IF NOT EXISTS turn_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id INTEGER REFERENCES turn_decisions(id),
    session_id TEXT,
    kin_uuid TEXT NOT NULL,      -- identity, never the name (characters.kin_uuid)
    kin_key TEXT,                -- readable, for the logs
    spoke INTEGER NOT NULL,      -- did they speak on that turn?
    approved INTEGER NOT NULL,   -- 1 = 👍, 0 = 👎
    rules_in_force TEXT,         -- JSON [rule_id] — what this tap is a verdict ON
    became_rule INTEGER,         -- rule id, or NULL: he tapped and made nothing
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
-- A RE-TAP IS AN UPDATE, NOT A DUPLICATE. He will change his mind, and two contradictory
-- verdicts on one moment would poison the scoring in a way nothing downstream could detect.
CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_once ON turn_feedback(turn_id, kin_uuid);
CREATE INDEX IF NOT EXISTS idx_feedback_session ON turn_feedback(session_id);
CREATE INDEX IF NOT EXISTS idx_turn_decisions_session ON turn_decisions(session_id);
CREATE INDEX IF NOT EXISTS idx_turn_decisions_msg ON turn_decisions(tim_message_id);
CREATE INDEX IF NOT EXISTS idx_kin_rules_kin ON kin_rules(kin_key, active);
CREATE INDEX IF NOT EXISTS idx_movies_session ON movies(session_id);
CREATE INDEX IF NOT EXISTS idx_ingestion_session ON ingestion_events(session_id);
CREATE INDEX IF NOT EXISTS idx_gemini_calls_session ON gemini_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_gemini_calls_ts ON gemini_calls(timestamp);
CREATE INDEX IF NOT EXISTS idx_mood_events_session ON mood_events(session_id, at_ms);
"""

DEFAULT_SETTINGS = {
    "emotes_visible": "true",
    "trivia_visible": "true",
    # HOW LOUD THE MOOD IS: off | subtle | full. Was a bare on/off toggle, and the
    # "on" state was so faint (6-13% opacity) that sixteen hand-tuned mood colours
    # were effectively invisible. Now the mood drives borders, the scrubber, the
    # accents and the wash — in every layout — so it needs a volume knob rather than
    # a switch. Two hours of a 40%-red screen during a horror film is genuinely
    # tiring, and that has to be Tim's call mid-movie, not mine at design time.
    "mood_gradient": "full",
    "capture_seconds": "30",
    "trivia_grounding": "true",
    "context_exchanges": "5",
    "auto_journal": "true",
    # Which family member (registry key) Movie Mode is currently talking to.
    # Legacy single-kin selector; the room below supersedes it. Kept as the
    # fallback for the paths not yet migrated (briefing, sign-off).
    "active_character": "eli",
    # The room: which kins are watching with Tim this session, and which of
    # them the next message is aimed at. Both are JSON lists of registry keys.
    # Empty room = fall back to single-kin behaviour on `active_character`.
    "room_kins": "[]",
    "addressed_kins": "[]",
    # Muted kins still hear everything and still reply — their Kindroid memory
    # stays in step with the movie. Their bubbles just arrive collapsed, so the
    # room doesn't become noise you have to read.
    "muted_kins": "[]",
    # Movie briefings: "always" | "first" (skip only the opening film's) | "never".
    "briefings": "always",
    # Physical-setting context: where Tim is and who's in the room.
    "active_venue": "living_room",
    "present_people": "[]",            # JSON list of presence.PEOPLE keys
    "venue_descriptions": "{}",        # JSON map venue_key -> saved description
    # How much of the video Gemini actually looks at when indexing a reaction
    # window: "low" | "default". A SETTING, not a constant, on purpose — LOW is
    # 4x cheaper and 1.3s faster, and measured on a real clip it found just as
    # many facets (8 vs 8). But LOW is also exactly what would flatten
    # "that acting was amazing" and "beautifully shot" into "nice shot", since
    # those need facial nuance and composition. If the craft targets ever come
    # back thin, this must be flippable mid-movie, baked, without a redeploy.
    "media_resolution": "low",
    # WHICH SHAPE THE ROOM TAKES. The only knob — there used to be two.
    #
    #   auto     work it out from the screen: wide -> theater, narrow -> compact
    #   theater  the film, with the room beside it
    #   desktop  the room on its own, three rails, no film
    #   compact  the phone shape
    #
    # `view_mode` (normal|theater) USED to live here too, and `layout: auto` was
    # resolved partly from it. Two settings for one decision, free to disagree — which
    # is why choosing a mode never quite did what it said on the tin. It's gone; any
    # stale row left in the settings table is simply ignored.
    #
    # "sidecar" is GONE as well. It meant "the dashboard, embedded in the theater's
    # iframe" — and nothing is embedded any more: theatre is a layout of this page,
    # with Plex as the iframe rather than the other way round.
    #
    # Below DESKTOP_MIN (900px) the client forces `compact` whatever this says. An
    # explicit choice is an escape hatch, not a licence to render three rails on a
    # phone.
    "layout": "auto",   # auto | compact | desktop | theater
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
        # THE CAST, as Plex knows it: [{"actor","character","thumb"}, …] in billing
        # order. Plex has always returned this on /library/metadata and the app has
        # always thrown it away. Stored on the row that's already the per-film record,
        # so a briefing regenerated after a restart still knows who is in the film.
        await self._ensure_column("movies", "cast_json", "TEXT")
        await self._ensure_column("ingestion_events", "peak_level", "INTEGER")
        # WHICH kin replied. `sender` only says 'eli' vs 'tim' vs 'system' — it
        # was written as the literal 'eli' for every companion, so the old
        # transcript can't tell you who spoke. Backfill those rows to 'eli',
        # which is who they actually were before the roster existed.
        if await self._ensure_column("messages", "character_key", "TEXT"):
            await self.conn.execute(
                "UPDATE messages SET character_key = 'eli' WHERE sender = 'eli'"
            )
        # A KEY IS A HANDLE, NOT AN IDENTITY. `bobby` is a name, and names change —
        # fix a typo in the registry and every rule Tim ever wrote for that man
        # orphans SILENTLY: the rows stay, they simply stop matching anyone, and
        # nothing on screen says so. So rules hang off a permanent UUID instead.
        #
        # `kin_key` stays for readability in the DB, but `kin_uuid` is the identity.
        if await self._ensure_column("kin_rules", "kin_uuid", "TEXT"):
            import characters
            for c in characters.load_roster():
                await self.conn.execute(
                    "UPDATE kin_rules SET kin_uuid = ? WHERE kin_key = ? AND "
                    "(kin_uuid IS NULL OR kin_uuid = '')",
                    (characters.kin_uuid(c.key), c.key),
                )
            log.info("kin_rules: backfilled identities")
        # Which session produced a rule — the raw material the end-of-session
        # consolidation works from.
        await self._ensure_column("kin_rules", "session_id", "TEXT")
        # A rule that has been folded into a general one. Kept, not deleted: it's the
        # record of an experiment Tim ran on his own room.
        await self._ensure_column("kin_rules", "merged_into", "INTEGER")

        # mood_gradient went from a boolean toggle to a volume knob. An existing
        # row still holds "true"/"false", which would render as neither off, subtle
        # nor full — i.e. silently broken.
        await self.conn.execute(
            "UPDATE settings SET value = 'full' WHERE key = 'mood_gradient' AND value = 'true'"
        )
        await self.conn.execute(
            "UPDATE settings SET value = 'off' WHERE key = 'mood_gradient' AND value = 'false'"
        )
        for key, value in DEFAULT_SETTINGS.items():
            await self.conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        # One-time seed of canonical venue descriptions (from Tim's reference
        # photos). Only fills an unset/empty value — never clobbers later edits
        # made via the in-app describe flow.
        await self._seed_venue_descriptions()
        await self.conn.commit()

    async def _seed_venue_descriptions(self) -> None:
        import presence
        row = await self.fetch_one(
            "SELECT value FROM settings WHERE key = 'venue_descriptions'"
        )
        current = (row["value"] if row else "") or ""
        try:
            existing = json.loads(current) if current.strip() else {}
        except (ValueError, TypeError):
            existing = {}
        if existing:
            return  # user (or a prior seed) already populated it
        await self.conn.execute(
            "INSERT INTO settings (key, value) VALUES ('venue_descriptions', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(presence.SEED_VENUE_DESCRIPTIONS),),
        )

    async def _ensure_column(self, table: str, column: str, coltype: str) -> bool:
        """Add a column if it's missing. True when it was actually added, so
        callers can run a one-time backfill without repeating it every boot.
        """
        assert self.conn is not None
        async with self.conn.execute(f"PRAGMA table_info({table})") as cur:
            cols = [row[1] for row in await cur.fetchall()]
        if column in cols:
            return False
        await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        return True

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

    # ─── Mood arc ───
    async def add_mood_event(
        self, session_id: str, at_ms: int, mood: str, rating_key: Optional[str] = None
    ) -> None:
        """Record the film's mood AT A PLAYHEAD POSITION.

        `at_ms` is the playhead, never the wall clock: the arc is the shape of the
        FILM, so pausing for twenty minutes must not stretch the ribbon and
        rewinding must not run it backwards.

        Deliberately does NOT dedupe on unchanged mood — a long flat stretch of
        dread is information, and the ribbon needs the samples to draw it.
        """
        await self.execute(
            "INSERT INTO mood_events (session_id, rating_key, at_ms, mood) VALUES (?, ?, ?, ?)",
            (session_id, rating_key, int(at_ms), mood),
        )

    async def get_mood_events(
        self, session_id: str, rating_key: Optional[str] = None
    ) -> list[dict]:
        """The arc for ONE film. On a marathon night the session holds several, and
        without the rating-key filter the ribbon would splice them together."""
        if rating_key:
            rows = await self.fetch_all(
                "SELECT at_ms, mood FROM mood_events WHERE session_id = ? AND rating_key = ? "
                "ORDER BY at_ms ASC",
                (session_id, rating_key),
            )
        else:
            rows = await self.fetch_all(
                "SELECT at_ms, mood FROM mood_events WHERE session_id = ? ORDER BY at_ms ASC",
                (session_id,),
            )
        return [{"at_ms": r["at_ms"], "mood": r["mood"]} for r in rows]

    # ─── The room's decisions, and the rules Tim writes from them ───
    #
    # Every one of these judgments used to live for exactly as long as a WebSocket
    # frame. The app was deciding who spoke, who stayed quiet and why, all evening,
    # and keeping none of it.

    async def add_turn_decision(
        self,
        *,
        session_id: str,
        tim_message_id: Optional[int],
        facts_json: str,
        decision_json: str,
        rationale: str = "",
    ) -> int:
        cur = await self.execute(
            "INSERT INTO turn_decisions "
            "(session_id, tim_message_id, facts_json, decision_json, rationale) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, tim_message_id, facts_json, decision_json, rationale),
        )
        return int(cur.lastrowid)

    async def get_turn_decision(self, turn_id: int) -> Optional[dict]:
        row = await self.fetch_one("SELECT * FROM turn_decisions WHERE id = ?", (turn_id,))
        return row_to_dict(row) if row else None

    async def get_turn_by_message(self, tim_message_id: int) -> Optional[dict]:
        row = await self.fetch_one(
            "SELECT * FROM turn_decisions WHERE tim_message_id = ? ORDER BY id DESC LIMIT 1",
            (tim_message_id,),
        )
        return row_to_dict(row) if row else None

    async def mark_turn_reported(self, turn_id: int) -> None:
        await self.execute("UPDATE turn_decisions SET reported = 1 WHERE id = ?", (turn_id,))

    async def set_turn_feedback(
        self,
        *,
        turn_id: int,
        session_id: Optional[str],
        kin_key: str,
        spoke: bool,
        approved: bool,
        rules_in_force: Optional[list[int]] = None,
    ) -> int:
        """Bank a 👍 / 👎. Instant, cheap, and it must NEVER block on a model.

        The tap is the part Tim actually does; if it doesn't feel free he won't do it,
        and the whole feedback loop dies of friction.

        Re-tapping REPLACES. He'll change his mind, and two contradictory verdicts on one
        moment would poison the scoring silently.
        """
        import characters
        uid = characters.kin_uuid(kin_key)
        await self.execute(
            "INSERT INTO turn_feedback "
            "(turn_id, session_id, kin_uuid, kin_key, spoke, approved, rules_in_force) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(turn_id, kin_uuid) DO UPDATE SET "
            "  approved = excluded.approved, "
            "  spoke = excluded.spoke, "
            "  rules_in_force = excluded.rules_in_force, "
            "  created_at = CURRENT_TIMESTAMP",
            (turn_id, session_id, uid, kin_key, int(spoke), int(approved),
             json.dumps(rules_in_force or [])),
        )
        row = await self.fetch_one(
            "SELECT id FROM turn_feedback WHERE turn_id = ? AND kin_uuid = ?",
            (turn_id, uid),
        )
        return int(row["id"]) if row else 0

    async def link_feedback_rule(self, feedback_id: int, rule_id: int) -> None:
        await self.execute(
            "UPDATE turn_feedback SET became_rule = ? WHERE id = ?", (rule_id, feedback_id)
        )

    async def get_turn_feedback(self, turn_id: int) -> list[dict]:
        rows = await self.fetch_all(
            "SELECT * FROM turn_feedback WHERE turn_id = ?", (turn_id,)
        )
        return rows_to_list(rows)

    async def score_rules(self, session_id: str) -> list[dict]:
        """Which of Tim's rules EARNED THEIR KEEP tonight, and which kept misfiring?

        A tap is a verdict on the rules that were actually in force at that moment:

            the rule fired, and he approved the outcome   -> it earned its keep
            the rule fired, and he rejected the outcome   -> it was WRONG there

        That's strong, direct evidence — not the weak correlation-hunting we threw out.
        A rule with a pile of refutations and almost no confirmations is a rule that is
        quietly making his room worse, and he'd never spot it by hand.
        """
        rows = await self.fetch_all(
            "SELECT tf.approved, tf.spoke, tf.rules_in_force "
            "FROM turn_feedback tf WHERE tf.session_id = ?",
            (session_id,),
        )
        tally: dict[int, dict] = {}
        for r in rows:
            try:
                ids = json.loads(r["rules_in_force"] or "[]")
            except (ValueError, TypeError):
                continue
            for rid in ids:
                t = tally.setdefault(int(rid), {"rule_id": int(rid), "kept": 0, "wrong": 0})
                # A rule was IN FORCE. Did the outcome it produced satisfy him?
                if r["approved"]:
                    t["kept"] += 1
                else:
                    t["wrong"] += 1
        out = []
        for rid, t in tally.items():
            rule = await self.fetch_one("SELECT * FROM kin_rules WHERE id = ?", (rid,))
            if not rule:
                continue
            t["rule_text"] = rule["rule_text"]
            t["kin_key"] = rule["kin_key"]
            out.append(t)
        return out

    async def get_kin_rules(self, kin_key: str) -> list[dict]:
        """A person's live rules. Looked up by IDENTITY, not by name.

        The key is a handle and handles change. Match on the UUID and a rename
        can never orphan a rule — which, if it happened, would be invisible: the rows
        would still be there, they'd just quietly stop applying to anyone.
        """
        import characters
        uid = characters.kin_uuid(kin_key)
        rows = await self.fetch_all(
            "SELECT * FROM kin_rules WHERE kin_uuid = ? AND active = 1 "
            "ORDER BY created_at ASC",
            (uid,),
        )
        out = []
        for r in rows:
            d = row_to_dict(r)
            try:
                d["when"] = json.loads(d.get("when_json") or "[]")
            except (ValueError, TypeError):
                # A rule whose scope we cannot read must NOT quietly become universal.
                # Park it with an impossible condition rather than let it fire everywhere.
                log.warning("rule %s has unreadable conditions — it will not fire", d.get("id"))
                d["when"] = [{"fact": "__broken__", "op": "is", "value": []}]
            out.append(d)
        return out

    async def add_kin_rule(
        self,
        *,
        kin_key: str,
        rule_text: str,
        verdict: str,
        when: list,
        rule_why: str = "",
        evidence: str = "",
        from_turn_id: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> int:
        import characters
        cur = await self.execute(
            "INSERT INTO kin_rules "
            "(kin_key, kin_uuid, rule_text, rule_why, evidence, verdict, when_json, "
            " from_turn_id, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kin_key, characters.kin_uuid(kin_key), rule_text, rule_why, evidence,
             verdict, json.dumps(when or []), from_turn_id, session_id),
        )
        return int(cur.lastrowid)

    async def get_session_rules(self, session_id: str) -> list[dict]:
        """The rules Tim wrote DURING one session — the raw material for consolidation."""
        rows = await self.fetch_all(
            "SELECT * FROM kin_rules WHERE session_id = ? AND active = 1 "
            "AND merged_into IS NULL ORDER BY kin_key, created_at",
            (session_id,),
        )
        out = []
        for r in rows:
            d = row_to_dict(r)
            try:
                d["when"] = json.loads(d.get("when_json") or "[]")
            except (ValueError, TypeError):
                d["when"] = []
            out.append(d)
        return out

    async def merge_rules_into(self, rule_ids: list[int], new_id: int) -> None:
        """Fold the originals into a consolidated rule.

        Deactivated, never deleted. Tim approved each of them, and the record of what
        he taught the room — and when — is worth more than the row it costs.
        """
        if not rule_ids:
            return
        qs = ",".join("?" for _ in rule_ids)
        await self.execute(
            f"UPDATE kin_rules SET active = 0, merged_into = ? WHERE id IN ({qs})",
            (new_id, *rule_ids),
        )

    async def bump_rule_hits(self, rule_ids: list[int]) -> None:
        """A rule that has never once fired is either wrong or scoped into oblivion.

        Tim should be told that, rather than left wondering why nothing changed.
        """
        if not rule_ids:
            return
        qs = ",".join("?" for _ in rule_ids)
        await self.execute(
            f"UPDATE kin_rules SET hits = hits + 1 WHERE id IN ({qs})",
            tuple(rule_ids),
        )

    async def deactivate_rule(self, rule_id: int) -> None:
        await self.execute("UPDATE kin_rules SET active = 0 WHERE id = ?", (rule_id,))

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
        character_key: Optional[str] = None,
    ) -> int:
        """Insert a message.

        `sender` is the lane ('tim' | 'eli' | 'system' | ...); `character_key`
        is WHICH kin, and is what a multi-kin transcript is actually keyed on.
        Every companion reply should carry one.
        """
        cursor = await self.execute(
            """INSERT INTO messages
            (session_id, sender, content, emote_text, spoken_text, scene_context,
             mood, stoned_level_tim, stoned_level_eli, trivia, frame_path, latency_ms,
             character_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id, sender, content, emote_text, spoken_text, scene_context,
                mood, stoned_level_tim, stoned_level_eli, trivia, frame_path, latency_ms,
                character_key,
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
        cast: Optional[list[dict[str, Any]]] = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self.execute(
            """INSERT INTO movies
            (session_id, plex_rating_key, title, year, director, runtime_minutes,
             briefing, started_at, cast_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, plex_rating_key, title, year, director, runtime_minutes,
             briefing, now, json.dumps(cast) if cast else None),
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
