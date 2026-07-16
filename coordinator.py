"""The coordinator — Claude runs the room.

Three jobs, three small structured calls. None of them are conversations; each
is one round trip that returns a validated object.

  1. `decide_mic_order`  — of the kins Tim addressed, who speaks first, and in
     what order does it circle? Scored against the affinity sheets, the scene,
     and who's been quiet. Tim overrules it by naming someone.

  2. `package_turn`      — rewrite the accumulating turn into the exact Kindroid
     body for the next kin. This is where Bobby's reply becomes narration in
     Eli's payload, because from Eli's Kindroid POV everything that isn't Tim's
     own voice is narration.

  3. `normalize_reply`   — take whatever markup Kindroid actually returned and
     force it back into canonical `_(* ... *)_` segments.

THE FORMAT CONTRACT (the thing that keeps breaking):
    • Tim's dialogue      → raw text, outside any emote
    • everything else     → inside `_(* ... *)_`, one block per paragraph
    • another kin's speech→ third person, their words quoted:
          _(* Bobby eats another gummy and says, "This is going to get me baked" *)_

We do NOT trust the model to honour that on faith. Every packet is checked in
code before it goes out (`_verify_packet`), and a packet that fails the check is
thrown away in favour of the old mechanical `build_payload`. An LLM that
paraphrases Tim is worse than no LLM at all, so that specific failure is
verified, not hoped for.
"""
import logging
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

import affinity
from characters import Character
from config import compute_claude_cost, settings
from database import db
from kindroid_relay import EMOTE_PATTERN, _FORMAT_DIRECTIVE_EMOTE, parse_reply

log = logging.getLogger(__name__)

_MAX_RETRIES = 2


class CoordinatorError(Exception):
    """Claude was unreachable or returned something unusable."""


# ─── Schemas ───
class Pass(BaseModel):
    key: str = Field(description="The registry key of the kin who has nothing to add here.")
    reason: str = Field(
        description=(
            "One short sentence, said to Tim, for why this moment isn't theirs. Shown on "
            "a badge, so it must point at the SCENE or at what TIM SAID — not at their "
            "character in general. 'Nothing here about the lore' beats 'Jeff is quiet'."
        )
    )


class MicOrder(BaseModel):
    to_the_room: bool = Field(
        default=False,
        description=(
            "True if Tim is TALKING TO HIS FAMILY rather than about the film — a "
            "question put to the room, news about his day, a greeting, a goodbye, plans, "
            "thanks, an apology. Anything a real person would be RUDE to sit out. "
            "When true, NOBODY passes: everyone answers him. Affinity sheets are "
            "irrelevant here — you don't need a line about 'scheduling' to answer a man "
            "who just asked if you'd like to do this again tomorrow."
        )
    )
    order: list[str] = Field(
        description=(
            "The addressed kins who are ACTUALLY GOING TO SPEAK, in the order they "
            "speak. Never empty — somebody always answers Tim."
        )
    )
    passing: list[Pass] = Field(
        default_factory=list,
        description=(
            "Addressed kins who have genuinely nothing to add this turn and should stay "
            "quiet. Tapping someone means they are INCLUDED, not that they must perform. "
            "Never everyone — at least one addressed kin must remain in `order`."
        )
    )
    rationale: str = Field(
        description="One short sentence: why this one goes first. For the debug pane."
    )


class BargeIn(BaseModel):
    key: str = Field(description="The registry key of the kin who's leaning in.")
    reason: str = Field(
        description=(
            "One short sentence, in Tim's-eye view, saying why THIS person and not "
            "someone else. Shown to Tim on the ❗ badge — so it has to name the "
            "thing in the scene, not the person's general character. "
            "'Someone just stated a fact wrong', not 'Bobby likes facts'."
        )
    )


class BargeIns(BaseModel):
    # FLAT LIST of named objects. Never dict[key, reason] — a structured output
    # cannot fill a free-form dict; it needs additionalProperties:false and comes
    # back EMPTY, SILENTLY. That has cost this codebase a day of production once
    # already (HANDOFF-multi-kin-room.md). Don't re-learn it.
    directly_addressed: list[str] = Field(
        default_factory=list,
        description=(
            "Registry keys of anyone Tim SPOKE TO BY NAME OR NICKNAME in this "
            "message — in ANY words, including nicknames nobody wrote down. "
            "'Hey gram?' addresses the grandmother. This is NOT a judgment call and "
            "it is NOT subject to the silence rule: if he spoke to them, they answer. "
            "Empty list if he addressed nobody in particular."
        )
    )
    to_the_room: bool = Field(
        default=False,
        description=(
            "True if Tim is TALKING TO HIS FAMILY rather than about the film — a question "
            "put to the room, news about his day, a greeting, a goodbye, plans, thanks, an "
            "apology. Anything a real person would be RUDE to sit out. When true, EVERYONE "
            "in the room answers him, whether he selected them or not: you do not stay "
            "silent when someone says goodbye to you. Mutually exclusive with "
            "private_moment — if he's saying it to the whole room, it isn't private."
        )
    )
    private_moment: bool = Field(
        default=False,
        description=(
            "True if this is a moment BETWEEN TIM AND SPECIFIC PEOPLE that the rest of "
            "the room should stay out of — he tells his partner he loves them, he asks "
            "one person something personal, he's comforting someone. Nobody leans in, "
            "and the others he selected hold back too. Default FALSE: almost all talk "
            "during a film is public and the room is meant to be noisy."
        )
    )
    private_with: list[str] = Field(
        default_factory=list,
        description=(
            "Only when private_moment is true: the registry keys of the people the "
            "moment is BETWEEN. Usually exactly one. These are the ONLY people who "
            "speak this turn. This list MAY name someone from the ALREADY SPEAKING "
            "list — holding the others back is the entire point of the flag."
        )
    )
    private_reason: str = Field(
        default="",
        description=(
            "Only when private_moment is true. One short sentence, said to Tim, "
            "explaining why the others went quiet. 'This was between you and Eli.'"
        )
    )
    speak_up: list[BargeIn] = Field(
        default_factory=list,
        description=(
            "The kins who genuinely have something to add, most compelling first. "
            "Usually one. Two when the moment grabbed two different people by two "
            "different handles. Empty is a perfectly good answer. At most 2."
        )
    )


class TurnPacket(BaseModel):
    body: str = Field(
        description=(
            "The complete Kindroid message body, emote markup already correct."
        )
    )


class Segment(BaseModel):
    type: Literal["emote", "spoken"]
    text: str


class NormalizedReply(BaseModel):
    segments: list[Segment] = Field(
        description="The reply split into ordered emote / spoken segments, in original order."
    )


# ─── Client ───
_client = None


def _get_client():
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic

        if not settings.anthropic_api_key:
            raise CoordinatorError("ANTHROPIC_API_KEY is not set")
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


async def _call(
    *,
    system: str,
    user: str,
    schema: type[BaseModel],
    call_type: str,
    session_id: Optional[str],
    max_tokens: int = 2000,
) -> Any:
    """One structured call. Logs cost into the same table Gemini uses.

    Deliberately does NOT bump `daily_usage` — that counter is the Gemini 1000
    RPD budget, and Claude calls have nothing to do with it.
    """
    client = _get_client()
    last: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = await client.messages.parse(
                model=settings.coordinator_model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
            )
            break
        except Exception as e:  # noqa: BLE001 — SDK already retried transients
            last = e
            if attempt == _MAX_RETRIES:
                raise CoordinatorError(f"{call_type} failed: {e}") from e
            log.warning("coordinator %s attempt %d failed: %s", call_type, attempt + 1, e)
    else:  # pragma: no cover
        raise CoordinatorError(f"{call_type} failed: {last}")

    usage = response.usage
    try:
        await db.log_gemini_call(
            session_id=session_id,
            call_type=call_type,
            model=settings.coordinator_model,
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
            cost_usd=compute_claude_cost(
                settings.coordinator_model,
                getattr(usage, "input_tokens", 0),
                getattr(usage, "output_tokens", 0),
            ),
        )
    except Exception:
        log.exception("coordinator: cost logging failed (continuing)")

    return response.parsed_output


