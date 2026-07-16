"""Reaction refine + intensity: the AI re-asks at every stage, and the sentence carries degree.

The clip is read once and cached. Every later refine is a text-only steer against the scene it
already knows — except the moment refine, which re-reads the CACHED clip (never the seedbox) so
the beat stays in the 45-second window. Intensity shapes the sentence the room hears.

The whole thing is stubbed the way test_reaction_search.py stubs the model, so it's
deterministic and free.
"""
import asyncio
import json
import sys
import types as _t
from unittest import mock

import gemini_brain

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{' OK ' if cond else 'FAIL'}] {label}" + (f"  — {detail}" if detail and not cond else ""))


def _brain(canned):
    """A GeminiBrain whose model call returns `canned` (dict) as JSON, nothing else live."""
    g = gemini_brain.GeminiBrain.__new__(gemini_brain.GeminiBrain)
    g.text_model = "stub"
    g._ensure_client = lambda: None
    captured = {}

    async def fake_call(parts, config, name, model=None):
        captured["prompt"] = parts[0] if parts else ""
        captured["name"] = name
        return _t.SimpleNamespace(text=json.dumps(canned))

    async def fake_track(response, **kw):
        return {}

    g._call_with_retry = fake_call
    g._track_call = fake_track
    return g, captured


print("=== INTENSITY: the default is a no-op; meh/WOW change the prompt ===")
SENT = {"options": [{"first_person": "I laughed", "third_person": "Tim laughed"}]}

g, cap = _brain(SENT)
asyncio.run(g.reaction_sentences(
    scene_description="s", moment_caption="c", moment_why="w",
    target_label="t", target_note="n", target_facet="story", emotion_key="cracking_up",
    intensity=2))
base = cap["prompt"]
check("normal keeps the old 'vary the manner' instruction", "Vary the manner" in base, base[-120:])
check("...and says nothing about strength", "How HARD" not in base)

for lv, word in ((1, "meh"), (3, "WOW")):
    g, cap = _brain(SENT)
    asyncio.run(g.reaction_sentences(
        scene_description="s", moment_caption="c", moment_why="w",
        target_label="t", target_note="n", target_facet="story", emotion_key="cracking_up",
        intensity=lv))
    check(f"level {lv} injects a fixed strength ({word})",
          "How HARD" in cap["prompt"] and word in cap["prompt"], cap["prompt"][-160:])
    check(f"...and drops the 'vary' instruction", "Vary the manner" not in cap["prompt"])

g, cap = _brain(SENT)
asyncio.run(g.reaction_sentences(
    scene_description="s", moment_caption="c", moment_why="w",
    target_label="t", target_note="n", target_facet="story", emotion_key="cracking_up",
    shade="more nervous than glad", steer="less theatrical"))
check("a shade rides into the prompt", "more nervous than glad" in cap["prompt"])
check("a steer rides into the prompt", "less theatrical" in cap["prompt"])

print("\n=== RETARGET: re-derives the beat's targets, keeps only real facets + emotion keys ===")
RT = {"targets": [
    {"label": "the flat way he signs", "facet": "performance", "note": "x",
     "emotions": ["admiring", "not_a_real_key", "reverent"]},
    {"label": "bogus", "facet": "not_a_facet", "note": "x", "emotions": ["awe"]},
]}
g, _ = _brain(RT)
out = asyncio.run(g.reaction_retarget(
    scene_description="s", moment_caption="c", moment_why="w", steer="his acting"))
check("the valid target survives", any(t["label"] == "the flat way he signs" for t in out), str(out))
check("a bad facet is dropped", not any(t["facet"] == "not_a_facet" for t in out))
good = next(t for t in out if t["facet"] == "performance")
check("a hallucinated emotion key is dropped", "not_a_real_key" not in good["emotions"], str(good["emotions"]))

print("\n=== REEMOTE: re-ranks, keys-only, hallucinations dropped ===")
g, _ = _brain({"emotions": ["scared", "not_real", "on_edge", "cant_look"]})
keys = asyncio.run(g.reaction_reemote(
    scene_description="s", moment_caption="c", target_label="t",
    target_facet="story", steer="more nervous"))
check("valid keys kept in order", keys[:1] == ["scared"], str(keys))
check("hallucinated key dropped", "not_real" not in keys, str(keys))

print("\n=== the draft prompt now guarantees cast + story ===")
check("REACTION_DRAFT_SYSTEM_PROMPT demands a target per character",
      "each character clearly on screen" in gemini_brain.REACTION_DRAFT_SYSTEM_PROMPT.lower()
      or "EACH character" in gemini_brain.REACTION_DRAFT_SYSTEM_PROMPT)
check("...and the story beat", "STORY BEAT" in gemini_brain.REACTION_DRAFT_SYSTEM_PROMPT)

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
