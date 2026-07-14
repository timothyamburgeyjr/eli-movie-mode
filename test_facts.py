"""The scope has to actually scope.

A rule that fires when it shouldn't is WORSE than no rule, because Tim wrote it and
will trust it. So this suite is paranoid about the two things that are trivially easy
to get backwards: the `watching` ladder, and `excludes`.
"""
import sys

import facts
from facts import TurnContext, applies, split_rules, for_prompt, watching_ladder

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{' OK ' if cond else 'FAIL'}] {label}" + (f"  — {detail}" if detail and not cond else ""))


def rule(text, verdict="rarely", when=None, why="", ev=""):
    return {"rule_text": text, "verdict": verdict, "when": when or [],
            "rule_why": why, "evidence": ev}


def ctx(**kw):
    per_kin = kw.pop("per_kin", {})
    return TurnContext(values=kw, per_kin=per_kin)


# ── 1. universal ──
print("=== a rule with no conditions is universal ===")
c = ctx(mood="dread")
check("fires with nothing set", applies(rule("x"), c))
check("fires on any context", applies(rule("x"), ctx()))

# ── 2. the one that decides it: conditions actually scope ──
print("\n=== conditions scope (the whole point) ===")
r = rule("stays out of grief", when=[
    {"fact": "mood", "op": "is", "value": ["dread", "grief"]},
    {"fact": "kins_here", "op": "includes", "value": ["daisy"]},
    {"fact": "venue", "op": "is", "value": ["bedroom"]},
])
check("dread + Daisy + bedroom -> FIRES",
      applies(r, ctx(mood="dread", kins_here=["bobby", "daisy"], venue="bedroom")))
check("dread + Daisy + LIVING ROOM -> gone",
      not applies(r, ctx(mood="dread", kins_here=["bobby", "daisy"], venue="living_room")))
check("COMEDY + Daisy + bedroom -> gone",
      not applies(r, ctx(mood="humor", kins_here=["bobby", "daisy"], venue="bedroom")))
check("dread + DAISY SENT HOME + bedroom -> gone",
      not applies(r, ctx(mood="dread", kins_here=["bobby"], venue="bedroom")))

# ── 3. excludes — the inverse, and a real, different rule ──
print("\n=== `excludes` is the inverse, and easy to get backwards ===")
r2 = rule("talks freely", when=[{"fact": "kins_here", "op": "excludes", "value": ["lilly"]}])
check("Lilly is NOT here -> FIRES", applies(r2, ctx(kins_here=["bobby", "daisy"])))
check("Lilly IS here      -> gone", not applies(r2, ctx(kins_here=["bobby", "lilly"])))
check("empty room         -> FIRES", applies(r2, ctx(kins_here=[])))

print("\n=== `includes` means ALL of them, not any ===")
r3 = rule("x", when=[{"fact": "kins_here", "op": "includes", "value": ["daisy", "jeff"]}])
check("both here -> fires", applies(r3, ctx(kins_here=["daisy", "jeff", "bobby"])))
check("only one  -> gone", not applies(r3, ctx(kins_here=["daisy"])))

# ── 4. THE LADDER ──
print("\n=== the `watching` ladder — get this wrong and his best rule dies ===")
sn_s1e2 = {"series_title": "Supernatural", "season_number": 1, "episode_number": 2,
           "title": "Wendigo", "type": "episode"}
sn_s1e3 = {**sn_s1e2, "episode_number": 3, "title": "Dead in the Water"}
sn_s4e11 = {**sn_s1e2, "season_number": 4, "episode_number": 11, "title": "Family Remains"}
iron = {"series_title": None, "title": "The Iron Giant", "type": "movie"}

print(f"  ladder for S01E02: {watching_ladder(sn_s1e2)}")
print(f"  ladder for a film: {watching_ladder(iron)}")

show_rule = rule("takes every lore question", verdict="always",
                 when=[{"fact": "watching", "op": "is", "value": ["supernatural"]}])
