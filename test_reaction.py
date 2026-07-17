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
import tempfile
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

print("\n=== SONG: listen to the clip first, then verify; degrade gracefully ===")


def _song_brain(by_name):
    """A GeminiBrain whose model calls are dispatched by call NAME, so the two-pass
    song lookup (listen → ground) can return a different reply for each pass, or raise."""
    g = gemini_brain.GeminiBrain.__new__(gemini_brain.GeminiBrain)
    g.text_model = "stub"
    g.draft_model = "stub"
    g._ensure_client = lambda: None
    calls = []

    async def fake_call(parts, config, name, model=None):
        calls.append(name)
        payload = by_name.get(name)
        if isinstance(payload, Exception):
            raise payload
        return _t.SimpleNamespace(text=json.dumps(payload))

    async def fake_track(response, **kw):
        return {}

    g._call_with_retry = fake_call
    g._track_call = fake_track
    return g, calls


# A tiny on-disk clip so the listen pass has bytes to read (well under the inline
# limit, so no upload path is exercised).
_clip = Path(tempfile.gettempdir()) / "song_test_clip.mp4"
_clip.write_bytes(b"\x00\x01\x02fake-mp4-bytes")

# 1) The ear names it; the search confirms, enriches, and quotes a lyric → found.
g, calls = _song_brain({
    "identify_song_listen": {"music_present": True, "title": "Tiny Dancer", "artist": "Elton John",
                             "confidence": "high", "cue": "piano ballad, male vocal"},
    "identify_song": {"found": True, "title": "Tiny Dancer", "artist": "Elton John",
                      "album": "Madman Across the Water", "year": "1971", "note": "the bus singalong",
                      "source": "Tunefind", "lyric": "Hold me closer, tiny dancer", "emotions": ["moved"]},
})
card = asyncio.run(g.identify_song(clip_path=_clip, movie_title="Almost Famous"))
check("listened BEFORE grounding", calls == ["identify_song_listen", "identify_song"], str(calls))
check("found and enriched from search", card["found"] and card["album"] == "Madman Across the Water", str(card))
check("carries a memorable lyric", card["lyric"] == "Hold me closer, tiny dancer", str(card))

# 2) The ear is sure but the GROUNDED pass errors → return the ear's track, not a 502.
g, calls = _song_brain({
    "identify_song_listen": {"music_present": True, "title": "Tuvan Throat Song", "artist": "",
                             "confidence": "high", "cue": "overtone singing"},
    "identify_song": gemini_brain.GeminiError("503 overloaded"),
})
card = asyncio.run(g.identify_song(clip_path=_clip, movie_title="X"))
check("a search failure falls back to the confident ear", card["found"] and card["title"] == "Tuvan Throat Song", str(card))
check("...crediting the clip audio as the source", card["source"] == "the clip audio", str(card))

# 3) The ear hears music it can't name; the search names it from the richer cue.
g, calls = _song_brain({
    "identify_song_listen": {"music_present": True, "title": "", "artist": "",
                             "confidence": "none", "cue": "mournful solo cello, slow"},
    "identify_song": {"found": True, "title": "Prospero's Books", "artist": "Michael Nyman",
                      "album": "", "year": "", "note": "score cue", "source": "IMDb", "emotions": []},
})
card = asyncio.run(g.identify_song(clip_path=_clip))
check("heard-but-unnamed still gets named by search", card["found"] and card["title"] == "Prospero's Books", str(card))

# 4) The ear hears NO music and nothing was pre-described → not found, and the grounded
#    search is never spent.
g, calls = _song_brain({
    "identify_song_listen": {"music_present": False, "title": "", "artist": "", "confidence": "none", "cue": ""},
    "identify_song": {"found": True, "title": "SHOULD NOT BE REACHED", "artist": "", "album": "",
                      "year": "", "note": "", "source": "", "emotions": []},
})
card = asyncio.run(g.identify_song(clip_path=_clip))
check("silence short-circuits to not-found", not card["found"], str(card))
check("...and never spends the grounded search", calls == ["identify_song_listen"], str(calls))

# 5) Backward compatible: no clip → the old text-only grounded path, one call.
g, calls = _song_brain({
    "identify_song": {"found": True, "title": "Stuck in the Middle with You", "artist": "Stealers Wheel",
                      "album": "", "year": "1972", "note": "the ear scene", "source": "Tunefind", "emotions": []},
})
card = asyncio.run(g.identify_song(movie_title="Reservoir Dogs", cue="70s soft rock on the radio"))
check("no clip -> text-only grounded still works", card["found"] and calls == ["identify_song"], str(calls))

_clip.unlink(missing_ok=True)

print("\n=== QUOTE: reacting to a line ships the exact words to the room ===")
import app
_draft = {
    "quotes": [
        {"text": "I know it was you, Fredo.", "speaker": "Michael", "target_key": "m1q0"},
        {"text": "", "speaker": "", "target_key": "m1q1"},  # empty line, should be ignored
    ]
}
# A quote target: the room hears Tim's sentence AND the line + who said it.
q = app._reaction_line_with_quote(_draft, {"key": "m1q0"}, "I couldn't breathe")
check("keeps his sentence", q.startswith("I couldn't breathe"), q)
check("carries the exact line", 'I know it was you, Fredo.' in q, q)
check("names the speaker", "Michael" in q, q)
# A quote with no speaker still ships the line, just unattributed.
q2 = app._reaction_line_with_quote(
    {"quotes": [{"text": "Run.", "speaker": "", "target_key": "m1q0"}]},
    {"key": "m1q0"}, "chills")
check("unattributed line still ships", 'Run.' in q2 and "chills" in q2, q2)
# A non-quote target (an ordinary facet) falls straight through, untouched.
plain = app._reaction_line_with_quote(_draft, {"key": "m1t0"}, "that shot floored me")
check("a non-quote target is untouched", plain == "that shot floored me", plain)
# An empty quote text is treated as no quote.
empty = app._reaction_line_with_quote(_draft, {"key": "m1q1"}, "wow")
check("an empty quote line is ignored", empty == "wow", empty)

print("\n=== SONG REACTION: the track, a word on the music, and a lyric ride to the room ===")
_song_draft = {"song": {"card": {
    "title": "Tiny Dancer", "artist": "Elton John",
    "note": "the tour-bus singalong that thaws the whole band",
    "lyric": "Hold me closer, tiny dancer",
}}}
s = app._reaction_line_with_song(_song_draft, {"key": "song"}, "I got chills")
check("keeps his sentence", s.startswith("I got chills"), s)
check("names the track + artist", 'Tiny Dancer' in s and 'Elton John' in s, s)
check("says something about the music", 'tour-bus singalong' in s, s)
check("quotes the lyric", 'Hold me closer, tiny dancer' in s, s)
# A non-song target is untouched.
plain_s = app._reaction_line_with_song(_song_draft, {"key": "m1t0"}, "nice shot")
check("a non-song target is untouched", plain_s == "nice shot", plain_s)
# A score cue with no lyric still names the track, just no quote.
_score_draft = {"song": {"card": {"title": "Time", "artist": "Hans Zimmer", "note": "the ascending ostinato", "lyric": ""}}}
sc = app._reaction_line_with_song(_score_draft, {"key": "song"}, "goosebumps")
check("instrumental: names it, no lyric", 'Time' in sc and 'Hans Zimmer' in sc and 'line from it' not in sc, sc)

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
