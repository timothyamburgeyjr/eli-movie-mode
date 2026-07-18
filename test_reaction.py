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
        # The multimodal calls pass [media_part, prompt], so parts[0] is a types.Part,
        # not the text. Take the first STRING — that's the prompt in every case.
        captured["prompt"] = next((p for p in (parts or []) if isinstance(p, str)), "")
        captured["name"] = name
        captured["config"] = config          # so the SCHEMA can be asserted, not just the text
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

print("\n=== the draft prompt still guarantees the people + the story ===")
# The rule was rewritten when `people` arrived: it used to demand a target per
# CHARACTER (filed under character/performance) and referred to a "cast" category
# that never existed as a facet. Now it demands a target per PERSON, named by actor.
check("REACTION_DRAFT_SYSTEM_PROMPT demands a target per person on screen",
      "EACH person clearly on screen" in gemini_brain.REACTION_DRAFT_SYSTEM_PROMPT)
check("...named by the ACTOR, not the character",
      "ACTOR'S REAL NAME" in gemini_brain.REACTION_DRAFT_SYSTEM_PROMPT)
check("...and the story beat", "BEAT ITSELF" in gemini_brain.REACTION_DRAFT_SYSTEM_PROMPT)

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

print("\n=== QUOTE SEARCH: find a line in the clip, verbatim; empty is honest ===")
_qclip = Path(tempfile.gettempdir()) / "quote_test_clip.mp4"
_qclip.write_bytes(b"\x00\x01\x02fake")
g, calls = _song_brain({
    "find_quote": {"quotes": [
        {"text": "Do you find me sadistic?", "speaker": "Bill", "offset_ms": 12000},
        {"text": "", "speaker": "x", "offset_ms": 1},          # blank dropped
        {"text": "No, kiddo", "speaker": "", "offset_ms": 999999},  # wild offset clamps
    ]},
})
res = asyncio.run(g.find_quote(_qclip, query="something sadistic", clip_seconds=45, movie_title="Kill Bill"))
check("used the find_quote call", calls == ["find_quote"], str(calls))
check("returns the line VERBATIM", res[0]["text"] == "Do you find me sadistic?", str(res))
check("keeps the speaker", res[0]["speaker"] == "Bill", str(res))
check("drops the blank line", all(r["text"] for r in res), str(res))
check("clamps a wild offset into the clip", res[-1]["offset_ms"] <= 45000, str(res))
g2, _ = _song_brain({"find_quote": {"quotes": []}})
none = asyncio.run(g2.find_quote(_qclip, query="a purple elephant", clip_seconds=45))
check("no match returns empty (never invents)", none == [], str(none))
_qclip.unlink(missing_ok=True)

print("\n=== INJECT QUOTES: searched lines slot in as reactable targets ===")
_idraft = {"moments": [
    {"key": "m0", "offset_ms": 5000, "targets": [{"key": "m0t0"}]},
    {"key": "m1", "offset_ms": 40000, "targets": [{"key": "m1t0"}]},
], "quotes": [{"text": "already here", "speaker": "Y", "moment_key": "m1", "target_key": "m1quote0"}]}
n = app._inject_quotes(_idraft, [
    {"text": "Do you find me sadistic?", "speaker": "Bill", "offset_ms": 11000},
    {"text": "ALREADY HERE", "speaker": "dup", "offset_ms": 0},   # case-insensitive dup dropped
])
check("added exactly the new line", n == 1, str(n))
check("anchored to the nearest beat by offset", _idraft["quotes"][0]["moment_key"] == "m0", str(_idraft["quotes"][0]))
check("searched line is prepended (shows first)", _idraft["quotes"][0]["text"] == "Do you find me sadistic?", str(_idraft["quotes"][0]))
_inj = next(t for t in _idraft["moments"][0]["targets"] if t["key"] != "m0t0")
check("injected target facet is writing", _inj["facet"] == "writing", str(_inj))
check("injected key is foreign (contains 'quote')", "quote" in _inj["key"], _inj["key"])
check("note carries speaker + verbatim line", "Bill" in _inj["note"] and "sadistic" in _inj["note"], _inj["note"])
_qline = app._reaction_line_with_quote(_idraft, {"key": _inj["key"]}, "I lost it")
check("the searched quote ships to the room verbatim", "Do you find me sadistic?" in _qline, _qline)

print("\n=== VERBATIM: the exact words reach the room, protected from condensing ===")
import inspect

import coordinator
import kindroid_relay

# The line he reacted to, handed over in the protected slot.
_vq = {"quotes": [{"text": "I never pretended I did.", "speaker": "Jonas", "target_key": "m1quote0"}]}
_v = app._reaction_verbatim(_vq, {"key": "m1quote0"})
check("quote verbatim names speaker + exact line",
      _v == 'Jonas says: "I never pretended I did."', _v)
