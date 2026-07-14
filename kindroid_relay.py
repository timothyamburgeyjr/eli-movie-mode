"""Kindroid API relay — POST to /v1/send-message, retry on transients, parse
the `_(*...*)_` emote convention out of Eli's replies.

Docs (such as they are) confirm the endpoint returns a single JSON blob, not a
stream, so callers see nothing until the full reply arrives. Budget 8-15s
typical latency, with a long tail to 20-30s; set timeouts accordingly.
"""
import asyncio
import logging
import re
from time import perf_counter
from typing import Any, Optional

import httpx

from config import settings

log = logging.getLogger(__name__)

RETRY_BACKOFFS = (2.0, 4.0, 8.0)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
DEFAULT_TIMEOUT = 180.0

# Kindroid wraps emote actions in _(*...*)_ — the underscores are italic
# markdown and the parens-asterisks delimit the action. Each paragraph may be
# wrapped separately, so we match non-greedily across lines.
EMOTE_PATTERN = re.compile(r"_\(\*(.+?)\*\)_", re.DOTALL)
# Bare `*text*` that isn't already inside a _(*...*)_ block. Same shortcut
# Tim can type on mobile; Eli sometimes uses it inconsistently too.
_BARE_STAR_PATTERN = re.compile(r"\*([^*\n]+?)\*")
_EXISTING_EMOTE_SPLIT = re.compile(r"(_\(\*[\s\S]+?\*\)_)")

# Content that *looks* like an emote action:
#   - Starts with a first-person pronoun (I, my, we, our)
#   - OR first word is a verb form: ≥3 chars ending in -s, -es, -ing, or -ed
# Non-verb words that match the suffix rule are blacklisted.
_EMOTE_PRONOUN_START = re.compile(
    r"^(?:I\b|I['\u2019]|my\b|we\b|our\b)",
    re.IGNORECASE,
)
_VERB_SUFFIX_PATTERN = re.compile(r"^([A-Za-z]{3,})(s|es|ing|ed)\b")
_NON_VERB_S_ENDINGS = {
    "this", "his", "its", "us", "is", "yes", "was", "has",
    "as", "hers", "theirs",
}


def _looks_like_emote(content: str) -> bool:
    stripped = (content or "").strip()
    if not stripped:
        return False
    if _EMOTE_PRONOUN_START.match(stripped):
        return True
    first_word = stripped.split(None, 1)[0].strip(",.!?;:").lower()
    if first_word in _NON_VERB_S_ENDINGS:
        return False
    return bool(_VERB_SUFFIX_PATTERN.match(first_word))


def expand_star_shortcuts(text: str) -> str:
    """Convert bare `*text*` to `_(*text*)_`, but ONLY when the content
    reads like a first-person action (pronoun start or verb form). This
    preserves `*Ex Machina*`, `*South Park*`, and other inline italic titles
    as-is while still catching real emotes like `*I squeeze your hand*` or
    `*turns to you and whispers*`. Existing `_(*...*)_` blocks are untouched.
    """
    if not text:
        return text
    parts = _EXISTING_EMOTE_SPLIT.split(text)
    for i, part in enumerate(parts):
        if i % 2 == 1:
            continue  # already-wrapped emote, leave alone
        parts[i] = _BARE_STAR_PATTERN.sub(
            lambda m: f"_(*{m.group(1)}*)_" if _looks_like_emote(m.group(1)) else m.group(0),
            part,
        )
    return "".join(parts)


class KindroidError(Exception):
    """Any non-transient failure talking to Kindroid."""


# NOTE — a "first-person guard" briefly lived here, rewriting Tim's reaction
# narration into the third person on the theory that Kindroid renders an emote as
# the RECIPIENT's action ("I snort" reaching Bobby as *Bobby* snorting).
#
# THAT IS WRONG. Kindroid renders the OUTGOING message as the SENDER's, and the
# sender is Tim. `_(* I snort *)_` means TIM snorts, to every recipient. First
# person is, and always was, the convention for Tim's own actions:
#
#     HANDOFF-multi-kin-room.md:71 — _(* I hold out the bag and Bobby reads the
#                                      label first… *)_
#     stoned_tracker.py            — "Tim's POV… first person, present tense"
#     REACTION_SYSTEM_PROMPT       — "FIRST PERSON (Tim's voice)"
#
# The guard mangled a working ingestion emote into
#     Tim's reaction, in his own words: "I hand Lilly a gummy…"
# and shipped it to the room before the logs caught it.
#
# The real landmines are DIFFERENT and are handled elsewhere: SECOND person ("you")
# broadcast to a room, and RAW TEXT aimed at one kin being overheard by another.
# See coordinator._PACKAGE_SYSTEM. Do not "fix" first person again.


