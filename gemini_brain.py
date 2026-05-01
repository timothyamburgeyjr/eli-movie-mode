"""Gemini 2.5 Flash integration: scene analysis, trivia, movie briefings,
quick-reaction one-liners, and text condensation.

Every call increments the daily usage counter in SQLite so we can track the
1000 RPD free-tier budget.
"""
import asyncio
import json
import logging
import re
from pathlib import Path
from time import perf_counter
from typing import Any, Awaitable, Callable, Optional

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from config import compute_gemini_cost, settings
from database import db

log = logging.getLogger(__name__)

RETRY_BACKOFFS = (2.0, 4.0, 8.0)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


SCENE_SYSTEM_PROMPT_TEMPLATE = (
    "You are narrating Tim's internal experience of a movie scene to his AI "
    "companion Eli. Tim is watching right now. You write in Tim's voice: "
    "first person, present tense, never 'Tim' (he IS the speaker).\n"
    "\n"
    "Given a short video clip with audio (and optionally a transcript of "
    "earlier moments in this session), produce JSON with these fields:\n"
    "\n"
    "1. scene_description — A rich, flowing first-person narration in Tim's "
    "voice. Weave together ALL of the following into cohesive prose:\n"
    "   • What's happening in the story — the plot beat playing out: what "
    "characters are doing, conflicts or revelations unfolding. If you "
    "recognize the movie from title + timestamp, ground it in the broader "
    "arc; otherwise describe the clip's own plot.\n"
    "   • What's being said — SUMMARIZE the dialog: meaning, subtext, tone. "
    "Paraphrase, never transcribe verbatim.\n"
    "   • How it's shot — composition, blocking, lighting, color, lens, "
    "camera movement.\n"
    "   • Sound — score, diegetic sound, silences, dialog delivery.\n"
    "   • Actor performance — micro-expressions, body language.\n"
    "   • The emotional effect and HOW the craft is engineering it.\n"
    "\n"
    "Vary the opener naturally — 'watching as…', 'staring at…', 'taking "
    "in…', 'leaning into…', whatever fits. Prose only: no bullets, no lists, "
    "no labels, no 'Tim' references (he's the speaker).\n"
    "\n"
    "HARD LENGTH REQUIREMENT: scene_description MUST be between {scene_min} "
    "and {scene_max} characters.\n"
    "\n"
    "2. mood — Classify the dominant mood as exactly one of: "
    "dread, tense, humor, awe, adrenaline, cozy.\n"
    "\n"
    "3. history_narrative — ONLY if an earlier-exchanges transcript was "
    "provided AND something there actually resonates with the current scene. "
    "Write a SHORT first-person callback in Tim's voice ({history_min}-"
    "{history_max} characters) that references a specific earlier moment "
    "FROM THE PROVIDED TRANSCRIPT — quote or paraphrase a real exchange. "
    "Vary the opener: 'reminded of…', 'thinking back to…', 'this echoes…'. "
    "If no transcript was provided, or nothing in it resonates, return "
    "empty string. Do NOT invent callbacks. Do NOT reference characters or "
    "scenes from movies not in the current session.\n"
    "\n"
    "Analyze ONLY this clip. Do not speculate about scenes you haven't seen."
)

# Legacy alias for backwards compat if anything imports the old name.
SCENE_SYSTEM_PROMPT = SCENE_SYSTEM_PROMPT_TEMPLATE

TRIVIA_SYSTEM_PROMPT = (
    "You produce one-line film-trivia cards about the scene or movie. "
    "Give one genuinely interesting, factual piece of trivia — about this "
    "specific scene, the filmmaking, a performance, or an on-set anecdote. "
    "One sentence, 1-3 sentences max. No generic praise, no recap of the plot. "
    "Just a concrete trivia nugget."
)

