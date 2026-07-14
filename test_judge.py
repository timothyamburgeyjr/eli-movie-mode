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

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
