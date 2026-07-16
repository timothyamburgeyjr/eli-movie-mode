"""The briefing reaches EVERYONE in the room — and an empty one is never silence.

WHAT HAPPENED
-------------
Tim switched to Bloody Mary. Gemini returned an empty briefing. The code did:

    if briefing_text.strip():
        await _send_to_kindroid_and_render(...)

An empty string fails that check, so the relay never ran. No exception, no system message, no
retry. Tim got a blank card and a family who had never been told a film had started — and he
only found out by opening their Kindroids one by one and seeing nothing there.

The briefing is not commentary. It is the lights going down: the one thing that tells everyone
PRESENT, talking or listening, that the show has begun. Silence is the one outcome it must
never produce.

THE ROOM, NOT THE ADDRESSED
---------------------------
Verified against a real briefing (msg 7115): the room was jeff/adam/thomas/bobby with only
adam in "talking to", and all four answered. The fan-out was already right. It was the empty
string that broke it.
"""
import inspect
import sys

import app

ok = True


def check(label: str, cond: bool, detail: str = "") -> None:
    global ok
    if not cond:
        ok = False
    print(f"  [{' OK ' if cond else 'FAIL'}] {label}" + (f"  — {detail}" if detail and not cond else ""))


print("=== an empty briefing is never silence ===")
src = inspect.getsource(app._generate_briefing_card)

check("the `if briefing_text.strip():` guard is GONE",
      "if briefing_text.strip():" not in src,
      "the guard that swallowed the empty briefing is still there")
check("an empty briefing falls back to a plain one",
      "_mechanical_briefing(plex_data, is_continuation)" in src)
check("...and Tim is told it did", "came back empty" in src)
check("the relay runs on the ROOM, not the addressed subset",
      "_room_and_addressed()" in src and "if room:" in src)

print("\n=== the plain briefing can be written from Plex alone, so it cannot fail ===")
CASES = [
    ("a film with everything",
     {"title": "Bloody Mary", "year": 2006, "director": "Peter Ellis",
      "summary": "A series of deaths linked to a mirror."}, True,
     ["Bloody Mary", "2006", "Peter Ellis", "mirror"]),
    ("a TV episode",
     {"title": "Phantom Traveler", "series_title": "Supernatural",
      "season_number": 1, "episode_number": 4, "summary": "A plane crash."}, True,
     ["Supernatural", "season 1", "episode 4", "Phantom Traveler"]),
    ("Plex gave us only a title",
     {"title": "Bloody Mary"}, False,
     ["Bloody Mary"]),
    ("Plex gave us NOTHING AT ALL",
     {}, False,
     ["something"]),          # it still says a film is starting. That is the whole job.
]
for label, data, cont, must in CASES:
    text = app._mechanical_briefing(data, cont)
    check(f"{label:28} -> a real briefing", bool(text.strip()) and len(text) > 20, repr(text))
    for m in must:
        check(f"   ...mentions {m!r}", m in text, text)

print("\n=== it is FIRST PERSON — Kindroid renders the outgoing message as the SENDER's ===")
# So this is Tim talking to the room, not a narrator describing Tim. Getting this backwards
# makes every kin think a stranger is announcing the film.
t = app._mechanical_briefing({"title": "Alien", "year": 1979}, False)
check("no third-person narration of Tim", " Tim " not in t and not t.startswith("Tim"), t)
check("it says the show is starting",
      any(w in t.lower() for w in ("putting on", "next up")), t)

print("\n=== a continuation says so (it is the SWITCH he wants warned about) ===")
first = app._mechanical_briefing({"title": "Alien"}, False)
later = app._mechanical_briefing({"title": "Aliens"}, True)
check("the opening film: 'Putting on'", "Putting on" in first, first)
check("a switch mid-session: 'Next up'", "Next up" in later, later)

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
