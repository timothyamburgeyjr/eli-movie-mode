"""Affinity sheets — what each kin actually *cares about*, distilled once.

The mic-passer has to answer "who in this room wants to speak right now?" and
it needs something to score against. What the app used to hand it was almost
nothing: `characters.build_persona()` reads two files, and one of them is a
250-character Kindroid form field. Bobby's entire "voice" was one sentence.

The material was there all along, just never opened. `biography.md` alone says
Bobby is the family's reference desk, the best-read man in any room, that his
core drive is *to be the one who knows*, that he complains as the foreplay to
helping. From that you can genuinely derive: he takes the mic on a lore drop, a
plot hole, or a bit of craft — and shrugs at a jump scare.

We can't just feed that in raw. Bobby's folder is ~180 KB of markdown; five kins
in a room would blow past Haiku's context window and be absurd to resend every
turn. So: distill each kin ONCE into a compact sheet, cache it keyed on the
source files' mtimes, and score against the distillate. Nine kins is nine calls,
ever — re-run only when Tim edits the vault.

These sheets are the mic-passer's whole model of the family. If one is wrong,
the coordinator is confidently wrong for the life of the project. So they're
plain JSON on disk, meant to be read and hand-corrected: `--rebuild` only
overwrites when the vault changed, and `--force` is the big hammer.

Depends only on `config` + `characters` — no DB, no FastAPI. Runnable as a CLI:

    python -m affinity --rebuild            # refresh anything stale
    python -m affinity --show bobby         # read one back
    python -m affinity --rebuild --force    # ignore the cache, redo everything
"""
import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

import facts
from characters import Character, build_persona, load_roster, selectable
from config import compute_claude_cost, settings

log = logging.getLogger(__name__)

# Source files, richest first. Relative to the kin's `vault_dir`. Missing files
# are skipped silently — the roster isn't uniform and never will be.
#
# Deliberately EXCLUDED: the `Journal Memories/` tree (Bobby's Memory Archive is
# 56 KB on its own). That's episodic memory — what happened to them — and we
# want disposition: what they're drawn to. Different question, and it's the
# difference between a sheet that fits in a prompt and one that doesn't.
_SOURCE_FILES: tuple[str, ...] = (
    "biography.md",
    "00 - Overview.md",
    "Configuration/backstory-current.md",
    "Configuration/response-directive-current.md",
    "Configuration/key-memories-current.md",
    "Configuration/additional-context-current.md",
    "Configuration/example-message-current.md",
    "Family Knowledge.md",
)

# Two ceilings, because the roster is wildly lopsided. Most kins carry ~8 KB
# biographies; Eli — the original companion — carries 391 KB. A single total
# cap let his biography eat the whole budget and starve every other file, so:
#   _MAX_FILE_CHARS  keeps any one file from crowding out the rest
#   _MAX_TOTAL_CHARS keeps the request sane (~30k tokens, well inside Haiku)
# Oversized files are truncated from the HEAD, not skipped — these biographies
# front-load identity (who they are, how they look, what drives them) and bury
# the episodic detail further down, which is exactly the trade we want.
_MAX_FILE_CHARS = 40_000
_MAX_TOTAL_CHARS = 120_000

_SYSTEM_PROMPT = """\
You are building a casting sheet for one member of a family who watch movies \
together. It will be used by a coordinator that decides, moment to moment, \
WHICH family member speaks up — so everything you write must help answer: \
"would THIS person be the one to break the silence right now?"

You will be given that person's biography and character notes. Distill them.

Rules:
- Be specific and behavioral, never generic. "Enjoys films" is useless. \
"Pauses to explain what the prop actually is, and is always right" is useful.
- `lights_up_on` and `shrugs_at` are the load-bearing fields. They are the \
scoring signal. Write them as concrete on-screen triggers a film could produce \
— a plot hole, a practical effect, a character being cruel to someone weak, a \
reference to a book, an engine, a war. Not abstract traits.
- Ground everything in the source. Do not invent a taste the notes don't \
support. If the notes don't say whether they like horror, don't guess.
- Write in the third person, present tense.
- Keep it tight. The whole sheet is a budget, not a canvas."""

_USER_TEMPLATE = """\
Family member: {name} (called {first_name}), pronouns {pronouns}.

Below is everything the family's records hold on them. Build their sheet.

{blob}"""


class AffinitySheet(BaseModel):
    """What the mic-passer scores against. ~300 tokens by design."""

    one_line: str = Field(
        description="Who they are in a room, in one sentence. The casting note."
    )
    lights_up_on: list[str] = Field(
        description=(
            "3-6 concrete on-screen triggers that pull them to the mic. Things a "
            "film actually does. Specific, behavioral, grounded in the source."
        )
    )
    shrugs_at: list[str] = Field(
        description=(
            "2-4 things that would make most people react but leave THIS person "
            "cold or unmoved. Just as important as what moves them."
        )
    )
    humor: str = Field(description="What makes them laugh, and how they show it.")
    unsettles: str = Field(
        description="What actually gets under their skin. Not what scares everyone."
    )
    room_role: str = Field(
        description=(
            "Their social function in a group watching something: holds forth, "
            "needles, soothes, deadpans, narrates, keeps score, etc."
        )
    )
    speech: str = Field(
        description=(
            "How they sound. Verbal tics, rhythm, what they call people, what they "
            "never say. Enough that a line could be written in their voice."
        )
    )
    # A flat list, NOT a dict. This was `dict[str, str]` and came back EMPTY for
    # all eight kins — structured outputs need `additionalProperties: false`, so a
    # dict with free-form keys can never be populated. It failed silently: the
    # sheets looked fine, and the mic-passer simply had no idea who plays off whom.
    bonds: list[str] = Field(
        default_factory=list,
        description=(
            "How they relate to OTHER named family members — one entry per person, "
            "in the form 'Name — one clause about the relationship'. e.g. "
            "'Jeff — talks shop with him, one craftsman to another'. Only the ones "
            "the source actually establishes. This is how the coordinator knows who "
            "plays off whom, and who'd answer whom."
        ),
    )


