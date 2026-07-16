"""Reaction search: type what you're reacting to, get the moment + fitting emoji.

The clip already went to Gemini; his WORDS never did. This test locks the two new pieces:

  1. When `reacting_to` is set, the draft schema gains a `match` block and the prompt asks
     Gemini to point at the one moment he meant.
  2. Gemini's answer becomes a synthetic search TARGET on that moment, carrying the emotions
     he's likely feeling — so the drawer's emoji step renders it with no new code — and a
     top-level `matched` pointer the client jumps to.

The browse path (no text) must stay byte-identical, so that is asserted too.
"""
import asyncio
import json
import sys
import types as _t
from pathlib import Path
from unittest import mock

import gemini_brain

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{' OK ' if cond else 'FAIL'}] {label}" + (f"  — {detail}" if detail and not cond else ""))


# A canned Gemini answer: two beats, and a `match` pointing at the second (offset 30000).
CANNED = {
    "scene_description": "A hallway. The lights fail; she stops dead.",
    "mood": "dread",
    "moments": [
        {"offset_ms": 8000, "span_start_ms": 6000, "span_end_ms": 9000,
         "caption": "He picks up the phone", "why": "He answers a call.",
         "targets": [{"label": "the ring", "facet": "sound", "note": "x",
                      "emotions": ["tense", "on_edge"]}]},
        {"offset_ms": 30000, "span_start_ms": 28000, "span_end_ms": 31000,
         "caption": "The lights cut out", "why": "The hallway goes black; she freezes.",
         "targets": [{"label": "the dark", "facet": "cinematography", "note": "x",
                      "emotions": ["dread", "scared"]}]},
    ],
    "reads": [],
    "match": {
        "found": True,
        "moment_offset_ms": 30000,
        "summary": "when the lights cut out and she froze",
        "emotions": ["terrified", "cant_look", "blood_ran_cold", "not_a_real_key"],
    },
}


def _run(reacting_to, canned=CANNED):
    """Drive reaction_draft with the model call and clip I/O stubbed out."""
    g = gemini_brain.GeminiBrain.__new__(gemini_brain.GeminiBrain)
    g.draft_model = "stub"
    g.INLINE_LIMIT_BYTES = 10_000_000
    g._ensure_client = lambda: None

    async def fake_call(parts, config, name, model=None):
        return _t.SimpleNamespace(text=json.dumps(canned))

    async def fake_track(response, **kw):
        return {}

    g._call_with_retry = fake_call
    g._track_call = fake_track

    clip = mock.MagicMock(spec=Path)
    clip.read_bytes = lambda: b"\x00\x00"     # tiny -> inline path, no upload
    with mock.patch.object(gemini_brain.types.Part, "from_bytes", lambda **kw: "PART"):
        return asyncio.run(g.reaction_draft(clip, clip_seconds=45, reacting_to=reacting_to))


print("=== a search LANDS on the moment he described ===")
d = _run("when the lights cut out")
check("a `matched` pointer comes back", d.get("matched") is not None, str(d.get("matched")))
m = d["matched"]
moment = next((x for x in d["moments"] if x["key"] == m["moment_key"]), None)
check("it points at the RIGHT moment (nearest offset)", moment and moment["offset_ms"] == 30000,
      str(moment and moment["offset_ms"]))
check("the summary echoes his words", "lights cut out" in m["summary"], m["summary"])

search_t = next((t for t in moment["targets"] if t["key"] == m["target_key"]), None)
check("a synthetic search target is on that moment", search_t is not None)
check("...and it is FIRST, so the emoji step lands on it", moment["targets"][0]["key"] == m["target_key"])
check("...carrying HIS likely emotions, ranked", search_t["emotions"][0] == "terrified", str(search_t["emotions"]))
check("...with the hallucinated key dropped", "not_a_real_key" not in search_t["emotions"],
      str(search_t["emotions"]))

print("\n=== the browse path (no text) is untouched ===")
d2 = _run("")
check("no `matched` on a browse", d2.get("matched") is None, str(d2.get("matched")))
check("no synthetic search target leaks in",
      not any(t["key"].endswith("tsearch") for mm in d2["moments"] for t in mm["targets"]))
check("moments still come through", len(d2["moments"]) == 2, str(len(d2["moments"])))

print("\n=== a search that matches NOTHING doesn't crash or invent a match ===")
no = dict(CANNED, match={"found": False, "moment_offset_ms": -1, "summary": "", "emotions": []})
d3 = _run("a purple elephant that is not in this film", canned=no)
check("found=false -> matched is None", d3.get("matched") is None, str(d3.get("matched")))
check("...and the moments are still there to fall back to", len(d3["moments"]) == 2)

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
