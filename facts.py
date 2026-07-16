"""Facts — everything the room knows about RIGHT NOW, and the rules that read them.

This is the foundation. Movie Mode's room decides who speaks by asking, of each
person, "is this moment yours?" — and until now the only evidence it had was a
casting sheet distilled from about 12% of that person's vault, tuned for reacting
to FILMS. So Bobby's sheet says he "shrugs at small talk", and the coordinator
would happily mute him on a goodbye from his own son-in-law.

Tim's answer: report a bad call, and make a RULE from it. But a rule without a
scope is a sledgehammer — "Bobby stays out of grief" would mute him at his own
brother's funeral. What he actually wants is:

    Bobby stays out of grief   WHEN DAISY IS IN THE ROOM   (she's the one who does that)
    └─────── the rule ───────┘ └──────── the conditions ────────┘

So: a FACT REGISTRY.

A fact is a named, exactly-observable property of the current turn. A rule is a
sentence plus a list of conditions over facts. And the key decision — the one that
makes this a foundation instead of a feature:

    DO NOT HARDCODE THE DIALS.

Every dial we hardcode is one we regret, and each new one would cost a migration,
a schema change and a UI change. Instead there is ONE registry, below. Adding a
dial is ONE entry in it: the report dialog renders its chips from this list, the
evaluator reads from this list, the profile builder is told about this list. One
source of truth, and the UI grows itself.

─── LAW vs LEAN ───────────────────────────────────────────────────────────

The CONDITIONS are always programmatic — Python evaluates them against the real
values, exactly, for free. A rule either applies or it doesn't; no judgment.

What happens NEXT depends on the rule's verdict:

    always / never   ->  LAW.  Enforced in Python, BEFORE the coordinator is
                               called. A kin excluded by a law is not discouraged
                               in the prompt — they are not IN the prompt.
    usually / rarely ->  LEAN. Goes into the prompt as guidance. The model weighs it.

That split exists because "he NEVER heckles in a public theatre" is not a
suggestion, and a law a model can politely disagree with is not a law. But "he
usually stays out of grief" SHOULD bend when Tim says his name — so it stays a lean.

Anything we can MEASURE becomes a hard condition. Anything Tim states ABSOLUTELY
becomes a hard rule. The model is left with only what genuinely needs judgment.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import emotions
import presence

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  The situation taxonomy
# ═══════════════════════════════════════════════════════════════════
#
# ONE SOURCE OF TRUTH, and three consumers: it is a fact's value set (here), the
# enum Gemini classifies into (gemini_brain), and the rows of every response
# profile (affinity). If these three ever drift apart, a profile row can never be
# matched by a turn and the rule built on it is dead on arrival.
#
# The split is the whole point. Tim does not only comment on films — he talks to
# his family. A remark about the lighting is something you can let lie. A question
# is not, and neither is a goodbye. The old sheets modelled only the right-hand
# column, which is precisely why they got the goodbye wrong.
SITUATIONS: list[dict] = [
    # ── Tim and the room ──
    {"key": "asks_room",      "group": "social", "label": "Asks the room a question"},
    {"key": "goodbye_plans",  "group": "social", "label": "Says goodbye / makes plans"},
    {"key": "his_day",        "group": "social", "label": "Shares news about his day"},
    {"key": "jokes",          "group": "social", "label": "Makes a joke"},
    {"key": "venting",        "group": "social", "label": "Is upset or venting"},
    {"key": "affection",      "group": "social", "label": "Is affectionate with one person"},
    {"key": "thanks_apology", "group": "social", "label": "Thanks or apologises"},
    {"key": "names_someone",  "group": "social", "label": "Names someone directly"},
    {"key": "rambling",       "group": "social", "label": "Is stoned and rambling"},
    # ── The film ──
    {"key": "plot_hole",      "group": "film",   "label": "A factual error or plot hole"},
    {"key": "lore",           "group": "film",   "label": "Lore / worldbuilding"},
    {"key": "craft",          "group": "film",   "label": "Craft — the shot, the score, the edit"},
    {"key": "performance",    "group": "film",   "label": "A performance"},
    {"key": "jump_scare",     "group": "film",   "label": "A jump scare"},
    {"key": "gore",           "group": "film",   "label": "Gore or violence"},
    {"key": "grief",          "group": "film",   "label": "Grief or an emotional beat"},
    {"key": "romance",        "group": "film",   "label": "Romance"},
    {"key": "comedy",         "group": "film",   "label": "Comedy"},
    {"key": "confusing",      "group": "film",   "label": "Something confusing"},
    {"key": "callback",       "group": "film",   "label": "A callback to earlier"},
    {"key": "film_ends",      "group": "film",   "label": "The film ends"},
]
SITUATION_KEYS: list[str] = [s["key"] for s in SITUATIONS]
SITUATION_LABELS: dict[str, str] = {s["key"]: s["label"] for s in SITUATIONS}

# TWO TRIGGERS, ALWAYS. A turn can be pulled two ways at once, and collapsing them
# into one value throws half the room away.
#
#   Tim: "wanna do this again tomorrow?"        <- a SOCIAL trigger. Everyone answers.
#   On screen: the monster tears someone apart  <- a FILM trigger. Bobby leans in.
#
# Both are true. Both are live. The first version of this fact forced Gemini to pick
# a single winner — and the prompt literally told it "read Tim's message first, NOT
# whatever is on screen", which meant the film trigger was discarded on every turn
# Tim happened to type something. A rule about jump scares would then never fire
# unless Tim sat in total silence.
#
# So: what TIM is doing, and what the SCREEN is doing, are two separate facts. A rule
# can be scoped to either, or to both.
SOCIAL_SITUATIONS: list[str] = [s["key"] for s in SITUATIONS if s["group"] == "social"]
FILM_SITUATIONS: list[str] = [s["key"] for s in SITUATIONS if s["group"] == "film"]

MOODS: list[str] = list(emotions.MOOD_COLORS.keys())

STONED_LABELS = ["sober", "lifted", "baked", "blasted"]   # index == level 0-3


# ═══════════════════════════════════════════════════════════════════
#  The registry
# ═══════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Fact:
    key: str
    label: str          # shown on the chip
    group: str          # how the dialog groups the "+ a dial" picker
    kind: str           # enum | set | ladder | level | number | flag | text
    blurb: str          # one line, shown to the LLM so it proposes sane scopes
    per_kin: bool = False        # resolved against ONE kin, not the room
    options: tuple = ()          # for enum/set: ({value,label,emoji}, ...)


def _opts(pairs) -> tuple:
    return tuple({"value": v, "label": l, "emoji": e} for v, l, e in pairs)


FACTS: dict[str, Fact] = {f.key: f for f in [
    # ── The scene ──
    Fact("mood", "Mood", "The scene", "enum",
         "The dominant mood of the scene on screen, from the 16-mood palette.",
         options=_opts((m, m.capitalize(), "🎭") for m in MOODS)),
    Fact("mood_arc", "Tension", "The scene", "enum",
         "Whether the tension is building, breaking, or steady across recent scenes.",
         options=_opts([("building", "Building", "📈"), ("breaking", "Breaking", "📉"),
                        ("steady", "Steady", "➡️")])),
    # TWO TRIGGERS. A turn can be pulled two ways at once — Tim asks the room a
    # question WHILE the monster tears someone apart. Both are live. Collapse them
    # into one value and you throw half the room away.
    Fact("tim_situation", "What Tim's doing", "The scene", "enum",
         "What TIM just did — asked the room a question, said goodbye, made a joke, "
         "vented. Empty when he typed nothing at all (a bare reaction). This is the "
         "SOCIAL trigger, and it is about him, not the film.",
         options=_opts((s["key"], s["label"], "💬")
                       for s in SITUATIONS if s["group"] == "social")),
    Fact("scene_situation", "What's on screen", "The scene", "enum",
         "What the FILM is doing right now — a jump scare, a plot hole, grief, a "
         "callback. This is the SCREEN trigger, and it is true whether or not Tim "
         "said anything.",
         options=_opts((s["key"], s["label"], "🎬")
                       for s in SITUATIONS if s["group"] == "film")),
    Fact("film_position", "Where in it", "The scene", "enum",
         "How far into the film/episode we are.",
         options=_opts([("opening", "The opening", "🎬"), ("body", "The middle", "▶️"),
                        ("climax", "The climax", "🔥"), ("credits", "The credits", "🎞️")])),

    # ── Where ──
    Fact("venue", "Where we're watching", "Where", "enum",
         "The room they're watching in. A public theatre is not the living room.",
         options=_opts((v["key"], v["label"], "📍") for v in presence.VENUES)),

    # ── Who's here ──
    Fact("kins_here", "Who's in the room", "Who's here", "set",
         "Which family members are watching. Someone behaves differently when their "
         "mother is in the room.",
         options=()),   # filled at runtime from the roster
    Fact("kins_talking", "Who Tim's talking to", "Who's here", "set",
         "Which family members Tim has tapped as the ones he's addressing.",
         options=()),
    Fact("people_here", "Who else is here", "Who's here", "set",
         "REAL people and animals in the room — Tim's dad, his mum, his gran, the dog. "
         "Not the AI family. 'He won't say that in front of Granny.'",
         options=_opts((p["key"], p["label"], p["emoji"]) for p in presence.PEOPLE)),

    # ── How stoned ──
    Fact("tim_stoned", "Tim is", "How stoned", "level",
         "How stoned TIM is, 0 sober to 3 blasted.",
         options=_opts((i, STONED_LABELS[i].capitalize(), "🌿") for i in range(4))),
    Fact("self_stoned", "They are", "How stoned", "level",
         "How stoned THIS PERSON is, 0 sober to 3 blasted.",
         per_kin=True,
         options=_opts((i, STONED_LABELS[i].capitalize(), "🌿") for i in range(4))),
    Fact("tim_method", "Tim took", "How stoned", "enum",
         "What Tim took — a joint hits different from an edible.",
         options=_opts([("smoke", "Smoked", "💨"), ("edible", "Edible", "🍪"),
                        ("dab", "Dab", "🔥")])),
    Fact("self_method", "They took", "How stoned", "enum",
         "What THIS PERSON took.", per_kin=True,
         options=_opts([("smoke", "Smoked", "💨"), ("edible", "Edible", "🍪"),
                        ("dab", "Dab", "🔥")])),

    # ── What we're watching ──
    #
    # A LADDER, not a flat value. The single most useful rule Tim will write —
    # "in Supernatural, Bobby takes every lore question" — is a SHOW-level rule.
    # Scoped by accident to S01E02 it fires once and never again.
    Fact("watching", "What we're watching", "Watching", "ladder",
         "What's on. This is a LADDER: a rule can be scoped to the whole show (fires "
         "on every episode), one season, one episode, or one film. Scope it as BROADLY "
         "as the rule actually means — a lore rule is about the SHOW, not one episode.",
         options=()),   # built per-turn from what's playing
    Fact("media_kind", "Film or TV", "Watching", "enum",
         "A film or a TV episode.",
         options=_opts([("movie", "A film", "🎬"), ("episode", "A TV episode", "📺")])),
    Fact("rewatch", "Seen it before", "Watching", "flag",
         "Whether they've watched this exact thing before in a previous session.",
         options=_opts([(True, "A rewatch", "🔁"), (False, "First time", "🆕")])),
    Fact("director", "Director", "Watching", "text",
         "The director. 'He never shuts up during a Carpenter.'"),
    Fact("year", "Year", "Watching", "number",
         "The year it came out."),

    # ── When ──
    Fact("time_of_day", "Time of day", "When", "enum",
         "Roughly when they're watching.",
         options=_opts([("afternoon", "Afternoon", "🌤️"), ("evening", "Evening", "🌆"),
                        ("late", "Late", "🌙"), ("small_hours", "The small hours", "🌑")])),
    Fact("weekday", "Day", "When", "enum",
         "Day of the week.",
         options=_opts((d.lower(), d, "📅") for d in
                       ["Monday", "Tuesday", "Wednesday", "Thursday",
                        "Friday", "Saturday", "Sunday"])),
    Fact("watching_for", "We've been at it", "When", "number",
         "Minutes since the session started. 'Three hours in, everyone's flagging.'"),

    # ── The room ──
    Fact("room_noise", "The room's been", "The room", "enum",
         "How chatty the room has been over the last few turns.",
         options=_opts([("quiet", "Quiet", "🤫"), ("normal", "Normal", "💬"),
                        ("loud", "Loud", "📢")])),
    Fact("quiet_turns", "They've been quiet", "The room", "number", per_kin=True,
         blurb="How many of Tim's turns since THIS PERSON last spoke."),
]}

FACT_GROUPS: list[str] = ["The scene", "Where", "Who's here", "How stoned",
                          "Watching", "When", "The room"]

# The verdicts, and which of them are LAW.
VERDICTS = ["always", "usually", "rarely", "never"]
LAWS = {"always", "never"}          # enforced in Python, before the model is called
LEANS = {"usually", "rarely"}       # go into the prompt as guidance

OPS = ["is", "is_not", "includes", "excludes", "at_least", "at_most"]


# ═══════════════════════════════════════════════════════════════════
#  The context — every fact, frozen, for one turn
# ═══════════════════════════════════════════════════════════════════

@dataclass
class TurnContext:
    """Every fact that was true when the room decided. FROZEN onto the turn.

    Freezing is what makes reporting honest. A week later Daisy has gone home and
    the mood has changed — but the rule Tim writes must be scoped to what was true
    WHEN THE ROOM GOT IT WRONG, not to whatever happens to be true when he
    finally gets round to reporting it.
    """

    values: dict[str, Any] = field(default_factory=dict)
    # Facts that differ per person (how stoned THEY are, how long THEY'VE been quiet).
    per_kin: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get(self, fact_key: str, kin_key: Optional[str] = None) -> Any:
        f = FACTS.get(fact_key)
        if f is not None and f.per_kin:
            return (self.per_kin.get(kin_key or "") or {}).get(fact_key)
        return self.values.get(fact_key)

    def to_json(self) -> str:
        return json.dumps({"values": self.values, "per_kin": self.per_kin},
                          default=str)

    @classmethod
    def from_json(cls, raw: Optional[str]) -> "TurnContext":
        if not raw:
            return cls()
        try:
            d = json.loads(raw)
            return cls(values=d.get("values") or {}, per_kin=d.get("per_kin") or {})
        except (ValueError, TypeError):
            log.warning("turn context was unreadable — treating it as empty")
            return cls()


def _film_position(offset_ms: int, duration_ms: int) -> Optional[str]:
    if not duration_ms:
        return None
    pct = offset_ms / duration_ms
    if pct < 0.08:
        return "opening"
    if pct > 0.94:
        return "credits"
    if pct > 0.78:
        return "climax"
    return "body"


def _time_of_day(now: datetime) -> str:
    h = now.hour
    if h < 5:
        return "small_hours"
    if h < 17:
        return "afternoon"
    if h < 21:
        return "evening"
    return "late"


def watching_ladder(plex: dict) -> list[str]:
    """The rungs a rule can be scoped to, broadest first.

    A condition matches if ANY rung matches — so a rule scoped to the show fires on
    every episode of it, and a rule scoped to the episode fires only there.

        Supernatural S01E02  ->  ["supernatural",
                                  "supernatural/s1",
                                  "supernatural/s1e2"]
        The Iron Giant       ->  ["the iron giant"]

    Getting this backwards would make Tim's single best rule — "in Supernatural,
    Bobby takes the lore" — either never fire, or fire during Casablanca.
    """
    if not plex:
        return []
    show = (plex.get("series_title") or "").strip().lower()
    if not show:
        title = (plex.get("title") or "").strip().lower()
        return [title] if title else []

    rungs = [show]
    season = plex.get("season_number")
    if season is not None:
        rungs.append(f"{show}/s{season}")
        ep = plex.get("episode_number")
        if ep is not None:
            rungs.append(f"{show}/s{season}e{ep}")
    return rungs


def watching_options(plex: dict) -> list[dict]:
    """The rungs, as pickable chips, for the dialog. Broadest first."""
    rungs = watching_ladder(plex)
    if not rungs:
        return []
    if not (plex.get("series_title") or ""):
        return [{"value": rungs[0], "label": plex.get("title") or rungs[0],
                 "emoji": "🎬", "note": "this film only"}]
    out = [{"value": rungs[0], "label": plex["series_title"], "emoji": "📺",
            "note": "any episode, any season"}]
    if len(rungs) > 1:
        out.append({"value": rungs[1],
                    "label": f"{plex['series_title']}, season {plex.get('season_number')}",
                    "emoji": "📺", "note": "this season"})
    if len(rungs) > 2:
        s, e = plex.get("season_number"), plex.get("episode_number")
        out.append({"value": rungs[2],
                    "label": f"S{s:02d}E{e:02d} “{plex.get('title') or ''}”",
                    "emoji": "📺", "note": "this episode only"})
    return out


# ═══════════════════════════════════════════════════════════════════
#  Building the context — reading the real world
# ═══════════════════════════════════════════════════════════════════
#
# BE PARANOID HERE. A wrong fact poisons every rule built on top of it, silently
# and forever: Tim writes "when Daisy's here", the fact says she isn't, the rule
# never fires, and he concludes the whole feature is broken. Every value below is
# read from the thing that actually knows it — never inferred, never guessed.

async def build_context(
    *,
    session_id: str,
    plex: Optional[dict],
    mood: Optional[str],
    tim_situation: Optional[str],
    scene_situation: Optional[str],
    room_keys: list[str],
    addressed_keys: list[str],
    settings_map: dict[str, str],
    quiet_turns: dict[str, int],
    lean_history: Optional[list[int]] = None,
    now: Optional[datetime] = None,
) -> TurnContext:
    """Freeze every fact about this turn. One call, per turn, before the room decides."""
    from database import db          # local import: facts.py stays importable from the CLI
    import stoned_tracker

    plex = plex or {}
    now = now or datetime.now()
    ctx = TurnContext()
    v = ctx.values

    # ── The scene: BOTH triggers ──
    v["mood"] = mood or None
    v["tim_situation"] = tim_situation or None      # what HE did (may be nothing)
    v["scene_situation"] = scene_situation or None  # what the SCREEN did
    v["mood_arc"] = await _mood_arc(session_id, plex.get("rating_key"))
    v["film_position"] = _film_position(
        int(plex.get("view_offset_ms") or 0), int(plex.get("duration_ms") or 0)
    )

    # ── Where, and who ──
    v["venue"] = settings_map.get("active_venue") or None
    v["kins_here"] = sorted(room_keys)
    v["kins_talking"] = sorted(addressed_keys)
    try:
        v["people_here"] = sorted(json.loads(settings_map.get("present_people") or "[]"))
    except (ValueError, TypeError):
        v["people_here"] = []

    # ── How stoned ──
    tim_level, tim_method, _ = await stoned_tracker.current_state("tim")
    v["tim_stoned"] = tim_level
    v["tim_method"] = tim_method

    # ── What we're watching ──
    v["watching"] = watching_ladder(plex)
    v["media_kind"] = plex.get("type") or None
    v["director"] = plex.get("director") or None
    v["year"] = plex.get("year") or None
    v["rewatch"] = await _is_rewatch(session_id, plex.get("rating_key"))

    # ── When ──
    v["time_of_day"] = _time_of_day(now)
    v["weekday"] = now.strftime("%A").lower()
    v["watching_for"] = await _session_minutes(session_id, now)

    # ── The room ──
    recent = lean_history or []
    total = sum(recent)
    v["room_noise"] = ("quiet" if total == 0 else "loud" if total >= len(recent) else "normal") \
        if recent else "quiet"

    # ── Per-kin facts ──
    for key in room_keys:
        level, method, _ = await stoned_tracker.current_state(key)
        ctx.per_kin[key] = {
            "self_stoned": level,
            "self_method": method,
            "quiet_turns": quiet_turns.get(key, 99),
        }

    return ctx


async def _mood_arc(session_id: str, rating_key: Optional[str]) -> Optional[str]:
    """Is the tension building, breaking, or steady? Read from the mood ticker's trail."""
    from database import db
    try:
        events = await db.get_mood_events(session_id, rating_key)
    except Exception:  # noqa: BLE001 — a missing arc must never kill a turn
        return None
    if len(events) < 2:
        return None
    heat = {"dread": 3, "horror": 3, "foreboding": 2, "tense": 3, "mystery": 2,
            "adrenaline": 3, "chaos": 3, "awe": 1, "triumph": 1, "humor": 0,
            "whimsy": 0, "romance": 1, "cozy": 0, "serene": 0, "melancholy": 1,
            "bittersweet": 1}
    tail = [heat.get(e.get("mood") or "", 1) for e in events[-3:]]
    if len(tail) < 2:
        return None
    if tail[-1] > tail[0]:
        return "building"
    if tail[-1] < tail[0]:
        return "breaking"
    return "steady"