check("an ordinary target has no verbatim", app._reaction_verbatim(_vq, {"key": "m1t0"}) == "")
# A song reaction hands over the track and the lyric.
_vs = {"song": {"card": {"title": "Tiny Dancer", "artist": "Elton John",
                         "lyric": "Hold me closer, tiny dancer"}}}
_vsong = app._reaction_verbatim(_vs, {"key": "song"})
check("song verbatim carries track + lyric",
      "Tiny Dancer" in _vsong and "Hold me closer" in _vsong, _vsong)

# The mechanical payload emits it — and KEEPS it when the scene is dropped for length.
_body = kindroid_relay.build_payload(
    scene_narration="a scene", verbatim_narration='Jonas says: "I never pretended I did."')
check("build_payload emits the verbatim block", "I never pretended I did." in _body, _body)
_body2 = kindroid_relay.build_payload(
    scene_narration="", history_narrative="h", verbatim_narration='X says: "keep me"')
check("verbatim survives a dropped scene", "keep me" in _body2, _body2)

# And the coordinator is told, in the prompt, that it may not paraphrase it away.
check("the packet template has a protected verbatim section",
      "MUST SURVIVE" in coordinator._PACKAGE_USER)
check("...and a rule forbidding paraphrase",
      "VERBATIM MEANS VERBATIM" in coordinator._PACKAGE_SYSTEM)

print("\n=== TRIVIA: the fact actually reaches the kins now ===")
check("a light-touch trivia relay exists", callable(getattr(app, "_relay_trivia_to_room", None)))
_tsrc = inspect.getsource(app.api_movie_trivia)
check("the trivia endpoint relays it to the room", "_relay_trivia_to_room" in _tsrc)
_rsrc = inspect.getsource(app._relay_trivia_to_room)
check("...as the protected verbatim, not a paraphrase", "verbatim=trivia_text" in _rsrc)
check("...react-only, so nobody holds forth", "react_only_all=True" in _rsrc)
check("...through the ORDINARY turn, not its own fan-out",
      "_run_relay(" in _rsrc and "_relay_to_kin(" not in _rsrc)

print("\n=== FACETS: fifteen of them, and the schema got SIMPLER ===")
check("all five new facets exist",
      {"people", "things", "place", "animals", "wardrobe"} <= set(gemini_brain.REACTION_FACETS),
      str(gemini_brain.REACTION_FACETS))
check("the old ten survive",
      {"story", "character", "performance", "writing", "cinematography", "editing",
       "sound", "production_design", "theme", "callback"} <= set(gemini_brain.REACTION_FACETS))
check("production_design no longer claims costume/props (they'd never move)",
      "costume" not in gemini_brain._FACET_HINTS.split("production_design")[1].split("\n")[0],
      gemini_brain._FACET_HINTS.split("production_design")[1].split("\n")[0])
check("the people/character distinction is spelled out",
      "Gene Hackman" in gemini_brain._FACET_HINTS and "Harry Caul" in gemini_brain._FACET_HINTS)
check("the draft prompt says an empty animals category is CORRECT",
      "EMPTY `animals` CATEGORY IS THE CORRECT ANSWER" in gemini_brain.REACTION_DRAFT_SYSTEM_PROMPT)
check("...and that a wrong name is worse than no name",
      "WRONG NAME IS WORSE THAN NO NAME" in gemini_brain.REACTION_DRAFT_SYSTEM_PROMPT)
check("the 'two rules' miscount is fixed",
      "THE THREE RULES THAT MATTER" in gemini_brain.REACTION_DRAFT_SYSTEM_PROMPT)


def _enums_under(node, path="", found=None):
    """Every enum in the schema, with its path — the guard against re-introducing
    the 400 INVALID_ARGUMENT that a deep enum caused once already."""
    found = [] if found is None else found
    if isinstance(node, dict):
        if "enum" in node:
            found.append(path)
        for k, v in node.items():
            _enums_under(v, f"{path}.{k}", found)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _enums_under(v, f"{path}[{i}]", found)
    return found


