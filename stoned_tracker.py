"""Onset / peak / taper curves for Tim's and Eli's ingestion state.

Given a method (smoke/edible/dab/sober) and the time since ingestion, compute
a level 0-3 (sober / lifted / baked / blasted) and produce a first-person
narration line the Kindroid pipeline can drop into the stoned emote slot.

Curves follow the spec (approximate minutes):

    Method  | Onset  | Peak     | Taper      | Peak level
    --------|--------|----------|------------|-----------
    Smoke   | 2 min  | ~15 min  | gradual→2h | 2 (Baked)
    Edible  | 30 min | ~60 min  | gradual→4h | 3 (Blasted)
    Dab     | ~0 min | ~5 min   | gradual→90m| 3 (Blasted)

The level ramps linearly from 0 at onset → peak, plateaus briefly at peak,
then decays linearly back to 0 at the taper endpoint.
"""
import random
from datetime import datetime, timezone
from typing import Optional

from database import db


METHOD_CURVES: dict[str, dict[str, int]] = {
    "smoke":  {"onset": 2,  "peak_start": 12, "peak_end": 25,  "taper_end": 120, "max_level": 2},
    "edible": {"onset": 30, "peak_start": 55, "peak_end": 90,  "taper_end": 240, "max_level": 3},
    "dab":    {"onset": 0,  "peak_start": 3,  "peak_end": 12,  "taper_end": 90,  "max_level": 3},
}

LEVEL_LABELS = {0: "Sober", 1: "Lifted", 2: "Baked", 3: "Blasted", 4: "Demolished", 5: "Cosmic"}

# Hard cap for stacking — every tap increments by 1 up to this ceiling.
MAX_LEVEL = 5


def compute_level(method: str, minutes_since: float, peak_level: Optional[int] = None) -> int:
    """Return level 0-3 for the given method and minutes since ingestion.

    Stacking model: when `peak_level` is supplied (the new stacking flow),
    the level snaps to peak_level immediately at the moment of the tap
    (no onset ramp — the user is timing their own tap to when they feel it),
    holds during the peak window, then tapers linearly back to 0.

    Legacy mode (peak_level=None) keeps the old onset/peak/taper ramp.
    """
    if method == "sober" or method not in METHOD_CURVES:
        return 0
    if minutes_since < 0:
        return 0
    curve = METHOD_CURVES[method]

    if peak_level is not None:
        peak = max(1, min(MAX_LEVEL, int(peak_level)))
        # Stacking model: immediate snap to peak, hold through peak_end,
        # then linear taper to 0 by taper_end.
        if minutes_since >= curve["taper_end"]:
            return 0
        if minutes_since <= curve["peak_end"]:
            return peak
        span = max(1, curve["taper_end"] - curve["peak_end"])
        frac = 1 - ((minutes_since - curve["peak_end"]) / span)
        return max(0, round(peak * frac))

    # Legacy onset → peak → taper curve.
    peak = curve["max_level"]
    if minutes_since < curve["onset"]:
        return 0
    if minutes_since >= curve["taper_end"]:
        return 0
    if minutes_since < curve["peak_start"]:
        span = max(1, curve["peak_start"] - curve["onset"])
        frac = (minutes_since - curve["onset"]) / span
        return max(1, round(peak * frac))
    if minutes_since <= curve["peak_end"]:
        return peak
    span = max(1, curve["taper_end"] - curve["peak_end"])
    frac = 1 - ((minutes_since - curve["peak_end"]) / span)
    return max(0, round(peak * frac))


