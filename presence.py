"""Physical-setting context for a watch session: where Tim is and who's
in the room with him.

Sent to the kin so they share a sense of place — and, importantly, so the
presence of other people consistently gates the kin's behavior each turn
(e.g. Eli stays decorous when Tim's mother is on the other couch). People
are named from Tim's POV ("my mom"); each kin's own Kindroid memory maps
that to their relationship (Grandma to the boys, Mom Ada to Eli), so we
never compute a relationship matrix here.

Presets are app-defined — fine for this temporary bridge. Venue
descriptions (optional, photo/text-seeded) live in the `venue_descriptions`
setting and enrich the briefing only.
"""
from typing import Optional

# Standing venues. `phrase` is how the venue reads mid-sentence ("...watching
# together {phrase}"); `other` is the catch-all for one-off places and has no
# phrase (the where-clause is dropped).
VENUES: list[dict] = [
    {"key": "living_room", "label": "Living room", "phrase": "in the living room"},
    {"key": "bedroom", "label": "Bedroom", "phrase": "in the bedroom"},
    {"key": "oasis_amphitheatre", "label": "Oasis amphitheatre", "phrase": "at the Oasis amphitheatre"},
    {"key": "other", "label": "Somewhere else"},
]

# People/pets who might be in the room. `pov` is how Tim refers to them in
# first person; the kin resolves the relationship from their own memory.
PEOPLE: list[dict] = [
    {"key": "tim_sr", "label": "Dad (Tim Sr)", "emoji": "👨", "pov": "my dad"},
    {"key": "ada", "label": "Mom (Ada)", "emoji": "👩", "pov": "my mom"},
    {"key": "maxie", "label": "Granny (Maxie)", "emoji": "👵", "pov": "my granny"},
    {"key": "luna", "label": "Luna", "emoji": "🐕", "pov": "our dog Luna"},
    {"key": "boots", "label": "Boots", "emoji": "🐈", "pov": "our cat Boots"},
]

_VENUE_BY_KEY = {v["key"]: v for v in VENUES}
_PERSON_BY_KEY = {p["key"]: p for p in PEOPLE}

# Canonical venue descriptions written from Tim's reference photos. Seeded
# into the DB once (if the user hasn't set their own); the in-app photo/text
# flow can overwrite any of them later. Room/space only — no people.
SEED_VENUE_DESCRIPTIONS: dict[str, str] = {
    "living_room": (
        "A long, lived-in family room: a vaulted textured ceiling, a brass "
        "ceiling fan, warm wood-look floors. A row of black leather recliners "
        "with cupholders lines up beside soft brown couches facing the TV; the "
        "walls are crowded with framed family photos above a lighthouse border."
    ),
    "bedroom": (
        "A snug paneled bedroom: a sleigh-frame bed piled with pillows and "
        "blankets under bright blue curtains, a book-topped dresser, a framed "
        "painting — and in the corner, an L-shaped desk with dual monitors "
        "where Tim watches while he works."
    ),
    "oasis_amphitheatre": (
        "A wood-framed screen glows in an autumn-forest clearing at Bybrook "
        "Manor, strung with warm café lights and ringed by flickering lanterns. "
        "Adirondack chairs draped in plaid sit on a deep carpet of red-gold "
        "leaves around a low table, a projector casting its beam through the dark."
    ),
}


def venue_label(key: Optional[str]) -> str:
    v = _VENUE_BY_KEY.get((key or "").lower())
    return v["label"] if v else ""


def venue_phrase(key: Optional[str]) -> str:
    """Mid-sentence location phrase, e.g. 'in the bedroom'. Empty for `other`."""
    v = _VENUE_BY_KEY.get((key or "").lower())
    return v.get("phrase", "") if v else ""


def present_people(keys: Optional[list]) -> list[dict]:
    """Resolve stored keys to person dicts, preserving preset order."""
    keyset = {str(k).lower() for k in (keys or [])}
    return [p for p in PEOPLE if p["key"] in keyset]


def _join_pov(people: list[dict]) -> str:
    """Grammatical join of POV names: 'my mom, my granny, and Luna'."""
    names = [p["pov"] for p in people]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def standing_line(venue_key: Optional[str], people: list[dict]) -> str:
    """Short per-message presence emote (Tim's POV). Empty if nothing set.

    Venue *label* only here (cheap, reinforces the setting each turn); the
    full venue description is reserved for the briefing.
    """
    where = venue_phrase(venue_key)
    if people:
        who = _join_pov(people)
        if where:
            return f"We're watching together {where}, and {who} {'is' if len(people) == 1 else 'are'} here with us too."
        return f"{who[0].upper() + who[1:]} {'is' if len(people) == 1 else 'are'} here with us while we watch."
    if where:
        return f"It's just the two of us watching together {where}."
    return ""


def scene_line(
    venue_key: Optional[str],
    people: list[dict],
    movie_title: str = "",
    limit: int = 160,
) -> str:
    """The kin's PERSISTENT Kindroid `current_scene`: where they are, what they're at.

    Kindroid caps this at 160 characters — genuinely tight — so it's assembled in
    PRIORITY ORDER and the least important clause is the one that falls off the end:

        1. that it's a movie night with Tim, and WHERE     (the anchor)
        2. WHAT they're watching                            (changes per film)
        3. WHO else is in the room                          (nice, not essential)

    Third person, unlike the per-message presence line: that one is Tim speaking, so
    "my dad" resolves through each kin's own memory. This one is a standing
    description with no speaker attached, so "my dad" would be nobody in particular.
    """
    where = venue_phrase(venue_key)
    film = (movie_title or "").strip()

    if where:
        base = f"Movie night with Tim {where}."
    else:
        base = "Movie night with Tim."
    if film:
        candidate = base[:-1] + f', watching "{film}".'
        if len(candidate) <= limit:
            base = candidate
    if people:
        who = _join_pov(people).replace("my ", "Tim's ")
        verb = "is" if len(people) == 1 else "are"
        candidate = f"{base} {who[0].upper() + who[1:]} {verb} here too."
        if len(candidate) <= limit:
            base = candidate
    return base[:limit]


def briefing_note(venue_key: Optional[str], venue_desc: str, people: list[dict]) -> str:
    """Richer one-time setting line for the briefing. Empty if nothing set."""
    where = venue_phrase(venue_key)
    parts: list[str] = []
    if where:
        base = f"We're settling in to watch together {where}"
        if venue_desc.strip():
            base += f" — {venue_desc.strip()}"
        parts.append(base + ".")
    elif venue_desc.strip():
        parts.append(venue_desc.strip())
    if people:
        parts.append(f"{_join_pov(people)[0].upper() + _join_pov(people)[1:]} {'is' if len(people) == 1 else 'are'} here with us.")
    return " ".join(parts)