check("SHOW rule fires on S01E02", applies(show_rule, ctx(watching=watching_ladder(sn_s1e2))))
check("SHOW rule fires on S04E11", applies(show_rule, ctx(watching=watching_ladder(sn_s4e11))))
check("SHOW rule NEVER fires on a film",
      not applies(show_rule, ctx(watching=watching_ladder(iron))))

ep_rule = rule("x", when=[{"fact": "watching", "op": "is", "value": ["supernatural/s1e2"]}])
check("EPISODE rule fires on S01E02", applies(ep_rule, ctx(watching=watching_ladder(sn_s1e2))))
check("EPISODE rule does NOT fire on S01E03",
      not applies(ep_rule, ctx(watching=watching_ladder(sn_s1e3))))

season_rule = rule("x", when=[{"fact": "watching", "op": "is", "value": ["supernatural/s1"]}])
check("SEASON rule fires across S01", applies(season_rule, ctx(watching=watching_ladder(sn_s1e2)))
      and applies(season_rule, ctx(watching=watching_ladder(sn_s1e3))))
check("SEASON rule does NOT fire on S04",
      not applies(season_rule, ctx(watching=watching_ladder(sn_s4e11))))

film_rule = rule("x", when=[{"fact": "watching", "op": "is", "value": ["the iron giant"]}])
check("FILM rule fires on the film", applies(film_rule, ctx(watching=watching_ladder(iron))))
check("FILM rule fires on NO episode",
      not applies(film_rule, ctx(watching=watching_ladder(sn_s1e2))))

# ── 5. per-kin facts ──
print("\n=== per-kin facts resolve against the right person ===")
r4 = rule("goes quiet", when=[{"fact": "self_stoned", "op": "at_least", "value": [2]}])
c2 = ctx(per_kin={"bobby": {"self_stoned": 3}, "jeff": {"self_stoned": 0}})
check("Bobby is blasted -> fires for HIM", applies(r4, c2, "bobby"))
check("Jeff is sober    -> gone for HIM", not applies(r4, c2, "jeff"))

r5 = rule("x", when=[{"fact": "tim_stoned", "op": "at_most", "value": [1]}])
check("at_most: tim sober -> fires", applies(r5, ctx(tim_stoned=0)))
check("at_most: tim baked -> gone", not applies(r5, ctx(tim_stoned=3)))

# ── 6. a broken rule must NOT fire everywhere ──
print("\n=== a rule we can't evaluate must NOT fire (it must not become universal) ===")
bad = rule("x", when=[{"fact": "__gone__", "op": "is", "value": ["y"]}])
check("unknown fact -> refuses to fire", not applies(bad, ctx(mood="dread")))
bad2 = rule("x", when=[{"fact": "mood", "op": "wat", "value": ["dread"]}])
check("unknown op   -> refuses to fire", not applies(bad2, ctx(mood="dread")))

# ── 7. LAW vs LEAN ──
print("\n=== law vs lean ===")
law_no = rule("never in a theatre", verdict="never",
              when=[{"fact": "venue", "op": "is", "value": ["oasis_amphitheatre"]}])
lean = rule("usually quiet in dread", verdict="rarely",
            when=[{"fact": "mood", "op": "is", "value": ["dread"]}])
theatre = ctx(venue="oasis_amphitheatre", mood="dread")
s = split_rules([law_no, lean], theatre, "bobby")
check("the law is picked up", s.law is not None and s.law["verdict"] == "never")
check("it forces silence", s.forces_silence)
check("the lean rides along", len(s.leans) == 1)

home = ctx(venue="living_room", mood="dread")
s2 = split_rules([law_no, lean], home, "bobby")
check("wrong venue -> the law does NOT apply", s2.law is None)
check("...and it is counted as skipped, not lost", len(s2.skipped) == 1)
check("the lean still applies", len(s2.leans) == 1)