async def _is_rewatch(session_id: str, rating_key: Optional[str]) -> bool:
    """Have we watched this exact thing in an EARLIER session?"""
    if not rating_key:
        return False
    from database import db
    try:
        row = await db.fetch_one(
            "SELECT 1 FROM movies WHERE plex_rating_key = ? AND session_id != ? LIMIT 1",
            (str(rating_key), session_id),
        )
    except Exception:  # noqa: BLE001
        return False
    return row is not None


async def _session_minutes(session_id: str, now: datetime) -> Optional[int]:
    from database import db
    try:
        row = await db.fetch_one("SELECT started_at FROM sessions WHERE id = ?", (session_id,))
    except Exception:  # noqa: BLE001
        return None
    if not row or not row["started_at"]:
        return None
    try:
        started = datetime.fromisoformat(str(row["started_at"]))
    except ValueError:
        return None
    if started.tzinfo is not None:
        started = started.replace(tzinfo=None)
        now = datetime.utcnow()
    return max(0, int((now - started).total_seconds() // 60))


# ═══════════════════════════════════════════════════════════════════
#  Matching — pure, exact, and free
# ═══════════════════════════════════════════════════════════════════

def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return list(v)
    return [v]


def _one(v: Any) -> str:
    """One value, as the string a condition would carry."""
    return str(v).strip().lower()


def _norm(vs: Any) -> set[str]:
    """A condition's values, and a fact's value, on the SAME footing.

    A CONDITION IS ALWAYS STRINGS. `RuleCondition.value` is `list[str]`, and the dial picker
    does `String(o.value)`. But the FACTS hold real types — `rewatch` is a bool, `tim_stoned`
    is an int, `year` is a number.

    So `have in want` was comparing `True` against `["True"]`, and `2` against `["2"]`, and
    getting False every single time. Measured: five of the six condition kinds could NEVER
    fire. Every rule anyone ever scoped to a stoned level, a rewatch, or a year has silently
    done nothing since the day the feature shipped — and it looked completely fine in the
    list, which is why nobody found it.

    This is why `_match_one` returning False is such a dangerous default: a rule that cannot
    match is indistinguishable, from the outside, from a rule whose moment simply hasn't come.
    """
    return {_one(v) for v in _as_list(vs)}


def _match_one(cond: dict, ctx: TurnContext, kin_key: Optional[str]) -> bool:
    fact_key = cond.get("fact")
    op = cond.get("op")
    want = _as_list(cond.get("value"))
    f = FACTS.get(fact_key or "")
    if f is None:
        # A condition on a fact we no longer have. Do NOT silently pass it — a rule
        # whose scope we can't evaluate must not fire, or it fires everywhere.
        log.warning("rule condition names an unknown fact %r — the rule will not fire", fact_key)
        return False

    have = ctx.get(fact_key, kin_key)

    if op == "is":
        if f.kind == "ladder":
            # ANY rung matching is a match. That's what makes a show-level rule
            # fire on every episode.
            return bool(_norm(have) & _norm(want))
        return _one(have) in _norm(want)
    if op == "is_not":
        if f.kind == "ladder":
            return not (_norm(have) & _norm(want))
        return _one(have) not in _norm(want)
    if op == "includes":
        # ALL of the wanted people/things must be present. "when Daisy AND Jeff are here."
        return set(want).issubset(set(_as_list(have)))
    if op == "excludes":
        # NONE of them. "when Daisy ISN'T here" — a real and different rule, and
        # trivially easy to get backwards.
        return not (set(want) & set(_as_list(have)))
    if op in ("at_least", "at_most"):
        if have is None or not want:
            return False
        try:
            have_n, want_n = float(have), float(want[0])
        except (TypeError, ValueError):
            return False
        return have_n >= want_n if op == "at_least" else have_n <= want_n

    log.warning("rule condition uses an unknown op %r — the rule will not fire", op)
    return False


def applies(rule: dict, ctx: TurnContext, kin_key: Optional[str] = None) -> bool:
    """Does this rule apply to THIS turn? Pure Python. Exact. No model, no guessing.

    A rule with no conditions is universal.
    """
    conds = rule.get("when") or []
    if not conds:
        return True
    return all(_match_one(c, ctx, kin_key) for c in conds)


def specificity(rule: dict) -> int:
    """How specific a rule is. More conditions = more specific = wins a law conflict."""
    return len(rule.get("when") or [])


def _cond_key(c: dict) -> tuple:
    val = c.get("value")
    val = tuple(sorted(str(v) for v in val)) if isinstance(val, (list, tuple, set)) else (str(val),)
    return (c.get("fact"), c.get("op"), val)


def would_widen(parents: list[dict], merged_conditions: list[dict]) -> Optional[str]:
    """Would this merge make a rule apply in MORE places than it used to? Returns why.

    THE TRAP, and it is a good one. The obvious check is "does the merged rule have at
    least as many conditions as the narrowest parent?" — and it is WRONG, because a
    UNIVERSAL parent has zero conditions. Merge:

        A: "stays out of grief WHEN DAISY IS HERE"   (1 condition)
        B: "stays out of romance"                    (0 conditions — universal)
             -> merged, 0 conditions

    ...and a count-based check waves it straight through, because 0 >= min(1, 0). But
    rule A has just lost "when Daisy is here" — Bobby now stays out of grief in every
    room, forever, and Tim would never learn why. He'd just see a man who had stopped
    talking to him.

    The correct test is about COVERAGE, not counting: **every parent's conditions must
    survive into the merged rule.** Each parent's condition set must be a SUBSET of the
    merged one. Then no parent can ever fire in a situation it didn't fire in before —
    only in fewer, which is always safe.
    """
    merged = {_cond_key(c) for c in (merged_conditions or [])}
    for p in parents:
        theirs = {_cond_key(c) for c in (p.get("when") or [])}
        lost = theirs - merged
        if lost:
            names = ", ".join(f"{f}" for f, _, _ in lost)
            return (
                f"“{p.get('rule_text', '?')}” would lose its condition on {names} — "
                f"it would start firing where it never used to"
            )
    return None


def is_law(rule: dict) -> bool:
    return (rule.get("verdict") or "") in LAWS


def describe_conditions(rule: dict) -> str:
    """The scope, in words Tim can read on a badge. '' for a universal rule."""
    conds = rule.get("when") or []
    if not conds:
        return "always"
    bits = []
    for c in conds:
        f = FACTS.get(c.get("fact") or "")
        label = f.label if f else (c.get("fact") or "?")
        vals = _as_list(c.get("value"))
        pretty = ", ".join(str(v) for v in vals)
        op = c.get("op")
        if op == "excludes":
            bits.append(f"{label} without {pretty}")
        elif op == "includes":
            bits.append(f"{label} incl. {pretty}")
        elif op == "is_not":
            bits.append(f"{label} not {pretty}")
        elif op == "at_least":
            bits.append(f"{label} ≥ {pretty}")
        elif op == "at_most":
            bits.append(f"{label} ≤ {pretty}")
        else:
            bits.append(f"{label} {pretty}")
    return " · ".join(bits)


# ═══════════════════════════════════════════════════════════════════
#  Splitting a kin's rules for one turn
# ═══════════════════════════════════════════════════════════════════

@dataclass
class RuleSplit:
    """What this kin's rules say about THIS turn."""

    law: Optional[dict] = None          # the winning law, if any
    law_conflict: Optional[tuple] = None  # two laws that tied — both demoted, Tim is told
    leans: list[dict] = field(default_factory=list)   # go in the prompt
    skipped: list[dict] = field(default_factory=list)  # applied? no. Tim is TOLD how many.

    @property
    def forces_speech(self) -> bool:
        return bool(self.law) and self.law.get("verdict") == "always"

    @property
    def forces_silence(self) -> bool:
        return bool(self.law) and self.law.get("verdict") == "never"


def split_rules(rules: list[dict], ctx: TurnContext, kin_key: str) -> RuleSplit:
    """Evaluate one kin's rules against the turn, and sort them into law / lean / skipped.

    LAW CONFLICTS: the more specific rule wins (more conditions = more specific).
    On an exact tie both are DEMOTED TO LEANS and the collision is surfaced — Tim
    wrote both rules and would otherwise never learn they fight. Silently picking
    one would be the worst outcome available to us.
    """
    out = RuleSplit()
    live: list[dict] = []
    for r in rules:
        if applies(r, ctx, kin_key):
            live.append(r)
        else:
            out.skipped.append(r)

    laws = [r for r in live if is_law(r)]
    leans = [r for r in live if not is_law(r)]

    if len(laws) == 1:
        out.law = laws[0]
    elif len(laws) > 1:
        laws.sort(key=specificity, reverse=True)
        top, runner = laws[0], laws[1]
        contradicts = top.get("verdict") != runner.get("verdict")
        if contradicts and specificity(top) == specificity(runner):
            # A genuine, unresolvable collision. Don't guess — hand it to the model
            # and TELL HIM.
            log.warning(
                "%s: two laws of equal specificity disagree (%r vs %r) — "
                "demoting both to leans and surfacing the conflict",
                kin_key, top.get("rule_text"), runner.get("rule_text"),
            )
            out.law_conflict = (top, runner)
            leans = laws + leans
        else:
            out.law = top
            # The losers still inform the model rather than vanishing.
            leans = [r for r in laws[1:]] + leans

    out.leans = leans
    return out


def _render_rule(r: dict, *, law: bool = False) -> str:
    """One rule, with its WHY.

    The `why` is not decoration. Without it a rule is a bald instruction and the
    model obeys it mechanically and stupidly at the edges — "Bobby stays out of
    grief" gets him to say nothing even when Tim asks him point blank. WITH it, the
    model has the SPIRIT of the rule and can act on a moment the conditions never
    anticipated:

        rarely — Stays out of grief between other people.  [Daisy is here]
            why: he's a doer, not a talker. He'd refill your glass, not explain
                 the curse.
            (Tim made this rule when Bobby explained the family curse over
             "god, poor Sam".)

    That last line grounds an abstraction in a real moment, which is worth more to a
    language model than any amount of restatement.
    """
    v = r["verdict"].upper() if law else r["verdict"]
    tail = "  (a LAW — already enforced)" if law else ""
    out = [f"  • {v} — {r['rule_text']}  [{describe_conditions(r)}]{tail}"]
    if r.get("rule_why"):
        out.append(f"      why: {r['rule_why']}")
    if r.get("evidence"):
        out.append(f"      (Tim made this rule when: {r['evidence']})")
    return "\n".join(out)


# THE HARD CEILING ON THE PROMPT — and it is not about tokens.
#
# The scope filter already does most of the work: a rule only reaches the prompt if
# its conditions actually hold. Bobby can accumulate a thousand rules and forty may
# match a given turn. Forty is fine for cost — it's pennies on Haiku.
#
# It is NOT fine for ATTENTION. Hand a model forty instructions about one man and it
# starts silently weighting some over others, and you cannot tell which. The rule Tim
# cared most about is exactly the one it quietly drops.
#
# So we send the N MOST SPECIFIC leans that match. Specificity is a real signal here:
# more conditions means the rule was aimed more deliberately at a moment like this
# one. Everything else stays on file, stays in the editor, and fires the moment IT is
# the sharpest thing in the room.
#
# Tim can write as many rules as he likes. The prompt never grows.
_MAX_LEANS_IN_PROMPT = 10


def for_prompt(split: RuleSplit, first_name: str) -> str:
    """The rules block the coordinator actually reads. '' when there's nothing to say.

    LAWS ARE NOT IN HERE. A law is enforced in Python before this prompt is ever
    built — a `never` means the person isn't in the conversation at all, and an
    `always` means they're already locked in. Restating it to the model would be
    asking it to weigh a decision that has already been taken, which is at best noise
    and at worst an invitation to argue.

    Only the LEANS go in, because a lean is the only kind of rule that actually needs
    judgment.
    """
    lines: list[str] = []

    # The one exception. A collision between two equally-specific laws is NOT enforced
    # — nobody wins — so it becomes a genuine judgment call, and the model has to be
    # told that it is holding one.
    if split.law_conflict:
        a, b = split.law_conflict
        lines.append(
            f"  • !! Two of Tim's rules collide here and neither is more specific:\n"
            f"      “{a['rule_text']}”  vs  “{b['rule_text']}”\n"
            f"      Neither is enforced. Use your judgment — and lean toward answering him."
        )

    leans = sorted(split.leans, key=specificity, reverse=True)
    shown, held = leans[:_MAX_LEANS_IN_PROMPT], leans[_MAX_LEANS_IN_PROMPT:]
    for r in shown:
        lines.append(_render_rule(r))

    if not lines:
        return ""

    head = f"TIM'S RULES FOR {first_name.upper()} — these APPLY right now:"
    # Telling him what was left out is deliberate. Silent filtering is exactly how you
    # end up staring at a rule you wrote, wondering why it did nothing.
    tail = []
    if held:
        tail.append(f"{len(held)} less specific")
    if split.skipped:
        tail.append(f"{len(split.skipped)} that do not apply here")
    if tail:
        n = len(held) + len(split.skipped)
        lines.append(
            f"  ({n} other rule{'s' if n != 1 else ''} on file — "
            f"{', '.join(tail)} — not shown)"
        )
    return head + "\n" + "\n".join(lines)