_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+")


def _wrap_paragraphs_as_emotes(text: str) -> list[str]:
    """Split text on blank lines and wrap each non-empty paragraph in its
    own `_(*...*)_` block. Kindroid's emote renderer doesn't handle
    multi-paragraph blocks — the markup leaks through as raw text. One
    block per paragraph avoids that.
    """
    if not text or not text.strip():
        return []
    paragraphs = _PARAGRAPH_SPLIT.split(text.strip())
    return [f"_(*{p.strip()}*)_" for p in paragraphs if p.strip()]


# Persistent formatting nudge prepended to every Kindroid payload. Tim
# tested this exact wording and it locks Eli's emote drift down — keep
# verbatim. The nested marker references inside are intentional and Eli
# parses them correctly; do NOT rewrite.
_FORMAT_DIRECTIVE_EMOTE = (
    "_(* DIRECTIVE: All Emotes have to go inside the _(* TEXT *)_ notation *)_"
)


def build_payload(
    *,
    scene_narration: str = "",
    history_narrative: str = "",
    stoned_narration: str = "",
    reaction_narration: str = "",
    presence_narration: str = "",
    quiet_film: str = "",
    quiet_room: str = "",
    typed_dialogue: str = "",
) -> str:
    """Assemble the Kindroid message body per our emote convention.

    Order:
      1. Format-directive emote (anti-drift reminder).
      2. Stoned-state directive emote (telling the kin how they're feeling).
      3. Presence emote (where they are + who else is in the room) — early so
         it consistently gates the kin's behavior/intimacy each turn.
      4. THE TWO QUIET-STRETCH EMOTES — what they've been sitting with since they
         last spoke. TWO separate blocks, never merged:
             quiet_film — what's happened ON SCREEN. The bridge from the last thing
                          they remember to now; without it they'd read the current
                          scene with a hole where the middle of the film should be.
             quiet_room — what's happened BETWEEN THE PEOPLE, and whether anyone said
                          their name. This one goes SECOND, closest to Tim's words,
                          because it's the one that most often needs answering.
         Both BEFORE the scene: they are the run-up to this moment, not a footnote.
      5. Scene / history / reaction emotes (one block per paragraph).
      6. Typed dialogue, plain.
    """
    lines: list[str] = [_FORMAT_DIRECTIVE_EMOTE]
    # Stoned directive (the kin's state) comes second so the formatting
    # reminder is the very first thing, and the altered-state framing is the
    # second thing they see — both before any scene/reaction content.
    lines.extend(_wrap_paragraphs_as_emotes(stoned_narration or ""))
    lines.extend(_wrap_paragraphs_as_emotes(presence_narration or ""))
    lines.extend(_wrap_paragraphs_as_emotes(quiet_film or ""))
    lines.extend(_wrap_paragraphs_as_emotes(quiet_room or ""))
    for block in (scene_narration, history_narrative, reaction_narration):
        lines.extend(_wrap_paragraphs_as_emotes(block or ""))
    body = "\n".join(lines)
    dialogue = (typed_dialogue or "").strip()
    if dialogue:
        body = f"{body}\n\n{dialogue}" if body else dialogue
    return body


