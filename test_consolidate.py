"""Consolidation must never WIDEN a rule.

    "Bobby stays out of grief WHEN DAISY IS HERE"
    "Bobby stays out of romance"
        -> "Bobby stays out of emotional scenes"     <- FORBIDDEN

That is broader than either parent. It would mute him in rooms Tim never asked about,
and Tim would never learn why — he'd just see a man who'd stopped talking to him. It
looks like tidying the entire time you're doing it, which is exactly what makes it
dangerous. So it is refused in CODE, not merely discouraged in a prompt.
"""
import asyncio
import sys

import coordinator
import facts
from coordinator import ConsolidatedRule, Consolidation, RuleCondition

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{' OK ' if cond else 'FAIL'}] {label}" + (f"  — {detail}" if detail and not cond else ""))


# The guard, calling the REAL function app._consolidate_session_rules uses — so this
# tests the actual decision, not a paraphrase of it that can drift away from it.
def guard(parents: list[dict], c: ConsolidatedRule) -> str:
    new_conds = [{"fact": x.fact, "op": x.op, "value": x.value} for x in c.conditions]
    if facts.would_widen(parents, new_conds):
        return "refused: widened"
    if any((p["verdict"] in facts.LAWS) != (c.verdict in facts.LAWS) for p in parents):
        return "refused: changed law/lean"
    return "allowed"


def r(id, text, verdict, when):
    return {"id": id, "rule_text": text, "verdict": verdict, "when": when}


print("=== THE FORBIDDEN MERGE ===")
grief = r(1, "Stays out of grief when Daisy's here", "rarely",
          [{"fact": "kins_here", "op": "includes", "value": ["daisy"]}])
romance = r(2, "Stays out of romance", "rarely", [])
widened = ConsolidatedRule(
    rule_text="Stays out of emotional scenes", rule_why="x", verdict="rarely",
    conditions=[], replaces=[1, 2],
)
# The narrowest parent has 0 conditions (romance is universal), so a 0-condition merge
# is technically not narrower... and THAT is the subtle trap. Widening is relative to
# the MOST CONSTRAINED parent, because merging a scoped rule with a universal one
# necessarily un-scopes the scoped one.
res = guard([grief, romance], widened)
print(f"  (narrowest parent has {min(len(p['when']) for p in [grief, romance])} conditions)")
check("merging a SCOPED rule into a UNIVERSAL one is caught",
      res == "refused: widened",
      f"got {res!r} — the grief rule would lose its 'when Daisy's here'")

print("\n=== a TRUE duplicate merge is allowed ===")
a = r(3, "He doesn't explain feelings", "rarely",
      [{"fact": "mood", "op": "is", "value": ["melancholy"]}])
b = r(4, "He won't put a feeling into words", "rarely",
      [{"fact": "mood", "op": "is", "value": ["melancholy"]}])
same = ConsolidatedRule(
    rule_text="He doesn't put feelings into words", rule_why="a doer, not a talker",
    verdict="rarely",
    conditions=[RuleCondition(fact="mood", op="is", value=["melancholy"])],
    replaces=[3, 4],
)
check("same thing, same conditions -> merged", guard([a, b], same) == "allowed")

print("\n=== NARROWING is always fine ===")
narrower = ConsolidatedRule(
    rule_text="He doesn't put feelings into words", rule_why="x", verdict="rarely",
    conditions=[RuleCondition(fact="mood", op="is", value=["melancholy"]),
                RuleCondition(fact="kins_here", op="includes", value=["daisy"])],
    replaces=[3, 4],
)
check("adding a condition -> allowed", guard([a, b], narrower) == "allowed")

print("\n=== a LEAN may never be promoted to a LAW ===")
promoted = ConsolidatedRule(
    rule_text="He never puts feelings into words", rule_why="x", verdict="never",
    conditions=[RuleCondition(fact="mood", op="is", value=["melancholy"])],
    replaces=[3, 4],
)
check("two leans -> a law is refused",
      guard([a, b], promoted) == "refused: changed law/lean",
      "a lean bends when Tim says your name; a law does not")

print("\n=== ...and a LAW may never be demoted to a LEAN ===")
law1 = r(5, "Never in a theatre", "never", [{"fact": "venue", "op": "is", "value": ["oasis_amphitheatre"]}])
law2 = r(6, "Never heckles in public", "never", [{"fact": "venue", "op": "is", "value": ["oasis_amphitheatre"]}])
demoted = ConsolidatedRule(
    rule_text="Rarely speaks in a theatre", rule_why="x", verdict="rarely",
    conditions=[RuleCondition(fact="venue", op="is", value=["oasis_amphitheatre"])],
    replaces=[5, 6],
)
check("two laws -> a lean is refused", guard([law1, law2], demoted) == "refused: changed law/lean")

kept = ConsolidatedRule(
    rule_text="Never heckles in a public theatre", rule_why="x", verdict="never",
    conditions=[RuleCondition(fact="venue", op="is", value=["oasis_amphitheatre"])],
    replaces=[5, 6],
)
check("two laws -> one law is allowed", guard([law1, law2], kept) == "allowed")

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
