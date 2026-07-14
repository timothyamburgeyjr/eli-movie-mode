"""The emotion thesaurus — the vocabulary the reaction drawer presents from.

WHY A THESAURUS AND NOT A LIST
------------------------------
The old reaction bar hand-curated 24 emoji into 6 categories. It failed, and the
failure mode is instructive: a hand-curated list always has holes, and you only
find them one at a time, mid-movie, annoyed. Tim found two in about a minute —
there was no *love* at all, and "repulsed" was quietly standing in for "I want to
throw up", which is why it never felt right. It wasn't.

So this is not a menu. It's a **vocabulary**. Gemini ranks ~6 of these against the
target Tim picked; the rest sit behind "all emotions". The size of the list is
therefore nearly free: it costs ~800 prompt tokens (about $0.0002) and only six
ever reach the screen.

KEYED BY MOOD, ON PURPOSE
-------------------------
The 16 moods already exist (gemini_brain.VALID_MOODS), already have 16 colours and
16 gradients in dashboard.html. Keying the thesaurus by mood means one table does
three jobs:

  1. COLOUR   — tints the emoji button, so the picker looks like the rest of the app.
  2. MOOD     — the *film's* mood (from the draft) drives the glow. Note the
                direction: the reaction never sets the mood, the FILM does. This
                preserves CLAUDE.md Key Design Decision #12 ("tracks the movie's
                mood, not Tim's mood").
  3. GROUPING — how Gemini knows a Freddy kill offers HORROR and DREAD, not HUMOR.

THE NULL FAMILY IS LOAD-BEARING
-------------------------------
"Bored", "unimpressed", "that's lazy" are reactions to *bad filmmaking*. They have
no home among the 16, because the 16 describe what the FILM is doing, not what Tim
thinks of it. They carry mood=None and MUST NOT recolour the room. Being bored is
not a mood the movie is in.

Emoji repeat across families on purpose (😤 in tense/adrenaline/triumph). The label
and the family colour disambiguate. Don't force uniqueness at the cost of the right
word — the right word is the entire point of this module.
"""
from __future__ import annotations

from typing import NamedTuple, Optional


class Emotion(NamedTuple):
    key: str            # stable id — this is what Gemini returns and the DB stores
    emoji: str
    label: str
    mood: Optional[str]  # one of VALID_MOODS, or None for the "no mood" family
    generic: bool = False  # the always-pinned verdict four


# The 16 mood colours, lifted from dashboard.html's --mood-* custom properties so
# the picker and the gradient can never drift apart.
# ─── The emotional colour space ───────────────────────────────────────
#
# TWO AXES, AND BOTH OF THEM MEAN SOMETHING.
#
# The old table was seven colours lifted off the original spec's mood list with an
# eighth bolted on. Nothing about #3060b0 said "tense" — the colours were arbitrary,
# and being arbitrary they could only ever be decoration.
#
#   HUE       = what KIND of feeling it is. Walk the wheel and it reads as a
#               circumplex: rose (wanting) -> oxblood (frozen threat) -> arterial
#               (swinging threat) -> orange (action) -> gold (laughter) ->
#               chartreuse (revulsion) -> green (safety) -> steel (wound tight) ->
#               blue (aching) -> violet (looking up) -> and round to rose.
#
#   INTENSITY = how far INTO that hue it goes. Foreboding is a dark, airless
#               oxblood; horror is lit up. Melancholy is a muted blue; grief is a
#               deep saturated one. Same hue, more heat.
#
# FEAR and FURY sit fourteen degrees apart on purpose. They are the same threat —
# one frozen, one swinging — and they are told apart by LIGHT, not hue: fear is
# starved of it, fury is arterial. This is the closest boundary in the system and
# the one doing the most work. If it ever needs more separation, push fury BRIGHTER
# rather than moving its hue, or the wheel stops meaning anything.
#
# WHY THIS SIZE. `tense` was 22.7% of every scene ever classified — one bucket
# carrying suspense, unease, awkwardness and half of mystery, because there was
# nowhere else to put them. And the app had no word at all for anger, grief,
# disgust, desire or hope: whole emotional regions a film can sit in that it was
# structurally unable to name. The vocabulary wasn't small, it was HOLED.
#
# All sixteen original moods survive unchanged, so the mood rows already in the DB
# still resolve. Nothing was dropped.
import colorsys