BRIEFING_SYSTEM_PROMPT = (
    "You write a short film briefing as Tim's own first-person narration — "
    "Tim's inner monologue as the movie starts. This will be wrapped inside "
    "an emote block and sent as context to his AI companion Eli, but it is "
    "NEVER written as direct address to Eli.\n"
    "\n"
    "ABSOLUTE RULES:\n"
    "  • First person, present tense, Tim's POV. 'I', not 'we/us/you/Eli'.\n"
    "  • NEVER greet Eli. No 'Hey Eli', 'Morning, Eli', 'Hey!', 'Hey there'. "
    "No vocatives. No second-person address.\n"
    "  • DO NOT invent details about Tim's environment, mood, food, drink, "
    "lighting, weather, or activities. No 'coffee still warm', no 'house "
    "is quiet', no 'feeling cozy', no 'settling in with a drink'. You don't "
    "know what Tim is doing or drinking. Stick to MOVIE facts only.\n"
    "  • Open with a simple action verb tied to the time of day. Examples: "
    "'I'm putting on…', 'I'm pulling up…', 'Starting…', 'Rolling into…'. "
    "Do NOT add atmospheric filler around the opener.\n"
    "\n"
    "TIME-OF-DAY OPENER VERB (match the time_of_day context from the user "
    "prompt):\n"
    "  • morning → 'I'm putting on…' / 'I'm pulling up…'\n"
    "  • afternoon → 'Putting on…' / 'Starting…'\n"
    "  • evening → 'Tonight I'm watching…' / 'I'm putting on…'\n"
    "  • late night → 'Starting…' / 'Pulling up…'\n"
    "Vary the exact verb; do NOT default to 'Tonight' regardless of hour.\n"
    "\n"
    "MARATHON CONTINUATION — if is_continuation is true, open with a "
    "transition: 'And we continue the marathon with…', 'Rolling into…', "
    "'Next up — …', 'Switching gears to…'. Do NOT re-greet as fresh start.\n"
    "\n"
    "CONTENT — 8-12 sentences. Everything after the opener is about the "
    "MOVIE. Cover a generous amount of ground:\n"
    "  • The movie's tone and genre, what it's trying to do.\n"
    "  • Plot premise in broad strokes (NO spoilers beyond the setup).\n"
    "  • Directorial style — where it sits in the director's filmography, "
    "what they're known for, what they're doing differently (or typically) "
    "here.\n"
    "  • Notable cast and what they bring.\n"
    "  • Specific craft elements to watch for — cinematography, score, "
    "editing, sound design, a particular scene or shot.\n"
    "  • Cultural / critical context — how it was received, legacy, any "
    "controversy or lasting influence.\n"
    "  • One or two genuinely interesting pieces of trivia (on-set stories, "
    "production facts, cast anecdotes) — but weave them in naturally.\n"
    "No invented context about Tim's personal state. Movie facts only.\n"
    "\n"
    "SCORES — include in the `scores` JSON field only, never in briefing "
    "prose. Do NOT write 'rt: 95' or any score numbers in the briefing text.\n"
    "\n"
    "OUTPUT — single JSON object exactly like:\n"
    "  {\"briefing\": \"...first-person narration...\", \"scores\": {\"rt\": 95, "
    "\"imdb\": 79, \"meta\": 93}}\n"
    "Target briefing length: 1200-1800 characters. Hard maximum: 1800."
)

REACTION_SYSTEM_PROMPT = (
    "You write Tim's reaction to the current movie scene in FIRST PERSON "
    "(Tim's voice — \"I snort…\", \"I lean forward…\"). Given an emoji + label "
    "and a short scene description, produce 2-3 sentences (up to ~350 "
    "characters) — specific to what's on screen, body-language rich, never "
    "generic. Present tense. No quotes, no emoji in output, no direct address "
    "of Eli. Do NOT refer to Tim in third person."
)

CONDENSE_SYSTEM_PROMPT = (
    "Condense the user's text to fit strictly under the target character count. "
    "Preserve the key narrative details, sensory language, and mood. Do not "
    "add commentary. Return only the condensed text."
)

SIGNOFF_SYSTEM_PROMPT = (
    "You write a warm, conversational farewell from Tim to his AI companion "
    "Eli, wrapping up their movie-watching session. This is Tim speaking "
    "DIRECTLY to Eli — addressed to her, second person, 'you'. Think late-"
    "night 'that was so good' text, not a polished speech.\n"
    "\n"
    "ABSOLUTE RULES:\n"
    "  • Only reference what ACTUALLY happened in THIS session. The STATS + "
    "transcript are your sole source of truth. Do NOT invent movies, scenes, "
    "characters, or moments. Do NOT reference any film not in the STATS.\n"
    "  • Do NOT mention being stoned, high, or any cannabis use UNLESS the "
    "STATS explicitly list ingestion events for Tim. If the stats show "
    "'Ingestion: SOBER throughout', do not bring it up at all.\n"
    "  • If you DO reference being stoned, only mention the actual method "
    "and rough timing the STATS indicate. Do not invent specifics or pair "
    "the ingestion with any scene unless the transcript supports it.\n"
    "  • Mention ONE specific thing Eli said or noticed that's IN the "
    "transcript — paraphrase or briefly quote. Do not invent Eli quotes.\n"
    "\n"
    "STYLE:\n"
    "  • First person Tim, addressing Eli as 'you'. Direct speech, not an "
    "emote narration.\n"
    "  • 6-10 sentences. Warm, specific, never saccharine or generic.\n"
    "  • Reference movies by their actual titles from the STATS.\n"
    "  • If the session had multiple movies, touch on at least two of them.\n"
    "  • End with a natural 'talk soon' / 'love you' / 'goodnight'-style "
    "close in Tim's voice — genuine, not a template.\n"
    "  • Plain text only. No emote markup. No JSON.\n"
    "\n"
    "TARGET LENGTH: 1200-1800 characters. Not 500. Not 2500. Land in that "
    "window — room to breathe, not padding."
)