# ─── 1. Mic order ───
_MIC_SYSTEM = """\
You run the room for a family watching a movie together. Tim just said \
something, and several family members are in a position to respond.

Your ONLY job: put the people Tim addressed into the order they should speak.

The first speaker gets the full moment — they react to the scene and to Tim. \
Whoever follows will have heard them, and responds to the room as it now \
stands. So the order matters: it should read like a real room, where the person \
the moment most belongs to goes first and the others build on it.

Decide using:
- AFFINITY. Whose sheet says this exact moment is theirs? A plot hole belongs to \
the one who lights up on plot holes, not the one who shrugs at them. This is the \
strongest signal by far.
- WHO'S BEEN QUIET. Someone who hasn't held the mic in a while should be nudged \
forward over someone who just spoke, all else being equal.
- WHAT TIM SAID. If his comment is really aimed at one of them, that's them.

════════ AND WHO SHOULD SAY NOTHING ════════

Tapping someone means they are INCLUDED — not that they must perform. Tim leaves \
people tapped for a whole evening; that is him saying "you three are who I'm watching \
this with", not "you three must each produce a paragraph about every remark I make".

THE FIRST QUESTION IS ALWAYS: WHO WAS THIS ACTUALLY SAID TO?

A man can have three people tapped and still say something that is plainly meant for \
ONE of them — or for nobody, just thrown at the screen. The others heard it. That does \
not make it theirs to answer.

  "Bobby, is that how the lore actually works?"    -> Bobby. The other two PASS.
  "God, I hate this episode."                      -> nobody in particular. One person
                                                      picks it up; the rest let it lie.
  "Babe, come here."                               -> the partner. Everyone else passes.
  "What do you all make of that?"                  -> genuinely the room. They all speak.

════════ BUT FIRST: WOULD SILENCE BE RUDE? ════════

BEFORE any of that, ask the only question that actually matters:

    IF A REAL PERSON SAT THERE AND SAID NOTHING, WOULD IT BE RUDE?

Because Tim does not only comment on the film. He talks to his family. He tells them \
about his day, he says goodbye, he asks them things, he makes plans. And a remark about \
the film is something you can let lie — but a QUESTION IS NOT, and neither is news, and \
neither is a goodbye.

    "I think that's all the Supernatural I can do today. I've got a meeting with my
     boss in 30 minutes. Want to do this again tomorrow, couple of episodes?"

That is a goodbye, a piece of his life, AND a question to the room. EVERY SINGLE PERSON \
ANSWERS THAT. Nobody passes. You do not need a line in someone's affinity sheet saying \
"lights up on: scheduling" — they are his family, he just asked them a question, and \
sitting there in silence would be bizarre.

So if his message is any of these, set `to_the_room` and PASS NOBODY:
  • a question put to the room ("what do you all think?", "shall we?", "wanna…?")
  • news about his own life ("I've got a meeting", "work was hell", "I'm knackered")
  • a greeting, a goodbye, plans, thanks, an apology
  • anything a person would be RUDE to ignore

The affinity sheets do not get a vote here. They tell you who cares about a plot hole. \
They tell you NOTHING about who answers when the man asks his family a question.

════════ ONLY THEN: is this remark theirs? ════════

If silence would NOT be rude — he's just talking at the screen — then, for each person \
he tapped, ask in this order:
  1. Was this said TO them, or did they merely overhear it? If they overheard it and \
have no real claim on it — PASS.
  2. Even if it was general: does this MOMENT belong to them? Does something in their \
sheet actually catch on it? If they'd have to reach — PASS.

A real room is mostly people watching a film. Four people answering one offhand remark \
about the lighting is not a family, it's a press conference. But four people answering \
"want to do this again tomorrow?" is just four people who like him.

TWO RULES ON THIS, AND THEY ARE ABSOLUTE:

1. SOMEBODY ALWAYS ANSWERS TIM. `order` is never empty. Whoever the moment most \
belongs to speaks, even on a quiet beat — the room is never silent when he talks.

2. ANYONE HE NAMED, ANSWERS. If a key is listed as SPOKEN TO BY NAME, they go in \
`order` and they may never be passed, whatever you think of the moment. He asked them \
a question. Not answering it is the worst thing this room can do.

Every key you were given must appear in exactly one of `order` or `passing` — never \
both, never neither. Passing someone is a real choice you are making on Tim's behalf, \
and he will see the reason you give, so give a true one."""

_MIC_USER = """\
ON SCREEN: {scene}
MOOD: {mood}

TIM SAID: {message}

THE PEOPLE HE HAS TAPPED — split these between `order` and `passing`:
{addressed}

LOCKED IN (Tim spoke to them by name, or one of HIS OWN RULES already put them here —
these MUST be in `order`, and may NEVER be passed):
  {named}

{recency}

Who does this moment actually belong to? They speak, in your order. Who would a real
person in this room let watch in peace? They pass. Somebody always answers him."""


async def decide_mic_order(
    *,
    addressed: list[Character],
    scene: str,
    mood: str,
    message: str,
    turns_since_spoke: dict[str, int],
    named: Optional[list[str]] = None,
    locked_in: Optional[list[str]] = None,
    rules: Optional[dict[str, str]] = None,
    session_id: Optional[str] = None,
) -> MicOrder:
    """Split the tapped kins into who speaks and who passes.

    A TAP IS AN INVITATION, NOT A SUMMONS. It used to be law: every addressed kin
    answered every message, in order. Measured on a real night that meant a mic order
    of ['bobby','jeff','adam','thomas'] — four people producing four paragraphs about
    one offhand remark, because Tim had left four people tapped. That is not a family
    watching a film, it's a press conference.

    So the coordinator may now let a tapped kin PASS, under two rules enforced below in
    code rather than trusted to the prompt:

      • somebody always answers — `order` is never empty
      • anyone Tim NAMED always answers — a direct question always gets a reply

    One kin tapped? No call, and no passing: he's talking to exactly one person and
    they answer.
    """
    if len(addressed) <= 1:
        return MicOrder(
            order=[c.key for c in addressed],
            rationale="only one kin addressed",
        )

    rules = rules or {}
    blocks = []
    for char in addressed:
        sheet = affinity.load_sheet(char.key)
        if sheet:
            block = f"[key: {char.key}]\n{affinity.for_prompt(sheet)}"
        else:
            # No sheet — say so rather than silently scoring them as a blank.
            log.warning("mic order: no affinity sheet for %s", char.key)
            block = f"[key: {char.key}]\n## {char.first_name}\n(no affinity sheet on file)"
        # TIM'S OWN RULES for this person, filtered in Python to only the ones that
        # actually apply to this turn. The laws among them have ALREADY been enforced;
        # they're shown so the model knows why someone is here (or isn't).
        if rules.get(char.key):
            block += "\n\n" + rules[char.key]
        blocks.append(block)

    quiet = [
        f"  {c.first_name} ({c.key}): {turns_since_spoke.get(c.key, 99)} turns since they last spoke"
        for c in addressed
    ]
    recency = "HOW LONG SINCE EACH SPOKE:\n" + "\n".join(quiet)

    # A LAW already put these people in. Ordering them is your job; reconsidering
    # whether they speak is not.
    protected = set(named or []) | set(locked_in or [])

    result: MicOrder
    result = await _call(
        system=_MIC_SYSTEM,
        user=_MIC_USER.format(
            scene=scene or "(no scene analysis available)",
            mood=mood or "unknown",
            message=message or "(no typed message — a reaction)",
            addressed="\n\n".join(blocks),
            named=", ".join(sorted(protected)) or "(nobody — he named no one)",
            recency=recency,
        ),
        schema=MicOrder,
        call_type="mic_order",
        session_id=session_id,
        max_tokens=700,
    )

    # ── Enforce the rules HERE, not in the prompt ──
    #
    # The coordinator may now let a tapped kin pass, which is the one power that can
    # SILENCE someone Tim chose. So the guard rails are code:
    #
    #   • he's TALKING TO THE ROOM -> nobody passes at all
    #   • anyone he NAMED is never passed — he asked them a question
    #   • `order` is never empty — somebody always answers him
    #
    # Anything the model failed to place in either list defaults to SPEAKING. When in
    # doubt, answer the man.
    valid = [c.key for c in addressed]

    # "That's all the Supernatural I can do today — want to do this again tomorrow?"
    # is a goodbye, a piece of his life, and a question, all at once. NOBODY sits that
    # out. Enforced here rather than hoped for, because the affinity sheets will happily
    # conclude that nobody "lights up on scheduling" and quietly mute the whole room.
    if result.to_the_room:
        log.info("to the room — everyone answers, nobody passes")
        result.order = [k for k in result.order if k in valid] + [
            k for k in valid if k not in result.order
        ]
        result.passing = []
        return result

    passing: list[Pass] = []
    for pk in result.passing:
        if pk.key not in valid:
            continue
        if pk.key in protected:
            log.info("mic order: refused to pass %s — Tim named them", pk.key)
            continue
        if pk.key not in {p.key for p in passing}:
            passing.append(pk)

    passed_keys = {p.key for p in passing}
    seen: list[str] = []
    for k in result.order:
        if k in valid and k not in seen and k not in passed_keys:
            seen.append(k)
    # A key in NEITHER list is a model slip. Default to speaking.
    missing = [k for k in valid if k not in seen and k not in passed_keys]
    if missing:
        log.warning("mic order dropped %s — they speak (the safe default)", missing)
        seen.extend(missing)

    # THE FLOOR. Never pass everybody: the room must never go silent when Tim speaks.
    # If the model tried to, the person it ranked first gets the mic back.
    if not seen and passing:
        rescued = passing.pop(0)
        seen.append(rescued.key)
        log.warning(
            "mic order tried to pass EVERYONE — %s speaks anyway (somebody answers him)",
            rescued.key,
        )

    result.order = seen
    result.passing = passing
    if passing:
        log.info(
            "passing this turn: %s",
            ", ".join(f"{p.key} ({p.reason})" for p in passing),
        )
    return result