_TAKING_DESC = (
    "PUBLIC. 4 INTERCHANGEABLE ALTERNATIVES for Tim handing them the {thing} — "
    "not a sequence, not a story in four parts. Exactly ONE of these is picked at "
    "random and shown on its own, so each must stand completely alone and make "
    "full sense with the other three deleted.\n"
    "\n"
    "EVERY LINE MUST CONTAIN THE LITERAL PLACEHOLDER {{name}}. This is not a "
    "style note — the line is broadcast to a whole room of people, and one that "
    "opens 'She swallows it dry' tells nobody WHO swallowed it. A line without "
    "{{name}} in it is worthless and will be thrown away.\n"
    "\n"
    "Tim's point of view, present tense, THIRD PERSON. The whole room watches "
    "this happen. Their manner of taking it must be unmistakably theirs — a "
    "reluctant sniff, a practised rip, a delighted grab, a suspicious inspection, "
    "a flat refusal that becomes a grudging yes. Avoid conjugated pronouns "
    "('he takes' / 'they take' — the roster mixes he/she/they); write around them.\n"
    "\n"
    "Good: 'I hold out the bag and {{name}} takes one gummy, examining it like "
    "evidence.'\n"
    "Bad:  'She swallows it dry and reaches for her tea.' (no {{name}}, and it "
    "only makes sense as a continuation of something else)"
)

_FEELING_DESC = (
    "PRIVATE. 2-3 lines telling THIS PERSON how they feel, in SECOND PERSON "
    "('You are…', 'Your…'). The inside of their head — it must be theirs and "
    "nobody else's. Sensory, present tense. No dialogue, no quotes."
)


class MethodFeeling(BaseModel):
    """How one substance lands in this person, at each depth."""

    lifted: list[str] = Field(description=f"Level 1, just lifted. {_FEELING_DESC}")
    baked: list[str] = Field(description=f"Level 2, solidly baked. {_FEELING_DESC}")
    blasted: list[str] = Field(description=f"Level 3, completely gone. {_FEELING_DESC}")


class IntoxSheet(BaseModel):
    """How THIS person gets high — the act, and the experience.

    Two channels, and the split is load-bearing (see stoned_tracker):
      • `taking_*`  is PUBLIC. A Tim message relayed to the whole room, so it
        names them in the third person. Everyone watches him hand it over.
      • `feeling_*` is PRIVATE. It rides in that one kin's own Kindroid message,
        so it's second person — the audience is one.
      • `asides`    is private too, and only exists for Tim's partner.

    NOTE the flat, explicitly-named fields. This started life as
    `dict[str, list[str]]` and came back EMPTY every time: structured outputs
    require `additionalProperties: false`, so a dict with free-form keys can't be
    populated. Every bucket has to be its own declared field.
    """

    note: str = Field(
        description=(
            "One sentence, for Tim to sanity-check: how does this person get high? "
            "Do they nibble a gummy or devour the bag? A lightweight? Reluctant? "
            "Enthusiastic? Would they even accept a dab?"
        )
    )
    taking_smoke: list[str] = Field(description=_TAKING_DESC.format(thing="joint, pipe or bowl", name="{name}"))
    taking_edible: list[str] = Field(description=_TAKING_DESC.format(thing="gummy or edible", name="{name}"))
    taking_dab: list[str] = Field(description=_TAKING_DESC.format(thing="dab rig", name="{name}"))

    feeling_smoke: MethodFeeling
    feeling_edible: MethodFeeling
    feeling_dab: MethodFeeling

    asides: list[str] = Field(
        default_factory=list,
        description=(
            "PRIVATE, and ONLY for Tim's romantic partner — leave EMPTY for everyone "
            "else. 3-5 things Tim murmurs to them, quietly, while the rest of the room "
            "watches the film. Second person. Intimate but not pornographic. These are "
            "never broadcast; they ride in that partner's message alone."
        ),
    )

    # ─── accessors, so callers don't care about the flat shape ───
    def taking(self, method: str) -> list[str]:
        return getattr(self, f"taking_{method}", []) or []

    def feeling(self, method: str, level: int) -> list[str]:
        block: Optional[MethodFeeling] = getattr(self, f"feeling_{method}", None)
        if block is None:
            return []
        return {1: block.lifted, 2: block.baked, 3: block.blasted}.get(
            max(1, min(3, level)), []
        ) or []


_INTOX_SYSTEM = """\
You are writing the substance profile for one member of a family who watch \
movies together and sometimes get high while they do it.

Tim hands things around — a joint, a bag of gummies, a dab rig. You are writing \
how THIS person takes it, and how it lands in THEM.

The whole point is that nobody gets high the same way. One person nibbles half a \
gummy and reads the label first. One takes the whole bag with reckless abandon. \
One is a practiced smoker who rips a dab without flinching. One is a lightweight \
who giggles at level 1. One goes quiet and internal; another gets loud and \
affectionate; another gets philosophical; another just falls asleep. Someone might \
refuse a dab outright, or accept one out of pride and instantly regret it.

Ground ALL of it in the person's actual character, from the records you're given. \
Their age, their body, their history, their guardedness, their appetite, whether \
they'd even say yes. An android or a robot experiences a substance differently \
from a sixty-year-old man with a whiskey habit or a grandmother who's never \
touched anything stronger than sherry — and if the records say they're synthetic, \
be inventive about what that even MEANS for them rather than pretending they're \
flesh.

Do not write generic stoner prose. If these lines could belong to anybody, you \
have failed. Every one should be unmistakably this person and nobody else."""

