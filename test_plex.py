"""Which session is Tim actually watching?

THE GHOST
---------
Plex Web does not clean up after itself. Switch episodes and the OLD session lingers in
/status/sessions — same machineIdentifier, same user, same device — and it is frequently
listed FIRST.

The picker was `metas[0]`. So when Tim moved from "Skin" to "Hook Man", the app kept reading
the ghost, never saw the rating_key change, never fired `media_change`, and therefore never
briefed the room, never analysed a scene, never did anything at all. Silently, for as long as
he kept watching. He found it because he switched an episode and nothing happened.

The old "prefer type == movie" rule never helped, because both were EPISODES — it fell
straight through to metas[0].

The payload below is the REAL one, captured from his server at the moment it was broken.
"""
import sys

import plex_monitor as pm

ok = True


def check(label: str, cond: bool, detail: str = "") -> None:
    global ok
    if not cond:
        ok = False
    print(f"  [{' OK ' if cond else 'FAIL'}] {label}" + (f"  — {detail}" if detail and not cond else ""))


def _sess(rating_key, title, ep, state, last_viewed, player="65jhj48ja7by6b"):
    return {
        "ratingKey": rating_key, "type": "episode",
        "title": title, "grandparentTitle": "Supernatural",
        "parentIndex": 1, "index": ep,
        "duration": 2500000, "viewOffset": 110000,
        "lastViewedAt": last_viewed,
        "Player": {"state": state, "product": "Plex Web", "device": "Windows",
                   "machineIdentifier": player, "title": "Plex Web"},
        "User": {"title": "timothy.318"},
        "Media": [{"container": "mkv", "Part": [{"key": "/x"}]}],
    }


# Captured live, in this order — the ghost FIRST, exactly as Plex sent it.
GHOSTED = {"MediaContainer": {"Metadata": [
    _sess("36889", "Skin", 6, "paused", 1757273811),        # the one he LEFT
    _sess("36890", "Hook Man", 7, "paused", 1757276292),    # the one he is ON
]}}

print("=== the ghost does not win ===")
old_first = GHOSTED["MediaContainer"]["Metadata"][0]["title"]
check("Plex really does list the ghost first", old_first == "Skin", old_first)

got = pm._extract_active(GHOSTED)
check("the picker follows the NEWEST session on that player",
      got["rating_key"] == "36890", f"picked {got['title']!r}")
check("...which is the episode he actually switched to", got["title"] == "Hook Man", got["title"])

cands = pm._candidates(GHOSTED)
check("one player -> ONE candidate (the ghost is dropped)", len(cands) == 1, str(len(cands)))

print("\n=== playing beats paused ===")
TWO_PLAYERS = {"MediaContainer": {"Metadata": [
    _sess("36889", "Skin", 6, "paused", 1757276999, player="livingroom-tv"),
    _sess("36890", "Hook Man", 7, "playing", 1757273811, player="tims-laptop"),
]}}
got = pm._extract_active(TWO_PLAYERS)
check("a PLAYING session outranks a more recent paused one",
      got["title"] == "Hook Man", got["title"])
check("two real players -> two candidates, so he gets asked",
      len(pm._candidates(TWO_PLAYERS)) == 2)

print("\n=== the sticky player: nobody else gets to yank him off his film ===")
# Someone starts something in another room. The app must NOT follow them.
got = pm._extract_active(TWO_PLAYERS, prefer_player="livingroom-tv")
check("we stay on the player we were already following",
      got["title"] == "Skin", got["title"])

print("\n=== TIM'S CHOICE OUTRANKS EVERY HEURISTIC ===")
# The whole point of asking him is that his answer is final.
got = pm._extract_active(TWO_PLAYERS, prefer_player="tims-laptop", pinned="36889")
check("a pin beats the sticky player", got["rating_key"] == "36889", got["title"])
# A pin only ever applies to a REAL candidate. That is not a limitation — the picker only
# ever OFFERS candidates, so Tim can't pin a ghost even if he wanted to. If he could, a
# stale session could be pinned forever and the app would sit on it until he noticed.
got = pm._extract_active(GHOSTED, pinned="36889")
check("a pin cannot resurrect a ghost (it isn't on the menu)",
      got["rating_key"] == "36890", got["title"])
got = pm._extract_active(GHOSTED, pinned="99999")
check("a pin on something that has GONE falls back, it doesn't stall",
      got["rating_key"] == "36890", got["title"])

print("\n=== the fields the picker depends on are actually carried through ===")
one = pm._candidates(GHOSTED)[0]
for f in ("player_id", "player_name", "last_seen", "rating_key", "state"):
    check(f"_parse_meta carries {f!r}", one.get(f) not in (None, ""), repr(one.get(f)))

print("\n=== nothing playing at all ===")
check("empty sessions -> None, not a crash",
      pm._extract_active({"MediaContainer": {"Metadata": []}}) is None)
check("garbage -> None, not a crash", pm._extract_active({}) is None)

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