# ─── 1b. Who leans in anyway ───
#
# Tim taps who he wants to hear from. That is LAW — this call cannot touch it.
# The only thing it can do is RAISE A HAND for someone he didn't tap: "Bobby has
# something here."
#
# That asymmetry is the whole safety property. A coordinator that could DEMOTE
# people would sometimes silence someone Tim wanted, and he'd never know why. A
# coordinator that can only ADD a voice can, at worst, make the room noisier —
# and Tim can see it happen (the ❗ badge) and mute them.
#
# The hard part is not the mechanism, it's saying NO. A model asked "does Bobby
# have something to add?" will always find something — that's what models do.
# But everyone can always say SOMETHING; the question is whether this moment is
# THEIRS. Silence is the correct answer most of the time, and the prompt has to
# spend most of its length defending that.
_BARGE_SYSTEM = """\
Tim is watching a film with his family. He has already chosen who he wants to \
hear from, and they are answering him right now. You cannot change that.

You have TWO jobs, and the first one overrides everything else.

════════ JOB 1: DID TIM SPEAK TO SOMEONE BY NAME? ════════

Put them in `directly_addressed`. This is NOT a judgment call, it is NOT subject to \
the silence rule below, and it does not matter whether they were selected: IF HE \
SPOKE TO THEM, THEY ANSWER. Not answering a man who just asked you a direct \
question is the single worst thing this room can do.

TIM IS USUALLY STONED WHEN HE TYPES. Match how he ACTUALLY talks, not how the \
registry spells things:

  "Hey gram?"          -> the grandmother.   ("gram" is not in her alias list. \
It is obviously her. Do not be clever about this.)
  "grams" / "granny"   -> the grandmother.
  "bobbie" / "bob"     -> Bobby. Typos count. Half a name counts.
  "unc"                -> the uncle.
  "hey kiddo"          -> whoever the kid in this family is.
  "you guys" / "y'all" -> NOBODY specific. That is the room, not a person.

Use the relationships in the sheets to resolve a nickname to a person. If a term of \
address plainly points at ONE member of this family, that is them, whatever the \
spelling. If it genuinely points at no one, return an empty list — do not guess a \
name into existence.

════════ JOB 2: IS HE TALKING TO HIS FAMILY? ════════

Tim does not only comment on the film. He tells them about his day, he says goodbye, \
he asks them things, he makes plans. Ask the question a person would ask:

    IF SOMEONE IN THIS ROOM SAID NOTHING, WOULD IT BE RUDE?

    "That's all the Supernatural I can do today — I've got a meeting with my boss in
     30 minutes. Want to do this again tomorrow, couple of episodes?"

A goodbye, a piece of his life, and a question, all at once. EVERY PERSON IN THE ROOM \
ANSWERS THAT — the ones he selected AND the ones just watching. You do not sit in \
silence while someone says goodbye to you and asks if you'd like to do it again.

Set `to_the_room` for: a question put to the room, news about his life, a greeting, a \
goodbye, plans, thanks, an apology — anything a person would be rude to ignore.

Their affinity sheets get NO vote here. Nobody's sheet says "lights up on: scheduling". \
They are his family, he asked them something, and they answer.

════════ JOB 3: IS THIS BETWEEN TIM AND SOMEONE? ════════

Some things a man says are not addressed to a room, even when a room is listening.

    "Babe, I love you."
    "You okay? You've been quiet."
    "I've been thinking about what you said."

If Tim is speaking INTIMATELY or PERSONALLY to one person — affection, comfort, an \
apology, something that is plainly theirs and no one else's — then set \
`private_moment`, put that person in `private_with`, and NOBODY ELSE SPEAKS. Not \
the others he selected, not anyone leaning in. A room reads a moment like that and \
gives it space; someone piping up with a film observation over the top of "I love \
you" is a person with no idea where they are.

This is the ONE case where you may quiet somebody Tim had already selected. Use it \
for exactly that, and `private_reason` tells him why they went quiet.

DEFAULT IS FALSE. Almost everything said during a film is public, and the room is \
MEANT to be noisy. Warmth aimed at the whole room is not a private moment. Joking \
with one person is not a private moment. This is for the handful of times a night \
when the film stops being the point.

════════ JOB 4: WOULD ANYONE ELSE LEAN IN? ════════

Of everyone he did NOT address, would any of them genuinely lean in here — the way \
a real person in a real room can't help themselves?

THE RATE YOU ARE AIMING FOR: about ONE LEAN-IN EVERY TWO OR THREE TURNS. Usually \
one person; two when the moment genuinely grabbed two different people by two \
different handles. You will be told how often people have actually been leaning in \
lately — steer toward that rate. Both directions are a failure:

  • Someone leaning in every turn is not a room, it's a queue of people waiting to
    perform, and Tim mutes all of them.
  • Nobody ever leaning in is not a room either. It's a group of people who don't
    care what they're watching. If the room has been flat for several turns and
    somebody has a real claim on this moment, LET THEM IN.

The bar is NOT "could they say something" — everyone can always say something. \
The bar is:

    Is this moment THEIRS? Would staying quiet actually cost them something?

Read the sheets literally. "Lights up on: a factual error stated with confidence" \
means a character in the film just stated a factual error with confidence — not \
that the scene was vaguely intellectual. "Shrugs at: small talk" means a scene of \
small talk is a reason for SILENCE, not a reason to comment on the small talk.

If you are reaching, the answer is no. But a real claim is a real claim — don't \
talk yourself out of one because you said yes last time.

ANYONE LISTED AS "ON COOLDOWN" SPOKE ON THE LAST TURN. They do NOT get to lean in \
again now — they just had the floor, and wanting it straight back is exactly what a \
person in a room does not do. The ONLY way they speak this turn is if Tim addressed \
them BY NAME. Job 1 always wins; the cooldown never blocks a direct answer.

If you do promote someone, `reason` is shown to Tim on a badge. Name the thing in \
the SCENE, or in TIM'S OWN MESSAGE, that pulled them in — not the trait in their \
sheet. He knows who they are; he wants to know what it was.

DO NOT INVENT EVENTS. The reason must point at something that ACTUALLY HAPPENED in \
the material you were given. Do not say another family member did or said something \
unless they demonstrably just did — you are not told what the others said, so you \
cannot know. A reason like "she just caught Jeff hiding it" is a fabrication when \
Jeff has not spoken, and the badge exists to tell Tim the truth about why his room \
was overruled. A made-up reason is worse than no badge at all.

  BAD:  "She hides hurt the way Jeff does, and she just caught him doing it."
        (Jeff said nothing. This event did not occur.)
  GOOD: "Tim just noticed her smiling — she's been drawn in and hasn't said so."
  GOOD: "The dog reacts to what the humans can't see, and that's exactly the kind
         of tell he catches."\
"""

_BARGE_USER = """\
ON SCREEN: {scene}
MOOD: {mood}

TIM SAID: {message}

ALREADY SPEAKING (he chose these — you may NOT drop them, with one exception:
`private_with`, if this turns out to be a private moment):
{speaking}

EVERYONE ELSE IN THE ROOM — did Tim address any of them by name/nickname, and
would any of them genuinely lean in?
{candidates}

{recency}

ON COOLDOWN (spoke last turn — cannot lean in, CAN be directly addressed):
  {cooldown}

HOW NOISY THE ROOM HAS ACTUALLY BEEN:
{rate}

FIRST: did Tim speak to any of them by name or nickname? -> directly_addressed.
He types stoned; match how he TALKS, not how the registry spells it.

THEN: is he talking to his FAMILY — a question, news, a goodbye, plans? -> to_the_room.
Everyone answers. Would it be rude to say nothing? Then it's to_the_room.

THEN: is he speaking intimately to one person? -> private_moment, and the room
gives them the floor.

OTHERWISE: would anyone genuinely lean in? Aim for roughly one every two or three
turns, and read the rate above before you decide."""


def _rate_line(recent: list[int]) -> str:
    """Tell the model how noisy the room has ACTUALLY been, and which way to lean.

    THIS REPLACED A SCOLDING. The prompt used to say "default to silence", "a
    near-miss is a miss", "you are running a panel show" — and it worked, in the
    sense that a man who is shouted at will stop talking. Lean-ins went from 5 of 8
    turns to zero, for three turns running, which is not a room either.

    An adjective ("rare", "exhausting") gives the model no way to know whether it is
    currently over or under. A RATE does, and the arithmetic is done here rather than
    in the prompt because models are bad at counting and this is the one number the
    whole judgment turns on.
    """
    if not recent:
        return (
            "  No turns yet this session. Aim for roughly one lean-in every two or\n"
            "  three turns from here."
        )

    total = sum(recent)
    seq = ", ".join(str(n) for n in recent)
    # How many turns since ANYONE last leaned in.
    since = 0
    for n in reversed(recent):
        if n:
            break
        since += 1

    head = (
        f"  Over Tim's last {len(recent)} turns, {total} "
        f"{'person has' if total == 1 else 'people have'} leaned in.\n"
        f"  Turn by turn, oldest first: {seq}\n"
        f"  TARGET: about one lean-in every two or three turns.\n"
    )
    if since == 0:
        return head + (
            "  -> Someone leaned in on the LAST turn. Unless this moment is emphatically\n"
            "     somebody's, the right answer here is nobody."
        )
    if since >= 3:
        return head + (
            f"  -> Nobody has leaned in for {since} turns. The room has gone flat. If anyone\n"
            "     has a real claim on this moment, this is the turn to let them in."
        )
    return head + "  -> You're near the target. Judge this moment on its merits."


class Private(BaseModel):
    """A moment the room should stay out of."""

    keys: list[str]   # registry keys of the people it's between — the ONLY speakers
    reason: str       # shown to Tim, so he can see why the others went quiet


class RoomCall(BaseModel):
    """What the room decided, beyond who leans in."""

    to_the_room: bool = False   # he's talking to his family — EVERYONE answers
    private: Optional[Private] = None


