"""Writing a rule in your own words.

THE ONE THAT MATTERS: if Tim said ALWAYS, the rule says ALWAYS.

He typed "bobby always jumps in when someone is being conned" and the model handed back
`usually` — quietly overruling the single thing he had been explicit about. He would not have
found out until the room failed to do what he told it to, and by then the rule would look
perfectly reasonable sitting in his list.

Asking a model harder is not a fix for that. The rest of this system already knows as much,
which is why LAWS execute in Python before a model is ever called. So the strength he typed
is ENFORCED, not requested, and this file is what says so.
"""
import sys

import app
import facts

ok = True


def check(label: str, cond: bool, detail: str = "") -> None:
    global ok
    if not cond:
        ok = False
    print(f"  [{' OK ' if cond else 'FAIL'}] {label}" + (f"  — {detail}" if detail and not cond else ""))


print("=== the strength Tim typed is the strength he gets ===")
CASES = [
    # what he wrote,                                what it must become
    ("bobby always jumps in when someone's conned",  "always"),
    ("he never talks over a goodbye",                "never"),
    ("he usually stays out of grief",                "usually"),
    ("he rarely comments on romance",                "rarely"),
    ("he tends to answer lore questions",            "usually"),
    ("he often chips in on the plot",                "usually"),
    ("he seldom bothers with the plot",              "rarely"),
    # No strength word at all -> the model decides, and its default is a LEAN. A casual
    # sentence must never be silently promoted to a law: a law overrides the room in code,
    # forever, without appeal.
    ("he talks about cars",                          None),
    ("bobby likes westerns",                         None),
    # NEGATION. "doesn't always" is emphatically NOT "always" — it means the opposite. Reading
    # the bare word here would invert his meaning while looking like it obeyed him.
    ("he doesn't always jump in",                    None),
    ("he does not always speak up",                  None),
    ("he won't always answer",                       None),
]
for text, want in CASES:
    got = app._stated_strength(text)
    check(f"{text!r:48} -> {want!r}", got == want, f"got {got!r}")

print("\n=== a law is a law, and a lean is a lean ===")
check("always/never are LAWS", facts.LAWS == {"always", "never"}, str(facts.LAWS))
check("usually/rarely are LEANS", facts.LEANS == {"usually", "rarely"}, str(facts.LEANS))
for text, want in CASES:
    if want:
        check(f"{text[:34]!r:36} -> is_law={want in facts.LAWS}",
              app._stated_strength(text) == want)

print("\n=== a condition naming a fact we don't have can NEVER fire, so it is refused ===")
# `_match_one` returns False for an unknown key and logs a warning nobody reads. The rule
# would save cleanly, sit in his list looking authoritative, and do nothing. Forever.
from fastapi import HTTPException  # noqa: E402


def refuses(conds) -> bool:
    try:
        app._check_conditions(conds)
        return False
    except HTTPException:
        return True


check("a real fact + a real op is accepted",
      not refuses([{"fact": "mood", "op": "is", "value": ["dread"]}]))
check("a HALLUCINATED fact is refused",
      refuses([{"fact": "bobby_mood", "op": "is", "value": ["angry"]}]))
check("a made-up op is refused",
      refuses([{"fact": "mood", "op": "vibes_with", "value": ["dread"]}]))
check("an empty condition list is fine (universal)", not refuses([]))
check("every op the model may emit is a real op",
      all(o in facts.OPS for o in ("is", "is_not", "includes", "excludes", "at_least", "at_most")))

print("\n=== the vocabulary is finite, and that is a fact about the world ===")
# There is no dial for "a con". The honest move is to leave the rule universal and SAY SO
# (scope_note), not to pin it to the nearest half-fitting fact — a rule scoped to
# `mood: tense` because he said "a con" fires on every tense scene in every film.
check("there is genuinely no `deception` situation",
      not any(s in facts.FILM_SITUATIONS for s in ("deception", "con", "betrayal")),
      str(facts.FILM_SITUATIONS))
import coordinator  # noqa: E402
check("so a proposal can explain itself (scope_note exists)",
      "scope_note" in coordinator.ProposedRule.model_fields)
check("...and it defaults to empty, so /propose is unaffected",
      coordinator.ProposedRule.model_fields["scope_note"].default == "")