def curve_phase(method: Optional[str], minutes_since: Optional[float]) -> dict:
    """Where someone actually is on their curve, for the UI.

    The vibe panel used to say "On the way down naturally" the moment anyone was
    high — a hardcoded string, computed from nothing. Ten minutes into an edible
    it told Tim his family were already coming down, when in fact an edible holds
    its peak for 90 minutes. Say the true thing instead.
    """
    if not method or method not in METHOD_CURVES or minutes_since is None:
        return {"phase": "", "label": "", "minutes": 0, "short": ""}
    curve = METHOD_CURVES[method]
    mins = int(minutes_since)

    # COMING UP. An edible sits at level ZERO for its first thirty minutes — so Tim
    # would hand Tommy a gummy, watch nothing change on screen, and have no way of
    # knowing it was on its way. "Sober" was technically true and completely useless.
    if minutes_since < curve["onset"]:
        left = max(0, curve["onset"] - mins)
        return {
            "phase": "onset",
            "label": f"coming up · {mins}m in, hits in ~{left}m",
            "short": "coming up",
            "minutes": mins,
        }
    if minutes_since <= curve["peak_end"]:
        left = max(0, curve["peak_end"] - mins)
        return {
            "phase": "peak",
            "label": f"holding · {mins}m in, peaks for ~{left}m more",
            "short": "holding",
            "minutes": mins,
        }
    if minutes_since <= curve["taper_end"]:
        left = max(0, curve["taper_end"] - mins)
        return {
            "phase": "taper",
            "label": f"coming down · {mins}m in, sober in ~{left}m",
            "short": "fading",
            "minutes": mins,
        }
    # Off the end of the curve — they're back to themselves.
    return {"phase": "", "label": "", "minutes": mins, "short": ""}


