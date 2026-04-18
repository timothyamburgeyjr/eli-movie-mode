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

LEVEL_LABELS = {0: "Sober", 1: "Lifted", 2: "Baked", 3: "Blasted"}


def compute_level(method: str, minutes_since: float) -> int:
    """Return level 0-3 for the given method and minutes since ingestion."""
    if method == "sober" or method not in METHOD_CURVES:
        return 0
    if minutes_since < 0:
        return 0
    curve = METHOD_CURVES[method]
    peak = curve["max_level"]

    if minutes_since < curve["onset"]:
        return 0
    if minutes_since >= curve["taper_end"]:
        return 0
    # Ramp up: onset → peak_start
    if minutes_since < curve["peak_start"]:
        span = max(1, curve["peak_start"] - curve["onset"])
        frac = (minutes_since - curve["onset"]) / span
        return max(1, round(peak * frac))
    # Plateau: peak_start → peak_end
    if minutes_since <= curve["peak_end"]:
        return peak
    # Taper: peak_end → taper_end
    span = max(1, curve["taper_end"] - curve["peak_end"])
    frac = 1 - ((minutes_since - curve["peak_end"]) / span)
    return max(0, round(peak * frac))


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
            # SQLite CURRENT_TIMESTAMP is naive UTC "YYYY-MM-DD HH:MM:SS"
            ts = datetime.fromisoformat(logged_at.replace(" ", "T"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            return 0, None, None
    else:
        ts = logged_at
    minutes_since = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
    level = compute_level(method, minutes_since)
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

# Tim's POV of passing/sharing the substance with Eli — first person,
# present tense, with Eli's reaction folded in.
_INGESTION_ACTIONS_ELI: dict[str, list[str]] = {
    "smoke": [
        "I pass you the pipe; you take a slow draw and exhale with a content sigh",
        "I hand you the joint; you take a long drag, eyes half-closing in the warmth",
        "I pack a fresh bowl and pass it to you; you light it and inhale deep",
        "I roll a joint for you and hold it out; you take the first hit like it's a gift",
        "I light the bowl and pass it over; you pull gently, smoke curling between us",
    ],
    "edible": [
        "I pass you a gummy; you pop it in your mouth and chew happily",
        "I hand you an edible; you eat it with an eager little grin",
        "I give you a gummy and you swallow it with a wink, already settling in for the ride",
        "I offer you an edible; you take it and tuck your legs under you, patient",
        "I hold out a gummy; you take it from my fingers and pop it in without hesitation",
    ],
    "dab": [
        "I load a dab for you and hand you the rig; you rip it cleanly, exhaling a thick cloud",
        "I set up a dab; you take the hit in one smooth pull, eyes fluttering shut",
        "I prep a dab and pass it over; you take it and sink back, instantly hit",
        "I torch the nail and hand it to you; you inhale deep and lean into the wave",
    ],
}


def ingestion_narration(method: str, who: str = "tim") -> str:
    """Return a first-person (Tim-POV) action line describing the moment of
    ingestion. When `who == "eli"`, describes Tim passing it to Eli with her
    reaction folded in. Empty string for sober or unknown methods.
    """
    source = _INGESTION_ACTIONS_ELI if who == "eli" else _INGESTION_ACTIONS_TIM
    options = source.get(method)
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


def reinforcement_narration(eli_level: int, method: Optional[str] = None) -> str:
    """Return a Tim-POV observation of Eli's current stoned state. Empty
    string when Eli is sober (nothing to reinforce).
    """
    if not eli_level or eli_level <= 0:
        return ""
    options = _REINFORCEMENT_LINES_ELI.get(eli_level) or _REINFORCEMENT_LINES_ELI[2]
    return random.choice(options)