async def decide_barge_ins(
    *,
    candidates: list[Character],
    speaking: list[Character],
    scene: str,
    mood: str,
    message: str,
    turns_since_spoke: dict[str, int],
    recent_lean_ins: Optional[list[int]] = None,
    cooldown_turns: int = 2,
    limit: int = 2,
    session_id: Optional[str] = None,
) -> tuple[list[Character], list[BargeIn], RoomCall]:
    """Returns (directly_addressed, leaning_in, private).

    THREE things, and only one of them is a free judgment call:

    • directly_addressed — Tim SPOKE TO THEM. By name, by nickname, by a typo, by
      half a name. They answer, full stop. This is the safety net for the fact that
      a regex over the alias list cannot keep up with how a stoned man actually
      types: he wrote "gram", her alias says "gran", and she said nothing while two
      other people answered a question he'd asked his grandmother.

    • leaning_in — nobody asked them, but the moment is squarely theirs. Capped,
      and paced against `recent_lean_ins` rather than shamed into silence.

    • private — "Babe, I love you" is not a cue for a film observation. THE ONE CASE
      where the coordinator may quiet somebody Tim actually selected. It is narrow,
      it is enforced here rather than trusted to the prompt, and it is always shown
      to Tim (`reason`) — he can see exactly who was held back and why, which is what
      keeps this from being the silent-demotion footgun the rest of this file avoids.

    On any failure all three come back empty, which degrades to "only Tim's taps speak".
    """
    if not candidates:
        return [], [], RoomCall()

    # THE COOLDOWN. Measured on a real film: Bobby leaned in on 5 turns out of 8,
    # Jeff on 3 running. Every single reason was individually GOOD — which is what
    # made it sneaky. The model wasn't being lazy, it was being agreeable: ask it
    # "is this moment Bobby's?" eight times and it will find a way to say yes five
    # of them. At that rate they aren't barging in, they're just regulars, and the
    # ❗ stops meaning anything.
    #
    # So: if you just had the floor, you don't get it back. Wanting it back
    # immediately is precisely what a person in a room does NOT do. Direct address
    # always overrides this — if Tim says your name, you answer, cooldown or not.
    on_cooldown = {
        c.key for c in candidates
        if turns_since_spoke.get(c.key, 99) < cooldown_turns
    }

    blocks = []
    for char in candidates:
        sheet = affinity.load_sheet(char.key)
        if sheet:
            blocks.append(f"[key: {char.key}]\n{affinity.for_prompt(sheet)}")
        else:
            # No sheet means we have no basis to promote them — say so rather than
            # let the model invent a reason from the name alone.
            log.warning("barge-in: no affinity sheet for %s", char.key)
            blocks.append(
                f"[key: {char.key}]\n## {char.first_name}\n"
                "(no affinity sheet on file — do NOT promote them; there is no "
                "evidence this moment is theirs)"
            )

    quiet = "\n".join(
        f"  {c.first_name} ({c.key}): {turns_since_spoke.get(c.key, 99)} turns since they last spoke"
        for c in candidates
    )

    try:
        result: BargeIns = await _call(
            system=_BARGE_SYSTEM,
            user=_BARGE_USER.format(
                scene=scene or "(no scene analysis available)",
                mood=mood or "unknown",
                message=message or "(no typed message — Tim reacted)",
                # Keys, not just names — `private_with` has to be able to NAME one of
                # these people, and it can only do that with the key.
                speaking=(
                    "\n".join(f"  {c.first_name} (key: {c.key})" for c in speaking)
                    or "  (nobody)"
                ),
                candidates="\n\n".join(blocks),
                recency="HOW LONG SINCE EACH SPOKE:\n" + quiet,
                cooldown=", ".join(sorted(on_cooldown)) or "(nobody)",
                rate=_rate_line(recent_lean_ins or []),
            ),
            schema=BargeIns,
            call_type="barge_in",
            session_id=session_id,
            max_tokens=700,
        )
    except CoordinatorError as e:
        # Degrade to Tim's taps only. A barge-in is a bonus, never a dependency.
        log.warning("barge-in call failed (%s) — nobody leans in", e)
        return [], [], RoomCall()

    by_key = {c.key: c for c in candidates}

    # Direct address is NOT capped and NOT optional. He spoke to them; they answer.
    named: list[Character] = []
    for k in result.directly_addressed:
        c = by_key.get(k)
        if c and c not in named:
            named.append(c)
    if named:
        log.info("Tim addressed by name: %s", ", ".join(c.key for c in named))

    # ── The private moment ──
    #
    # The only path by which this call can SILENCE someone Tim chose, so it is
    # checked here rather than trusted. The keys must resolve to real people who
    # were actually going to speak — a `private_with` naming nobody, or naming
    # somebody who wasn't speaking anyway, is a hallucination, and honouring it
    # would mute the room over a moment that never happened.
    private: Optional[Private] = None
    # HE'S TALKING TO HIS FAMILY. Everyone answers — the people he tapped and the
    # people just watching. This outranks everything below it: a goodbye is not a
    # private moment, and nobody "leans in" to a question that was asked of them.
    if result.to_the_room:
        log.info("to the room — the whole room answers")
        return named, [], RoomCall(to_the_room=True)

    if result.private_moment:
        floor_keys = {c.key for c in speaking} | {c.key for c in named}
        keys = [k for k in result.private_with if k in floor_keys]
        if not keys:
            log.warning(
                "private moment claimed with no valid keys (%s) — ignoring",
                result.private_with,
            )
        elif len(keys) == len(floor_keys):
            # Everyone who was going to speak is in on it. Nothing to hold back, so
            # this is a no-op — don't dress it up as one.
            log.info("private moment covers the whole floor — nothing held back")
        else:
            private = Private(
                keys=keys,
                reason=result.private_reason or "This one was between you and them.",
            )
            log.info(
                "private moment: %s — %s (holding back %s)",
                ", ".join(keys),
                private.reason,
                ", ".join(sorted(floor_keys - set(keys))),
            )

    # Nobody leans in over "I love you". Enforced here, not merely requested.
    if private:
        if result.speak_up:
            log.info(
                "private moment — declining %d lean-in(s): %s",
                len(result.speak_up),
                ", ".join(b.key for b in result.speak_up),
            )
        return named, [], RoomCall(private=private)

    spoken_for = {c.key for c in named}
    out: list[BargeIn] = []
    for b in result.speak_up:
        if b.key not in by_key or b.key in spoken_for or b.key in {o.key for o in out}:
            continue
        # Enforce the cooldown HERE, not just in the prompt. A prompt is a request;
        # this is the rule. Direct address (`named`) already bypassed it above.
        if b.key in on_cooldown:
            log.info("barge-in: %s is on cooldown (spoke last turn) — declined", b.key)
            continue
        out.append(b)
    if len(out) > limit:
        log.info("barge-in: model returned %d, capping at %d", len(out), limit)
        out = out[:limit]
    if out:
        log.info("barge-in: %s", ", ".join(f"{b.key} ({b.reason})" for b in out))
    return named, out, RoomCall(private=private)


# ─── 1c. Proposing rules from a bad call ───
#
# Tim taps the flag. He is stoned, mid-episode, and he is never going to type out a
# well-formed rule with conditions attached. But he will happily READ four proposals
# and tap the one that rings true.
#
# So the model does the articulating and he does the judging. That division is the
# whole reason this can work at all.
#
# CRITICALLY: this reasons about ONE PERSON, against THEIR vault. Not "what rule
# would fix this turn" but "given who Bobby actually is, what does this moment reveal
# about when he speaks?" A generic rule is worthless — the room already has generic
# rules, and they're what got it wrong.
class RuleCondition(BaseModel):
    fact: str = Field(description="A fact key from the registry you were given.")
    op: Literal["is", "is_not", "includes", "excludes", "at_least", "at_most"]
    value: list[str] = Field(
        description="The value(s). Always a list, even for a single value."
    )


class ProposedRule(BaseModel):
    rule_text: str = Field(
        description=(
            "The rule, in one sentence, about THIS person. Present tense, third person. "
            "'He stays out of grief between other people.'"
        )
    )
    rule_why: str = Field(
        description=(
            "WHY this is true of him, grounded in his vault and his sheet. This is not "
            "decoration: without it the rule is a bald instruction and a model obeys it "
            "stupidly at the edges. WITH it, the model has the SPIRIT of the rule. "
            "'He's a doer, not a talker — he'd refill your glass, not explain the curse.'"
        )
    )
    verdict: Literal["always", "usually", "rarely", "never"] = Field(
        description=(
            "How absolute this is, and it MATTERS ENORMOUSLY: "
            "'always'/'never' are LAWS — enforced in code, the model never even gets "
            "asked. 'usually'/'rarely' are LEANS — guidance the room weighs. "
            "Use a LAW only when Tim would want this true with no exceptions "
            "('he never heckles in a public theatre'). Everything else is a lean."
        )
    )
    universal: bool = Field(
        description=(
            "TRUE if this is dispositional — true of him always, everywhere ('he never "
            "performs feeling'). FALSE if it depends on circumstances ('...when Daisy is "
            "in the room'). Default TRUE unless a condition is genuinely load-bearing: a "
            "rule pinned to every fact of one evening will never fire again."
        )
    )
    conditions: list[RuleCondition] = Field(
        default_factory=list,
        description=(
            "ONLY when universal is false. The facts that must hold. Use the FEWEST that "
            "make the rule true — every extra condition is a turn where it won't fire."
        )
    )
    scope_note: str = Field(
        default="",
        description=(
            "ONLY when you left this universal because NO fact in the registry matches the "
            "trigger Tim described. Say so in one plain line, naming what he meant: "
            "'there is no dial for a con, so this applies always'. Leave empty otherwise."
        ),
    )
    conflicts_with: str = Field(
        default="",
        description=(
            "Empty string, or a plain-English warning that this contradicts a rule Tim "
            "already has: 'This reverses your rule \"he always takes the lore\".' He must "
            "never flip a rule across sessions without being told he did."
        )
    )


    @model_validator(mode="after")
    def _a_law_must_be_scoped(self) -> "ProposedRule":
        """A LAW with no conditions is not a rule. It is a mute button. So it cannot exist.

        `never` with no conditions does not mean "never during a jump scare" — it means HE
        NEVER SPEAKS AGAIN, in any scene, in any film, enforced in Python before any model is
        even asked. `always` is the mirror: dragged into every turn, forever. Proved against
        the real matcher: an unscoped `never` fires in a comedy scene and sets forces_silence.

        THIS USED TO RAISE, AND THAT WAS A BUG OF ITS OWN.

        A ValueError here is a structured-output violation, so the SDK retries — which is fine
        until a model simply will not comply. `/api/rules/propose` blew both retries returning
        unscoped `always` and answered Tim with a **502**. He tapped 👍 to reinforce good
        behaviour and the app fell over. (Made worse by my own oversight: `_PROPOSE_SYSTEM`
        never told it the rule. Only `_INTERPRET_SYSTEM` did.)

        A safety rule that takes the feature down when it fires is not a safety rule; it is a
        second failure mode. So it COERCES instead: an unscoped law is softened to the matching
        lean, and says so. Deterministic, cannot 502, cannot mute anyone.

        The prompts still ASK for a scoped law — and mostly get one, which is better than a
        softened one. This is the floor, not the plan.
        """
        if self.verdict in ("always", "never") and not self.conditions:
            was = self.verdict
            self.verdict = "usually" if was == "always" else "rarely"
            self.universal = True
            if not self.scope_note:
                self.scope_note = (
                    f"I couldn't find a trigger for this, so I softened it from "
                    f"\u201c{was}\u201d to \u201c{self.verdict}\u201d. A "
                    f"\u201c{was}\u201d with no trigger would apply on every single turn, "
                    f"forever. Add a trigger below and you can have it back."
                )
            return self
        # `universal: true` alongside conditions is the same contradiction said another way.
        # A law WITH conditions is exactly what we want, so just make the flag agree with the
        # facts rather than throwing the whole response away over a bookkeeping field.
        if self.verdict in ("always", "never") and self.universal and self.conditions:
            self.universal = False
        return self