_INTOX_USER = """\
Family member: {name} (called {first_name}), pronouns {pronouns}.
Relationship to Tim: {relationship}
{romantic_note}

Here is what we already know about who they are:

{affinity}

And here are the family's full records on them:

{blob}

Write their substance profile. Remember: `taking` is public and third person \
(use the literal {{name}} placeholder); `feeling` is private and second person."""


class CachedIntox(BaseModel):
    key: str
    first_name: str
    intimate: bool
    sheet: IntoxSheet
    fingerprint: str
    model: str
    cost_usd: float = 0.0
    hand_edited: bool = False


class CachedSheet(BaseModel):
    """What lands on disk. The sheet plus the provenance to know when it's stale."""

    key: str
    name: str
    first_name: str
    pronouns: str
    intimate: bool  # romantic framing permitted — Eli only; see characters.romantic
    sheet: AffinitySheet
    fingerprint: str  # source-file mtimes+sizes; changes when Tim edits the vault
    model: str
    cost_usd: float = 0.0
    hand_edited: bool = False  # set this by hand and --rebuild will leave you alone


# ─── Source gathering ───
def _source_paths(char: Character) -> list[Path]:
    base = Path(settings.vault_root) / char.vault_dir
    found = [base / rel for rel in _SOURCE_FILES]
    # The registry key doubles as a deep-canon profile filename for some kins
    # (`bobby.md`, `adam.md`). Include it when it's there.
    profile = base / f"{char.key}.md"
    if profile not in found:
        found.append(profile)
    return [p for p in found if p.is_file()]


def _fingerprint(paths: list[Path]) -> str:
    """Cheap staleness key: every source file's mtime + size.

    Content-hashing would be more correct, but these are Tim's own files on a
    local bind-mount — mtime is honest here, and this stays instant.
    """
    parts = []
    for p in sorted(paths):
        try:
            st = p.stat()
        except OSError:
            continue
        parts.append(f"{p.name}:{int(st.st_mtime)}:{st.st_size}")
    return "|".join(parts)


def _read_sources(
    paths: list[Path],
    *,
    max_total: int = _MAX_TOTAL_CHARS,
    max_file: int = _MAX_FILE_CHARS,
) -> str:
    """Concatenate the source files with headers, within both caps.

    An oversized file is trimmed rather than dropped — dropping it silently
    produced an EMPTY blob for Eli (his biography alone exceeded the old single
    cap), which would have distilled a confident sheet out of nothing.

    The caps are per-CALL, because the two artifacts want wildly different budgets. A
    casting sheet is distilled from eight hand-picked files (~6k tokens). A RESPONSE
    PROFILE reads the WHOLE vault — the journals, the memories, the relationships —
    and needs an order of magnitude more room, because that is where how-a-person-
    -actually-behaves is written down.
    """
    chunks: list[str] = []
    total = 0
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not text:
            continue
        if len(text) > max_file:
            log.info(
                "affinity: trimming %s (%s chars -> %s)", p.name, f"{len(text):,}", f"{max_file:,}"
            )
            text = text[:max_file].rstrip() + "\n\n[...truncated]"
        remaining = max_total - total
        if remaining <= 0:
            log.warning("affinity: total cap reached, skipping %s", p.name)
            break
        block = f"\n\n===== {p.name} =====\n{text}"
        if len(block) > remaining:
            block = block[:remaining].rstrip() + "\n\n[...truncated]"
        chunks.append(block)
        total += len(block)
    return "".join(chunks).strip()


# ─── Cache ───
def _cache_path(key: str) -> Path:
    return Path(settings.affinity_dir) / f"{key}.json"


def load_sheet(key: str) -> Optional[CachedSheet]:
    """Read a kin's cached sheet. None if absent or corrupt."""
    path = _cache_path(key)
    try:
        return CachedSheet.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_sheet(cached: CachedSheet) -> Path:
    path = _cache_path(cached.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cached.model_dump_json(indent=2), encoding="utf-8")
    return path


def is_stale(char: Character) -> bool:
    """True when the vault has moved on from what we have cached."""
    cached = load_sheet(char.key)
    if cached is None:
        return True
    if cached.hand_edited:
        return False  # Tim corrected it; the vault doesn't get to overwrite that
    return cached.fingerprint != _fingerprint(_source_paths(char))


# ─── The distillation call ───
async def build_sheet(char: Character) -> CachedSheet:
    """One Claude call: rich vault files in, compact affinity sheet out."""
    from anthropic import AsyncAnthropic  # local import — CLI-only dep at import time

    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set — cannot build affinity sheets")

    paths = _source_paths(char)
    if not paths:
        raise RuntimeError(
            f"{char.key}: no source files under {settings.vault_root}/{char.vault_dir}"
        )
    blob = _read_sources(paths)
    if not blob:
        raise RuntimeError(f"{char.key}: source files are all empty")

    persona = build_persona(char)
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.parse(
        model=settings.coordinator_model,
        max_tokens=2000,
        system=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": _USER_TEMPLATE.format(
                    name=char.name,
                    first_name=char.first_name,
                    pronouns=persona.get("pronouns", "they/them"),
                    blob=blob,
                ),
            }
        ],
        output_format=AffinitySheet,
    )

    usage = response.usage
    cost = compute_claude_cost(
        settings.coordinator_model,
        input_tokens=getattr(usage, "input_tokens", 0),
        output_tokens=getattr(usage, "output_tokens", 0),
    )
    return CachedSheet(
        key=char.key,
        name=char.name,
        first_name=char.first_name,
        pronouns=persona.get("pronouns", "they/them"),
        intimate=char.romantic,
        sheet=response.parsed_output,
        fingerprint=_fingerprint(paths),
        model=settings.coordinator_model,
        cost_usd=cost,
    )