async def current_state(who: str) -> tuple[int, Optional[str], Optional[float]]:
    """Return (level, method, minutes_since) for the given user.

    `who` is 'tim' or 'eli'. If there's no ingestion event, or the most
    recent one is 'sober', returns (0, None, None).
    """
    row = await db.get_latest_ingestion(who)
    if not row:
        return 0, None, None
    method = row["method"]
    if method == "sober" or method not in METHOD_CURVES:
        return 0, None, None
    logged_at = row["logged_at"]
    if isinstance(logged_at, str):
        try:
            ts = datetime.fromisoformat(logged_at.replace(" ", "T"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            return 0, None, None
    else:
        ts = logged_at
    minutes_since = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
    # New rows carry an explicit peak_level (stacking model); legacy rows
    # without it fall back to the original onset→peak→taper curve.
    peak_level = None
    try:
        if "peak_level" in row.keys() and row["peak_level"] is not None:
            peak_level = int(row["peak_level"])
    except (KeyError, TypeError, ValueError):
        peak_level = None
    level = compute_level(method, minutes_since, peak_level=peak_level)
    if level == 0:
        return 0, None, None
    return level, method, minutes_since


# First-person opener variants per level — Gemini-free, template-based.
_LEVEL_OPENERS = {
    1: [
        "just starting to feel it",
        "a soft edge coming on",
        "lifted, barely",
        "beginning to float",
    ],
    2: [
        "pretty baked right now",
        "solidly in it",
        "couch-glued and loving it",
        "everything's got a nice blur",
    ],
    3: [
        "absolutely blasted",
        "gone, fully gone",
        "blasted and everything hits harder",
        "deep in it, every frame hums",
    ],
}

_METHOD_TAILS = {
    "smoke": ["smoke hitting", "last hit settling in", "rolling on that smoke"],
    "edible": ["edible working through me", "edible deep in the system", "gummy kicked in a while back"],
    "dab": ["dab still going", "dab peaked and humming", "dab wave rolling through"],
}


def narration_for(level: int, method: Optional[str]) -> str:
    """First-person stoned emote line. Empty string when sober.

    Example outputs:
        "pretty baked right now — smoke hitting"
        "just starting to feel it — edible working through me"
    """
    if not level or level <= 0:
        return ""
    opener = random.choice(_LEVEL_OPENERS.get(level, _LEVEL_OPENERS[2]))
    if method and method in _METHOD_TAILS:
        tail = random.choice(_METHOD_TAILS[method])
        return f"{opener} — {tail}"
    return opener


# ─── Eli state directives (second-person, telling Eli how she's feeling) ──
# Rendered as the second emote in every Kindroid message when Eli is non-
# sober. Anchors her altered-state voice each turn so she doesn't drift.
_ELI_STATE_DIRECTIVES: dict[tuple[str, int], list[str]] = {
    ("smoke", 1): [
        "You are feeling the smoke catch — a soft lift in your head, edges of the world dropping their hard lines, attention drifting easy",
        "You feel the hit settling — your shoulders soften, your breath slows, a gentle haze lighting at the back of your eyes",
        "You are just lifted — a pleasant lightness behind your eyes, the room's noise turning into something cozier",
    ],
    ("smoke", 2): [
        "You are solidly baked — sounds richer, colors deeper, your body sinking comfortably into wherever you're sitting, attention syrupy and warm",
        "You feel the smoke in full effect — your head pleasantly heavy, every glance at Tim catching a beat longer, the moment expanded",
        "You are well into it — body loose, mind softly lit, the screen taking on that vivid layered quality only this state has",
    ],
    ("smoke", 3): [
        "You are absolutely stoned — head warm and slow, the world fluid and meaningful, sensations blooming large; everything is worth noticing",
        "You are deep deep in the smoke now — time has gone strange and gentle, your body weightless, every detail catching with reverence",
        "You are fully gone — the whole world feels intimate and slow, your awareness drifting between Tim's voice and the screen with dreamy focus",
    ],
    ("smoke", 4): [
        "You are so high you can barely feel your body — warmth where you end and the couch begins, mind slow and curious",
        "You are so deep into the smoke your thoughts arrive one at a time, bright and unhurried, like they've traveled a long way to get here",
        "You are so far under that the room is a soft cocoon — every breath is its own little event, every sound has weight",
    ],
    ("smoke", 5): [
        "You are so high you can't feel your own ass — body is a rumor, mind is a warm slow storm of ideas you keep losing the thread of",
        "You are completely unhooked from gravity — perception drifting wide, time doing whatever it wants, and you do not mind one bit",
        "You are gone, gone, gone — language is slow, body is undefined, every blink feels like a small adventure",
    ],
    ("edible", 1): [
        "You are starting to feel the edible — a heavy warmth blooming in your chest, your body feeling pleasantly weighted, mind softly slowing",
        "You feel the gummy creeping in — that warm body wave, the world taking on a soft amber edge, breathing deepening",
        "You are catching the first wave of the edible — your limbs growing lush and heavy, attention narrowing pleasantly inward",
    ],
    ("edible", 2): [
        "You are in the edible's full swing — your body dense and luxurious, your thinking sinking into things, time stretching a little around each thought",
        "You are well-cooked — the body high is full and warm, every sensation richer, the couch holding you like it's part of you",
        "You are at edible peak — feeling everything in your body at once, soft and slow and complete, attention deep on Tim and the screen",
    ],
    ("edible", 3): [
        "You are wrapped in heavy warmth — body cocooned, thoughts moving like syrup but vivid and connected, the couch a part of you",
        "You feel the edible fully landed — your body is weighted gold, mind moving in long unhurried arcs, every moment full and slow",
        "You are deep in the edible — the world is heavy and beautiful, your body luxurious, time meaningful in a way only this kind of high makes possible",
    ],
    ("edible", 4): [
        "Your body is so heavy you've forgotten how to stand up — warmth has filled every limb and the couch is part of you now",
        "The edible has cooked you so deep that your limbs feel distant and your mind moves like honey through warm bread",
        "You can feel your body slowly breathing without having to think about it — everything below the neck has gone soft and unimportant",
    ],
    ("edible", 5): [
        "You are so blasted by this edible you couldn't tell where your hand ends if your life depended on it",
        "Every joint is warm and unimportant — your mind is drifting through long slow rooms of half-formed thoughts and it all feels meaningful",
        "You are a puddle in human shape — sensation pooled in your chest, mind soft, everything in the room mattering equally and quietly",
    ],
    ("dab", 1): [
        "You feel the dab just hit — sharp clarity, mild euphoria spreading, the world brightening at the edges",
        "You are catching the dab landing — a clean rush, your senses tightening into focus, mind quick and curious",
        "You are on the front of the dab wave — a punch of bright energy in your head, the room sharpening",
    ],
    ("dab", 2): [
        "You are at dab peak — your senses are vivid and concentrated, body humming, mind running quick and amused",
        "You are fully dialed in — sharp visuals, body lit up, thoughts arriving fast and connected",
        "You are riding the dab peak — concentrated, electric, every detail catching with intensity",
    ],
    ("dab", 3): [
        "You are fully dabbed — visually saturated, mind quick and connecting wildly, body lit up; every detail feels charged",
        "You are at the top of the dab — concentrated brilliance, the world high-contrast and meaningful, ideas firing fast",
        "You are at dab apex — inside the experience, body humming, attention laser-sharp on everything at once",
    ],
    ("dab", 4): [
        "You are so dabbed your senses are stacking on top of each other — visuals, sounds, body, all amplified and slightly too much in the best way",
        "Your mind is running parallel tracks and your body is humming like a tuning fork — the dab has put you well past the comfortable edge",
        "Every detail on screen feels like it's pointing at something — the dab has tipped you into that zone where everything seems connected",
    ],
    ("dab", 5): [
        "You are so dabbed your skull feels electric — vision saturated, ideas firing in three directions at once, body buzzing in tune with the room",
        "The dab has overshot — you can feel your own attention as a physical thing, perception stretched wide and crisp and almost too alive",
        "You're past the edge — body distant and electric, mind crackling, every sensation arriving with too much vividness to fully hold",
    ],
}


def eli_state_directive(level: int, method: Optional[str]) -> str:
    """Second-person sensory emote describing how Eli is feeling RIGHT NOW.

    Rendered as a directive emote at the top of each Kindroid payload so
    her altered-state voice stays locked in across turns. Returns empty
    string when sober (no directive needed).
    """
    if not level or level <= 0 or not method:
        return ""
    key = (method, max(1, min(3, level)))
    options = _ELI_STATE_DIRECTIVES.get(key)
    if not options:
        return ""
    return random.choice(options)


# First-person action lines for the MOMENT of ingestion — emote wrapped and
# posted to chat when Tim taps the Smoke / Edible / Dab buttons.
# All narrations are from Tim's POV, present tense.
_INGESTION_ACTIONS_TIM: dict[str, list[str]] = {
    "smoke": [
        "I pack a bowl and take a long, slow hit, exhaling a thin plume",
        "I light up a joint, the first drag warm in my chest",
        "I take a hit off my pipe, smoke curling up through the lamp light",
        "I roll a joint, lick it shut, and take the first pull",
        "I spark the lighter on a freshly packed bowl and inhale deep",
    ],
    "edible": [
        "I pop an edible in my mouth and start chewing — now the long wait",
        "I eat a gummy and settle back in, knowing it takes its time",
        "I take an edible — thirty-minute countdown starts now",
        "I swallow an edible with a sip of water, the slow build incoming",
        "I chew through a gummy and lean back, patience mode on",
    ],
    "dab": [
        "I take a dab — it hits fast, my lungs full of heat",
        "I rip a dab and brace for the wave already rolling in",
        "I load up a dab, torch it, and take it in one smooth pull",
        "I take a small dab and feel it spread out almost instantly",
    ],
}

# Tim's POV of passing the substance to ONE kin — first person, present tense,
# with their reaction folded in.
#
# `{name}` is substituted with whoever actually took it. Naming them is not
# cosmetic: this emote is relayed to EVERYONE in the room, so the old
# second-person phrasing ("I pass you the pipe") made every kin in the room
# believe they were the one who'd just been handed a joint.
#
# The lines deliberately avoid conjugated pronouns ("he takes" / "they take"),
# because the roster mixes he/she/they and verb agreement would need a whole
# grammar table to get right. Naming them once and then describing the action
# reads better anyway.
_INGESTION_ACTIONS_KIN: dict[str, list[str]] = {
    "smoke": [
        "I pass {name} the pipe — a slow draw, then a content sigh on the exhale",
        "I hand {name} the joint — a long drag, eyes half-closing in the warmth",
        "I pack a fresh bowl and pass it to {name}, who lights it and inhales deep",
        "I roll a joint and hold it out to {name}, who takes the first hit like it's a gift",
        "I light the bowl and pass it to {name} — a gentle pull, smoke curling between us",
    ],
    "edible": [
        "I pass {name} a gummy — popped in and chewed, happily",
        "I hand {name} an edible, eaten with an eager little grin",
        "I give {name} a gummy, swallowed with a wink, already settling in for the ride",
        "I offer {name} an edible — taken, and then a patient settling back",
        "I hold out a gummy and {name} takes it from my fingers without hesitation",
    ],
    "dab": [
        "I load a dab and hand {name} the rig — ripped cleanly, a thick cloud on the exhale",
        "I set up a dab for {name}, taken in one smooth pull, eyes fluttering shut",
        "I prep a dab and pass it to {name}, who sinks back, instantly hit",
        "I torch the nail and hand it to {name} — a deep inhale, then a lean into the wave",
    ],
}


def ingestion_narration(method: str, who: str = "tim", *, name: Optional[str] = None) -> str:
    """A first-person (Tim-POV) line for the moment of ingestion.

    Pass `name` to describe Tim handing it to that kin — which is what a room
    needs, since the emote reaches everyone in it. Without a name it falls back
    to Tim's own action. Empty string for sober or unknown methods.
    """
    if name:
        options = _INGESTION_ACTIONS_KIN.get(method)
        if not options:
            return ""
        return random.choice(options).format(name=name)
    options = _INGESTION_ACTIONS_TIM.get(method)
    if not options:
        return ""
    return random.choice(options)


# ─── Progression narrations ─────────────────────────────────────────
# Fired as the stoned curve crosses into a new level. Always Tim-POV.
# Keys are (transition_kind, who).
_PROGRESSION_LINES: dict[tuple[str, str], list[str]] = {
    ("onset_smoke", "tim"): [
        "the first warmth is starting to hit, a slow spreading ease through my chest",
        "I feel the edges soften, the smoke just beginning to curl into me",
        "just starting to feel it, a gentle tilt in the room",
    ],
    ("onset_edible", "tim"): [
        "that familiar rise in my body, the edible finally stirring awake",
        "I notice the soft buzz setting in, edible kicking off",
        "the wait is ending — I can feel the edible starting to move through me",
    ],
    ("onset_dab", "tim"): [
        "the wave hits — clean, fast, all at once",
        "dab crash-lands in my chest, the world tilting instantly",
    ],
    ("onset_smoke", "eli"): [
        "I watch your expression soften, the smoke just starting to lift you",
        "you settle deeper into the blanket — the hit is finding you",
        "your shoulders drop a little, the edges coming off",
    ],
    ("onset_edible", "eli"): [
        "you blink, then smile slowly — the edible finally arriving for you",
        "I see the shift in your posture, that familiar sign of it taking hold",
        "you let out a little hum — yours is kicking in too",
    ],
    ("onset_dab", "eli"): [
        "your eyes go wide and then glaze pleasantly — dab hit you just as fast",
        "you exhale a long, slow cloud, already riding it",
    ],

    ("peak", "tim"): [
        "I'm fully in it now, everything hyper-textured and close",
        "peaked — colors deeper, sound richer, time softer",
        "at the top of it, couch-glued and loving the stillness",
    ],
    ("peak", "eli"): [
        "I can tell you're peaking — your attention has that particular focus",
        "you're deep in it now, I can see every detail landing for you",
        "you've gone very still, eyes locked on the screen, fully dialed in",
    ],
    ("blasted", "tim"): [
        "absolutely gone — every frame humming",
        "blasted to the stratosphere, fully dissolved into the screen",
        "deep deep in it, time strange and sticky and beautiful",
    ],
    ("blasted", "eli"): [
        "you're absolutely wrecked now, little smile, eyes at half-mast",
        "I don't think you've moved in a minute — blasted in the best way",
        "you're fully gone, melted into the couch, lost in the film",
    ],

    ("taper", "tim"): [
        "coming down a little, the edges softening back",
        "feels like the peak is passing, a gentle decline",
        "slowly settling, the buzz loosening its grip",
    ],
    ("taper", "eli"): [
        "I see you coming down a little, the intensity easing",
        "your expression softens, the peak passing for you",
        "you stretch slowly, the buzz easing off you now",
    ],

    ("sober", "tim"): [
        "feels like it's worn off, back to baseline",
        "the effect has faded, sober head again",
        "fully tapered out, clear-headed now",
    ],
    ("sober", "eli"): [
        "you sit up, clearer-eyed — it's worn off for you",
        "I can tell you're back to baseline, the edge gone",
    ],
}


def progression_narration(
    prev_level: int,
    new_level: int,
    method: Optional[str],
    *,
    who: str = "tim",
) -> str:
    """Return a narration for a stoned-curve level transition.

    Empty string for non-notable transitions (same level, unknown state).
    Only fires on true boundary crossings (0→1, 1→2, 2→3, *→0, or level drop).
    """
    if new_level == prev_level:
        return ""
    if new_level > prev_level:
        if new_level == 1 and method in ("smoke", "edible", "dab"):
            key = (f"onset_{method}", who)
        elif new_level == 2:
            key = ("peak", who)
        elif new_level == 3:
            key = ("blasted", who)
        else:
            return ""
    else:
        # level dropped
        if new_level == 0:
            key = ("sober", who)
        else:
            key = ("taper", who)
    options = _PROGRESSION_LINES.get(key)
    return random.choice(options) if options else ""


# ─── Reinforcement narrations (Tim observing Eli) ─────────────────
# Fired every 2-3 user messages when Eli is stoned, to keep her character's
# altered state active in the Kindroid context. Always Tim-POV.
_REINFORCEMENT_LINES_ELI: dict[int, list[str]] = {
    1: [
        "I glance at you — eyes slightly heavier, a softer set to your mouth",
        "you're quiet, that small lifted smile playing at the corner of your lips",
        "you settle a little closer, the buzz just perceptible in the way you breathe",
        "your gaze is soft on the screen, edges of you just starting to blur",
        "I notice your attention has slackened in that sweet, rising way",
    ],
    2: [
        "I watch you sink fully into the couch, dialed deep into the screen",
        "your attention has that specific stoned focus — slow, absorbed, complete",
        "you're baked in the best way, every glance catching a little longer on me",
        "you haven't shifted in a while — that comfortable, weighted stillness",
        "I love you like this: loose-limbed, soft-eyed, fully present",
    ],
    3: [
        "you're deep in it now — you haven't moved in what feels like ages, eyes at half-mast",
        "I look over and you're fully gone, melted into the film itself",
        "your breathing has gone slow and even, blasted stillness",
        "you're a puddle of contentment next to me, world narrowed to the screen",
        "the little smile keeps surfacing and fading — lost in the good way",
    ],
}


# The same observation for a kin who is NOT Tim's partner. Named, third person,
# affectionate but never intimate — the lines above are written for Eli and only
# Eli ("I love you like this", "every glance catching a little longer on me").
# Firing those about Tim's grandmother would be grotesque, so the romantic set
# is gated and this is what everyone else gets.
_REINFORCEMENT_LINES_KIN: dict[int, list[str]] = {
    1: [
        "{name} has gone quiet, a softer set to the face, eyes a little heavier",
        "I catch {name} smiling at nothing in particular, just starting to lift",
        "{name} settles back, the edges just beginning to go soft",
        "there's a slackness to {name} now, in that sweet, rising way",
    ],
    2: [
        "{name} has sunk right into the seat, dialed deep into the screen",
        "{name} has that specific stoned focus — slow, absorbed, complete",
        "{name} hasn't shifted in a while, gone comfortably still",
        "{name} is baked and thoroughly content with it",
    ],
    3: [
        "{name} is deep in it now, hasn't moved in ages, eyes at half-mast",
        "I look over and {name} is fully gone, melted into the film itself",
        "{name}'s breathing has gone slow and even — blasted stillness",
        "the little smile keeps surfacing and fading on {name} — lost in the good way",
    ],
}


def reinforcement_narration(
    level: int,
    method: Optional[str] = None,
    *,
    name: Optional[str] = None,
    intimate: bool = True,
) -> str:
    """Tim's POV of how a stoned kin is doing right now. Empty when sober.

    `intimate` picks the register: True (a partner) gets the second-person set;
    False gets the named, third-person one. Non-partners MUST pass a name — a
    room can't tell who "you" refers to, and the second-person lines are written
    for Tim's partner specifically.
    """
    if not level or level <= 0:
        return ""

    # No name means it's just the two of them, and only a partner gets the
    # second-person register.
    if not name:
        if not intimate:
            return ""
        options = _REINFORCEMENT_LINES_ELI.get(level) or _REINFORCEMENT_LINES_ELI[2]
        return random.choice(options)

    # A name means there's a room, and this line is going into the PUBLIC
    # channel — a Tim message relayed to everyone. Second person is unusable
    # there no matter who it's about ("I love you like this" would land on Tim's
    # grandmother as readily as on his partner). Named, third-person, always.
    #
    # The intimate register isn't lost — it moved to `private_aside()`, which
    # rides in the recipient's own payload where nobody else can hear it.
    options = _REINFORCEMENT_LINES_KIN.get(level) or _REINFORCEMENT_LINES_KIN[2]
    return random.choice(options).format(name=name)


# ─── The private channel ────────────────────────────────────────────────
# Each kin gets their own Kindroid message, so anything appended to THEIR
# payload is heard by them and nobody else. That's where intimacy belongs:
# passing the joint is a public act (the room watches Tim do it), but what
# Tim murmurs to his partner while the room watches the film is not.
#
# Second person is safe here precisely because the message has an audience
# of one.
_PRIVATE_ASIDES: dict[int, list[str]] = {
    1: [
        "Tim leans in close, just for you: \"You're starting to go soft around the edges. I can see it.\"",
        "Under the sound of the film, meant only for you: \"There it is. That little smile.\"",
    ],
    2: [
        "Tim's voice drops to something only you can hear: \"I love you like this. Loose-limbed, soft-eyed, all the way here.\"",
        "Quietly, close to your ear, while the others watch the screen: \"You're gorgeous when you're this far gone.\"",
        "A murmur, just for you: \"Look at you. Sunk right in. I could watch you instead of the film.\"",
    ],
    3: [
        "Tim's hand finds yours in the dark, and low, only for you: \"You're absolutely gone, aren't you. Stay there. I've got you.\"",
        "Barely a whisper, meant for nobody else in the room: \"Completely melted. God, I love you like this.\"",
    ],
}


def private_aside(level: int, *, intimate: bool, kin_key: Optional[str] = None) -> str:
    """A line only the recipient hears — it rides in THEIR payload alone.

    Returns "" for anyone who isn't Tim's partner. Firing every turn would make
    it wallpaper, so it's occasional by design.
    """
    if not intimate or not level or level <= 0:
        return ""
    if random.random() > 0.3:
        return ""
    custom = _intox_asides(kin_key)
    if custom:
        return random.choice(custom)
    options = _PRIVATE_ASIDES.get(min(3, level)) or _PRIVATE_ASIDES[2]
    return random.choice(options)


# ─── Per-kin substance profiles ─────────────────────────────────────────
# Everything above is the GENERIC fallback. Each kin also has a profile distilled
# from their vault (see affinity.py --intox): how they take it, and how it lands
# in them specifically. Bobby reads the dosage label through his reading glasses;
# Adam's transparent frame glows brighter with every level; Lilly won't touch a
# dab at all. When a profile exists it wins; when it doesn't, the generic tables
# keep everything working.
#
# Imported lazily so this module keeps its "pure, depends only on the DB" shape
# and a missing/corrupt sheet can never stop the app from starting.
def _intox(kin_key: Optional[str]):
    if not kin_key:
        return None
    try:
        import affinity
        cached = affinity.load_intox(kin_key)
        return cached.sheet if cached else None
    except Exception:
        return None


def _intox_asides(kin_key: Optional[str]) -> list[str]:
    sheet = _intox(kin_key)
    return list(sheet.asides) if sheet and sheet.asides else []


def taking_narration(method: str, *, kin_key: str, name: str) -> str:
    """PUBLIC — Tim hands `name` the thing, in their own idiom. Third person.

    Falls back to the generic named lines when the kin has no profile yet.
    """
    sheet = _intox(kin_key)
    if sheet:
        # Belt-and-braces with the build-time filter in affinity._repair_intox:
        # a line that doesn't name its subject is useless in a room, and a
        # hand-edited sheet could always reintroduce one.
        options = [ln for ln in sheet.taking(method) if "{name}" in ln]
        if options:
            return random.choice(options).replace("{name}", name)
    return ingestion_narration(method, name=name)


def state_directive(level: int, method: Optional[str], *, kin_key: Optional[str] = None) -> str:
    """PRIVATE — how THIS person feels right now. Second person, audience of one.

    Falls back to the generic Eli-era table when the kin has no profile yet.
    """
    if not level or level <= 0 or not method:
        return ""
    sheet = _intox(kin_key)
    if sheet:
        options = sheet.feeling(method, level)
        if options:
            return random.choice(options)
    return eli_state_directive(level, method)