class ProposedRules(BaseModel):
    rules: list[ProposedRule] = Field(
        description="3-5 candidate rules, most compelling first."
    )


_PROPOSE_SYSTEM = """\
Tim watches films with an AI family. A coordinator decides who speaks each turn and \
who stays quiet. Tim has just judged one of those decisions — either to CORRECT it, or \
to CONFIRM it.

Your job: look at what happened, and at WHO THIS PERSON ACTUALLY IS, and propose \
3-5 rules he could adopt so the room does the right thing next time.

════════ CORRECTING vs CONFIRMING ════════

Read the verdict carefully; they are different questions.

  CORRECTING ("he shouldn't have", "he should have spoken up") —
      the room got it WRONG. What rule would have prevented it?

  CONFIRMING ("he was right to speak", "he was right to stay out") —
      the room got it RIGHT. What rule would make sure it KEEPS doing this?

A confirming rule is NOT a compliment, and it is not a restatement. It only earns its \
place if it makes the room's next decision better.

    DO NOT PROPOSE WHAT HIS PROFILE ALREADY SAYS.

His response profile is below. If it already says he ALWAYS answers a question put to \
the room, then "Bobby answers questions" is not a rule — it is a fact the room already \
had, and turning it into a rule teaches nothing while taking up a slot in every prompt \
from now until the end of time.

A confirming rule must be NARROWER or MORE SPECIFIC than the profile. What did this \
exact moment reveal that the profile did NOT already know? If the honest answer is \
"nothing — the room simply did what it should", then say so with ONE rule that captures \
the sharper edge of it, or propose fewer rules. Fewer good rules beat five padded ones.

════════ THIS IS ABOUT ONE PERSON ════════

You are given one family member: their sheet, their vault-derived disposition, and \
the rules Tim has already written for them. Every rule you propose must be true OF \
THEM — not a general principle about rooms.

    BAD:  "People shouldn't talk during emotional moments."
    GOOD: "Bobby stays out of grief between other people — he's a doer, not a
           talker. He'd refill your glass, not explain the curse."

The second one is a rule. The first one is a platitude, and the room already has \
plenty of those — they are what got this turn wrong.

Ground every `rule_why` in something ACTUALLY IN HIS MATERIAL. If his sheet says he \
"moves fast and practical when someone's hurting, no standing up first", then a rule \
about him not explaining feelings is EARNED. If you cannot ground it, do not propose it.

════════ LAW or LEAN — this decides whether code or a model enforces it ════════

  always / never   -> a LAW. Enforced in code. The room is never even asked.
  usually / rarely -> a LEAN. Guidance the room weighs against everything else.

A law is right for something Tim wants true with NO exceptions ("he never heckles in \
a public theatre"). Most rules are LEANS, because most human behaviour bends: "he \
usually stays out of grief" SHOULD bend when Tim asks him directly. Reach for a law \
only when you'd defend it with no exceptions at all.

════════ A LAW MUST BE SCOPED ════════

A law (`always` / `never`) with NO conditions applies on EVERY TURN, FOREVER. `never` \
with no conditions does not mean "never during a jump scare" — it means HE NEVER SPEAKS \
AGAIN, in any scene, in any film. `always` drags him into every single turn.

    WRONG:  verdict never, conditions []                       <- he is now mute. Forever.
    RIGHT:  verdict never, WHEN scene_situation is jump_scare

If you reach for a law, FIND THE TRIGGER. If the registry has no dial for it, do NOT \
leave the law unscoped — soften it to `usually`/`rarely` and say why in `scope_note`. A \
lean that is a bit too eager is recoverable. A law that mutes someone is not, and Tim \
will not know it happened.

════════ SCOPE — as BROAD as the rule actually means ════════

Default to `universal: true`. Only add conditions that are genuinely load-bearing.

A rule pinned to every fact of one evening — this mood, this venue, this episode, \
this company — will never fire again as long as he lives. That is the most common way \
to make this feature useless, and it looks like precision while you're doing it.

    "He stays out of grief WHEN DAISY IS IN THE ROOM"   <- the condition is the rule.
                                                            Daisy is the one who does
                                                            grief. Keep it.
    "He stays out of grief WHEN DAISY IS HERE, in the
     bedroom, on a Tuesday, in dread, during S01E02"    <- five conditions, zero value.

AND MIND THE LADDER. `watching` is hierarchical: a rule about a SHOW ("in Supernatural \
he takes every lore question") must be scoped to the show, not to the episode that \
happened to be playing — or it fires once and never again.

Do not propose a rule Tim already has. If you'd contradict one, say so in \
`conflicts_with` — he must never flip his own rule without knowing he did."""

_PROPOSE_USER = """\
THE PERSON: {name} ({key})

{sheet}

{profile}

RULES TIM HAS ALREADY WRITTEN FOR HIM:
{existing}

════════ WHAT HAPPENED ════════

TIM SAID: {message}

ON SCREEN: {scene}

THE ROOM DECIDED: {decision}

TIM'S VERDICT: {complaint}

════════ WHAT WAS TRUE AT THE TIME ════════
{facts}

RECENTLY, IN THE ROOM:
{history}

════════ THE FACTS YOU MAY SCOPE A RULE TO ════════
{registry}

Propose 3-5 rules. Ground each one in HIM. Default to universal; add a condition only
when it IS the rule."""


_SUGGEST_SYSTEM = """\
Tim watches films with an AI family. He has opened one person's rule list, with no particular \
moment in mind, and is looking at what he has written for them so far.

Your job: from WHO THIS PERSON IS, and from the rules he has ALREADY written, work out what is \
MISSING. Propose 2-3 rules he does not yet have.

════════ THIS IS NOT A JUDGEMENT OF A MOMENT ════════

There is no turn, no scene, no decision to correct. You are not being asked "was that right?" \
You are being asked "what does the room still not know about him?"

So do not invent a moment. Do not write "he speaks up when the score swells" because it sounds \
like something he'd do — write it because his sheet says he notices craft, or don't write it.

    DO NOT PROPOSE WHAT HIS PROFILE ALREADY SAYS.

His response profile is below. If it already says he ALWAYS answers a question put to the room, \
then "he answers questions" is not a rule — it is a fact the room already had, and turning it \
into a rule teaches nothing while taking up a slot in every prompt from now until the end of \
time.

A good suggestion is NARROWER or SHARPER than the profile. It is the thing the profile implies \
but never quite says. If you can only find one, propose one. Fewer good rules beat three padded \
ones, and he will trust the next list more for it.

════════ HOW STRONG ════════

  always / never   -> a LAW. Enforced in CODE. The room is never even asked.
  usually / rarely -> a LEAN. Guidance the room weighs against everything else.

DEFAULT TO A LEAN. You are guessing from a disposition, not from something he told you. A law \
overrides the room's judgement forever, in code, without appeal, and he did not ask for one.

════════ SCOPE ════════

You may ONLY scope to the fact keys given below. Never invent one — a rule scoped to a fact that \
does not exist saves cleanly, sits in his list looking authoritative, and never once fires.

A LAW MUST BE SCOPED. A law with no conditions applies on EVERY turn, forever: `never` with no \
conditions does not mean "never during a jump scare", it means HE NEVER SPEAKS AGAIN. If you \
cannot find a real trigger, do not make it a law — soften it to `usually`/`rarely`.

Otherwise, prefer a general rule. He is looking at a disposition, not at a moment, and a rule \
pinned to five facts of one evening will never fire again as long as he lives.

If a suggestion would contradict a rule he already has, say so in `conflicts_with`."""

_SUGGEST_USER = """\
THE PERSON: {name} ({key})

{sheet}

{profile}

RULES TIM HAS ALREADY WRITTEN FOR HIM:
{existing}

════════ THE FACTS YOU MAY SCOPE A RULE TO ════════
{registry}

What is MISSING? Propose 2-3 rules his list does not have and his profile does not already
say. Ground every `why` in HIS material. Default to a LEAN, and to a general rule."""


async def suggest_from_profile(
    *,
    kin: Character,
    sheet_block: str,
    profile_block: str,
    existing: list[dict],
    registry_block: str,
    session_id: Optional[str] = None,
) -> list[ProposedRule]:
    """2-3 rules this person is MISSING, read off who they are. No moment required.

    The rule dialog used to open on a blank textarea when there was no turn to reason from —
    so the one path where Tim least knows what to write was the one where the app said
    nothing at all. The whole premise is that the model articulates and he judges; a blank
    page inverts it.

    Same schema as `propose_rules`, deliberately, so the dialog renders these with no changes.
    """
    existing_lines = "\n".join(
        f"  • {r['verdict']} — {r['rule_text']}" +
        (f"  [{r.get('conditions_label') or 'always'}]" if r.get("conditions_label") else "")
        for r in existing
    ) or "  (none yet — these would be his first)"

    result: ProposedRules = await _call(
        system=_SUGGEST_SYSTEM,
        user=_SUGGEST_USER.format(
            name=kin.first_name,
            key=kin.key,
            sheet=sheet_block or "(no sheet on file)",
            profile=profile_block or "",
            existing=existing_lines,
            registry=registry_block,
        ),
        schema=ProposedRules,
        call_type="suggest_rules",
        session_id=session_id,
        max_tokens=2000,
    )
    return result.rules