print("")
print("=== A CONDITION IS STRINGS. A FACT IS NOT. ===")
# RuleCondition.value is list[str], and the dial picker does String(o.value) — so a condition
# ALWAYS carries strings. But `rewatch` is a bool, `tim_stoned` an int, `year` a number. The
# matcher compared them directly, so `True in ["True"]` was False and `2 in ["2"]` was False.
#
# FIVE OF THE SIX CONDITION KINDS COULD NEVER FIRE. Every rule ever scoped to a stoned level,
# a rewatch or a year has silently done nothing since the feature shipped — and looked
# completely fine in the list, which is why it went unnoticed. A rule that CANNOT match is
# indistinguishable, from outside, from a rule whose moment hasn't come.
TYPED = [
    ("rewatch",    True,    ["True"], True,  "a rewatch rule"),
    ("rewatch",    True,    ["true"], True,  "...case-insensitively"),
    ("rewatch",    False,   ["True"], False, "...and not on a first watch"),
    ("tim_stoned", 2,       ["2"],    True,  "when Tim is baked"),
    ("tim_stoned", 1,       ["2"],    False, "...but not merely lifted"),
    ("year",       1999,    ["1999"], True,  "a 1999 film"),
    ("mood",       "dread", ["dread"], True, "a mood (a string all along)"),
]
for key, have, want, expect, label in TYPED:
    ctx = facts.TurnContext(values={key: have})
    got = facts.applies({"when": [{"fact": key, "op": "is", "value": want}]}, ctx)
    check(f"{label:34} {key}={have!r} vs {want}", got == expect, f"got {got}")
    inv = facts.applies({"when": [{"fact": key, "op": "is_not", "value": want}]}, ctx)
    check(f"   ...and is_not is its exact inverse", inv == (not expect))

print("")
print("=== the show/episode ladder still fires on ANY rung ===")
lad = facts.TurnContext(values={"watching": ["supernatural", "supernatural/s1", "supernatural/s1e4"]})
check("a SHOW rule fires on an episode",
      facts.applies({"when": [{"fact": "watching", "op": "is", "value": ["supernatural"]}]}, lad))
check("an EPISODE rule fires on that episode",
      facts.applies({"when": [{"fact": "watching", "op": "is", "value": ["supernatural/s1e4"]}]}, lad))
check("and neither fires on a different film",
      not facts.applies({"when": [{"fact": "watching", "op": "is", "value": ["the iron giant"]}]}, lad))

print("")
print("=== a kin key is checked against the REAL roster ===")
# The model produced `kins_talking = ['tim']`. Tim is not a kin. `kins_here`/`kins_talking`
# have no static options (they're built from the live roster), so nothing checked them.
check("a real kin is accepted",
      not refuses([{"fact": "kins_here", "op": "includes", "value": ["daisy"]}]))
check("'tim' is NOT a kin, and is refused",
      refuses([{"fact": "kins_talking", "op": "includes", "value": ["tim"]}]))


print("")
print("=== A SAFETY RULE THAT TAKES THE FEATURE DOWN IS A SECOND FAILURE MODE ===")
# The "a law must be scoped" validator used to RAISE. A ValueError is a structured-output
# violation, so the SDK retries — fine, until a model simply will not comply. /api/rules/propose
# blew both retries returning unscoped `always` and answered Tim with a 502. He tapped a
# thumbs-up to REINFORCE GOOD BEHAVIOUR and the app fell over.
#
# (Made worse by my own oversight: _PROPOSE_SYSTEM was never told the rule. Only
# _INTERPRET_SYSTEM was. So the model had no idea what it was violating.)
#
# It COERCES now. An unscoped law is softened to the matching lean and says so: deterministic,
# cannot 502, cannot mute anyone.
for bad, want in (("always", "usually"), ("never", "rarely")):
    p_ = coordinator.ProposedRule(rule_text="He speaks.", rule_why="x",
                                  verdict=bad, universal=True, conditions=[])
    check(f"an unscoped {bad!r} is softened to {want!r}, not fatal", p_.verdict == want, p_.verdict)
    check(f"   ...and it says it did", bool(p_.scope_note), p_.scope_note)
    check(f"   ...so it can never be a law", p_.verdict not in facts.LAWS)

_scoped = coordinator.ProposedRule(
    rule_text="He never speaks during a jump scare.", rule_why="y", verdict="never",
    universal=True,
    conditions=[coordinator.RuleCondition(fact="scene_situation", op="is", value=["jump_scare"])])
check("a properly SCOPED law survives untouched", _scoped.verdict == "never", _scoped.verdict)
check("   ...and its `universal` flag is corrected to match the facts", _scoped.universal is False)

print("")
print("=== every prompt that can produce a rule KNOWS a law must be scoped ===")
for name in ("_PROPOSE_SYSTEM", "_INTERPRET_SYSTEM", "_SUGGEST_SYSTEM"):
    check(f"{name} says so", "A LAW MUST BE SCOPED" in getattr(coordinator, name))

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