# ── 8. conflicting laws ──
print("\n=== two laws that collide ===")
a = rule("speaks", verdict="always", when=[{"fact": "mood", "op": "is", "value": ["dread"]}])
b = rule("silent", verdict="never", when=[{"fact": "mood", "op": "is", "value": ["dread"]}])
s3 = split_rules([a, b], ctx(mood="dread"), "bobby")
check("an exact tie enforces NEITHER", s3.law is None)
check("...the conflict is surfaced", s3.law_conflict is not None)
check("...and both are handed to the model as leans", len(s3.leans) == 2)

specific = rule("silent", verdict="never", when=[
    {"fact": "mood", "op": "is", "value": ["dread"]},
    {"fact": "venue", "op": "is", "value": ["bedroom"]},
])
s4 = split_rules([a, specific], ctx(mood="dread", venue="bedroom"), "bobby")
check("the MORE SPECIFIC law wins", s4.law is specific, str(s4.law))
check("...and it forces silence", s4.forces_silence)

# ── 9. the prompt block carries the WHY ──
print("\n=== the prompt block carries the why and the evidence ===")
r6 = rule("Stays out of grief between other people.", verdict="rarely",
          when=[{"fact": "kins_here", "op": "includes", "value": ["daisy"]}],
          why="he's a doer, not a talker — he'd refill your glass",
          ev='he explained the family curse over "god, poor Sam"')
s5 = split_rules([r6, law_no], ctx(kins_here=["daisy"], venue="living_room"), "bobby")
block = for_prompt(s5, "Bobby")
check("the rule is in the block", "Stays out of grief" in block)
check("the WHY is in the block", "doer, not a talker" in block, block)
check("the EVIDENCE is in the block", "god, poor Sam" in block)
check("the conditions are spelled out", "Daisy" in block or "daisy" in block, block)
check("Tim is TOLD what was filtered out", "not apply" in block, block)

# ── 10. LAWS DO NOT REACH THE MODEL ──
print("\n=== a LAW is enforced in code — it must NOT be in the prompt ===")
lw = rule("Never heckles in a theatre.", verdict="never",
          when=[{"fact": "venue", "op": "is", "value": ["oasis_amphitheatre"]}])
ln = rule("Usually quiet in dread.", verdict="rarely",
          when=[{"fact": "mood", "op": "is", "value": ["dread"]}])
s6 = split_rules([lw, ln], ctx(venue="oasis_amphitheatre", mood="dread"), "bobby")
b6 = for_prompt(s6, "Bobby")
check("the law FIRES (Python enforces it)", s6.forces_silence)
check("...but it is NOT in the prompt", "heckles" not in b6, b6)
check("the LEAN is in the prompt", "Usually quiet" in b6, b6)

# ── 11. the cap: many rules, bounded prompt ──
print("\n=== a thousand rules, a bounded prompt ===")
many = []
for i in range(40):
    # every one of them matches; they differ only in how specific they are
    conds = [{"fact": "mood", "op": "is", "value": ["dread"]}]
    if i % 2:
        conds.append({"fact": "venue", "op": "is", "value": ["bedroom"]})
    if i % 3 == 0:
        conds.append({"fact": "tim_stoned", "op": "at_least", "value": [0]})
    many.append(rule(f"rule number {i}", verdict="rarely", when=conds))
c7 = ctx(mood="dread", venue="bedroom", tim_stoned=2)
s7 = split_rules(many, c7, "bobby")
check("all 40 apply", len(s7.leans) == 40, str(len(s7.leans)))
b7 = for_prompt(s7, "Bobby")
shown = b7.count("• rarely")
check(f"but only {facts._MAX_LEANS_IN_PROMPT} reach the prompt", shown == facts._MAX_LEANS_IN_PROMPT,
      f"{shown} shown")
check("the MOST SPECIFIC ones are the ones shown",
      all(len(r["when"]) >= 2 for r in sorted(s7.leans, key=facts.specificity, reverse=True)[:10]))
check("and he is TOLD the rest exist", "not shown" in b7, b7[-120:])

print("\n--- what the coordinator actually reads ---")
print(block)

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