# family -> (hue°, sat_lo, sat_hi, light_lo, light_hi, what the hue MEANS)
FAMILY_HUES: dict[str, tuple[int, int, int, int, int, str]] = {
    "desire":    (334, 55, 72, 40, 54, "wanting"),
    "fear":      (352, 52, 78, 17, 33, "threat, frozen"),
    "fury":      (  6, 70, 88, 38, 52, "threat, swinging"),
    "kinetic":   ( 24, 65, 82, 38, 50, "something is HAPPENING"),
    "levity":    ( 44, 60, 82, 40, 54, "the room is laughing"),
    "revulsion": ( 68, 42, 62, 28, 42, "get it away from me"),
    "serenity":  (158, 38, 56, 30, 44, "safe, held, still"),
    "tension":   (200, 44, 62, 32, 46, "wound tight"),
    "sorrow":    (224, 44, 66, 26, 46, "aching"),
    "wonder":    (272, 48, 68, 38, 54, "looking up at something bigger"),
}


class Mood(NamedTuple):
    family: str
    intensity: float   # 0..1 — how far into the family's hue
    label: str         # what the button says
    blurb: str         # what Gemini is told it means


MOODS: dict[str, Mood] = {
    # fear — threat, frozen
    "foreboding":  Mood("fear", 0.28, "Foreboding", "quiet menace, the calm before"),
    "dread":       Mood("fear", 0.58, "Dread", "slow-burn doom, weight closing in"),
    "paranoia":    Mood("fear", 0.72, "Paranoia", "watched, hunted, trusting no one"),
    "horror":      Mood("fear", 1.00, "Horror", "visceral terror, the thing is HERE"),
    # fury — threat, swinging
    "anger":       Mood("fury", 0.45, "Anger", "seething, wronged, holding it in"),
    "rage":        Mood("fury", 0.95, "Rage", "violence, wrath, coming apart"),
    # kinetic
    "adrenaline":  Mood("kinetic", 0.62, "Adrenaline", "directed high-energy action"),
    "chaos":       Mood("kinetic", 0.95, "Chaos", "frantic disorientation"),
    # levity
    "whimsy":      Mood("levity", 0.32, "Whimsy", "playful, quirky, light on its feet"),
    "humor":       Mood("levity", 0.66, "Humor", "comedy, laughter, irony"),
    "absurd":      Mood("levity", 0.92, "Absurd", "gleefully ridiculous, off the rails"),
    # revulsion
    "distaste":    Mood("revulsion", 0.38, "Distaste", "grubby, off, faintly wrong"),
    "disgust":     Mood("revulsion", 0.90, "Disgust", "revolting, physically repellent"),
    # serenity
    "serene":      Mood("serenity", 0.32, "Serene", "peaceful, contemplative, still"),
    "cozy":        Mood("serenity", 0.62, "Cozy", "warm, safe, held"),
    # tension
    "unease":      Mood("tension", 0.30, "Unease", "something is off, can't name it"),
    "mystery":     Mood("tension", 0.48, "Mystery", "intrigue, puzzles, investigation"),
    "awkward":     Mood("tension", 0.58, "Awkward", "social cringe, want to look away"),
    "tense":       Mood("tension", 0.82, "Tense", "held-breath suspense"),
    # sorrow
    "longing":     Mood("sorrow", 0.30, "Longing", "yearning, wanting what isn't there"),
    "bittersweet": Mood("sorrow", 0.46, "Bittersweet", "joy and sorrow layered together"),
    "melancholy":  Mood("sorrow", 0.62, "Melancholy", "sadness, wistfulness, loss"),
    "grief":       Mood("sorrow", 1.00, "Grief", "devastation, the floor gone"),
    # wonder
    "hope":        Mood("wonder", 0.36, "Hope", "light ahead, something might hold"),
    "triumph":     Mood("wonder", 0.70, "Triumph", "victory, exhilaration, earned it"),
    "awe":         Mood("wonder", 0.92, "Awe", "wonder, spectacle, breathtaking craft"),
    # desire
    "tender":      Mood("desire", 0.32, "Tender", "gentle affection, care"),
    "romance":     Mood("desire", 0.62, "Romance", "love, intimacy, falling"),
    "desire":      Mood("desire", 0.94, "Desire", "heat, wanting, charged"),
}