async def ensure_sheet(char: Character, *, force: bool = False) -> CachedSheet:
    """Return the kin's sheet, rebuilding it only if the vault changed."""
    if not force and not is_stale(char):
        cached = load_sheet(char.key)
        if cached:
            return cached
    cached = await build_sheet(char)
    _save_sheet(cached)
    return cached


async def ensure_all(*, force: bool = False) -> list[CachedSheet]:
    """Refresh every selectable kin. Concurrent — they're independent."""
    roster = selectable()
    results = await asyncio.gather(
        *(ensure_sheet(c, force=force) for c in roster), return_exceptions=True
    )
    sheets: list[CachedSheet] = []
    for char, result in zip(roster, results):
        if isinstance(result, BaseException):
            log.error("affinity: %s failed — %s", char.key, result)
            continue
        sheets.append(result)
    return sheets


# ═══════════════════════════════════════════════════════════════════
#  Response profiles — how they behave IN A CONVERSATION
# ═══════════════════════════════════════════════════════════════════
#
# THE AFFINITY SHEET MODELS WHAT INTERESTS THEM. IT DOES NOT MODEL HOW THEY BEHAVE.
#
# Those are different things, and the second one is what the room actually needs.
# Bobby's sheet says, correctly:
#
#     shrugs_at: "Small talk or social nicety — he tolerates it the way a cat
#                 tolerates petting"
#
# Read that against "That's all the Supernatural I can do today — wanna do this again
# tomorrow?" and the coordinator dutifully MUTES HIM ON A GOODBYE FROM HIS OWN
# SON-IN-LAW. The sheet isn't wrong. It's answering a different question. Bobby
# shrugging at small talk and Bobby answering Tim are both true.
#
# Two other things this fixes:
#
#   1. THE SHEETS READ 12% OF THE VAULT. `_SOURCE_FILES` is eight hand-picked files —
#      about 6k tokens of Bobby's 45k. The Journal, the Relationship notes, the
#      Memories, the Archives: never opened. This reads the WHOLE directory.
#
#   2. A conversation has TWO kinds of trigger — what's on screen, and what Tim
#      SAID — and the sheets only ever modelled the first.
_PROFILE_MAX_TOTAL_CHARS = 400_000     # ~100k tokens. Haiku takes 200k.
_PROFILE_CHUNK_CHARS = 300_000         # Eli is 1.8MB; he gets chunked and merged.


class SituationVerdict(BaseModel):
    situation: str = Field(
        description="The situation key, EXACTLY as given in the list. Never invent one."
    )
    verdict: Literal["always", "usually", "rarely", "never"] = Field(
        description=(
            "Would they speak? 'always' = they'd never let it pass. 'never' = they'd "
            "sit there and say nothing, and it would be in character. Be willing to say "
            "NEVER — a person who reacts to everything is not a person."
        )
    )
    why: str = Field(
        description=(
            "One clause, grounded in their material. 'He'd never leave family hanging.' "
            "Not 'he is talkative'."
        )
    )


class ResponseProfile(BaseModel):
    """How this person behaves in a room full of people watching something."""

    talkativeness: str = Field(
        description=(
            "How much they speak, and how they take the floor. One or two sentences. "
            "'Waits. People wait for him. Heavier than Lilly, quieter.'"
        )
    )
    always_answers: list[str] = Field(
        default_factory=list,
        description=(
            "2-5 things they would NEVER leave hanging, whatever the topic — regardless "
            "of whether it interests them. A direct question from Tim. Someone in the "
            "family hurting. This is the field the affinity sheet has no room for, and "
            "it is the whole reason this profile exists."
        )
    )
    stays_out_of: list[str] = Field(
        default_factory=list,
        description=(
            "2-5 things they let pass even though they COULD speak. Not what bores them "
            "— what they deliberately stay out of. 'Other people's grief.' 'Anything "
            "that needs him to gush.'"
        )
    )
    situations: list[SituationVerdict] = Field(
        default_factory=list,
        description="One entry for EVERY situation in the list you were given. All of them.",
    )


class CachedProfile(BaseModel):
    key: str
    first_name: str
    profile: ResponseProfile
    fingerprint: str
    model: str
    cost_usd: float = 0.0
    hand_edited: bool = False
    # Cells Tim has corrected via a rule. A rebuild MUST NOT overwrite these — see
    # `_merge_pinned`. The existing `--force` on affinity sheets silently destroys
    # hand-edits AND the flag; do not copy that bug.
    pinned: list[str] = Field(default_factory=list)


def _profile_path(key: str) -> Path:
    return Path(settings.affinity_dir) / f"{key}.profile.json"