print("\n=== THE 400 GUARD: no enum may live under moments[] ===")
CAST = [{"actor": "Gene Hackman", "character": "Harry Caul"}]
DRAFT_CANNED = {
    "scene_description": "s", "mood": "tense", "reads": [], "quotes": [],
    "music": {"playing": False, "cue": ""},
    "moments": [{
        "offset_ms": 1000, "span_start_ms": 0, "span_end_ms": 2000,
        "caption": "c", "why": "w",
        "targets": [
            {"label": "Gene Hackman", "facet": "people", "note": "Gene Hackman — plays Harry Caul", "emotions": ["love"]},
            {"label": "Brad Pitt", "facet": "people", "note": "not in this film at all", "emotions": ["love"]},
            {"label": "the mud-caked Bronco", "facet": "things", "note": "a car", "emotions": ["admiring"]},
            {"label": "he goes still", "facet": "story", "note": "the beat", "emotions": ["tense_all_over"]},
        ],
    }],
}
_clipf = Path(tempfile.gettempdir()) / "facet_test_clip.mp4"
_clipf.write_bytes(b"\x00\x01\x02fake")


def _draft(cast):
    g, cap = _brain(DRAFT_CANNED)
    g.draft_model = "stub"
    out = asyncio.run(g.reaction_draft(_clipf, clip_seconds=45, movie_title="The Conversation", cast=cast))
    return out, cap


res, cap = _draft(CAST)
_enums = _enums_under(getattr(cap["config"], "response_schema", None) or {})
_deep = [p for p in _enums if "moments" in p]
check("NO enum anywhere under moments[] (the 400 guard)", _deep == [], str(_deep))
check("...while flat enums (mood) are still allowed", any("mood" in p for p in _enums), str(_enums))

print("\n=== THE PEOPLE GATE: a credited name ships, an invented one does not ===")
_labels = [t["label"] for t in res["moments"][0]["targets"]]
check("the credited actor survives", "Gene Hackman" in _labels, str(_labels))
check("the actor who isn't in this film is DROPPED", "Brad Pitt" not in _labels, str(_labels))
check("non-people targets are untouched", "the mud-caked Bronco" in _labels, str(_labels))
res_nocast, _ = _draft([])
_l2 = [t["label"] for t in res_nocast["moments"][0]["targets"]]
check("with NO cast list, every people target is dropped",
      "Gene Hackman" not in _l2 and "Brad Pitt" not in _l2, str(_l2))
check("...and the rest of the beat still stands", "the mud-caked Bronco" in _l2, str(_l2))
check("the cast rides into the prompt as ground truth",
      "Gene Hackman" in cap["prompt"] and "ground truth" in cap["prompt"].lower(), cap["prompt"][:200])
_clipf.unlink(missing_ok=True)

print("\n=== SENTENCES: only `people` may leave the clip ===")
for facet, want in (("people", True), ("story", False), ("character", False), ("performance", False)):
    g, cap = _brain(SENT)
    asyncio.run(g.reaction_sentences(
        scene_description="s", moment_caption="c", moment_why="w",
        target_label="Gene Hackman", target_note="n", target_facet=facet,
        emotion_key="cracking_up"))
    got = "THIS IS ABOUT A PERSON" in cap["prompt"]
    check(f"{facet}: career-level block {'present' if want else 'ABSENT'}", got is want)

print("\n=== RETARGET knows the new facets, and is gated too ===")
RT2 = {"targets": [
    {"label": "Gene Hackman", "facet": "people", "note": "x", "emotions": ["love"]},
    {"label": "Brad Pitt", "facet": "people", "note": "x", "emotions": ["love"]},
    {"label": "the shearling coat", "facet": "wardrobe", "note": "x", "emotions": ["admiring"]},
]}
g, cap = _brain(RT2)
out = asyncio.run(g.reaction_retarget(
    scene_description="s", moment_caption="c", moment_why="w", steer="the coat", cast=CAST))
_rl = [t["label"] for t in out]
check("a new facet (wardrobe) survives retarget", "the shearling coat" in _rl, str(_rl))
check("the credited actor survives", "Gene Hackman" in _rl, str(_rl))
check("the invented actor is dropped here too", "Brad Pitt" not in _rl, str(_rl))
check("the facet hints are now injected (they never were)",
      "wardrobe" in cap["prompt"] and "that jacket" in cap["prompt"], cap["prompt"][-400:])

print("\n=== VERBATIM: every target reaches the room, not just quotes/songs ===")
_pv = app._reaction_verbatim({}, {"key": "m0t1", "facet": "people",
                                  "label": "Gene Hackman",
                                  "note": "Gene Hackman — plays Harry Caul"})
check("a people target names the actor AND the part", "Gene Hackman" in _pv and "Harry Caul" in _pv, _pv)
_tv = app._reaction_verbatim({}, {"key": "m0t2", "facet": "things", "label": "the mud-caked Bronco"})
check("an ordinary target ships its label", "mud-caked Bronco" in _tv, _tv)
check("a target with no label still yields nothing",
      app._reaction_verbatim({}, {"key": "m0t3", "facet": "story", "label": ""}) == "")

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