def _hex(h: float, s: float, light: float) -> str:
    r, g, b = colorsys.hls_to_rgb(h / 360, light / 100, s / 100)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _ramp(family: str, intensity: float) -> tuple[float, float, float]:
    h, s_lo, s_hi, l_lo, l_hi, _ = FAMILY_HUES[family]
    return h, s_lo + (s_hi - s_lo) * intensity, l_lo + (l_hi - l_lo) * intensity


def mood_color(mood: str) -> str:
    """The mood's own light. Borders, the glow, the theatre bezel."""
    m = MOODS[mood]
    return _hex(*_ramp(m.family, m.intensity))


def mood_tint(mood: str) -> str:
    """The readable version — same hue, lifted off the floor. Text, icons, labels."""
    m = MOODS[mood]
    h, s, light = _ramp(m.family, m.intensity)
    return _hex(h, s * 0.72, min(78, light + 28))


def family_color(family: str) -> str:
    """A family's own light: the middle of its ramp."""
    h, s_lo, s_hi, l_lo, l_hi, _ = FAMILY_HUES[family]
    return _hex(h, (s_lo + s_hi) / 2, (l_lo + l_hi) / 2)


# ── The public names. Everything downstream reads these. ──
MOOD_FAMILIES: dict[str, str] = {k: m.family for k, m in MOODS.items()}
MOOD_COLORS: dict[str, str] = {k: mood_color(k) for k in MOODS}
MOOD_TINTS: dict[str, str] = {k: mood_tint(k) for k in MOODS}
MOOD_LABELS: dict[str, str] = {k: m.label for k, m in MOODS.items()}
FAMILY_COLORS: dict[str, str] = {f: family_color(f) for f in FAMILY_HUES}
FAMILY_LABELS: dict[str, str] = {
    "fear": "Afraid", "fury": "Furious", "tension": "Tense", "kinetic": "Charged",
    "wonder": "Awed", "levity": "Light", "serenity": "Calm", "desire": "Wanting",
    "sorrow": "Aching", "revulsion": "Repulsed",
}

NULL_MOOD_COLOR = "#555566"  # --text-muted; the "no mood" family stays grey


def mood_family(mood: Optional[str]) -> str:
    """Which register is the room in? '' when there's no mood."""
    return MOOD_FAMILIES.get((mood or "").lower(), "")


def palette() -> dict:
    """The whole colour system, for the browser.

    The JS used to carry a HAND-COPY of this table, and the comment above it cheerfully
    claimed the two "can never drift apart". They absolutely could: add a mood in Python,
    forget the JS, and it falls through to no-colour in silence. It is injected into the
    template now, so there is exactly one source of truth.
    """
    return {
        "moods": {
            k: {"family": m.family, "color": MOOD_COLORS[k], "tint": MOOD_TINTS[k],
                "label": m.label}
            for k, m in MOODS.items()
        },
        "families": {
            f: {"color": FAMILY_COLORS[f], "label": FAMILY_LABELS[f]}
            for f in FAMILY_HUES
        },
        "null_color": NULL_MOOD_COLOR,
    }


# ─── The four generics ────────────────────────────────────────────────
#
# THE LOW-EFFORT FLOOR. Sometimes you don't want a thesaurus, you want "that shot?
# loved it." These are pinned above the ranked emoji on every emotion step,
# always, regardless of what the film is doing.
#
# They are mood=None: loving a *horror* scene does not make the room romantic.
#
# This is what makes the depth safe. Tim's original complaint was "I only use 2 or
# 3 of them" — with a floor this cheap, that stops being a failure. Using only the
# generics on a lazy night is a perfectly good way to use the app.
GENERICS: list[Emotion] = [
    Emotion("loved_it", "🤩", "Loved it", None, generic=True),
    Emotion("liked_it", "👍", "Liked it", None, generic=True),
    Emotion("disliked_it", "👎", "Didn't like it", None, generic=True),
    Emotion("hated_it", "🤮", "Hated it", None, generic=True),
]