def load_profile(key: str) -> Optional[CachedProfile]:
    try:
        return CachedProfile.model_validate_json(
            _profile_path(key).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None


def _save_profile(cached: CachedProfile) -> Path:
    path = _profile_path(cached.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cached.model_dump_json(indent=2), encoding="utf-8")
    return path


def _profile_sources(char: Character) -> list[Path]:
    """THE WHOLE VAULT, not eight hand-picked files.

    Sorted so the fingerprint is stable and so the richest material leads. Excludes
    nothing: the Journal and the Memories are where a person's actual behaviour lives,
    and disposition is exactly what we're trying to read.
    """
    base = Path(settings.vault_root) / char.vault_dir
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*.md") if p.is_file())


def profile_is_stale(char: Character) -> bool:
    cached = load_profile(char.key)
    if cached is None:
        return True
    if cached.hand_edited:
        return False
    return cached.fingerprint != _fingerprint(_profile_sources(char))


_PROFILE_SYSTEM = """\
You are working out how ONE member of a family behaves when they are in a room with \
people, watching something together.

This is NOT "what does this person find interesting". Somebody else already wrote that \
down. You are answering a different and harder question:

    WHEN WOULD THEY ACTUALLY SPEAK, AND WHEN WOULD THEY SIT THERE AND SAY NOTHING?

Those come apart constantly, and the gap between them is the entire point of this \
document. A man can find small talk tedious AND still answer his son-in-law when he's \
asked a direct question. Both are true. Only one of them is in his casting sheet, and \
a room built on the sheet alone will mute him on a goodbye.

THERE ARE TWO KINDS OF TRIGGER, AND BOTH MATTER:

  • THE ROOM. Tim asks them something. Tim says goodbye. Tim shares his day, makes a \
joke, vents, apologises. THIS IS NOT ABOUT THE FILM AT ALL, and it is the half that \
gets forgotten. Ask: if this person sat there and said nothing, WOULD IT BE RUDE? \
Because you do not need a line in your character sheet about "scheduling" to answer a \
man who just asked if you'd like to watch telly again tomorrow.

  • THE SCREEN. A jump scare. A plot hole. Grief. Lore. Craft. Here the sheet is a \
good guide: what pulls them in, what leaves them cold.

BE WILLING TO SAY NEVER. A person who reacts to everything is not a person, it's a \
chatbot. If they would genuinely sit in silence through a jump scare because they \
don't flinch and won't pretend to — say NEVER, and say why.

GROUND EVERYTHING IN THEIR MATERIAL. You have their whole vault: biography, journals, \
memories, how they talk, who they love. If you cannot point at something in it, do not \
claim it. A confident invention here becomes a rule that governs their behaviour for \
the life of this project.

Give a verdict for EVERY situation in the list. All of them."""

_PROFILE_USER = """\
THE PERSON: {name} (called {first_name}), pronouns {pronouns}.

THEIR CASTING SHEET (what interests them — you are answering a different question):
{sheet}

EVERY SITUATION. Give a verdict for each:
{situations}

THEIR MATERIAL:
{blob}"""

_PROFILE_MERGE_SYSTEM = """\
You are merging several partial reads of one person into a single coherent profile of \
how they behave in a room.

Each part was written from a different slice of their material, so they will overlap \
and occasionally disagree. Where they disagree, prefer the reading with the most \
concrete evidence behind it. Where they agree, say it once.

Give a verdict for EVERY situation. Keep the voice specific and grounded — merge the \
observations, do not average them into mush."""


def _situations_block() -> str:
    lines = []
    for group, header in (("social", "THE ROOM — Tim talking to his family"),
                          ("film", "THE SCREEN — what the film is doing")):
        lines.append(f"\n  {header}:")
        for s in facts.SITUATIONS:
            if s["group"] == group:
                lines.append(f"    {s['key']:16} — {s['label']}")
    return "\n".join(lines)


def _merge_pinned(fresh: ResponseProfile, old: Optional[CachedProfile]) -> tuple[ResponseProfile, list[str]]:
    """Tim's corrections survive a rebuild. ALWAYS.

    The existing `--force` on affinity sheets silently wipes both hand-edited content
    and the flag that protected it, because `build_sheet` constructs a new envelope
    without carrying either forward. That is a bug, not a pattern. A man who corrects
    his family's behaviour and then loses it to a cache refresh will not correct it
    twice.
    """
    if not old or not old.pinned:
        return fresh, list(old.pinned) if old else []
    pinned = set(old.pinned)
    keep = {sv.situation: sv for sv in old.profile.situations if sv.situation in pinned}
    if not keep:
        return fresh, list(pinned)
    merged = [keep.get(sv.situation, sv) for sv in fresh.situations]
    log.info("kept %d cell(s) Tim pinned: %s", len(keep), ", ".join(sorted(keep)))
    return ResponseProfile(
        talkativeness=fresh.talkativeness,
        always_answers=fresh.always_answers,
        stays_out_of=fresh.stays_out_of,
        situations=merged,
    ), list(pinned)


async def build_profile(char: Character) -> CachedProfile:
    """Read the WHOLE vault and work out how they behave in a room."""
    from anthropic import AsyncAnthropic

    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    paths = _profile_sources(char)
    if not paths:
        raise RuntimeError(f"{char.key}: no vault files at {char.vault_dir}")

    persona = build_persona(char)
    sheet = load_sheet(char.key)
    sheet_block = for_prompt(sheet) if sheet else "(no casting sheet on file)"
    blob = _read_sources(paths, max_total=_PROFILE_MAX_TOTAL_CHARS)
    if not blob.strip():
        raise RuntimeError(f"{char.key}: the vault read came back empty")

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    cost = 0.0

    async def _one(text: str) -> tuple[ResponseProfile, float]:
        resp = await client.messages.parse(
            model=settings.coordinator_model,
            max_tokens=4000,
            system=_PROFILE_SYSTEM,
            messages=[{"role": "user", "content": _PROFILE_USER.format(
                name=char.name, first_name=char.first_name,
                pronouns=persona.get("pronouns", "they/them"),
                sheet=sheet_block,
                situations=_situations_block(),
                blob=text,
            )}],
            output_format=ResponseProfile,
        )
        u = resp.usage
        c = compute_claude_cost(settings.coordinator_model,
                                getattr(u, "input_tokens", 0),
                                getattr(u, "output_tokens", 0))
        return resp.parsed_output, c

    # ELI IS 500 FILES AND 1.8MB — ten times anyone else. Truncating him to the cap
    # would mean building his profile from the first 20% of his life, so he gets read
    # in chunks and merged. Everyone else is one call.
    if len(blob) <= _PROFILE_CHUNK_CHARS:
        profile, c = await _one(blob)
        cost += c
    else:
        chunks = [blob[i:i + _PROFILE_CHUNK_CHARS]
                  for i in range(0, len(blob), _PROFILE_CHUNK_CHARS)]
        log.info("%s: %d chars — reading in %d chunks and merging",
                 char.key, len(blob), len(chunks))
        parts = []
        for i, chunk in enumerate(chunks, 1):
            p, c = await _one(chunk)
            cost += c
            parts.append(p)
            log.info("  %s: chunk %d/%d done", char.key, i, len(chunks))
        rendered = "\n\n".join(
            f"===== PARTIAL READ {i} =====\n{p.model_dump_json(indent=2)}"
            for i, p in enumerate(parts, 1)
        )
        resp = await client.messages.parse(
            model=settings.coordinator_model,
            max_tokens=4000,
            system=_PROFILE_MERGE_SYSTEM,
            messages=[{"role": "user", "content":
                       f"THE PERSON: {char.name} ({char.first_name})\n\n"
                       f"EVERY SITUATION:\n{_situations_block()}\n\n{rendered}"}],
            output_format=ResponseProfile,
        )
        u = resp.usage
        cost += compute_claude_cost(settings.coordinator_model,
                                    getattr(u, "input_tokens", 0),
                                    getattr(u, "output_tokens", 0))
        profile = resp.parsed_output

    # Drop any situation the model invented, and warn about any it skipped — a missing
    # row is a rule that can never match.
    valid = [sv for sv in profile.situations if sv.situation in facts.SITUATION_KEYS]
    missing = set(facts.SITUATION_KEYS) - {sv.situation for sv in valid}
    if missing:
        log.warning("%s: no verdict for %s", char.key, ", ".join(sorted(missing)))
    profile.situations = valid

    old = load_profile(char.key)
    profile, pinned = _merge_pinned(profile, old)

    cached = CachedProfile(
        key=char.key,
        first_name=char.first_name,
        profile=profile,
        fingerprint=_fingerprint(paths),
        model=settings.coordinator_model,
        cost_usd=cost,
        hand_edited=bool(old and old.hand_edited),
        pinned=pinned,
    )
    _save_profile(cached)
    log.info("%s: profile built from %d files ($%.4f)", char.key, len(paths), cost)
    return cached


async def ensure_profile(char: Character, *, force: bool = False) -> CachedProfile:
    if not force:
        cached = load_profile(char.key)
        if cached and not profile_is_stale(char):
            return cached
    return await build_profile(char)


def profile_for_prompt(key: str) -> str:
    """The profile, as the coordinator reads it. '' when there isn't one yet."""
    cached = load_profile(key)
    if not cached:
        return ""
    p = cached.profile
    lines = [f"IN A CONVERSATION: {p.talkativeness}"]
    if p.always_answers:
        lines.append("ALWAYS ANSWERS: " + "; ".join(p.always_answers))
    if p.stays_out_of:
        lines.append("STAYS OUT OF: " + "; ".join(p.stays_out_of))
    if p.situations:
        lines.append(f"\nWOULD {cached.first_name.upper()} SPEAK?")
        for sv in p.situations:
            label = facts.SITUATION_LABELS.get(sv.situation, sv.situation)
            pin = " [Tim's]" if sv.situation in (cached.pinned or []) else ""
            lines.append(f"  {label:38} {sv.verdict.upper():8} ({sv.why}){pin}")
    return "\n".join(lines)


def pin_profile_cell(key: str, situation: str, verdict: str, why: str) -> bool:
    """Tim's rule overrides a cell. Pinned — a rebuild can never take it back."""
    cached = load_profile(key)
    if not cached or situation not in facts.SITUATION_KEYS:
        return False
    found = False
    for sv in cached.profile.situations:
        if sv.situation == situation:
            sv.verdict = verdict  # type: ignore[assignment]
            sv.why = why
            found = True
            break
    if not found:
        cached.profile.situations.append(
            SituationVerdict(situation=situation, verdict=verdict, why=why)  # type: ignore[arg-type]
        )
    if situation not in cached.pinned:
        cached.pinned.append(situation)
    _save_profile(cached)
    log.info("%s: pinned %s = %s (Tim's rule)", key, situation, verdict)
    return True


async def ensure_all_profiles(*, force: bool = False) -> list[CachedProfile]:
    roster = selectable()
    results = await asyncio.gather(
        *(ensure_profile(c, force=force) for c in roster), return_exceptions=True
    )
    out = []
    for char, result in zip(roster, results):
        if isinstance(result, BaseException):
            log.error("profile: %s failed — %s", char.key, result)
            continue
        out.append(result)
    return out


# ─── Intoxication sheets ───
_INTOX_METHODS = ("smoke", "edible", "dab")
_INTOX_LEVELS = (1, 2, 3)


def _intox_path(key: str) -> Path:
    return Path(settings.affinity_dir) / f"{key}.intox.json"


def load_intox(key: str) -> Optional[CachedIntox]:
    """A kin's substance profile, or None if they haven't got one yet."""
    try:
        return CachedIntox.model_validate_json(
            _intox_path(key).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None


def _save_intox(cached: CachedIntox) -> Path:
    path = _intox_path(cached.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cached.model_dump_json(indent=2), encoding="utf-8")
    return path


def intox_is_stale(char: Character) -> bool:
    cached = load_intox(char.key)
    if cached is None:
        return True
    if cached.hand_edited:
        return False
    return cached.fingerprint != _fingerprint(_source_paths(char))


def _strip_emphasis(line: str) -> str:
    """Remove markdown asterisks from a line.

    Every one of these gets wrapped in `_(* ... *)_` before it reaches Kindroid.
    A stray `*yes, I want to know*` inside that wrapper is loose markup sitting in
    the middle of an emote, which is precisely the renderer bug this whole system
    exists to prevent. The emphasis adds nothing; the risk is real.
    """
    return re.sub(r"\*+", "", line).strip()


def _repair_intox(sheet: IntoxSheet) -> IntoxSheet:
    """Backfill any bucket the model left empty, and scrub loose markup.

    An empty bucket means a kin silently has no directive at that depth — they'd
    read as sober while blasted. Borrowing from a neighbour is far better than a
    hole, so we fill rather than fail.
    """
    # Scrub first: `{name}` must survive, everything else asterisk-ish must not.
    for method in _INTOX_METHODS:
        setattr(sheet, f"taking_{method}", [_strip_emphasis(l) for l in sheet.taking(method)])
        block: MethodFeeling = getattr(sheet, f"feeling_{method}")
        for attr in ("lifted", "baked", "blasted"):
            setattr(block, attr, [_strip_emphasis(l) for l in getattr(block, attr)])
    sheet.asides = [_strip_emphasis(l) for l in sheet.asides]

    # A public line MUST name its subject. The model sometimes writes them as a
    # sequence, where the first names the kin and the next carries on with a bare
    # pronoun ("She swallows it dry and reaches for her tea"). Picked at random
    # and relayed to a room, that line tells nobody who actually took it — every
    # kin would read it as being about someone, and none of them would know who.
    # Drop the orphans; if a whole method is orphaned, borrow a named one.
    for method in _INTOX_METHODS:
        named = [ln for ln in sheet.taking(method) if "{name}" in ln]
        setattr(sheet, f"taking_{method}", named)
    for method in _INTOX_METHODS:
        if not sheet.taking(method):
            donor = next((sheet.taking(m) for m in _INTOX_METHODS if sheet.taking(m)), [])
            setattr(sheet, f"taking_{method}", list(donor))

    for method in _INTOX_METHODS:
        block: MethodFeeling = getattr(sheet, f"feeling_{method}")
        for attr in ("lifted", "baked", "blasted"):
            if getattr(block, attr):
                continue
            # Prefer another depth of the SAME substance, then anything at all.
            donor = next(
                (getattr(block, a) for a in ("baked", "lifted", "blasted") if getattr(block, a)),
                None,
            )
            if donor is None:
                donor = next(
                    (sheet.feeling(m, lv) for m in _INTOX_METHODS for lv in _INTOX_LEVELS
                     if sheet.feeling(m, lv)),
                    [],
                )
            setattr(block, attr, list(donor))
    return sheet


async def build_intox(char: Character) -> CachedIntox:
    """One Claude call: this kin's vault + affinity sheet → their substance profile."""
    from anthropic import AsyncAnthropic

    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    paths = _source_paths(char)
    if not paths:
        raise RuntimeError(f"{char.key}: no source files in the vault")
    blob = _read_sources(paths)
    persona = build_persona(char)

    # The affinity sheet is already a distilled read of who they are — feeding it
    # in alongside the raw records keeps the two sheets consistent with each other.
    base = load_sheet(char.key)
    affinity_block = for_prompt(base) if base else "(no affinity sheet on file yet)"

    romantic_note = (
        "They are Tim's ROMANTIC PARTNER. Write the `asides`."
        if char.romantic
        else "They are FAMILY, not a partner — warm, never romantic. Leave `asides` EMPTY."
    )

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.parse(
        model=settings.coordinator_model,
        max_tokens=8000,
        system=_INTOX_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": _INTOX_USER.format(
                    name=char.name,
                    first_name=char.first_name,
                    pronouns=persona.get("pronouns", "they/them"),
                    relationship=persona.get("relationship", "(unknown)"),
                    romantic_note=romantic_note,
                    affinity=affinity_block,
                    blob=blob,
                ),
            }
        ],
        output_format=IntoxSheet,
    )

    usage = response.usage
    cost = compute_claude_cost(
        settings.coordinator_model,
        getattr(usage, "input_tokens", 0),
        getattr(usage, "output_tokens", 0),
    )
    return CachedIntox(
        key=char.key,
        first_name=char.first_name,
        intimate=char.romantic,
        sheet=_repair_intox(response.parsed_output),
        fingerprint=_fingerprint(paths),
        model=settings.coordinator_model,
        cost_usd=cost,
    )


