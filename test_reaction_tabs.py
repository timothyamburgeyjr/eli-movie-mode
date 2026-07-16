"""The three-tab reaction drawer: Quote and Song ride the same rails as a moment.

Two backend pieces are locked here, both with the model call stubbed so it's
deterministic and free (the pattern from test_reaction_search.py):

  1. reaction_draft now also returns `quotes[]` (heard lines) and `music` (is
     something playing + a heard cue). Each real quote is injected as a `writing`
     target on its nearest beat, so picking a quote later resolves by key exactly
     like any target. A dialogue-free clip yields an empty quote list.

  2. identify_song is the grounded, TEXT-only track lookup for the Song tab — it
     keeps only real, ranked emotion keys and refuses to name a track it can't find.
"""
import asyncio
import json
import sys
import types as _t
from pathlib import Path
from unittest import mock

import emotions
import gemini_brain

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{' OK ' if cond else 'FAIL'}] {label}" + (f"  — {detail}" if detail and not cond else ""))


# Two beats. A quote lands on the second (offset 30000); one quote has empty text
# and must be dropped. Emotion lists carry a hallucinated key to prove it's gated.
DRAFT = {
    "scene_description": "A kitchen at dawn. He pours coffee; she needles him.",
    "mood": "cozy",
    "moments": [
        {"offset_ms": 5000, "span_start_ms": 4000, "span_end_ms": 6000,
         "caption": "He pours coffee", "why": "Morning routine.",
         "targets": [{"label": "the steam off the mug", "facet": "cinematography",
                      "note": "x", "emotions": ["admiring"]}]},
        {"offset_ms": 30000, "span_start_ms": 29000, "span_end_ms": 31000,
         "caption": "She needles him", "why": "A dig about the toast.",
         "targets": [{"label": "her flat delivery", "facet": "performance",
                      "note": "x", "emotions": ["cracking_up"]}]},
    ],
    "reads": [],
    "quotes": [
        {"text": "You always burn the toast.", "speaker": "Mara",
         "moment_offset_ms": 30000, "emotions": ["cracking_up", "not_a_real_key", "admiring"]},
        {"text": "   ", "speaker": "nobody", "moment_offset_ms": 5000, "emotions": []},
    ],
    "music": {"playing": True, "cue": "a warm fingerpicked acoustic guitar, a man humming"},
}


def _draft(canned=DRAFT):
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
    clip.read_bytes = lambda: b"\x00\x00"
    with mock.patch.object(gemini_brain.types.Part, "from_bytes", lambda **kw: "PART"):
        return asyncio.run(g.reaction_draft(clip, clip_seconds=45))


print("=== QUOTES land as writing targets on the right beat ===")
d = _draft()
check("the empty-text quote is dropped", len(d["quotes"]) == 1, str(d["quotes"]))
q = d["quotes"][0]
check("the real line survives", q["text"] == "You always burn the toast.", q["text"])
check("with its speaker", q["speaker"] == "Mara", q["speaker"])

beat = next((m for m in d["moments"] if m["key"] == q["moment_key"]), None)
check("it points at the nearest beat (offset 30000)", beat and beat["offset_ms"] == 30000,
      str(beat and beat["offset_ms"]))
tgt = next((t for t in (beat or {}).get("targets", []) if t["key"] == q["target_key"]), None)
check("a target with the quote's key is injected on that beat", tgt is not None)
check("...as a `writing` facet", tgt and tgt["facet"] == "writing", str(tgt and tgt["facet"]))
check("...labelled with the line", tgt and tgt["label"] == '"You always burn the toast."',
      str(tgt and tgt["label"]))
check("...the note carries speaker + line for the sentence writer",
      tgt and "Spoken by Mara" in tgt["note"], str(tgt and tgt["note"]))
check("...and the hallucinated emotion key is dropped",
      tgt and tgt["emotions"] == ["cracking_up", "admiring"], str(tgt and tgt["emotions"]))
check("the injected key is recognisably a quote (so the Reaction tab hides it)",
      "quote" in q["target_key"], q["target_key"])

print("\n=== MUSIC presence rides along on the same call ===")
check("music.playing comes through", d["music"]["playing"] is True, str(d["music"]))
check("...with the heard cue", "acoustic guitar" in d["music"]["cue"], d["music"]["cue"])

print("\n=== a wordless, silent clip disables both tabs ===")
SILENT = dict(DRAFT, quotes=[], music={"playing": False, "cue": ""})
d2 = _draft(SILENT)
check("no dialogue -> empty quotes (All Is Lost case)", d2["quotes"] == [], str(d2["quotes"]))
check("no music -> playing is false", d2["music"]["playing"] is False, str(d2["music"]))
check("...and no quote target leaked onto any beat",
      not any("quote" in t["key"] for m in d2["moments"] for t in m["targets"]))


# ─── identify_song (grounded, text-only) ─────────────────────────────
def _song(canned):
    g = gemini_brain.GeminiBrain.__new__(gemini_brain.GeminiBrain)
    g._ensure_client = lambda: None

    async def fake_call(parts, config, name, model=None):
        return _t.SimpleNamespace(text=json.dumps(canned))

    async def fake_track(response, **kw):
        return {}

    g._call_with_retry = fake_call
    g._track_call = fake_track
    return asyncio.run(g.identify_song(
        movie_title="Test Film", movie_context="", cue="a man humming", timestamp_label="0:30"))


# Two real emotion keys from the live vocabulary + a fake, to prove the gate.
REAL = list(emotions.BY_KEY.keys())
FOUND = {
    "found": True, "title": "Heroes", "artist": "David Bowie", "album": "\"Heroes\"",
    "year": "1977", "note": "the needle-drop as she walks out", "source": "Tunefind",
    "emotions": [REAL[0], "definitely_not_a_key", REAL[1]],
}

print("\n=== identify_song names a real track and gates the emotions ===")
c = _song(FOUND)
check("found is true", c["found"] is True, str(c.get("found")))
check("title comes through", c["title"] == "Heroes", c["title"])
check("artist comes through", c["artist"] == "David Bowie", c["artist"])
check("the hallucinated emotion key is dropped",
      c["emotions"] == [REAL[0], REAL[1]], str(c["emotions"]))
check("source is kept for the card", c["source"] == "Tunefind", c["source"])

print("\n=== identify_song refuses to invent a track ===")
c2 = _song({"found": False, "title": "", "artist": "", "emotions": []})
check("found=false stays false", c2["found"] is False, str(c2))
c3 = _song({"found": True, "title": "   ", "artist": "x", "emotions": []})
check("found=true but no title -> treated as not found", c3["found"] is False, str(c3))

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