# ─── The thesaurus ────────────────────────────────────────────────────
THESAURUS: list[Emotion] = [
    # DREAD
    Emotion("terrified", "😱", "Terrified", "dread"),
    Emotion("scared", "😨", "Scared", "dread"),
    Emotion("sick_with_worry", "😰", "Sick with worry", "dread"),
    Emotion("cant_look", "🫣", "Can't look", "dread"),
    Emotion("bracing", "😖", "Bracing for it", "dread"),
    Emotion("blood_ran_cold", "🥶", "Blood ran cold", "dread"),
    # HORROR
    Emotion("appalled", "😵", "Appalled", "horror"),
    Emotion("skin_crawling", "🫠", "Skin crawling", "horror"),
    Emotion("brutal", "💀", "That was brutal", "horror"),
    Emotion("screaming", "😱", "Screaming", "horror"),
    # PARANOIA
    Emotion("being_hunted", "🏃", "Being hunted", "paranoia"),
    Emotion("trust_no_one", "🫥", "Trust no one", "paranoia"),
    Emotion("theyre_lying", "🤥", "They're lying", "paranoia"),
    Emotion("look_behind_you", "🙄", "Look behind you", "paranoia"),
    # ANGER
    Emotion("furious", "😠", "Furious", "anger"),
    Emotion("thats_not_right", "😤", "That's not right", "anger"),
    Emotion("seething", "🫩", "Seething", "anger"),
    Emotion("hate_him", "🖕", "I hate him", "anger"),
    # RAGE
    Emotion("get_him", "😡", "GET HIM", "rage"),
    Emotion("apoplectic", "🤬", "Apoplectic", "rage"),
    Emotion("burn_it_down", "🔥", "Burn it down", "rage"),
    Emotion("blood_boiling", "🌋", "Blood boiling", "rage"),
    # DISTASTE
    Emotion("grubby", "🫤", "Grubby", "distaste"),
    Emotion("dont_like_him", "😒", "Don't like him", "distaste"),
    Emotion("sleazy", "🤨", "Sleazy", "distaste"),
    # DISGUST — these finally have a home
    Emotion("want_to_throw_up", "🤮", "Want to throw up", "disgust"),
    Emotion("revolted", "🤢", "Revolted", "disgust"),
    Emotion("gross", "🫣", "That's GROSS", "disgust"),
    Emotion("get_it_away", "🙅", "Get it away from me", "disgust"),
    # FOREBODING
    Emotion("uneasy", "😟", "Uneasy", "foreboding"),
    Emotion("somethings_off", "🫤", "Something's off", "foreboding"),
    Emotion("being_watched", "👁", "Being watched", "foreboding"),
    Emotion("its_coming", "😧", "It's coming", "foreboding"),
    # TENSE
    Emotion("on_edge", "😬", "On edge", "tense"),
    Emotion("holding_breath", "😤", "Holding my breath", "tense"),
    Emotion("gripping_couch", "🫦", "Gripping the couch", "tense"),
    Emotion("unbearable", "⏳", "Unbearable", "tense"),
    Emotion("wound_up", "😳", "Wound up", "tense"),
    # UNEASE
    Emotion("somethings_wrong", "😟", "Something's wrong", "unease"),
    Emotion("dont_like_this", "😬", "I don't like this", "unease"),
    Emotion("hairs_up", "🫨", "Hairs on end", "unease"),
    # AWKWARD
    Emotion("cringing", "😖", "Cringing", "awkward"),
    Emotion("second_hand_embarrassment", "🫣", "Second-hand embarrassment", "awkward"),
    Emotion("oh_no_dont", "🙈", "Oh no, don't", "awkward"),
    Emotion("so_uncomfortable", "😅", "So uncomfortable", "awkward"),
    # MYSTERY
    Emotion("whats_going_on", "🤔", "What's going on", "mystery"),
    Emotion("suspicious", "👀", "Suspicious", "mystery"),
    Emotion("intrigued", "🧐", "Intrigued", "mystery"),
    Emotion("lost", "❓", "Lost", "mystery"),
    Emotion("piecing_it_together", "🕵", "Piecing it together", "mystery"),
    # ADRENALINE
    Emotion("pumped", "🔥", "Pumped", "adrenaline"),
    Emotion("electrified", "⚡", "Electrified", "adrenaline"),
    Emotion("heart_pounding", "💓", "Heart pounding", "adrenaline"),
    Emotion("hell_yes", "🤘", "Hell yes", "adrenaline"),
    Emotion("cant_sit_still", "🏃", "Can't sit still", "adrenaline"),
    # CHAOS
    Emotion("unhinged", "🤪", "Unhinged", "chaos"),
    Emotion("overwhelmed", "😵‍💫", "Overwhelmed", "chaos"),
    Emotion("what_is_happening", "🫨", "What is happening", "chaos"),
    Emotion("gleefully_lost", "🙃", "Gleefully lost", "chaos"),
    # AWE
    Emotion("floored", "🤯", "Floored", "awe"),
    Emotion("breathless", "😮", "Breathless", "awe"),
    Emotion("stunned", "🫢", "Stunned", "awe"),
    Emotion("admiring", "🤌", "Admiring", "awe"),
    Emotion("reverent", "✨", "Reverent", "awe"),
    Emotion("didnt_see_it_coming", "👏", "Didn't see that coming", "awe"),
    # HOPE
    Emotion("please", "🙏", "Please, please", "hope"),
    Emotion("maybe_theyll_make_it", "🤞", "Maybe they'll make it", "hope"),
    Emotion("theres_a_way", "🕯", "There's a way", "hope"),
    # TRIUMPH
    Emotion("fist_in_the_air", "🙌", "Fist in the air", "triumph"),
    Emotion("vindicated", "😤", "Vindicated", "triumph"),
    Emotion("so_proud", "🥹", "So proud", "triumph"),
    Emotion("cheering", "🎉", "Cheering", "triumph"),
    Emotion("earned_it", "💪", "Earned it", "triumph"),
    # HUMOR
    Emotion("cracking_up", "😂", "Cracking up", "humor"),
    Emotion("dying", "🤣", "Dying", "humor"),
    Emotion("wheezing", "😆", "Wheezing", "humor"),
    Emotion("snickering", "😏", "Snickering", "humor"),
    Emotion("nervous_laugh", "😅", "Nervous laugh", "humor"),
    Emotion("dead", "💀", "Dead", "humor"),
    # WHIMSY
    Emotion("charmed", "🥰", "Charmed", "whimsy"),
    Emotion("tickled", "😌", "Tickled", "whimsy"),
    Emotion("delighted", "🦋", "Delighted", "whimsy"),
    Emotion("playful", "🎈", "Playful", "whimsy"),
    # ABSURD
    Emotion("what_am_i_watching", "🫨", "What am I watching", "absurd"),
    Emotion("gloriously_stupid", "🤪", "Gloriously stupid", "absurd"),
    Emotion("off_the_rails", "🎢", "Off the rails", "absurd"),
    Emotion("no_notes", "🤌", "No notes", "absurd"),
    # ROMANCE
    Emotion("love", "❤️", "Love", "romance"),
    Emotion("swooning", "🥰", "Swooning", "romance"),
    Emotion("smitten", "😍", "Smitten", "romance"),
    Emotion("butterflies", "🫠", "Butterflies", "romance"),
    # DESIRE — `turned_on` was filed under romance because there was nowhere else.
    Emotion("turned_on", "🥵", "Turned on", "desire"),
    Emotion("oh_my", "😳", "Oh my", "desire"),
    Emotion("charged", "💋", "Charged", "desire"),
    # TENDER
    Emotion("tender", "🤍", "Tender", "tender"),
    Emotion("aching_for_them", "🫂", "Aching for them", "tender"),
    Emotion("look_after_them", "🪽", "Look after them", "tender"),
    # COZY
    Emotion("just_enjoying", "🍿", "Just enjoying", "cozy"),
    Emotion("content", "😊", "Content", "cozy"),
    Emotion("warm", "☺️", "Warm", "cozy"),
    Emotion("safe", "🫶", "Safe", "cozy"),
    # SERENE
    Emotion("calm", "😌", "Calm", "serene"),
    Emotion("peaceful", "🕊", "Peaceful", "serene"),
    Emotion("floating", "🌊", "Floating", "serene"),
    Emotion("still", "🧘", "Still", "serene"),
    # MELANCHOLY
    Emotion("sad", "😢", "Sad", "melancholy"),
    Emotion("gutted", "😭", "Gutted", "melancholy"),
    Emotion("hollow", "🫥", "Hollow", "melancholy"),
    Emotion("aching", "😞", "Aching", "melancholy"),
    Emotion("wistful", "🌧", "Wistful", "melancholy"),
    # LONGING
    Emotion("yearning", "🌙", "Yearning", "longing"),
    Emotion("wish_theyd_stayed", "🍃", "Wish they'd stayed", "longing"),
    Emotion("homesick", "🏚", "Homesick for it", "longing"),
    # GRIEF
    Emotion("devastated", "😭", "Devastated", "grief"),
    Emotion("i_cant", "💔", "I can't", "grief"),
    Emotion("floor_gone", "🕳", "The floor just went", "grief"),
    Emotion("inconsolable", "⚰️", "Inconsolable", "grief"),
    # BITTERSWEET
    Emotion("moved", "🥹", "Moved", "bittersweet"),
    Emotion("teary", "😢", "Teary", "bittersweet"),
    Emotion("heartbroken_but_glad", "💔", "Heartbroken but glad", "bittersweet"),
    Emotion("full_hearted", "🫀", "Full-hearted", "bittersweet"),
    Emotion("poignant", "🍂", "Poignant", "bittersweet"),
    # NO MOOD — reactions to bad filmmaking. These must NOT recolour the room.
    Emotion("unimpressed", "🙄", "Unimpressed", None),
    Emotion("bored", "🥱", "Bored", None),
    Emotion("losing_me", "😐", "Losing me", None),
    Emotion("not_buying_it", "🤨", "Not buying it", None),
    Emotion("thats_lazy", "😑", "That's lazy", None),
    Emotion("meh", "🫤", "Meh", None),
]


