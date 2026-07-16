"""The four taps must produce the RIGHT verdict.

    spoke   + 👍  ->  right to jump in     ->  SPEAKS
    spoke   + 👎  ->  shouldn't have       ->  STAYS QUIET
    skipped + 👍  ->  right to stay out    ->  STAYS QUIET
    skipped + 👎  ->  should have spoken   ->  SPEAKS

    i.e.  SPEAKS  <=>  spoke == approved

Get this backwards and every rule Tim writes teaches the room the EXACT OPPOSITE of what
he meant — and it would look correct the entire time, because the dialog would still say
sensible things and the rule would still save. The only way to catch it is to assert on
the direction the server derives, and on the verdict the model is TOLD to aim for.
"""
import io
import sys

# Windows' console is cp1252 and cannot encode 👍. Force UTF-8 rather than lose the test.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{' OK ' if cond else 'FAIL'}] {label}" + (f"  — {detail}" if detail and not cond else ""))


# The exact expression from app.api_feedback. Lifted so a drift between them fails here.
def direction_for(spoke: bool, approved: bool) -> tuple[str, bool]:
    wants_speech = (spoke == approved)
    direction = (
        "was_right_to_speak"      if spoke and approved else
        "shouldnt_have_spoken"    if spoke else
        "was_right_to_stay_quiet" if approved else
        "should_have_spoken"
    )
    return direction, wants_speech


print("=== the four combinations ===")
CASES = [
    # spoke, thumbs-up, expected direction,          should the rule make him SPEAK?
    (True,  True,  "was_right_to_speak",      True,   "he spoke and Tim liked it"),
    (True,  False, "shouldnt_have_spoken",    False,  "he spoke and shouldn't have"),
    (False, True,  "was_right_to_stay_quiet", False,  "he stayed out and Tim liked it"),
    (False, False, "should_have_spoken",      True,   "he stayed out and shouldn't have"),
]
for spoke, up, want_dir, want_speech, why in CASES:
    d, speech = direction_for(spoke, up)
    icon = "👍" if up else "👎"
    state = "spoke  " if spoke else "skipped"
    check(f"{state} + {icon} -> {want_dir:24} rule says {'SPEAKS' if want_speech else 'STAYS QUIET':11} ({why})",
          d == want_dir and speech == want_speech,
          f"got {d} / speech={speech}")

print("\n=== the invariant that makes it one rule ===")
check("SPEAKS <=> spoke == approved",
      all(direction_for(s, a)[1] == (s == a) for s in (True, False) for a in (True, False)))

print("\n=== the two CONFIRMING cases are distinguishable from the two CORRECTING ones ===")
confirming = {direction_for(True, True)[0], direction_for(False, True)[0]}
correcting = {direction_for(True, False)[0], direction_for(False, False)[0]}
check("a 👍 always confirms", all(d.startswith("was_right") for d in confirming), str(confirming))
check("a 👎 always corrects", not any(d.startswith("was_right") for d in correcting), str(correcting))
check("no direction is reachable two ways", len(confirming | correcting) == 4)

print("\n=== the app's own prompt map covers all four ===")
import re
src = open("app.py", encoding="utf-8").read()
block = src[src.index("complaint, wants = {"):src.index("}.get(direction,")]
for _, _, d, want_speech, _ in CASES:
    check(f"{d:24} is in the prompt map", f'"{d}"' in block)
    # And the model must be told to aim the RIGHT way.
    seg = block.split(f'"{d}"')[1].split("),")[0] if f'"{d}"' in block else ""
    says_speak = "SPEAK" in seg and "STAY QUIET" not in seg and "STAYING QUIET" not in seg
    says_quiet = "STAY QUIET" in seg or "STAYING QUIET" in seg
    check(f"  ...and it aims at {'SPEAK' if want_speech else 'QUIET'}",
          (says_speak if want_speech else says_quiet),
          seg.replace("\n", " ")[:100])


# ===============================================================================
#  WHERE `spoke` COMES FROM
#
#  Everything above tests what the server does GIVEN (spoke, approved). It never asked
#  where `spoke` came from -- and that is exactly the hole the real bug walked through.
#
#  The relay gives the mic to speakers + leaners + floor. The frozen decision was storing
#  only `speaking`, which is the coordinator's PLAN. So a kin who BARGED IN and talked was
#  recorded as having stayed quiet. Bobby leaned in, spoke, Tim tapped a thumbs-up to
#  reinforce it -- and the dialog asked him to explain why Bobby had been right to say
#  nothing. A rule written from that would have taught the room the precise opposite of
#  what he meant, and it would have looked correct at every step.
# ===============================================================================
print("")
print("=== `spoke` means WHO GOT THE MIC, not who was on the plan ===")

import app  # noqa: E402

SPOKE_CASES = [
    ("on the coordinator's mic order", {"speaking": ["bobby"]}, True),
    ("BARGED IN (this was the bug)", {"speaking": ["adam"],
                                      "leaned_in": [{"key": "bobby", "reason": "x"}]}, True),
    ("pulled in off the floor", {"speaking": ["adam"], "floor": ["bobby"]}, True),
    ("forced to speak by a LAW", {"speaking": ["adam"],
                                  "law_forced": [{"key": "bobby", "reason": "x"}]}, True),
    ("stayed silent", {"speaking": ["adam"], "silent": ["bobby"]}, False),
    ("tapped, but PASSED", {"speaking": ["adam"],
                            "passed": [{"key": "bobby", "reason": "not for me"}]}, False),
    ("held back for a private moment", {"speaking": ["adam"],
                                        "held_back": [{"key": "bobby", "reason": "x"}]}, False),
    ("silenced by a LAW", {"speaking": ["adam"],
                           "law_silenced": [{"key": "bobby", "reason": "x"}]}, False),
]
for _label, _decision, _want in SPOKE_CASES:
    _got = "bobby" in app._who_spoke(_decision)
    check(f"{_label:34} -> spoke={_want}", _got == _want, f"got {_got}")

print("")
print("=== the real turn 25, exactly as it was frozen ===")
TURN_25 = {
    "speaking": ["adam"],
    "leaned_in": [{"key": "bobby", "reason": "Bobby knows every trick in that book."}],
    "floor": [], "held_back": [], "passed": [], "law_forced": [], "law_silenced": [],
    "silent": ["jeff", "thomas"],
}
check("bobby spoke (he leaned in)", "bobby" in app._who_spoke(TURN_25))
check("adam spoke", "adam" in app._who_spoke(TURN_25))
check("jeff did not", "jeff" not in app._who_spoke(TURN_25))
check("thomas did not", "thomas" not in app._who_spoke(TURN_25))
_d, _speech = direction_for(True, True)
check("...so a thumbs-up on him asks about SPEAKING",
      _d == "was_right_to_speak" and _speech, _d)

print("")
print("=== a turn frozen with the new `spoke` key is trusted verbatim ===")
check("explicit `spoke` wins over the buckets",
      app._who_spoke({"spoke": ["bobby"], "speaking": [], "leaned_in": []}) == {"bobby"})

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