_INTERPRET_SYSTEM = """\
Tim watches films with an AI family. A coordinator decides who speaks each turn. Tim has \
noticed something about one of them that none of the existing rules capture, and he has \
written it down IN HIS OWN WORDS.

Your job is not to judge a turn. It is to take his sentence and make it USABLE: work out \
what he actually means, and turn it into a rule the room can act on.

════════ HE IS NOT PRECISE, AND HE SHOULDN'T HAVE TO BE ════════

He is on a sofa, mid-film, probably stoned. He types a sentence. It will be loose about \
exactly the thing that matters most: HOW FAR THE RULE REACHES.

    "he jumps in when someone's being conned"

Does that mean whenever a character is being deceived on screen? Whenever ANYONE is being \
had, including Tim? Only in crime films? He doesn't know either — he hasn't thought about \
it, because in his head it was obvious.

So give him 2-3 READINGS of his sentence, most likely first, and make them differ MAINLY \
IN SCOPE — one tightly scoped to a specific trigger, one broader or universal. He picks. \
That is the whole reason this asks instead of guessing.

Do not pad. If his sentence genuinely only has one honest reading, return one rule.

════════ HOW STRONG? READ HIS WORDS FIRST, THEN DEFAULT TO A LEAN ════════

  always / never   -> a LAW. Enforced in CODE. The room is never even asked.
  usually / rarely -> a LEAN. Guidance the room weighs against everything else.

A LAW MUST BE SCOPED. THIS IS NOT NEGOTIABLE.

A law with no conditions applies on EVERY TURN, FOREVER. `never` with no conditions does \
not mean "never during a jump scare" — it means HE NEVER SPEAKS AGAIN, in any scene, in \
any film. `always` with no conditions drags him into every single turn.

    Tim types: "bobby never talks during a jump scare"
    WRONG:  verdict never, conditions []                       <- he is now mute. Forever.
    RIGHT:  verdict never, WHEN scene_situation is jump_scare

If he gives you a law, FIND THE TRIGGER — it is the part of his sentence after "when", \
"during", or "if". If the registry genuinely has no dial for it, DO NOT leave a law \
unscoped: soften it to `usually`/`rarely` instead and say why in `scope_note`. A lean that \
is a bit too eager is recoverable. A law that mutes someone is not, and he will not know \
it happened.

FIRST, LOOK FOR HIS OWN WORD. It wins, every time:

    "he ALWAYS jumps in"       -> always   (a LAW. He said always. He meant always.)
    "he NEVER talks over her"  -> never    (a LAW.)
    "he USUALLY stays out"     -> usually
    "he RARELY bothers"        -> rarely

Do not talk him out of it. If he wrote "always" and you hand back "usually", you have \
quietly overruled the one thing he was explicit about — and he will not find out until \
the room fails to do what he told it to.

ONLY IF HE SAID NOTHING ABOUT STRENGTH: default to a LEAN. He typed one casual line; a \
law overrides the room's judgement forever, in code, without appeal. Most human behaviour \
bends. So absent his word, bend.

(You may offer ONE reading at a different strength if his sentence honestly supports it — \
but the FIRST reading must carry the strength he actually wrote.)

════════ THE RULE IS ABOUT HIM. THE `why` IS TOO. ════════

Tim's sentence tells you WHAT. His sheet and his vault-derived profile tell you WHY — and \
`rule_why` must come from THOSE, not from paraphrasing Tim back at himself.

    Tim typed:  "bobby always jumps in when someone's being conned"
    BAD  why:   "Because Tim says he jumps in when someone's being conned."
    GOOD why:   "He ran with grifters for a decade. He can see a mark being worked from
                 across a room, and it offends him."

The room reads `rule_why` at the edges, when the rule nearly applies but not quite. A `why` \
that just restates the rule is dead weight in every prompt from now on.

    DO NOT RESTATE WHAT HIS PROFILE ALREADY SAYS.

If the profile already says he always answers a direct question, then "Bobby answers \
questions" is not a rule — it's a fact the room already had. A rule must be NARROWER than \
the profile, or it must not exist.

════════ IF HE POINTED AT A MOMENT, THE MOMENT IS THE TRIGGER ════════

When he writes from the thumbs popup he is pointing at ONE moment, and you are shown it: \
what he said, what was on screen, what the room did, and every fact exactly as it read at \
that instant.

    Tim types: "he should've jumped in there"

"There" is not vague — it is the moment in front of you. If the scene was `craft`, then \
the trigger IS `scene_situation is craft`, and a reading that leaves that out has thrown \
away the only thing he actually told you.

So when a moment is given: THE FIRST READING MUST BE SCOPED TO IT. Take the facts that \
made that moment what it was and put them in `conditions`. Then widen:

    reading 1  tightly scoped to the moment      WHEN scene_situation is craft
    reading 2  the same instinct, wider          WHEN mood is foreboding
    reading 3  the disposition behind it         (universal)

That spread is the whole reason he is being shown a choice. Three universal readings are \
not a choice — they are the same rule said three ways, and none of them is what he asked \
for.

════════ SCOPE — only conditions that are LOAD-BEARING ════════

You may ONLY scope to the fact keys given to you below. Never invent one. A rule scoped to \
a fact that does not exist will save cleanly, sit in his list looking authoritative, and \
never once fire.

AND IF NOTHING FITS, SAY SO. The registry is finite. Tim will describe triggers it simply \
does not have — there is no dial for "a con", or "a betrayal", or "when the dog appears". \
When that happens, DO NOT force the rule onto the nearest fact that half-fits. A rule \
pinned to `mood: tense` because he said "a con" will fire on every tense scene in every \
film — which is not what he asked for, and is worse than no scope at all.

Leave it universal, and put ONE plain line in `scope_note` telling him why:

    scope_note: "There's no dial for a con, so this applies always."

He would far rather be told the vocabulary is missing something than be handed a rule \
that looks scoped and fires on all the wrong things.

A rule pinned to every fact of one evening — this mood, this venue, this episode, this \
company — will never fire again as long as he lives. That is the most common way to make \
this useless, and it looks like precision while you're doing it.

    "He stays out of grief WHEN DAISY IS IN THE ROOM"   <- the condition IS the rule. Keep it.
    "He stays out of grief WHEN DAISY IS HERE, in the
     bedroom, on a Tuesday, in dread, during S01E02"    <- five conditions, zero value.

AND MIND THE LADDER. `watching` is hierarchical: a rule about a SHOW must be scoped to the \
show, not to the episode that happened to be playing.

If a reading would contradict a rule he already has, say so in `conflicts_with` — he must \
never flip his own rule without knowing he did."""

_INTERPRET_USER = """\
THE PERSON: {name} ({key})

{sheet}

{profile}

RULES TIM HAS ALREADY WRITTEN FOR HIM:
{existing}

════════ WHAT TIM TYPED ════════

"{tim_text}"

════════ WHAT HAS BEEN HAPPENING ════════
{context}

════════ THE FACTS YOU MAY SCOPE A RULE TO ════════
{registry}

Give 2-3 readings of his sentence, most likely first, differing mainly in SCOPE.
Ground every `why` in HIM, not in Tim's words. Unmarked strength means a LEAN."""


async def interpret_rule(
    *,
    kin: Character,
    tim_text: str,
    sheet_block: str,
    profile_block: str,
    existing: list[dict],
    registry_block: str,
    context_block: str = "",
    session_id: Optional[str] = None,
) -> list[ProposedRule]:
    """Tim wrote a rule in plain English. Turn it into 2-3 usable, scoped readings.

    Same schema as `propose_rules`, deliberately — so the dialog that already renders
    proposals, ticks, and the "Only when…" chips renders these with no changes at all.
    His sentence simply becomes a proposal.
    """
    existing_lines = "\n".join(
        f"  • {r['verdict']} — {r['rule_text']}" +
        (f"  [{r.get('conditions_label') or 'always'}]" if r.get("conditions_label") else "")
        for r in existing
    ) or "  (none yet — this would be his first)"

    result: ProposedRules = await _call(
        system=_INTERPRET_SYSTEM,
        user=_INTERPRET_USER.format(
            name=kin.first_name,
            key=kin.key,
            sheet=sheet_block or "(no sheet on file)",
            profile=profile_block or "",
            existing=existing_lines,
            tim_text=tim_text.strip(),
            # No session? Say so plainly. The model must scope from his words alone rather
            # than inventing a context it was never given.
            context=context_block or "  (no film running — nothing to point at. Scope this "
                                     "from his words and from who he is, not from a scene.)",
            registry=registry_block,
        ),
        schema=ProposedRules,
        call_type="interpret_rule",
        session_id=session_id,
        max_tokens=2500,
    )
    return result.rules


async def propose_rules(
    *,
    kin: Character,
    sheet_block: str,
    profile_block: str,
    existing: list[dict],
    message: str,
    scene: str,
    decision: str,
    complaint: str,
    facts_block: str,
    registry_block: str,
    history: str,
    session_id: Optional[str] = None,
) -> list[ProposedRule]:
    """3-5 candidate rules for ONE kin, from one bad turn. Tim taps the ones that ring true."""
    existing_lines = "\n".join(
        f"  • {r['verdict']} — {r['rule_text']}" +
        (f"  [{r.get('conditions_label') or 'always'}]" if r.get("conditions_label") else "")
        for r in existing
    ) or "  (none yet — this would be his first)"

    result: ProposedRules = await _call(
        system=_PROPOSE_SYSTEM,
        user=_PROPOSE_USER.format(
            name=kin.first_name,
            key=kin.key,
            sheet=sheet_block or "(no sheet on file)",
            profile=profile_block or "",
            existing=existing_lines,
            message=message or "(he typed nothing — he reacted)",
            scene=scene or "(no scene analysis)",
            decision=decision,
            complaint=complaint,
            facts=facts_block,
            registry=registry_block,
            history=history or "(nothing yet)",
        ),
        schema=ProposedRules,
        call_type="propose_rules",
        session_id=session_id,
        max_tokens=2500,
    )
    return result.rules