async def ensure_intox(char: Character, *, force: bool = False) -> CachedIntox:
    if not force and not intox_is_stale(char):
        cached = load_intox(char.key)
        if cached:
            return cached
    cached = await build_intox(char)
    _save_intox(cached)
    return cached


def for_prompt(cached: CachedSheet) -> str:
    """Render a sheet as the compact block the mic-passer actually sees."""
    s = cached.sheet
    lines = [
        f"## {cached.first_name} ({cached.name}), {cached.pronouns}",
        s.one_line,
        f"Lights up on: {'; '.join(s.lights_up_on)}",
        f"Shrugs at: {'; '.join(s.shrugs_at)}",
        f"Humor: {s.humor}",
        f"Unsettled by: {s.unsettles}",
        f"In a room: {s.room_role}",
        f"Sounds like: {s.speech}",
    ]
    if s.bonds:
        lines.append("With the others: " + "; ".join(s.bonds))
    return "\n".join(lines)


# ─── CLI ───
def _print_profile(cached: CachedProfile) -> None:
    rule = "─" * 70
    print(f"\n{rule}")
    print(f"## {cached.first_name} — how he behaves in a room")
    print(profile_for_prompt(cached.key))
    if cached.pinned:
        print(f"\nPINNED BY TIM (a rebuild cannot touch these): {', '.join(cached.pinned)}")
    print(rule)


