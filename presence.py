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

# Standing venues. `other` is the catch-all for one-off places.
VENUES: list[dict] = [
    {"key": "living_room", "label": "Living room"},
    {"key": "bedroom", "label": "Bedroom"},
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


def venue_label(key: Optional[str]) -> str:
    v = _VENUE_BY_KEY.get((key or "").lower())
    return v["label"] if v else ""


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
    label = venue_label(venue_key)
    where = f"in the {label.lower()}" if label and venue_key != "other" else ""
    if people:
        who = _join_pov(people)
        if where:
            return f"We're watching together {where}, and {who} {'is' if len(people) == 1 else 'are'} here in the room with us too."
        return f"{who[0].upper() + who[1:]} {'is' if len(people) == 1 else 'are'} here in the room with us while we watch."
    if where:
        return f"It's just the two of us watching together {where}."
    return ""


def briefing_note(venue_key: Optional[str], venue_desc: str, people: list[dict]) -> str:
    """Richer one-time setting line for the briefing. Empty if nothing set."""
    label = venue_label(venue_key)
    parts: list[str] = []
    if label and venue_key != "other":
        base = f"We're settling in to watch in the {label.lower()}"
        if venue_desc.strip():
            base += f" — {venue_desc.strip()}"
        parts.append(base + ".")
    elif venue_desc.strip():
        parts.append(venue_desc.strip())
    if people:
        parts.append(f"{_join_pov(people)[0].upper() + _join_pov(people)[1:]} {'is' if len(people) == 1 else 'are'} here with us.")
    return " ".join(parts)