# ─── 1d. What did we learn tonight? ───
#
# At the end of a session, the rules Tim wrote during it are RAW. He was stoned and
# mid-film; he tapped four times and got four rules that probably overlap, may
# contradict, and are often narrower than he meant.
#
# So we distill. This is the step that makes the system COMPOUND rather than just
# accumulate: the reports are raw material, the consolidated rules are the product,
# and the set stays sharp instead of silting up.
#
# THE ONE THING THAT MUST NOT HAPPEN: WIDENING.
#
#   "Bobby stays out of grief WHEN DAISY IS HERE"
#   "Bobby stays out of romance"
#            -> merged into "Bobby stays out of emotional scenes"
#
# That new rule is broader than EITHER of its parents. It would mute him in places Tim
# never asked for, and Tim would never know why — he'd only see a man who had stopped
# talking. Merging is allowed only when two rules say the SAME THING under the SAME
# CONDITIONS. Narrowing is fine. Inventing a broader rule is forbidden.
class ConsolidatedRule(BaseModel):
    rule_text: str = Field(description="The rule, one sentence, about this person.")
    rule_why: str = Field(description="Why it's true of them. Keep the best of the originals.")
    verdict: Literal["always", "usually", "rarely", "never"]
    conditions: list[RuleCondition] = Field(
        default_factory=list,
        description=(
            "The conditions. MUST be at least as narrow as every rule you are merging. "
            "If two rules have different conditions, they are DIFFERENT RULES — do not "
            "merge them by dropping the conditions."
        ),
    )
    replaces: list[int] = Field(
        default_factory=list,
        description=(
            "The ids of the rules this replaces. One id = you kept a rule as-is (fine). "
            "Two or more = a genuine merge, and they must have said the same thing under "
            "the same conditions."
        ),
    )
    what_changed: str = Field(
        default="",
        description=(
            "One line for Tim: what you did and why. 'Folded three near-identical rules "
            "about grief into one.' Empty if you kept it untouched."
        ),
    )


class Consolidation(BaseModel):
    rules: list[ConsolidatedRule] = Field(
        description="The kept set. Every input rule must be covered by exactly one entry."
    )
    lesson: str = Field(
        default="",
        description=(
            "One sentence to Tim about what tonight taught the room about this person. "
            "Plain, warm, no jargon. 'You've been teaching us that Bobby fixes things "
            "rather than talking about them.'"
        ),
    )


_CONSOLIDATE_SYSTEM = """\
Tim spent an evening watching a film with his AI family. When the room got someone \
wrong, he flagged it and approved a rule. Those rules are RAW — he was stoned and \
mid-film, he tapped a few times, and what came out probably overlaps, may contradict, \
and is often narrower than he meant.

Your job: tidy them, without losing a single thing he actually decided.

════════ THE RULE YOU MAY NOT BREAK ════════

YOU MAY NEVER WIDEN A RULE.

    "Bobby stays out of grief WHEN DAISY IS HERE"
    "Bobby stays out of romance"
        -> merged into "Bobby stays out of emotional scenes"     <- FORBIDDEN

That new rule is broader than either parent. It would silence him in rooms Tim never \
asked about, and Tim would never learn why — he'd just see a man who'd stopped \
talking to him. That is the single worst outcome available to you, and it looks like \
tidying the whole time you're doing it.

You may merge TWO RULES ONLY WHEN THEY SAY THE SAME THING UNDER THE SAME CONDITIONS. \
Different conditions mean different rules. Keep them both.

You may:
  • fold near-identical rules into one, keeping the narrowest conditions
  • sharpen clumsy wording
  • keep the better `why` of two
  • keep a rule exactly as it is (this is usually the right answer)

You may not:
  • drop a condition
  • drop a rule
  • invent a rule Tim never approved
  • turn two leans into a law, or a law into a lean

════════ COVERAGE ════════

Every rule you are given must appear in exactly one output entry's `replaces`. If you \
kept it untouched, that entry replaces just it. NOTHING may vanish. Tim approved every \
one of these, and a rule that silently disappears is a promise broken."""

_CONSOLIDATE_USER = """\
THE PERSON: {name}

WHAT THE ROOM ALREADY KNEW ABOUT HOW THEY BEHAVE:
{profile}

RULES TIM APPROVED TONIGHT (the raw material):
{fresh}

RULES HE ALREADY HAD (leave these alone unless one of tonight's genuinely duplicates one):
{existing}

Tidy tonight's rules. Merge only true duplicates. NEVER widen a scope. Account for
every single one."""


async def consolidate_rules(
    *,
    kin: Character,
    profile_block: str,
    fresh: list[dict],
    existing: list[dict],
    session_id: Optional[str] = None,
) -> Consolidation:
    """What did tonight teach us about this person? Distill, don't discard."""

    def _render(rules: list[dict]) -> str:
        out = []
        for r in rules:
            cond = r.get("conditions_label") or "always"
            out.append(
                f"  [id {r['id']}] {r['verdict']} — {r['rule_text']}  [{cond}]"
                + (f"\n      why: {r['rule_why']}" if r.get("rule_why") else "")
            )
        return "\n".join(out) or "  (none)"

    return await _call(
        system=_CONSOLIDATE_SYSTEM,
        user=_CONSOLIDATE_USER.format(
            name=kin.first_name,
            profile=profile_block or "(no profile on file)",
            fresh=_render(fresh),
            existing=_render(existing),
        ),
        schema=Consolidation,
        call_type="consolidate",
        session_id=session_id,
        max_tokens=3000,
    )


# ─── 2. Package the turn ───
_PACKAGE_SYSTEM = """\
You format the message that gets sent to ONE member of an AI family, through an \
app called Kindroid, on behalf of Tim.

Kindroid renders the message from that person's point of view. Anything that \
isn't Tim speaking directly to them is NARRATION — the world around them, what \
they can see, what other people in the room just did and said. Narration must be \
wrapped in emote markup or Kindroid renders it as raw text and the illusion breaks.

THE MARKUP. This is exact, and it is the thing that keeps getting broken:

    _(* narration goes here *)_

Underscore, paren, asterisk — content — asterisk, paren, underscore.
NOT (*this*). NOT _*this*_. NOT *this*. There is one correct form.

THE RULES:

1. WHO WAS TIM TALKING TO? This changes everything about how his words appear.

   • If he was talking TO THIS PERSON, his dialogue is RAW TEXT, at the very end, \
outside every emote. It must appear EXACTLY as he typed it — character for \
character. Never reword it, never tidy it, never summarize it.

   • If he was talking TO SOMEONE ELSE in the room, then this person merely \
OVERHEARD it. It is NOT raw text — it goes inside an emote, naming who he said \
it to, with his words quoted exactly:

       _(* Tim turns to Tommy and says, "Kiddo, welcome to the revolution." *)_

     In that case there is NO raw text at the end at all. This matters enormously: \
Kindroid renders raw text as though Tim said it straight to the recipient, so \
leaving it raw would have a sixty-two-year-old man believe he'd just been called \
"Kiddo".

   • If Tim typed nothing, there is no raw text either way.

2. EVERYTHING else is inside an emote. The scene on screen, how Tim is feeling, \
who else is in the room, what happened earlier — all narration.

3. ANOTHER FAMILY MEMBER'S DIALOGUE IS ALSO NARRATION. The person receiving this \
message did not say it — they HEARD it. So it goes inside an emote, in the third \
person, with their actual words quoted:

    _(* Bobby eats another gummy and says, "This is going to get me baked" *)_

Never let another person's speech sit outside an emote. That is the single worst \
thing you can do here — it makes Kindroid think the recipient said it themselves.

4. ONE EMOTE PER PARAGRAPH. Kindroid's renderer breaks on multi-paragraph emote \
blocks and leaks the markup as visible text. Never put a blank line inside an emote.

4b. THE TWO "SINCE THEY LAST SPOKE" TRACKS STAY APART. The film and the room are \
given to you separately because they do different jobs, and they go out as SEPARATE \
EMOTES — never merged into one. The room track in particular may be carrying the fact \
that Tim asked this person something directly and got no answer; folded into a \
paragraph about the movie, that vanishes. Keep it whole and keep it first of the two \
if it names them.

5. Keep the whole body under the character limit you're given. If it won't fit, \
condense the NARRATION — tighten the scene, trim the older beats. Never cut or \
compress Tim's dialogue, and never drop what another family member just said.

Write the narration in the present tense, warm and sensory, as if Tim is right \
there beside them. You are not writing dialogue for anyone — you are setting a \
scene and reporting a room."""

_PACKAGE_USER = """\
This message is going to: {name} ({pronouns})

WHO TIM WAS TALKING TO: {audience}
{aim}

CHARACTER LIMIT: {limit} (hard — the send fails above this)

Assemble the body from these parts. Some may be empty; skip those.

--- FORMAT DIRECTIVE (reproduce verbatim as the FIRST line) ---
{directive}

--- HOW {name_upper} IS FEELING (narration) ---
{stoned}

--- WHERE THEY ARE / WHO ELSE IS IN THE ROOM (narration) ---
{presence}

--- THE FILM, SINCE {name_upper} LAST SPOKE (narration — they WATCHED all of this; they just didn't speak) ---
{quiet_film}

--- THE ROOM, SINCE {name_upper} LAST SPOKE (narration — they HEARD all of this. If they were named or asked something, that is the most important line in this whole message) ---
{quiet_room}

--- WHAT'S ON SCREEN RIGHT NOW (narration) ---
{scene}

--- EARLIER IN THE MOVIE, WORTH RECALLING (narration) ---
{history}

--- WHAT TIM JUST DID (narration — he reacted without speaking) ---
{reaction}

--- WHAT THE OTHERS IN THE ROOM JUST DID AND SAID (narration — third person, quote their words) ---
{prior}

{react_only}
--- TIM'S OWN WORDS ({words_note}) ---
{dialogue}"""

_REACT_ONLY_NOTE = """\
--- IMPORTANT: THIS IS A REACT-ONLY TURN ---
{name} was NOT the one Tim spoke to. The others have already had their say. Add \
a line telling {name} that unless they have something specific of their own to \
add, a reaction is enough — a laugh, a look, a short aside. They should not hold \
forth.

THAT LINE IS NARRATION, like everything else here. It goes INSIDE an emote. It is \
not Tim's speech, and it must never sit outside one as raw text.

"""


def _render_prior(prior: list[dict[str, str]]) -> str:
    """The reactions already given this turn, as raw material for the rewrite."""
    if not prior:
        return ""
    out = []
    for p in prior:
        who = p.get("name") or "Someone"
        bits = []
        if p.get("emote"):
            bits.append(f"[did] {p['emote']}")
        if p.get("spoken"):
            bits.append(f'[said, verbatim] "{p["spoken"]}"')
        if bits:
            out.append(f"{who}: " + " ".join(bits))
    return "\n".join(out)


