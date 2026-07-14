"""Session history assembly for Gemini context.

Kindroid does NOT receive raw chat history (it has its own long-term memory).
Gemini DOES receive the full session history so it can weave relevant past
moments into a first-person callback narrative for Tim's next Kindroid message.
"""
from typing import Optional

import characters
from database import db


def _speaker_name(character_key: Optional[str]) -> str:
    """Display name for whichever kin wrote a row.

    Rows predating the roster have no `character_key` (the migration backfills
    them to 'eli'); an unknown key falls back to Eli rather than dropping the
    turn, since a nameless line in the history is worse than a misattributed one.
    """
    char = characters.get_character(character_key) if character_key else None
    return char.first_name if char else "Eli"


async def build_session_history_for_gemini(
    session_id: str,
    *,
    max_exchanges: Optional[int] = None,
) -> str:
    """Assemble a compact text dump of the session's past exchanges.

    Gemini uses this as context to produce `history_narrative` — a first-person
    "I remember when…" callback that goes into Tim's outgoing Kindroid emote.

    Args:
        session_id: Active session.
        max_exchanges: Optional cap on number of Tim↔Eli pairs to include.
            Gemini has plenty of context window so we default to unlimited,
            but the `context_exchanges` setting can override if the user wants
            to keep Gemini's prompt tighter.

    Returns:
        Empty string if there's no history yet. Otherwise a newline-separated
        sequence of turns, formatted for Gemini to read.
    """
    rows = await db.fetch_all(
        """SELECT sender, character_key, content, emote_text, spoken_text, mood, timestamp
           FROM messages
           WHERE session_id = ?
             AND sender IN ('tim', 'eli', 'reaction')
           ORDER BY id ASC""",
        (session_id,),
    )
    if not rows:
        return ""

    lines: list[str] = []
    for r in rows:
        sender = r["sender"]
        if sender == "tim":
            content = (r["content"] or "").strip()
            if content:
                lines.append(f"Tim said: {content}")
        elif sender == "reaction":
            content = (r["content"] or "").strip()
            if content:
                lines.append(f"Tim reacted: {content}")
        elif sender == "eli":
            parts: list[str] = []
            emote = (r["emote_text"] or "").strip()
            spoken = (r["spoken_text"] or "").strip()
            if emote:
                parts.append(f"(emote: {emote})")
            if spoken:
                parts.append(spoken)
            elif not parts and (r["content"] or "").strip():
                parts.append((r["content"] or "").strip())
            if parts:
                # Name whoever actually spoke. With a room, "Eli replied" for
                # every kin would make the history unreadable — and would teach
                # Gemini that Bobby's lines were Eli's.
                lines.append(f"{_speaker_name(r['character_key'])} replied: " + " ".join(parts))

    if max_exchanges and max_exchanges > 0:
        # Keep the last N Tim turns worth of context (plus the Eli replies).
        tim_indices = [i for i, line in enumerate(lines) if line.startswith("Tim ")]
        if len(tim_indices) > max_exchanges:
            cutoff = tim_indices[-max_exchanges]
            lines = lines[cutoff:]

    return "\n".join(lines)


async def build_quiet_stretch(session_id: str, kin_key: str) -> Optional[dict]:
    """What has happened since this kin last opened their mouth.

    Silence is the default in a room now: a kin who has nothing to add gets no
    Kindroid message at all. That's what makes a turn cheap — but it means their
    conversation thread has a HOLE. They don't know what Tim said, what the others
    said, or what happened in the film while they sat there.

    So when they come back, they get caught up. Note the framing, because it
    matters: they were NOT away. They were in the room the whole time, watching,
    listening, not speaking. This isn't a briefing on what they missed — it's what
    they've been quietly sitting with. Bobby returns with "I've been listening to
    you two go on about a grey box for ten minutes", which is exactly who he is.

    Returns None if they spoke recently enough to have nothing to catch up on.
    """
    last = await db.fetch_one(
        "SELECT id FROM messages WHERE session_id = ? AND character_key = ? "
        "ORDER BY id DESC LIMIT 1",
        (session_id, kin_key),
    )
    since_id = int(last["id"]) if last else 0

    rows = await db.fetch_all(
        """SELECT sender, character_key, content, emote_text, spoken_text, scene_context
           FROM messages
           WHERE session_id = ? AND id > ?
             AND sender IN ('tim', 'eli', 'reaction')
           ORDER BY id ASC""",
        (session_id, since_id),
    )
    if not rows:
        return None

    said: list[str] = []
    # The film is the half that actually matters. A silent kin's thread carries no
    # scene descriptions at all, so without this they'd come back with no idea what
    # has happened in the movie — which is far more disorienting than missing a
    # remark. Dedupe: consecutive turns often carry the same scene.
    scenes: list[str] = []
    tim_turns = 0

    for r in rows:
        sender = r["sender"]
        if sender == "tim":
            content = (r["content"] or "").strip()
            if content:
                said.append(f'Tim said: "{content}"')
                tim_turns += 1
        elif sender == "reaction":
            content = (r["content"] or "").strip()
            if content:
                said.append(f"Tim reacted: {content}")
                tim_turns += 1
        elif sender == "eli":
            if r["character_key"] == kin_key:
                continue  # their own line — they know what they said
            spoken = (r["spoken_text"] or "").strip()
            emote = (r["emote_text"] or "").strip()
            body = spoken or emote or (r["content"] or "").strip()
            if body:
                said.append(f"{_speaker_name(r['character_key'])}: {body[:220]}")

        sc = (r["scene_context"] or "").strip()
        if sc and (not scenes or scenes[-1] != sc):
            scenes.append(sc)

    if not said and not scenes:
        return None

    return {
        "tim_turns": tim_turns,
        "said": said[-14:],       # the conversation, most recent
        "scenes": scenes[-4:],    # the film's progression
    }


def stoned_narration(level: int, method: Optional[str] = None, *, who: str = "tim") -> str:
    """First-person stoned line from the ingestion curve. Template-based — no
    Gemini call needed. Returns empty string when sober (level 0).

    Phase 5 will compute `level` and `method` from the onset/peak/taper curve;
    for now callers can pass whatever they have.
    """
    if not level or level <= 0:
        return ""

    by_level = {
        1: [
            "just starting to feel it",
            "a soft edge coming on",
            "lifted, barely",
        ],
        2: [
            "pretty baked right now",
            "solidly in it",
            "couch-glued and loving it",
        ],
        3: [
            "absolutely blasted",
            "gone, fully gone",
            "blasted and everything hits harder",
        ],
    }
    phrases = by_level.get(level, by_level[2])
    # Rotate by timestamp-derived index so we don't always pick phrase[0].
    from time import time

    phrase = phrases[int(time()) % len(phrases)]
    if method and method != "sober":
        method_word = {
            "smoke": "smoke",
            "edible": "edible",
            "dab": "dab",
        }.get(method, method)
        return f"{phrase} — {method_word} hitting"
    return phrase