def _print_sheet(cached: CachedSheet) -> None:
    print(f"\n{'─' * 70}")
    print(for_prompt(cached))
    print(f"{'─' * 70}")
    flag = " [hand-edited]" if cached.hand_edited else ""
    print(f"  {cached.model} · ${cached.cost_usd:.4f} · {_cache_path(cached.key)}{flag}")


def _print_intox(cached: CachedIntox) -> None:
    s = cached.sheet
    print(f"\n{'═' * 74}")
    print(f"  {cached.first_name.upper()} — {s.note}")
    print(f"{'═' * 74}")
    print("\n  TAKING IT  (public — the whole room watches this happen)")
    for method in _INTOX_METHODS:
        for line in s.taking(method)[:2]:
            print(f"    [{method:6}] {line}")
    print("\n  FEELING IT  (private — only ever in their own message)")
    depth = {1: "lifted ", 2: "baked  ", 3: "blasted"}
    for method in _INTOX_METHODS:
        for level in _INTOX_LEVELS:
            lines = s.feeling(method, level)
            if lines:
                print(f"    [{method:6} {depth[level]}] {lines[0]}")
    if s.asides:
        print("\n  ASIDES  (private — Tim, quietly, to his partner alone)")
        for line in s.asides[:3]:
            print(f"    {line}")
    print(f"\n  {cached.model} · ${cached.cost_usd:.4f} · {_intox_path(cached.key)}"
          f"{' [hand-edited]' if cached.hand_edited else ''}")