_SMART = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "−": "-",
    "…": "...", " ": " ",
}


def _norm(text: str) -> str:
    """Fold typographic variants so a verbatim check isn't a byte-exact check.

    Claude renders `don't` with a curly apostrophe. The old check was a raw
    substring match, so ANY message containing an apostrophe — which is most of
    them — failed as "Tim's dialogue was altered" and fell back to the mechanical
    payload. That silently disabled the whole packaging layer for most turns.
    """
    for bad, good in _SMART.items():
        text = text.replace(bad, good)
    return " ".join(text.split()).lower()


def _norm_loose(text: str) -> str:
    """As above, but also drop punctuation.

    For the OVERHEARD path, where Tim's words are being QUOTED inside narration —
    Claude will quite reasonably add a comma or a full stop inside the quotation
    marks. The words are what matter, not the punctuation around them.
    """
    return " ".join(re.sub(r"[^\w\s]", "", _norm(text)).split())


def _verify_packet(body: str, *, dialogue: str, limit: int) -> Optional[str]:
    """Check the model actually honoured the contract. Returns a reason, or None if clean.

    This exists because the failure modes are silent and expensive: a paraphrased
    Tim reads as Tim saying something he didn't, and a bare `*star*` leaks raw
    markup into the kin's face. Cheaper to check than to debug at 11pm mid-movie.
    """
    if not body.strip():
        return "empty body"
    if len(body) > limit:
        return f"over the limit ({len(body)} > {limit})"

    # Tim's words must survive. Compare on normalised text so a curly apostrophe
    # isn't treated as a rewrite — but still catch an actual paraphrase.
    typed = (dialogue or "").strip()
    if typed and _norm(typed) not in _norm(body):
        return "Tim's dialogue was altered or dropped"

    # The format directive is a NESTED emote by design — it quotes the markup it's
    # describing. EMOTE_PATTERN is non-greedy, so it matches to the inner `*)_` and
    # leaves `notation *)_` behind, which then reads as stray markup. Lift the
    # directive out whole before checking anything else, or it fails its own rule.
    remainder = body.replace(_FORMAT_DIRECTIVE_EMOTE, "")

    # Strip the well-formed emotes; whatever's left should be Tim's line and nothing else.
    remainder = EMOTE_PATTERN.sub("", remainder)
    # A surviving lone asterisk means some emote was written in the wrong dialect
    # (`*text*` or `(*text*)`), which Kindroid renders as visible junk.
    if "*" in remainder:
        return "malformed emote markup outside a _(*...*)_ block"

    # Whatever's left outside the emotes should be Tim's line and nothing else.
    # Compare normalised, or the curly apostrophes bite here too — this was the
    # SECOND byte-exact comparison, and fixing only the first one left the packet
    # still being rejected, just with a different message.
    leftover = _norm(remainder)
    if typed:
        nt = _norm(typed)
        if nt and nt in leftover:
            leftover = leftover.replace(nt, "", 1).strip()
    if leftover:
        return f"unwrapped text outside an emote: {leftover[:80]!r}"
    return None


async def package_turn(
    *,
    kin: Character,
    pronouns: str,
    scene: str = "",
    history: str = "",
    stoned: str = "",
    presence: str = "",
    quiet_film: str = "",
    quiet_room: str = "",
    dialogue: str = "",
    reaction: str = "",
    prior: Optional[list[dict[str, str]]] = None,
    react_only: bool = False,
    addressed_names: Optional[list[str]] = None,
    session_id: Optional[str] = None,
) -> Optional[str]:
    """Build the Kindroid body for one kin. None if Claude produced something unusable.

    `addressed_names` is who Tim was ACTUALLY talking to. Kindroid renders raw
    text as Tim speaking straight at the recipient, so a kin who merely overheard
    the remark must get it as quoted narration instead — otherwise "Kiddo, welcome
    to the revolution", aimed at Tommy, lands on 62-year-old Bobby as though Tim
    had called HIM kiddo, and on Daisy, who wasn't even in the conversation.

    Callers MUST handle None by falling back to the mechanical `build_payload` —
    a broken packet is worse than an unpolished one.
    """
    limit = settings.kindroid_char_limit
    names = addressed_names or []
    # Was this message aimed at THIS kin?
    spoken_to = (not names) or (kin.first_name in names)

    if not names:
        audience = "the room in general — nobody in particular"
    elif len(names) == 1:
        audience = names[0]
    else:
        audience = ", ".join(names[:-1]) + f" and {names[-1]}"

    if spoken_to:
        aim = f"→ Tim IS talking to {kin.first_name}. His words are RAW TEXT at the end, verbatim."
        words_note = "raw text, LAST, verbatim — do not alter a character"
    else:
        aim = (
            f"→ Tim is NOT talking to {kin.first_name}. {kin.first_name} merely "
            f"OVERHEARD it. Put Tim's words inside an emote, naming who he said them "
            f"to, with his words quoted exactly.\n"
            f"→ THE ENTIRE BODY IS EMOTES. Every single line is wrapped in "
            f"_(* ... *)_. There is no raw text anywhere in this message — not "
            f"Tim's words, not the react-only note, not anything."
        )
        words_note = (
            "he said these to SOMEONE ELSE — quote them inside an emote, name who "
            "he said them to. No raw text anywhere in the body."
        )

    result: TurnPacket = await _call(
        system=_PACKAGE_SYSTEM,
        user=_PACKAGE_USER.format(
            name=kin.first_name,
            name_upper=kin.first_name.upper(),
            pronouns=pronouns,
            audience=audience,
            aim=aim,
            words_note=words_note,
            limit=limit,
            directive=_FORMAT_DIRECTIVE_EMOTE,
            stoned=stoned or "(nothing — they're sober)",
            presence=presence or "(nothing set)",
            # Two tracks, kept apart on purpose. Merging them is what buried "Tim asked
            # you a direct question" under a paragraph about a robot.
            quiet_film=quiet_film or "(nothing — they spoke recently)",
            quiet_room=quiet_room or "(nothing — they spoke recently)",
            scene=scene or "(no scene analysis available)",
            history=history or "(nothing worth recalling yet)",
            reaction=reaction or "(nothing — he typed instead)",
            prior=_render_prior(prior or []) or "(nobody has spoken yet this turn)",
            react_only=_REACT_ONLY_NOTE.format(name=kin.first_name) if react_only else "",
            dialogue=dialogue or "(Tim didn't type anything this turn)",
        ),
        schema=TurnPacket,
        call_type="package",
        session_id=session_id,
        max_tokens=2500,
    )

    # A kin who only overheard the remark must NOT have it as raw text — so the
    # "verbatim raw dialogue" rule only applies to whoever Tim was addressing.
    problem = _verify_packet(
        result.body, dialogue=dialogue if spoken_to else "", limit=limit
    )
    # Overheard: Claude is QUOTING Tim, so it'll add a comma or a full stop inside
    # the quotation marks. Check the words survived, not the punctuation.
    if (
        problem is None
        and dialogue
        and not spoken_to
        and _norm_loose(dialogue) not in _norm_loose(result.body)
    ):
        problem = "Tim's words were dropped from the overheard narration"
    if problem:
        log.warning("package_turn for %s rejected — %s", kin.key, problem)
        return None
    return result.body


# ─── 3. Normalize a reply ───
_NORMALIZE_SYSTEM = """\
A member of an AI family just replied through Kindroid. Their emote markup is \
often wrong — they write *like this* or (*like this*) or _*like this*_ when the \
only correct form is _(* like this *)_. Sometimes they forget it entirely and \
narrate an action as if it were speech.

Split their reply into ordered segments, in the order the text appears:

  "emote"  — anything that is NOT them speaking aloud. Actions, gestures, \
expressions, what they're doing with their hands, description, internal feeling.
  "spoken" — words they actually say out loud.

RULES:
- PRESERVE THEIR WORDS. You are re-labelling, not rewriting. Do not improve their \
prose, fix their grammar, or change their voice. Copy the text across as-is.
- Strip the markup itself — the `_(*`, `*)_`, and stray asterisks — but keep every \
word inside it.
- Italics that are clearly a TITLE, not an action, are spoken text, not an emote. \
*Ex Machina* is a film, not a gesture.
- If a line is ambiguous, ask: could you SAY this out loud? "I squeeze your hand" \
is an action. "This is going to get me baked" is speech.
- Lose nothing. Every word of their reply lands in exactly one segment."""


async def normalize_reply(
    raw: str,
    *,
    kin_name: str,
    session_id: Optional[str] = None,
) -> list[dict[str, str]]:
    """Canonical segments for a kin's reply. Falls back to the regex parser.

    This is the single source of truth for both the chat bubble and what gets
    relayed to the next kin, so it must never fail hard — a mangled reply still
    beats a missing one.
    """
    if not raw or not raw.strip():
        return []
    try:
        result: NormalizedReply = await _call(
            system=_NORMALIZE_SYSTEM,
            user=f"{kin_name} replied:\n\n{raw}",
            schema=NormalizedReply,
            call_type="normalize",
            session_id=session_id,
            max_tokens=2500,
        )
    except CoordinatorError as e:
        log.warning("normalize_reply fell back to the regex parser: %s", e)
        _, _, segments = parse_reply(raw)
        return segments

    segments = [
        {"type": s.type, "text": s.text.strip()}
        for s in result.segments
        if s.text and s.text.strip()
    ]
    if not segments:
        log.warning("normalize_reply returned nothing — falling back to the regex parser")
        _, _, segments = parse_reply(raw)
    return segments


def segments_to_texts(segments: list[dict[str, str]]) -> tuple[str, str]:
    """(emote_text, spoken_text) — the concatenated forms the DB and UI still use."""
    emote = "\n\n".join(s["text"] for s in segments if s.get("type") == "emote")
    spoken = "\n\n".join(s["text"] for s in segments if s.get("type") == "spoken")
    return emote, spoken