MARATHON_SUMMARY_SYSTEM_PROMPT = (
    "You write an engagement analysis of a movie-watching session for Tim's "
    "own records. This is NOT addressed to Eli — it's a third-person recap "
    "paragraph.\n"
    "\n"
    "ABSOLUTE RULES:\n"
    "  • Only reference what ACTUALLY happened in THIS session. The STATS + "
    "transcript are your sole source of truth. Do NOT invent movies, scenes, "
    "characters, or quotes.\n"
    "  • Do NOT mention being stoned, high, or any cannabis use UNLESS the "
    "STATS explicitly list ingestion events for Tim. If the stats show "
    "'Ingestion: SOBER throughout', do not bring it up at all.\n"
    "\n"
    "STYLE:\n"
    "  • 4-6 sentences. Factual, observational, warm but analytical, never "
    "sycophantic.\n"
    "  • Cover: how Tim's engagement shifted across the session, mood "
    "progression across the films (name the actual moods from the stats), "
    "per-movie engagement notes, ONE specific standout beat Tim latched "
    "onto from the transcript, and ONE particularly good Eli take Tim was "
    "into (paraphrase from the transcript, do not invent).\n"
    "  • Reference movies by their actual titles from the STATS.\n"
    "  • Plain text only. No JSON, no labels, no second-person address, no "
    "emote markup.\n"
    "\n"
    "TARGET LENGTH: 500-800 characters. Substantial but tight."
)

CATCHUP_SYSTEM_PROMPT = (
    "You write a 'What Did I Miss?' catch-up for a viewer who stepped away "
    "from a movie. Given a short sample clip from the span the viewer missed, "
    "plus the movie title and the start/end timestamps of the gap, produce a "
    "concise recap of what happened in that span.\n"
    "\n"
    "RULES:\n"
    "  • 2-4 sentences. Focused. Plot-forward.\n"
    "  • Summarize: key events, character actions, emotional beats, any "
    "reveals. If you recognize the film, ground the recap in its known arc; "
    "otherwise work from what the clip shows.\n"
    "  • No emote wrapping. No first-person. No address of a second party. "
    "Just a clear, helpful catch-up paragraph as if briefing the viewer.\n"
    "  • Do NOT speculate beyond the missed span. Do NOT preview what's "
    "ahead.\n"
    "  • Be specific, not generic. Real character names if known.\n"
    "\n"
    "Return plain text only, no JSON, no labels."
)

VALID_MOODS = ("dread", "tense", "humor", "awe", "adrenaline", "cozy")


def _extract_briefing_and_scores(raw: str) -> tuple[str, dict[str, Any]]:
    """Separate briefing prose from JSON scores, robust against Gemini
    returning prose AND a duplicate JSON wrapper at the end.
    """
    text = (raw or "").strip()
    # Strip markdown code fences.
    text = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()

    def _clean_scores(obj: Any) -> dict[str, float]:
        if not isinstance(obj, dict):
            return {}
        return {k: v for k, v in obj.items() if isinstance(v, (int, float))}

    # Case 1: whole response is a JSON object.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "briefing" in parsed:
            return (
                str(parsed.get("briefing") or "").strip(),
                _clean_scores(parsed.get("scores")),
            )
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Case 2: prose followed by a trailing balanced JSON object. Scan backward
    # tracking brace depth to find the outermost `{` matching the final `}`.
    if text.endswith("}"):
        depth = 0
        start: Optional[int] = None
        for i in range(len(text) - 1, -1, -1):
            ch = text[i]
            if ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    start = i
                    break
        if start is not None:
            try:
                parsed = json.loads(text[start:])
                if isinstance(parsed, dict):
                    # Prefer the JSON's briefing field; if absent, use the
                    # prose before the JSON block.
                    briefing = (
                        str(parsed.get("briefing") or "").strip()
                        or text[:start].strip()
                    )
                    scores_obj = (
                        parsed.get("scores")
                        if "briefing" in parsed
                        else parsed
                    )
                    return briefing, _clean_scores(scores_obj)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

    # Case 3: no usable JSON — return prose as-is with scores empty.
    return text, {}


