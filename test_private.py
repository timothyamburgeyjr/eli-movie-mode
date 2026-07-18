"""Some moments are between two people.

Handing someone a gummy, or noticing that one person has gone quiet and soft-eyed,
used to go out as a normal turn: the whole room got it, all eight answered a thing
that was plainly for one of them, and a full clip + Gemini scene analysis was spent
describing a movie that had nothing to do with it.

These are now PRIVATE: one kin, and no movie context at all.
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
sent = []


async def _fake_relay_to_kin(session_id, kin, **kw):
    sent.append({"kin": kin.key, "scene": kw.get("scene"), "history": kw.get("history"),
                 "reaction": kw.get("reaction"), "addressed": kw.get("addressed_names")})
    return None


async def _fake_room():
    ks = [characters.get_character(k) for k in ROOM]
    return ks, ks


class _Mgr:
    async def broadcast(self, *a, **k):
        pass


app._relay_to_kin = _fake_relay_to_kin
app._room_and_addressed = _fake_room
app.manager = _Mgr()
app._pipeline_paused = lambda: False


async def _noop_presence():
    return "presence"


async def _noop_stage(*a, **k):
    pass


app._presence_standing_line = _noop_presence
app._stage = _noop_stage

print("=== a private moment reaches ONE person ===")
sent.clear()
asyncio.run(app._run_relay("s1", scene="a movie scene", history="earlier stuff",
                           reaction="I hand Jeff a gummy", only_kin="jeff"))
check("exactly one kin was told", [s["kin"] for s in sent] == ["jeff"], str([s["kin"] for s in sent]))
check("the other three never heard it", len(sent) == 1, str(len(sent)))
check("they're addressed directly, not overhearing", sent[0]["addressed"] == ["Jeff"], str(sent[0]["addressed"]))
check("the moment itself rides along", "gummy" in sent[0]["reaction"], sent[0]["reaction"])

print("\n=== and carries NO movie context ===")
check("the scene is dropped even when one was passed", sent[0]["scene"] == "", repr(sent[0]["scene"]))
check("...and so is the history", sent[0]["history"] == "", repr(sent[0]["history"]))

print("\n=== an unknown kin is a no-op, not a crash ===")
sent.clear()
asyncio.run(app._run_relay("s1", scene="", history="", reaction="x", only_kin="nobody"))
check("nobody is told, nothing raises", sent == [], str(sent))

print("\n=== without only_kin, the room still works as before ===")
import inspect
_src = inspect.getsource(app._run_relay)
check("the private branch is opt-in (guarded on only_kin)", "if only_kin:" in _src)
check("the normal path still passes the mic around", "speakers + leaners" in _src)

print("\n=== both private callers are wired ===")
_fire = inspect.getsource(app._fire_stoned_emote)
check("a private emote skips the scene pipeline entirely",
      "_process_tim_message" in _fire and "return" in _fire.split("if only_kin:")[1].split("_process_tim_message")[0])
check("handing something over is private", "only_kin=kin.key" in inspect.getsource(app.api_log_ingestion))
check("the high-tracking observation is private too",
      "only_kin=kin.key" in inspect.getsource(app._maybe_fire_reinforcement))

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