ALL: list[Emotion] = GENERICS + THESAURUS
BY_KEY: dict[str, Emotion] = {e.key: e for e in ALL}


def get(key: str) -> Optional[Emotion]:
    return BY_KEY.get(key)


def color_for(e: Emotion) -> str:
    return MOOD_COLORS.get(e.mood or "", NULL_MOOD_COLOR)


def mood_for_key(key: str) -> Optional[str]:
    """The mood family of an emotion, or None.

    NOTE: this is NOT used to set the app's mood gradient — the FILM sets that,
    via the draft's own mood classification (Decision #12). This is only for
    tinting the button and for grouping in the "all emotions" browser.
    """
    e = BY_KEY.get(key)
    return e.mood if e else None


def by_mood() -> dict[Optional[str], list[Emotion]]:
    """Grouped for the 'all emotions' browser. Insertion order = MOOD_COLORS order,
    with the null family last."""
    out: dict[Optional[str], list[Emotion]] = {m: [] for m in MOOD_COLORS}
    out[None] = []
    for e in THESAURUS:
        out.setdefault(e.mood, []).append(e)
    return {m: v for m, v in out.items() if v}


def prompt_vocabulary() -> str:
    """The thesaurus as Gemini sees it — the whole point of the module.

    Gemini gets the KEYS and must return keys, never free text. Grouping by mood
    is what lets it rank sensibly: a Freddy kill surfaces the horror/dread keys,
    and reacting to the *craft* of that same kill surfaces the awe keys.
    """
    lines: list[str] = []
    for mood, emos in by_mood().items():
        head = mood.upper() if mood else "NO MOOD (reactions to bad filmmaking)"
        lines.append(f"{head}: " + ", ".join(f"{e.key} ({e.label})" for e in emos))
    return "\n".join(lines)


VALID_EMOTION_KEYS: list[str] = [e.key for e in ALL]