async def send_message(
    message: str,
    *,
    ai_id: Optional[str] = None,
    image_urls: Optional[list[str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """POST `message` to Kindroid, retry on transients, return parsed reply.

    Returns:
        {
            "raw": str,               # original unparsed reply
            "emote_text": str,        # all _(*...*)_ blocks joined, emotes only
            "spoken_text": str,       # everything outside emote blocks, cleaned
            "latency_ms": int,
            "status": int,            # HTTP status of the successful call
        }
    """
    # Per-message ai_id (the selected family member) overrides the configured
    # default, so we can route to any Kindroid character on the account.
    effective_ai_id = ai_id or settings.kindroid_ai_id
    if not settings.kindroid_api_key or not effective_ai_id:
        raise KindroidError("Kindroid credentials not configured")
    if not message or not message.strip():
        raise KindroidError("Empty message")
    if len(message) > settings.kindroid_char_limit:
        raise KindroidError(
            f"Message too long ({len(message)} > {settings.kindroid_char_limit})"
        )

    payload: dict[str, Any] = {
        "ai_id": effective_ai_id,
        "message": message,
        # CRITICAL: Kindroid's API now defaults to streaming (SSE). Without
        # this flag we read only the first keepalive byte before the stream
        # finishes and get a single space as the "reply." Always force a
        # single buffered response.
        "stream": False,
    }
    if image_urls:
        payload["image_urls"] = image_urls

    headers = {
        "Authorization": f"Bearer {settings.kindroid_api_key}",
        "Content-Type": "application/json",
    }

    start = perf_counter()
    last_err: Optional[Exception] = None
    last_status: Optional[int] = None

    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(len(RETRY_BACKOFFS)):
            try:
                response = await client.post(
                    settings.kindroid_url, json=payload, headers=headers
                )
            except httpx.TimeoutException as e:
                last_err = e
                if attempt + 1 < len(RETRY_BACKOFFS):
                    delay = RETRY_BACKOFFS[attempt]
                    log.warning(
                        "kindroid timeout, retrying in %.0fs (attempt %d/%d)",
                        delay, attempt + 1, len(RETRY_BACKOFFS),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise KindroidError(f"kindroid timed out after {timeout}s") from e
            except httpx.HTTPError as e:
                last_err = e
                if attempt + 1 < len(RETRY_BACKOFFS):
                    await asyncio.sleep(RETRY_BACKOFFS[attempt])
                    continue
                raise KindroidError(f"kindroid network error: {e}") from e

            last_status = response.status_code
            if response.status_code in RETRYABLE_STATUS and attempt + 1 < len(RETRY_BACKOFFS):
                delay = RETRY_BACKOFFS[attempt]
                log.warning(
                    "kindroid %s, retrying in %.0fs (attempt %d/%d)",
                    response.status_code, delay, attempt + 1, len(RETRY_BACKOFFS),
                )
                await asyncio.sleep(delay)
                continue
            if response.status_code >= 400:
                raise KindroidError(
                    f"kindroid {response.status_code}: {response.text[:300]}"
                )

            # Success — Kindroid returns the reply as plain text in the body.
            # Some proxies or future versions may wrap it in JSON, so we try
            # JSON first and fall back to raw text.
            raw_body = response.text or ""
            log.info(
                "kindroid HTTP %s body_chars=%d body_preview=%r ct=%s",
                response.status_code,
                len(raw_body),
                raw_body[:300],
                response.headers.get("content-type", ""),
            )
            api_payload: Any = None
            raw = raw_body
            try:
                data = response.json()
            except ValueError:
                data = None
            if isinstance(data, dict):
                api_payload = data
                log.info("kindroid JSON keys=%s", list(data.keys()))
                extracted = _extract_reply_text(data)
                if extracted:
                    raw = extracted
                else:
                    log.warning(
                        "kindroid response JSON had no usable reply key; data=%r",
                        {k: (v if not isinstance(v, str) or len(v) < 200 else v[:200] + "…")
                         for k, v in data.items()},
                    )

            emote_text, spoken_text, segments = parse_reply(raw)
            return {
                "raw": raw,
                "emote_text": emote_text,
                "spoken_text": spoken_text,
                "segments": segments,
                "latency_ms": int((perf_counter() - start) * 1000),
                "status": response.status_code,
                "api_payload": api_payload,
            }

    raise KindroidError(f"kindroid exhausted retries (last status {last_status}, {last_err})")


def _extract_reply_text(data: dict[str, Any]) -> str:
    """Handle both documented and observed response shapes."""
    if not isinstance(data, dict):
        return ""
    # Official docs expose the reply under assorted keys across versions.
    for key in ("ai_response", "message", "response", "text", "reply"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def parse_reply(raw: str) -> tuple[str, str, list[dict[str, str]]]:
    """Parse a Kindroid reply into ordered segments.

    Returns:
        (emote_text_concat, spoken_text_concat, segments)

    - `segments` preserves original order: alternating {type:"emote"|"spoken", text:...}
    - `emote_text_concat` / `spoken_text_concat` are kept for backwards compat
      with any code that wants the concatenated forms.

    Any bare `*text*` (multi-word) in the raw reply is expanded to
    `_(*text*)_` first — Eli sometimes uses the mobile-style shortcut, and
    we want segments parsed consistently with Tim's outgoing messages.
    """
    if not raw:
        return "", "", []
    raw = expand_star_shortcuts(raw)

    segments: list[dict[str, str]] = []
    pos = 0
    for match in EMOTE_PATTERN.finditer(raw):
        if match.start() > pos:
            leading = raw[pos : match.start()].strip()
            if leading:
                segments.append({"type": "spoken", "text": leading})
        emote = match.group(1).strip()
        if emote:
            segments.append({"type": "emote", "text": emote})
        pos = match.end()
    if pos < len(raw):
        tail = raw[pos:].strip()
        if tail:
            segments.append({"type": "spoken", "text": tail})

    emote_text = "\n\n".join(s["text"] for s in segments if s["type"] == "emote")
    spoken_text = "\n\n".join(s["text"] for s in segments if s["type"] == "spoken")
    return emote_text, spoken_text, segments