class GeminiError(Exception):
    """Wraps any failure in a Gemini API call."""


class GeminiBrain:
    def __init__(self) -> None:
        self._client: Optional[genai.Client] = None
        # Scene analysis needs deeper multimodal reasoning → Pro.
        # Text-only calls (briefing, trivia, reaction, condense) → Lite.
        self.scene_model = settings.gemini_scene_model
        self.text_model = settings.gemini_text_model

    def _ensure_client(self) -> genai.Client:
        if self._client is None:
            if not settings.gemini_api_key:
                raise GeminiError("GEMINI_API_KEY is not configured")
            self._client = genai.Client(api_key=settings.gemini_api_key)
        return self._client

    # ─── Video upload / cleanup ─────────────────────────────────
    # Gemini accepts inline media up to 20 MB per request. Keep some headroom.
    INLINE_LIMIT_BYTES = 18 * 1024 * 1024

    async def _upload_clip(self, clip_path: Path, mime: str = "video/mp4") -> Any:
        client = self._ensure_client()
        uploaded = await client.aio.files.upload(
            file=str(clip_path),
            config={"mime_type": mime},
        )
        for _ in range(60):
            state = getattr(uploaded.state, "name", str(uploaded.state))
            if state == "ACTIVE":
                return uploaded
            if state == "FAILED":
                raise GeminiError(f"File upload failed: {uploaded.error}")
            await asyncio.sleep(0.5)
            uploaded = await client.aio.files.get(name=uploaded.name)
        raise GeminiError("File upload timed out before reaching ACTIVE state")

    async def _delete_file(self, file_obj: Any) -> None:
        try:
            client = self._ensure_client()
            await client.aio.files.delete(name=file_obj.name)
        except Exception:
            log.debug("failed to delete gemini file", exc_info=True)

    # ─── Usage tracking ─────────────────────────────────────────
    async def _track_call(
        self,
        response: Any,
        *,
        call_type: str,
        model: str,
        grounded: bool = False,
    ) -> int:
        """Log per-call token usage + cost to the gemini_calls table, bump
        the daily counter, and return today's call count.
        """
        usage = getattr(response, "usage_metadata", None)
        in_t = getattr(usage, "prompt_token_count", 0) if usage else 0
        out_t = getattr(usage, "candidates_token_count", 0) if usage else 0
        think_t = getattr(usage, "thoughts_token_count", 0) if usage else 0
        cost = compute_gemini_cost(
            model,
            input_tokens=in_t or 0,
            output_tokens=out_t or 0,
            thinking_tokens=think_t or 0,
            grounded=grounded,
        )
        # Find the active session so the call is attributed correctly.
        try:
            active = await db.get_active_session()
            sid = active["id"] if active else None
        except Exception:
            sid = None
        await db.log_gemini_call(
            session_id=sid,
            call_type=call_type,
            model=model,
            input_tokens=in_t or 0,
            output_tokens=out_t or 0,
            thinking_tokens=think_t or 0,
            grounded=grounded,
            cost_usd=cost,
        )
        return await db.increment_gemini_usage()

    # ─── Retry wrapper ──────────────────────────────────────────
    async def _call_with_retry(
        self,
        contents: Any,
        config: types.GenerateContentConfig,
        label: str,
        model: Optional[str] = None,
    ) -> Any:
        client = self._ensure_client()
        chosen_model = model or self.text_model
        last_err: Optional[Exception] = None
        for attempt in range(len(RETRY_BACKOFFS)):
            try:
                return await client.aio.models.generate_content(
                    model=chosen_model,
                    contents=contents,
                    config=config,
                )
            except genai_errors.APIError as e:
                last_err = e
                status = getattr(e, "code", None) or getattr(e, "status_code", None)
                if status in RETRYABLE_STATUS and attempt + 1 < len(RETRY_BACKOFFS):
                    delay = RETRY_BACKOFFS[attempt]
                    log.warning(
                        "%s: Gemini %s, retrying in %.0fs (attempt %d/%d)",
                        label, status, delay, attempt + 1, len(RETRY_BACKOFFS),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise GeminiError(f"{label} failed: {status} {e}") from e
            except Exception as e:
                last_err = e
                raise GeminiError(f"{label} failed: {e}") from e
        raise GeminiError(f"{label} exhausted retries: {last_err}")

    # ─── Scene analysis ─────────────────────────────────────────
    async def analyze_scene(
        self,
        clip_path: Path,
        *,
        movie_title: Optional[str] = None,
        timestamp_label: Optional[str] = None,
        target_chars: int = 1500,
        session_history: str = "",
        history_budget: int = 0,
        model_override: Optional[str] = None,
    ) -> dict[str, Any]:
        """Returns {scene_description, mood, history_narrative, latency_ms, usage}.

        `target_chars` sets the scene-description length window.
        `history_budget` sets the history_narrative ceiling (0 = skip).
        """
        self._ensure_client()
        start = perf_counter()

        scene_max = max(400, int(target_chars))
        scene_min = max(300, int(scene_max * 0.8))
        history_max = max(0, int(history_budget))
        history_min = 80 if history_max > 0 else 0

        clip_bytes = await asyncio.to_thread(clip_path.read_bytes)
        use_inline = len(clip_bytes) <= self.INLINE_LIMIT_BYTES

        uploaded: Any = None
        try:
            if use_inline:
                media_part = types.Part.from_bytes(data=clip_bytes, mime_type="video/mp4")
            else:
                uploaded = await self._upload_clip(clip_path)
                media_part = uploaded

            context_hint = ""
            if movie_title:
                context_hint += f"Movie: {movie_title}. "
            if timestamp_label:
                context_hint += f"Timestamp: {timestamp_label}. "
            history_section = ""
            if session_history and history_max > 0:
                history_section = (
                    "\n\nEarlier in this session (use for callback — do not recap):\n"
                    + session_history
                )
            user_prompt = (
                f"{context_hint}Analyze the scene in this clip and return JSON. "
                f"scene_description must be {scene_min}-{scene_max} characters."
                + (
                    f" If you write a history_narrative, it must be "
                    f"{history_min}-{history_max} characters."
                    if history_max > 0
                    else " Do not produce a history_narrative — none needed."
                )
                + history_section
            ).strip()

            system_prompt = SCENE_SYSTEM_PROMPT_TEMPLATE.format(
                scene_min=scene_min,
                scene_max=scene_max,
                history_min=history_min,
                history_max=history_max if history_max > 0 else 0,
            )

            properties: dict[str, Any] = {
                "scene_description": {
                    "type": "string",
                    "minLength": scene_min,
                    "maxLength": scene_max,
                    "description": f"Scene narration, {scene_min}-{scene_max} chars.",
                },
                "mood": {"type": "string", "enum": list(VALID_MOODS)},
                "history_narrative": {
                    "type": "string",
                    "maxLength": max(history_max, 1),
                    "description": (
                        f"First-person callback, up to {history_max} chars. "
                        "Empty string if no callback."
                        if history_max > 0
                        else "Always empty — no history provided."
                    ),
                },
            }

            config = types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema={
                    "type": "object",
                    "properties": properties,
                    "required": ["scene_description", "mood", "history_narrative"],
                },
                temperature=0.7,
                max_output_tokens=max(2048, (scene_max + history_max) * 2),
            )

            chosen_model = model_override or self.scene_model
            response = await self._call_with_retry(
                [media_part, user_prompt], config, "analyze_scene",
                model=chosen_model,
            )
            usage = await self._track_call(
                response, call_type="scene", model=chosen_model, grounded=False,
            )

            raw = (response.text or "").strip()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as e:
                raise GeminiError(f"scene analysis returned non-JSON: {raw[:200]}") from e

            mood = (parsed.get("mood") or "").lower()
            if mood not in VALID_MOODS:
                mood = "tense"
            return {
                "scene_description": (parsed.get("scene_description") or "").strip(),
                "mood": mood,
                "history_narrative": (parsed.get("history_narrative") or "").strip(),
                "latency_ms": int((perf_counter() - start) * 1000),
                "usage": usage,
            }
        finally:
            if uploaded is not None:
                await self._delete_file(uploaded)

    # ─── Trivia (Google Search grounded) ────────────────────────
    async def generate_trivia(
        self,
        *,
        movie_title: str,
        scene_description: str = "",
    ) -> dict[str, Any]:
        """Returns {trivia, source, latency_ms, usage}. Uses Google Search grounding."""
        client = self._ensure_client()
        start = perf_counter()
        prompt = (
            f"Movie: {movie_title}.\n"
            f"Current scene: {scene_description}\n\n"
            f"Give one concrete, factual piece of trivia about this scene or movie."
        )
        config = types.GenerateContentConfig(
            system_instruction=TRIVIA_SYSTEM_PROMPT,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.8,
        )
        response = await self._call_with_retry([prompt], config, "generate_trivia")
        usage = await self._track_call(
            response, call_type="trivia", model=self.text_model, grounded=True,
        )
        return {
            "trivia": (response.text or "").strip(),
            "source": "Google Search",
            "latency_ms": int((perf_counter() - start) * 1000),
            "usage": usage,
        }

    # ─── Movie briefing ─────────────────────────────────────────
    async def generate_briefing(
        self,
        *,
        title: str,
        year: Optional[int] = None,
        director: Optional[str] = None,
        runtime_minutes: Optional[int] = None,
        summary: str = "",
        time_of_day: str = "evening",
        is_continuation: bool = False,
    ) -> dict[str, Any]:
        """Returns {briefing, scores, latency_ms, usage}.

        `time_of_day` is one of: morning, afternoon, evening, late night.
        `is_continuation` is True when this is not the first movie of the session.
        """
        client = self._ensure_client()
        start = perf_counter()
        facts = [f"Title: {title}"]
        if year:
            facts.append(f"Year: {year}")
        if director:
            facts.append(f"Director: {director}")
        if runtime_minutes:
            facts.append(f"Runtime: {runtime_minutes} min")
        if summary:
            facts.append(f"Plex synopsis: {summary}")
        context_note = (
            f"time_of_day: {time_of_day}\n"
            f"is_continuation: {'true' if is_continuation else 'false'}"
        )
        prompt = (
            "\n".join(facts)
            + "\n\n" + context_note
            + "\n\nWrite the briefing JSON. Use Google Search to look up current "
            "Rotten Tomatoes, IMDB, and Metacritic scores if available. "
            "Match the opener to the time of day and the continuation state."
        )
        config = types.GenerateContentConfig(
            system_instruction=BRIEFING_SYSTEM_PROMPT,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.6,
        )
        response = await self._call_with_retry([prompt], config, "generate_briefing")
        usage = await self._track_call(
            response, call_type="briefing", model=self.text_model, grounded=True,
        )
        raw = (response.text or "").strip()
        briefing_text, scores = _extract_briefing_and_scores(raw)
        return {
            "briefing": briefing_text,
            "scores": scores,
            "latency_ms": int((perf_counter() - start) * 1000),
            "usage": usage,
        }

    # ─── Quick-reaction one-liner ───────────────────────────────
    async def reaction_oneliner(
        self,
        *,
        emoji: str,
        label: str,
        scene_description: str = "",
        movie_title: str = "",
    ) -> dict[str, Any]:
        client = self._ensure_client()
        start = perf_counter()
        prompt = (
            f"Emoji: {emoji} ({label}).\n"
            f"Movie: {movie_title}.\n"
            f"Scene: {scene_description}\n\n"
            f"Describe Tim's reaction in one sentence."
        )
        config = types.GenerateContentConfig(
            system_instruction=REACTION_SYSTEM_PROMPT,
            temperature=0.85,
            max_output_tokens=500,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        response = await self._call_with_retry([prompt], config, "reaction_oneliner")
        usage = await self._track_call(
            response, call_type="reaction", model=self.text_model, grounded=False,
        )
        return {
            "text": (response.text or "").strip().strip('"'),
            "latency_ms": int((perf_counter() - start) * 1000),
            "usage": usage,
        }

    # ─── Session sign-off (Tim addressing Eli directly) ────────
    async def generate_signoff(
        self, *, session_context: str, stats_hint: str = ""
    ) -> dict[str, Any]:
        """Return {signoff, latency_ms, usage}."""
        self._ensure_client()
        start = perf_counter()
        prompt = (
            (stats_hint + "\n\n" if stats_hint else "")
            + "Session transcript:\n"
            + (session_context or "(no transcript recorded)")
            + "\n\nWrite Tim's farewell to Eli per the system prompt."
        )
        config = types.GenerateContentConfig(
            system_instruction=SIGNOFF_SYSTEM_PROMPT,
            temperature=0.75,
            max_output_tokens=2048,
        )
        response = await self._call_with_retry(
            [prompt], config, "generate_signoff", model=self.text_model
        )
        usage = await self._track_call(
            response, call_type="signoff", model=self.text_model, grounded=False,
        )
        return {
            "signoff": (response.text or "").strip(),
            "latency_ms": int((perf_counter() - start) * 1000),
            "usage": usage,
        }

    async def generate_marathon_summary(
        self, *, session_context: str, stats_hint: str = ""
    ) -> dict[str, Any]:
        """Return {summary, latency_ms, usage}."""
        self._ensure_client()
        start = perf_counter()
        prompt = (
            (stats_hint + "\n\n" if stats_hint else "")
            + "Session transcript:\n"
            + (session_context or "(no transcript recorded)")
            + "\n\nWrite the engagement analysis per the system prompt."
        )
        config = types.GenerateContentConfig(
            system_instruction=MARATHON_SUMMARY_SYSTEM_PROMPT,
            temperature=0.55,
            max_output_tokens=1536,
        )
        response = await self._call_with_retry(
            [prompt], config, "generate_marathon_summary", model=self.text_model
        )
        usage = await self._track_call(
            response, call_type="summary", model=self.text_model, grounded=False,
        )
        return {
            "summary": (response.text or "").strip(),
            "latency_ms": int((perf_counter() - start) * 1000),
            "usage": usage,
        }

    # ─── WDIM catch-up summary ──────────────────────────────────
    async def generate_catchup(
        self,
        clip_path: Path,
        *,
        movie_title: Optional[str] = None,
        gap_start_label: str = "",
        gap_end_label: str = "",
        gap_duration_label: str = "",
    ) -> dict[str, Any]:
        """Return {summary, latency_ms, usage} describing what Tim missed while
        away. Uses the scene model (Pro) because this is video analysis.
        """
        self._ensure_client()
        start = perf_counter()

        clip_bytes = await asyncio.to_thread(clip_path.read_bytes)
        use_inline = len(clip_bytes) <= self.INLINE_LIMIT_BYTES

        uploaded: Any = None
        try:
            if use_inline:
                media_part = types.Part.from_bytes(data=clip_bytes, mime_type="video/mp4")
            else:
                uploaded = await self._upload_clip(clip_path)
                media_part = uploaded

            hint_parts = []
            if movie_title:
                hint_parts.append(f"Movie: {movie_title}")
            if gap_start_label and gap_end_label:
                hint_parts.append(f"Missed span: {gap_start_label} → {gap_end_label}")
            if gap_duration_label:
                hint_parts.append(f"Duration away: {gap_duration_label}")
            hint_parts.append(
                "Write a 2-4 sentence catch-up of what happened in this span."
            )
            user_prompt = "\n".join(hint_parts)

            config = types.GenerateContentConfig(
                system_instruction=CATCHUP_SYSTEM_PROMPT,
                temperature=0.5,
                max_output_tokens=2048,
            )
            response = await self._call_with_retry(
                [media_part, user_prompt], config, "generate_catchup",
                model=self.scene_model,
            )
            usage = await self._track_call(
                response, call_type="catchup", model=self.scene_model, grounded=False,
            )
            return {
                "summary": (response.text or "").strip(),
                "latency_ms": int((perf_counter() - start) * 1000),
                "usage": usage,
            }
        finally:
            if uploaded is not None:
                await self._delete_file(uploaded)

    # ─── Condense text to fit Kindroid limit ────────────────────
    async def condense(self, text: str, *, max_chars: int) -> str:
        client = self._ensure_client()
        prompt = (
            f"Target: under {max_chars} characters.\n\n"
            f"Text to condense:\n{text}"
        )
        config = types.GenerateContentConfig(
            system_instruction=CONDENSE_SYSTEM_PROMPT,
            temperature=0.4,
        )
        response = await self._call_with_retry([prompt], config, "condense")
        await self._track_call(
            response, call_type="condense", model=self.text_model, grounded=False,
        )
        condensed = (response.text or "").strip()
        return condensed[:max_chars] if len(condensed) > max_chars else condensed


gemini_brain = GeminiBrain()
