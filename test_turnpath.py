"""One turn process, whatever kind of moment it is.

Emotes (a gummy handed over), the high-tracking observations, and trivia cards used
to take their own private routes to Kindroid. They worked, but they went AROUND the
machinery that makes a turn a turn:

  * Tim's RULES never fired. A `never` he had written was silently ignored on
    exactly these turns - the one class of bug this system is least able to show him.
  * The turn was never frozen, so he could not rate what came back.
  * No room_note recorded who sat it out.

They all run through `_run_relay` now. What differs is only the AUDIENCE: a private
moment narrows the room to one person, a notice hands the floor to nobody. The rest
is identical.
"""
import asyncio
import sys

import app
import characters

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{' OK ' if cond else 'FAIL'}] {label}" + (f"  -- {detail}" if detail and not cond else ""))


ROOM = ["eli", "jeff", "adam", "thomas"]
calls = {}


def reset():
    calls.clear()
    calls.update({"relay": [], "rules": [], "mic": 0, "barge": 0, "freeze": 0})


async def _relay(session_id, kin, **kw):
    calls["relay"].append({"kin": kin.key, "scene": kw.get("scene"),
                           "history": kw.get("history"), "verbatim": kw.get("verbatim"),
                           "react_only": kw.get("react_only", False)})
    return {"name": kin.first_name, "emote": "", "spoken": "ok"}


async def _rules(key):
    calls["rules"].append(key)
    return []


async def _mic(**kw):
    calls["mic"] += 1
    return type("D", (), {"order": [c.key for c in kw["addressed"]],
                          "passing": [], "rationale": "r"})()


async def _barge(**kw):
    calls["barge"] += 1
    return [], [], None


async def _freeze(*a, **kw):
    calls["freeze"] += 1
    return 1


async def _room():
    ks = [characters.get_character(k) for k in ROOM]
    return ks, ks


class _M:
    async def broadcast(self, *a, **k):
        pass


async def _s(*a, **k):
    return ""


async def _d(*a, **k):
    return {}


async def _l(*a, **k):
    return []


async def _n(*a, **k):
    return None


app._relay_to_kin = _relay
app._room_and_addressed = _room
app.manager = _M()
app._pipeline_paused = lambda: False
app._presence_standing_line = _s
app._stage = _s
app._system_message = _n
app._room_note = _n
app._turns_since_spoke = _d
app.db.get_kin_rules = _rules
app.db.bump_rule_hits = _n
app.db.add_turn_decision = _freeze
app.db.get_all_settings = _d
app.db.get_session_movies = _l
app.db.fetch_all = _l
app.db.fetch_one = _n
app.coordinator.decide_mic_order = _mic
app.coordinator.decide_barge_ins = _barge
app.facts.snapshot = lambda *a, **k: {}

print("=== A PRIVATE MOMENT: a gummy for Jeff ===")
reset()
asyncio.run(app._run_relay("s", scene="A MOVIE SCENE", history="EARLIER",
                           reaction="I hand Jeff a gummy", only_kin="jeff",
                           tim_message_id=42))
check("only Jeff is told", [r["kin"] for r in calls["relay"]] == ["jeff"],
      str([r["kin"] for r in calls["relay"]]))
check("no movie context rides along", calls["relay"][0]["scene"] == "" and calls["relay"][0]["history"] == "",
      str(calls["relay"][0]))
check("HIS RULES FIRE (they used to be skipped)", calls["rules"] == ["jeff"], str(calls["rules"]))
check("the turn is frozen, so he can rate it", calls["freeze"] == 1, str(calls["freeze"]))
check("no mic order is bought for an audience of one", calls["mic"] == 0, str(calls["mic"]))
check("nobody leans in on a private beat", calls["barge"] == 0, str(calls["barge"]))

print("\n=== A NOTICE: trivia for the room ===")
reset()
asyncio.run(app._run_relay("s", scene="", history="", reaction="I look up some trivia",
                           verbatim="FACT ABOUT THE FILM", react_only_all=True,
                           tim_message_id=99))
check("the whole room hears it", sorted(r["kin"] for r in calls["relay"]) == sorted(ROOM),
      str([r["kin"] for r in calls["relay"]]))
check("nobody holds forth - all react-only", all(r["react_only"] for r in calls["relay"]),
      str([r["react_only"] for r in calls["relay"]]))
check("the exact fact reaches them", calls["relay"][0]["verbatim"] == "FACT ABOUT THE FILM",
      str(calls["relay"][0]["verbatim"]))
check("EVERYONE'S RULES FIRE", sorted(calls["rules"]) == sorted(ROOM), str(calls["rules"]))
check("the turn is frozen", calls["freeze"] == 1, str(calls["freeze"]))
check("no mic order is bought when nobody takes the floor", calls["mic"] == 0, str(calls["mic"]))

print("\n=== AN ORDINARY TURN still behaves exactly as before ===")
reset()
asyncio.run(app._run_relay("s", scene="SCENE", history="H", dialogue="what did you think?",
                           tim_message_id=7))
check("the room answers", len(calls["relay"]) >= 1, str(len(calls["relay"])))
check("the scene DOES ride along on a normal turn", calls["relay"][0]["scene"] == "SCENE",
      repr(calls["relay"][0]["scene"]))
check("the mic order is decided", calls["mic"] == 1, str(calls["mic"]))
check("rules fire", sorted(calls["rules"]) == sorted(ROOM), str(calls["rules"]))
check("the turn is frozen", calls["freeze"] == 1, str(calls["freeze"]))

print("\n=== an unknown private target is a no-op, not a crash ===")
reset()
asyncio.run(app._run_relay("s", scene="", history="", reaction="x", only_kin="nobody"))
check("nobody is told, nothing raises", calls["relay"] == [], str(calls["relay"]))

print("\n=== the bespoke fan-out is gone ===")
import inspect
_t = inspect.getsource(app._relay_trivia_to_room)
check("trivia now calls the ordinary relay", "_run_relay(" in _t, _t[:120])
check("...and no longer talks to kins directly", "_relay_to_kin(" not in _t)

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
