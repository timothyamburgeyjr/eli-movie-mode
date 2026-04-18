"""Session history assembly for Gemini context.

Kindroid does NOT receive raw chat history (it has its own long-term memory).
Gemini DOES receive the full session history so it can weave relevant past
moments into a first-person callback narrative for Tim's next Kindroid message.
"""
from typing import Optional

from database import db


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
        """SELECT sender, content, emote_text, spoken_text, mood, timestamp
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
                lines.append("Eli replied: " + " ".join(parts))

    if max_exchanges and max_exchanges > 0:
        # Keep the last N Tim turns worth of context (plus the Eli replies).
        tim_indices = [i for i, line in enumerate(lines) if line.startswith("Tim ")]
        if len(tim_indices) > max_exchanges:
            cutoff = tim_indices[-max_exchanges]
            lines = lines[cutoff:]

    return "\n".join(lines)


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