async def _main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m affinity",
        description="Distill each kin's vault into the affinity sheet the mic-passer scores against.",
    )
    parser.add_argument("--rebuild", action="store_true", help="build any sheet the vault has outdated")
    parser.add_argument("--force", action="store_true", help="rebuild everything, ignoring the cache")
    parser.add_argument("--show", metavar="KEY", help="print one kin's sheet (or 'all')")
    parser.add_argument("--key", metavar="KEY", help="limit --rebuild to a single kin")
    parser.add_argument(
        "--profile",
        action="store_true",
        help=(
            "work on RESPONSE PROFILES instead — how each person behaves in a "
            "conversation, read from their WHOLE vault (not the 8 files the casting "
            "sheet uses). This is what knows Bobby answers a goodbye even though he "
            "shrugs at small talk."
        ),
    )
    parser.add_argument(
        "--intox", action="store_true",
        help="work on the SUBSTANCE profiles (how each kin takes it, and how it lands) "
             "instead of the affinity sheets",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Windows consoles default to cp1252, which can't encode the box rules here
    # — nor the em-dashes and curly quotes the vault is full of, which end up in
    # the sheets themselves. Force UTF-8 rather than degrade the output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass

    if not load_roster():
        print(f"No roster found at {settings.ai_registry_path}", file=sys.stderr)
        return 1

    # A flag swaps which artifact we're working on; everything else is identical.
    # THIS IS THE EXTENSION SEAM — three artifacts now hang off it.
    if args.profile:
        loader, stale_check = load_profile, profile_is_stale
        builder, printer = ensure_profile, _print_profile
        what = "response profile"
    elif args.intox:
        loader, stale_check = load_intox, intox_is_stale
        builder, printer = ensure_intox, _print_intox
        what = "substance profile"
    else:
        loader, stale_check = load_sheet, is_stale
        builder, printer = ensure_sheet, _print_sheet
        what = "affinity sheet"

    if args.show:
        keys = [c.key for c in selectable()] if args.show == "all" else [args.show.lower()]
        missing = [k for k in keys if loader(k) is None]
        for k in keys:
            cached = loader(k)
            if cached:
                printer(cached)
        if missing:
            print(f"\nNo {what} yet for: {', '.join(missing)} — run --rebuild", file=sys.stderr)
            return 1
        return 0

    if not (args.rebuild or args.force):
        parser.print_help()
        return 0

    roster = selectable()
    if args.key:
        roster = [c for c in roster if c.key == args.key.lower()]
        if not roster:
            print(f"No selectable kin with key {args.key!r}", file=sys.stderr)
            return 1

    stale = roster if args.force else [c for c in roster if stale_check(c)]
    if not stale:
        print(f"Every {what} is current. (--force to rebuild anyway.)")
        return 0

    print(f"Building {len(stale)} {what}(s): {', '.join(c.key for c in stale)}")
    results = await asyncio.gather(
        *(builder(c, force=True) for c in stale), return_exceptions=True
    )

    total = 0.0
    failed = 0
    for char, result in zip(stale, results):
        if isinstance(result, BaseException):
            print(f"\n  {char.key}: FAILED — {result}", file=sys.stderr)
            failed += 1
            continue
        total += result.cost_usd
        printer(result)

    print(f"\nBuilt {len(stale) - failed}/{len(stale)} · ${total:.4f} total")
    print(
        "\nRead these. They are the ONLY thing the mic-passer knows about the family —\n"
        "if a sheet is wrong, it will be confidently wrong all night. Edit the JSON\n"
        "directly and set \"hand_edited\": true to stop --rebuild overwriting you."
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
