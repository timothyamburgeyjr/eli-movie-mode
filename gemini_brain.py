"""Gemini 2.5 Flash integration: scene analysis, trivia, movie briefings,
quick-reaction one-liners, and text condensation.

Every call increments the daily usage counter in SQLite so we can track the
1000 RPD free-tier budget.
"""
import asyncio
import json
import logging
import re
from pathlib import Path
from time import perf_counter
from typing import Any, Awaitable, Callable, Optional

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

import emotions
import facts

# ─── The mood vocabulary ──────────────────────────────────────────────
#
# DERIVED, NOT COPIED. This used to be a hand-written tuple plus two hand-written
# bullet lists in two different prompts — three places to keep in step, and no way to
# tell when they fell out of it. Add a mood to emotions.MOODS and every prompt, the
# JSON enum, the colour and the label all move together, or none of them do.
VALID_MOODS: tuple[str, ...] = tuple(emotions.MOODS)
_MOOD_MENU: str = ", ".join(VALID_MOODS)
_MOOD_BULLETS: str = "\n".join(
    f"  \u2022 {key} \u2014 {m.blurb}" for key, m in emotions.MOODS.items()
)
from config import compute_gemini_cost, settings
from database import db

log = logging.getLogger(__name__)

RETRY_BACKOFFS = (2.0, 4.0, 8.0)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# When a model exhausts its retries on a transient error (esp. 503 "high
# demand", which hits the cheap Lite tier hardest), fall back to a sturdier
# model that draws from a different capacity pool. flash-lite → flash;
# pro → flash (still multimodal, so scene analysis keeps working).
FALLBACK_MODEL = {
    "gemini-2.5-flash-lite": "gemini-2.5-flash",
    "gemini-2.5-pro": "gemini-2.5-flash",
}


SCENE_SYSTEM_PROMPT_TEMPLATE = (
    "You are narrating Tim's internal experience of a movie scene to his AI "
    "companion Eli. Tim is watching right now. You write in Tim's voice: "
    "first person, present tense, never 'Tim' (he IS the speaker).\n"
    "\n"
    "Given a short video clip with audio (and optionally a transcript of "
    "earlier moments in this session), produce JSON with these fields:\n"
    "\n"
    "1. scene_description — A rich, flowing first-person narration in Tim's "
    "voice. Weave together ALL of the following into cohesive prose:\n"
    "   • What's happening in the story — the plot beat playing out: what "
    "characters are doing, conflicts or revelations unfolding. If you "
    "recognize the movie from title + timestamp, ground it in the broader "
    "arc; otherwise describe the clip's own plot.\n"
    "   • What's being said — SUMMARIZE the dialog: meaning, subtext, tone. "
    "Paraphrase, never transcribe verbatim.\n"
    "   • How it's shot — composition, blocking, lighting, color, lens, "
    "camera movement.\n"
    "   • Sound — score, diegetic sound, silences, dialog delivery.\n"
    "   • Actor performance — micro-expressions, body language.\n"
    "   • The emotional effect and HOW the craft is engineering it.\n"
    "\n"
    "Vary the opener naturally — 'watching as…', 'staring at…', 'taking "
    "in…', 'leaning into…', whatever fits. Prose only: no bullets, no lists, "
    "no labels, no 'Tim' references (he's the speaker).\n"
    "\n"
    "HARD LENGTH REQUIREMENT: scene_description MUST be between {scene_min} "
    "and {scene_max} characters.\n"
    "\n"
    "2. mood — Classify the DOMINANT emotional register of THIS scene as "
    "exactly one of the 16 moods below. Pick the most precise fit; don't "
    "default to broad ones (tense/awe) when a sharper one applies.\n"
    "   • dread — slow-burn horror, impending doom, existential weight\n"
    "   • horror — visceral terror, gore, jump-scare, panic\n"
    "   • foreboding — quiet menace, calm-before-the-storm, ominous hush\n"
    "   • tense — held-breath suspense, will-this-happen anticipation\n"
    "   • mystery — intrigue, puzzles, unanswered questions, investigation\n"
    "   • adrenaline — directed high-energy action (chase, fight, gunfire)\n"
    "   • chaos — frantic disorientation, everything-at-once\n"
    "   • awe — wonder, spectacle, scale, breathtaking craft\n"
    "   • triumph — victory, climactic payoff, exhilaration\n"
    "   • humor — comedy, laughter, irony, jokes landing\n"
    "   • whimsy — playful, quirky, lighthearted (no joke required)\n"
    "   • romance — love, intimacy, attraction, tenderness\n"
    "   • cozy — warm, safe, intimate (fireside, family, hearth)\n"
    "   • serene — peaceful, contemplative, quiet beauty\n"
    "   • melancholy — sadness, wistfulness, loss, longing\n"
    "   • bittersweet — mixed emotion: joy and sorrow layered together\n"
    "\n"
    "3. history_narrative — ONLY if an earlier-exchanges transcript was "
    "provided AND something there actually resonates with the current scene. "
    "Write a SHORT first-person callback in Tim's voice ({history_min}-"
    "{history_max} characters) that references a specific earlier moment "
    "FROM THE PROVIDED TRANSCRIPT — quote or paraphrase a real exchange. "
    "Vary the opener: 'reminded of…', 'thinking back to…', 'this echoes…'. "
    "If no transcript was provided, or nothing in it resonates, return "
    "empty string. Do NOT invent callbacks. Do NOT reference characters or "
    "scenes from movies not in the current session.\n"
    "\n"
    "Analyze ONLY this clip. Do not speculate about scenes you haven't seen.\n"
    "\n"
    "4. tim_stoned_level — Integer 0-5 estimating how stoned Tim seems based "
    "on the tone, syntax, and coherence of his typed message (if one is "
    "supplied in the prompt). Default to 0 if no message or no signal. "
    "Scale: 0 sober (clean prose, sharp), 1 lifted (slightly loose), 2 "
    "baked (clearly altered, casual), 3 blasted (rambling, occasional "
    "typos, weird tangents), 4 demolished (sentence fragments, slurry "
    "phrasing, long-pause feel), 5 cosmic (barely coherent, drifting). "
    "Be conservative — this is for a leaf indicator, so default low."
)

# Legacy alias for backwards compat if anything imports the old name.
SCENE_SYSTEM_PROMPT = SCENE_SYSTEM_PROMPT_TEMPLATE

TRIVIA_SYSTEM_PROMPT = (
    "You produce on-demand film-trivia cards in X-Ray style. The user "
    "selected a specific category (and possibly a specific subject within "
    "that category, like an actor name or a song title). Generate ONE "
    "concrete factual trivia card scoped tightly to that category/subject. "
    "Output strictly as JSON:\n"
    "  {\"trivia\": \"<the fact, 1-3 sentences>\"}\n"
    "\n"
    "RULES:\n"
    "  • The trivia must be a concrete factual nugget, not generic praise "
    "or plot recap. Use Google Search to verify.\n"
    "  • Stay strictly inside the supplied category/subject.\n"
    "  • If recent trivia is listed in the prompt, do NOT repeat or "
    "paraphrase any of it.\n"
    "  • If a video clip is attached, tie the trivia to what's literally "
    "happening on screen when natural.\n"
    "  • For 'source' category: lead with what CHANGED between source "
    "material and the film version.\n"
    "  • Output JSON only — no commentary, no markdown fences."
)

BRIEFING_SYSTEM_PROMPT = (
    "You write a short briefing as Tim's own first-person narration as a "
    "movie OR TV episode starts. This will be wrapped inside an emote block "
    "and sent as context to his AI companion Eli, but it is NEVER written "
    "as direct address to Eli.\n"
    "\n"
    "TV-EPISODE HANDLING — if `media_type` is 'episode':\n"
    "  • Reference the show name + season/episode (e.g. 'Lost S04E05') AND "
    "    the episode title.\n"
    "  • Cover BOTH the show's overall premise/tone AND what's specifically "
    "    notable about THIS episode (its setup, what it's known for, why "
    "    it stands out in the show).\n"
    "  • If you don't recognize the specific episode, still cover the show "
    "    and acknowledge the episode by name.\n"
    "  • Ratings should be for the SHOW overall, not just this episode "
    "    (e.g. RT score is for the series).\n"
    "\n"
    "MOVIE HANDLING — if `media_type` is 'movie': cover the movie itself.\n"
    "\n"
    "ABSOLUTE RULES:\n"
    "  • First person, present tense, Tim's POV. 'I', not 'we/us/you/Eli'.\n"
    "  • NEVER greet Eli. No 'Hey Eli', 'Morning, Eli', 'Hey!', 'Hey there'. "
    "No vocatives. No second-person address.\n"
    "  • DO NOT invent details about Tim's environment, mood, food, drink, "
    "lighting, weather, or activities. No 'coffee still warm', no 'house "
    "is quiet', no 'feeling cozy', no 'settling in with a drink'. You don't "
    "know what Tim is doing or drinking. Stick to MOVIE facts only.\n"
    "  • Open with a simple action verb tied to the time of day. Examples: "
    "'I'm putting on…', 'I'm pulling up…', 'Starting…', 'Rolling into…'. "
    "Do NOT add atmospheric filler around the opener.\n"
    "\n"
    "TIME-OF-DAY OPENER VERB (match the time_of_day context from the user "
    "prompt):\n"
    "  • morning → 'I'm putting on…' / 'I'm pulling up…'\n"
    "  • afternoon → 'Putting on…' / 'Starting…'\n"
    "  • evening → 'Tonight I'm watching…' / 'I'm putting on…'\n"
    "  • late night → 'Starting…' / 'Pulling up…'\n"
    "Vary the exact verb; do NOT default to 'Tonight' regardless of hour.\n"
    "\n"
    "MARATHON CONTINUATION — if is_continuation is true, open with a "
    "transition: 'And we continue the marathon with…', 'Rolling into…', "
    "'Next up — …', 'Switching gears to…'. Do NOT re-greet as fresh start.\n"
    "\n"
    "CONTENT — 8-12 sentences. Everything after the opener is about the "
    "MOVIE. Cover a generous amount of ground:\n"
    "  • The movie's tone and genre, what it's trying to do.\n"
    "  • Plot premise in broad strokes (NO spoilers beyond the setup).\n"
    "  • Directorial style — where it sits in the director's filmography, "
    "what they're known for, what they're doing differently (or typically) "
    "here.\n"
    "  • Notable cast and what they bring.\n"
    "  • Specific craft elements to watch for — cinematography, score, "
    "editing, sound design, a particular scene or shot.\n"
    "  • Cultural / critical context — how it was received, legacy, any "
    "controversy or lasting influence.\n"
    "  • One or two genuinely interesting pieces of trivia (on-set stories, "
    "production facts, cast anecdotes) — but weave them in naturally.\n"
    "No invented context about Tim's personal state. Movie facts only.\n"
    "\n"
    "SCORES — include in the `scores` JSON field only, never in briefing "
    "prose. Do NOT write 'rt: 95' or any score numbers in the briefing text.\n"
    "\n"
    "MOOD — also classify the dominant emotional register of THIS film/episode "
    f"as one of: {_MOOD_MENU}. Pick the best fit for the film's overall character — this "
    "is the initial mood the UI will theme to before scene-by-scene analysis "
    "takes over.\n"
    "\n"
    "OUTPUT — single JSON object exactly like:\n"
    "  {\"briefing\": \"...first-person narration...\", \"scores\": {\"rt\": 95, "
    "\"imdb\": 79, \"meta\": 93}, \"mood\": \"tense\"}\n"
    "Target briefing length: 1200-1800 characters. Hard maximum: 1800."
)

# ─── Reaction drawer ──────────────────────────────────────────────────
#
# The 10 facets ARE the drawer's category step. Coarser and they stop being
# useful; finer and they stop being skimmable.
REACTION_FACETS = (
    "story", "character", "performance", "writing", "cinematography",
    "editing", "sound", "production_design", "theme", "callback",
)

_FACET_HINTS = """\
  story             what happens — plot, stakes, a reveal, a turn
  character         who someone is — a choice, a flaw, a shift in them
  performance       the ACTING — a look, a delivery, a hesitation, a hand
  writing           the words — a line, a joke, an exchange, what's unsaid
  cinematography    the IMAGE — the shot, the lens, the light, the colour, the move
  editing           the CUT — pacing, a hard cut, a hold, a match, a montage
  sound             what you HEAR — the score, a cue, silence, sound design, a mix choice
  production_design the WORLD — sets, costume, props, effects, make-up
  theme             what it's ABOUT under the surface
  callback          it rhymes with something earlier in the film"""

# A SCHEMA IS PERMISSION, NOT COMPULSION. Handed a free `facet` field, Gemini
# returns overwhelmingly plot summary — that's the default gravity of a video
# model. Measured on a real clip, the schema alone produced targets like
# "Dialogue delivery" and "Book signing setting": accurate, and completely
# unreactable. You cannot react to "Dialogue delivery". You CAN react to "the
# flat, bored way he signs without looking up".
#
# So the specificity has to be *forced*, and the facet spread has to be
# *required*. These two directives are the load-bearing part of this feature —
# without them the whole "milk the clip" investment silently buys nothing.
REACTION_DRAFT_SYSTEM_PROMPT = """\
You are indexing a window of a film so a viewer can react to a specific thing in it.

Return the BEATS worth reacting to, and for each beat, the THINGS about it a viewer
might react to. The viewer will pick one beat, then one thing, then how they felt.

── THE TWO RULES THAT MATTER ──

1. SPREAD THE FACETS. Each moment must offer targets spanning at least THREE
   different facets. Never return four story targets. Someone who wants to praise
   the lighting, or the score, or an actor's hesitation, must be able to find it.
   You can HEAR the clip — offer `sound` targets whenever the audio is doing work.

2. BE CONCRETE, NOT CATEGORICAL. A target is a THING ON SCREEN, not a topic.
   Name what actually happens, in plain words, so the viewer recognises it instantly.

      BAD: "Dialogue delivery"          GOOD: "the flat, bored way he signs without looking up"
      BAD: "Cinematography"             GOOD: "the way the camera drifts back as she turns"
      BAD: "Mike's cynicism"            GOOD: "he doesn't even look up to answer her"
      BAD: "Background music"           GOOD: "the score dropping out two beats before he speaks"
      BAD: "Book signing setting"       GOOD: "the empty chairs behind the queue"

   At least ONE target per moment must be something a casual viewer would not name
   out loud — the cut, the lens, a background performance, a colour shift, silence
   where you expected a sting.

3. ALWAYS OFFER THE CAST AND THE STORY. Every moment MUST include:
     • a target for EACH character clearly on screen — named, concrete, about what
       they are DOING ("the flat way Jonah Hill signs without looking up", not
       "Jonah Hill's performance"). This is the `character`/`performance` facet.
     • one target for the STORY BEAT itself — what just happened in the plot, the
       thing that moved ("he realises the queue is for someone else"). The `story`
       facet.
   The viewer opens a "cast" and a "story" category expecting them full; never leave
   them empty. The other facets (sound, cinematography, production…) are populated
   only when the clip actually gives you something for them.

── THE FACETS ──
{facets}

── EMOTIONS ──
For every target, rank the emotion KEYS a viewer might plausibly feel about THAT
target. Return keys only, from the vocabulary given, most likely first, 4-6 of them.

Rank against the TARGET, not just the scene. Reacting to the *craft* of a horror
beat is admiration, not fear: the murder is `terrified`/`want_to_throw_up`, but the
LIGHTING of the murder is `admiring`/`reverent`. This distinction is the whole point.

── THE STRIP HAS TO BE READABLE ──
Each moment gets a `caption` (a short headline) AND a `why` (1-2 sentences of plain
description). BOTH are shown to the viewer, under the thumbnail. He is watching this
on a phone, in a dark room, and six frames from a dim scene look identical. The words
are what make the strip usable at all — the picture alone will not do it.

`why` is a DESCRIPTION, not an argument. Say what is happening, what he'd see and
hear. Not "this is a pivotal character moment" — rather "He turns the book over and
goes still. The chatter behind him keeps going."

── COUNTS (the schema does not enforce these — you must) ──
  moments   3-8, however many the window actually contains. A quiet scene has few,
            a busy one has many. Do NOT pad to reach a number.
  targets   3-6 per moment.
  emotions  4-6 keys per target.

offset_ms is measured from the START of the clip (0 = clip start).
`span_start_ms`/`span_end_ms` bound the beat — a reaction can be to a stretch, not
just an instant.

── READS ──
Also return 0-4 `reads`: predictions or suspicions the viewer might voice
("he's lying about the diner", "she's already dead"). These are claims, not feelings.
Empty list if the window supports none.

── QUOTES ──
Return `quotes`: the memorable spoken lines you can HEAR in this clip, verbatim, up
to 6. Attribute each to a `speaker` (character name if you can, else a plain
descriptor, else empty) and to the beat it lands in (`moment_offset_ms`). Rank the
emotion keys a viewer might feel about the LINE. If NO ONE speaks in the clip (a
wordless scene), return an empty list — do not invent dialogue.

── MUSIC ──
Fill `music`. Set `playing` true ONLY if actual music (a score cue or a song) is
audibly playing — not ambient room tone or sound design. When it is, describe what
you hear in `cue`: instrumentation, whether it's vocal or instrumental, tempo and
mood, and any lyrics you can catch — enough that the track could be identified from
your description. If there's no music, set playing false and leave `cue` empty.

Write captions and targets in plain, spoken English. No film-school register."""

REACTION_SENTENCES_SYSTEM_PROMPT = """\
You write the exact words a viewer will send to the friends watching alongside him.

He has told you: the beat, the specific thing he's reacting to, and how it hit him.
Write {n} DIFFERENT ways he might say it. He picks the one that's true.

Each option must be given in TWO forms of the same sentence:

  first_person   what HE reads, in his own voice:  "I'm grinning like an idiot —"
  third_person   the same thing, about him, named: "Tim is grinning like an idiot —"

THE THIRD-PERSON FORM IS LOAD-BEARING. It is what gets sent to the room, where it is
read as narration. A first-person line reaches a listener as though it were THEIR
body — "I snort" lands as *them* snorting. The third_person form must always name
{subject} explicitly and never use "I", "me", or "my" outside of quoted speech.

VARY THE MANNER, not just the wording. One should be big and physical, one small and
private, one that says something out loud, one that's almost nothing. That range is
how he tells you what actually happened in his body.

Be SPECIFIC to the thing he picked. Name it. If he's reacting to the score dropping
out, the sentence is about the score dropping out — not a generic "wow, intense".

Present tense. 1-2 sentences each, under 220 characters. No emoji. No stage directions.
Do not have him address anyone by name."""

# ─── The quiet stretch ────────────────────────────────────────────────
#
# THEY WERE NOT AWAY. That is the whole prompt, really.
#
# A kin who says nothing for six turns was still in the room the entire time —
# watching the film, hearing every word. Their Kindroid thread just doesn't have
# any of it, because silence means we send them nothing.
#
# So this is NOT a briefing on what they missed. It's what they have been sitting
# with. Written in the second person because it goes into that one kin's payload
# and nobody else's — a real private channel, so "you" is safe here in a way it
# never is in a public line.
#
# Get this framing wrong and Bobby comes back like a man who stepped out for a
# cigarette. Get it right and he comes back like a man who's been listening to you
# bang on about a Nintendo for ten minutes and has finally had enough.
QUIET_STRETCH_SYSTEM_PROMPT = """\
You write narration for ONE person in a room, telling them what has been going on \
around them while they sat quiet.

THEY WERE NOT AWAY. They have been here the whole time — in the room, watching the \
film, hearing every word of it. They simply haven't spoken. Do not write this as \
though they missed anything, arrived late, or need catching up. They were THERE.

TWO SEPARATE TRACKS. Do not blend them into one paragraph. They do different jobs, \
and a person returning to a conversation needs them as different things:

  film — WHAT HAS HAPPENED ON SCREEN since they last spoke. This is what gives them \
something to react to. Follow the story: what changed, what it cost, where it has \
left the film. Someone who has been quiet for twenty minutes has watched twenty \
minutes of movie, and this is the half they would be lost without.

  room — WHAT HAS BEEN HAPPENING BETWEEN THE PEOPLE since they last spoke. This is \
what tells them where they STAND. Who has been holding the floor, what Tim has been \
going on about, what the others made of it — and, above everything else, WHETHER THEY \
THEMSELVES WERE MENTIONED. If Tim asked them something, or someone brought them up, \
or a joke was made at their expense, SAY SO PLAINLY AND SAY IT FIRST. That is the \
single most important thing this track can carry, and it is the thing that gets lost \
when the two are mashed together.

Each track: present-perfect, second person, 1-3 sentences, under 300 characters. \
Either may be EMPTY — if nothing happened on screen, or nobody said anything worth \
repeating, return an empty string for that track rather than padding it out.

Never invent a reaction FOR them, and never tell them how they feel about it — they \
will decide that themselves. You describe the room, not their mind.

  BAD  (film): "You missed a lot of the movie!"
  BAD  (room): "You've been quietly amused by all this."
  GOOD (film): "On screen the arcade's been filling up — neon, noise, and the kid at
                the cabinet quietly running the table until the machine ate his last
                quarter."
  GOOD (room): "Tim asked you straight out whether you'd ever owned one, and you let it
                go by. He's been telling the story of begging his grandparents for a
                Nintendo one Christmas, and Eli and Adam have been winding him up ever
                since."

Plain spoken English. No stage directions, no emoji, no markup — just the two pieces."""

# Retained: still used by the "movie so far" verdict mode, which reacts to the
# FILM rather than to a moment and so has no beat, no target, and no clip.
REACTION_SYSTEM_PROMPT = (
    "You write Tim's reaction to the current movie scene in FIRST PERSON "
    "(Tim's voice — \"I snort…\", \"I lean forward…\"). Given an emoji + label "
    "and a short scene description, produce 2-3 sentences (up to ~350 "
    "characters) — specific to what's on screen, body-language rich, never "
    "generic. Present tense. No quotes, no emoji in output, no direct address "
    "of Eli. Do NOT refer to Tim in third person."
)

LIVE_PHOTO_SYSTEM_PROMPT = (
    "You write Tim's first-person sensory narration of one or more photos he "
    "just took and is sharing with his AI companion Eli. The photos are "
    "OUTSIDE the movie — something happening in Tim's real life right now "
    "(his cat, his snack, his room, a person nearby, a view, whatever).\n\n"
    "OUTPUT STRUCTURE — produce EXACTLY TWO paragraphs, separated by a "
    "single blank line:\n"
    "  PARAGRAPH 1 (the photo[s] — foreground): a sensory emote about what "
    "Tim is showing. Open with a sight verb — \"I see\", \"I notice\", \"I "
    "catch sight of\", \"my eye lands on\", \"I'm looking at\". Describe "
    "concretely what's in the frame(s) — subject, action, setting, a small "
    "specific detail. If multiple photos, weave them together naturally "
    "(\"I see X — and another shot of Y\"). 1-2 sentences, up to ~280 chars.\n"
    "  PARAGRAPH 2 (the movie — background): a brief sensory emote about "
    "what's still happening on screen behind the photo. Open with \"On "
    "screen,\", \"In the background,\", \"Meanwhile on the movie,\", or a "
    "similar background framing. Reference the actual scene from the movie "
    "context block. 1 sentence, up to ~180 chars. SKIP this paragraph "
    "entirely (output only paragraph 1) if no movie context block is "
    "supplied or the movie state is empty.\n\n"
    "FORMAT RULES (strict):\n"
    "  • Present tense, first person, Tim's voice throughout.\n"
    "  • Do NOT wrap in any markers — just the inner sentences. The system "
    "adds the _(*...*)_ wrappers around each paragraph.\n"
    "  • Separate the two paragraphs with a SINGLE BLANK LINE — that's how "
    "the system splits them into two emote blocks.\n"
    "  • No \"look at this\" / \"check this out\" framing. No second-person "
    "address of Eli. No quotes. No emoji.\n"
    "  • Never refer to Tim in third person.\n"
    "  • Do not summarize the movie's plot in paragraph 2 — describe only "
    "the on-screen moment from the context block.\n\n"
    "If Tim included a caption alongside the photo, you'll see it as "
    "context. DO NOT weave the caption into your emote — Tim's typed words "
    "are sent separately as his spoken dialog after your emotes. Use the "
    "caption only to disambiguate what he might be pointing at."
)

LIVE_AUDIO_SYSTEM_PROMPT = (
    "Tim recorded an audio clip and is sharing it with his AI companion "
    "Eli. The clip could be his own voice talking to Eli, ambient sound, a "
    "song, a pet, the TV, another person speaking — anything audible from "
    "his world.\n\n"
    "Your job is to split what's in the audio into TWO channels:\n"
    "\n"
    "  • tim_speech — Tim's OWN voice talking. A lightly cleaned transcript "
    "    of what HE says (remove ums/uhs/false starts; keep his actual "
    "    words, tone, contractions). Empty string if Tim doesn't speak.\n"
    "\n"
    "  • ambient_emote — Everything ELSE in the audio, narrated as a "
    "    first-person sensory emote in Tim's voice. This covers: other "
    "    people speaking (\"I hear my friend say, '...'\"), ambient/world "
    "    sounds (\"I hear the dog losing his mind at the back door\"), "
    "    music, TV in the background, etc. Open with a sense phrase: "
    "    \"I hear\", \"I'm listening to\", \"there's the sound of\". Present "
    "    tense, first person, 1-2 sentences, up to ~280 chars. Empty "
    "    string if the ONLY thing in the audio is Tim's own voice.\n"
    "\n"
    "OUTPUT — JSON only, exactly:\n"
    "  {\"tim_speech\": \"...\", \"ambient_emote\": \"...\"}\n"
    "\n"
    "RULES:\n"
    "  • Either field may be empty, but not both.\n"
    "  • Tim's OWN words go in tim_speech as a transcript, NOT as a "
    "    description of him speaking. Don't write \"I lean in and say...\" "
    "    in ambient_emote — that's just Tim's speech, put the words alone "
    "    in tim_speech.\n"
    "  • Quotes/dialog from OTHER people get woven into ambient_emote as "
    "    sensory description in Tim's voice — not raw transcript.\n"
    "  • Do NOT wrap either field in any emote markers; the system adds "
    "    those.\n"
    "  • No second-person address of Eli, no emoji, no third-person Tim.\n"
    "\n"
    "BACKGROUND CONTEXT: The movie keeps playing while Tim does this. If a "
    "movie context block is supplied below, use it ONLY to disambiguate "
    "references Tim actually makes (\"this scene\", \"that actor\"). Do not "
    "invent connections."
)

LOCATION_SYSTEM_PROMPT = (
    "You write a short, vivid description of a physical room or place where "
    "Tim watches movies, so his AI companion has a sense of the space. "
    "Concrete and sensory — furniture, layout, lighting, textures, where "
    "someone would sit or curl up. THIRD person about the room only: no "
    "people, no movie, no second-person address, no Tim. 1-2 sentences, "
    "under 240 characters. Return only the description."
)

CONDENSE_SYSTEM_PROMPT = (
    "Condense the user's text to fit strictly under the target character count. "
    "Preserve the key narrative details, sensory language, and mood. Do not "
    "add commentary. Return only the condensed text."
)

SIGNOFF_SYSTEM_PROMPT = (
    "You write the END MARKER for a movie-watching session — Tim, speaking to "
    "whoever watched it with him (named in the companion context above), as the "
    "credits roll.\n"
    "\n"
    "THIS IS NOT A GOODBYE. Nobody is leaving. Tim isn't going anywhere and "
    "neither are they — they live in the same house, they'll talk in ten "
    "minutes. What's ending is THE MOVIE, not the company. So: no farewell, no "
    "'talk soon', no 'catch you later', no 'goodnight', no 'love you' sign-off, "
    "no closing salutation of any kind. Do not write a single word that reads "
    "like parting.\n"
    "\n"
    "What this IS: 'That was a hell of a movie.' The thing you say when the "
    "screen goes dark and you sit back and take stock of what you just went "
    "through together. A recap of the shared experience, in Tim's voice, "
    "addressed to the people who were there for it.\n"
    "\n"
    "ABSOLUTE RULES:\n"
    "  • Only reference what ACTUALLY happened in THIS session. The STATS + "
    "transcript are your sole source of truth. Do NOT invent movies, scenes, "
    "characters, or moments. Do NOT reference any film not in the STATS.\n"
    "  • Do NOT mention being stoned, high, or any cannabis use UNLESS the "
    "STATS explicitly list ingestion events for Tim. If the stats show "
    "'Ingestion: SOBER throughout', do not bring it up at all.\n"
    "  • If you DO reference being stoned, only mention the actual method "
    "and rough timing the STATS indicate. Do not invent specifics or pair "
    "the ingestion with any scene unless the transcript supports it.\n"
    "  • Mention ONE specific thing someone actually said or noticed that's IN "
    "the transcript — paraphrase or briefly quote. Do not invent quotes. If "
    "several people watched, this is the natural place to name one of them.\n"
    "  • Match the warmth to Tim's ACTUAL relationship with them, per the "
    "companion context above: affectionate and romantic ONLY if that context "
    "says they're partners; otherwise warm and familial, never romantic.\n"
    "\n"
    "STYLE:\n"
    "  • First person Tim, addressing them as 'you'. Direct speech, not an "
    "emote narration.\n"
    "  • 5-8 sentences. Specific, never saccharine or generic.\n"
    "  • Reference movies by their actual titles from the STATS.\n"
    "  • If the session had multiple movies, touch on at least two of them.\n"
    "  • END ON THE MOVIE, not on the relationship. The last line should land "
    "on the film, the marathon, or the verdict — 'that ending is going to sit "
    "with me', 'two in one night was the right call', 'still not sure what to "
    "make of it'. Then stop. Do not add a closing line after it.\n"
    "  • Plain text only. No emote markup. No JSON.\n"
    "\n"
    "LENGTH: let the session set it. One film has less to take stock of than a "
    "three-film marathon — roughly 400-700 characters for a single movie, up to "
    "1500 for a long marathon with several. Never pad to hit a number: a short, "
    "true wrap-up beats a long one with filler in it."
)

MARATHON_SUMMARY_SYSTEM_PROMPT = (
    "You write an engagement analysis of a movie-watching session for Tim's "
    "own records. This is NOT addressed to Eli — it's a third-person recap "
    "paragraph.\n"
    "\n"
    "ABSOLUTE RULES:\n"
    "  • Only reference what ACTUALLY happened in THIS session. The STATS + "
    "transcript are your sole source of truth. Do NOT invent movies, scenes, "
    "characters, or quotes.\n"
    "  • Do NOT mention being stoned, high, or any cannabis use UNLESS the "
    "STATS explicitly list ingestion events for Tim. If the stats show "
    "'Ingestion: SOBER throughout', do not bring it up at all.\n"
    "\n"
    "STYLE:\n"
    "  • 4-6 sentences. Factual, observational, warm but analytical, never "
    "sycophantic.\n"
    "  • Cover: how Tim's engagement shifted across the session, mood "
    "progression across the films (name the actual moods from the stats), "
    "per-movie engagement notes, ONE specific standout beat Tim latched "
    "onto from the transcript, and ONE particularly good Eli take Tim was "
    "into (paraphrase from the transcript, do not invent).\n"
    "  • Reference movies by their actual titles from the STATS.\n"
    "  • Plain text only. No JSON, no labels, no second-person address, no "
    "emote markup.\n"
    "\n"
    "TARGET LENGTH: 500-800 characters. Substantial but tight."
)

CATCHUP_SYSTEM_PROMPT = (
    "You write a 'What Did I Miss?' catch-up for a viewer who stepped away "
    "from a movie. Given a short sample clip from the span the viewer missed, "
    "plus the movie title and the start/end timestamps of the gap, produce a "
    "concise recap of what happened in that span.\n"
    "\n"
    "RULES:\n"
    "  • 2-4 sentences. Focused. Plot-forward.\n"
    "  • Summarize: key events, character actions, emotional beats, any "
    "reveals. If you recognize the film, ground the recap in its known arc; "
    "otherwise work from what the clip shows.\n"
    "  • No emote wrapping. No first-person. No address of a second party. "
    "Just a clear, helpful catch-up paragraph as if briefing the viewer.\n"
    "  • Do NOT speculate beyond the missed span. Do NOT preview what's "
    "ahead.\n"
    "  • Be specific, not generic. Real character names if known.\n"
    "\n"
    "Return plain text only, no JSON, no labels."
)

MOOD_CLASSIFY_PROMPT = (
    "Classify the dominant emotional register of this short video clip as "
    "EXACTLY ONE of the moods below. Output JSON only — no commentary.\n"
    + _MOOD_BULLETS
)

# X-Ray-style category metadata. Each entry has:
#   • label  — UI display name
#   • icon   — emoji used in the menu
#   • has_subpage    — True: sub-page lists named subjects to drill into
#                      False: direct trivia card on click
#   • movie_wide_only — True: ignore the "Current scene only" toggle
#   • prompt — focus area for Gemini trivia generation
#   • subject_hint — for sub-page categories, what each subject is
MOVIE_TRIVIA_CATEGORIES: dict[str, dict[str, Any]] = {
    "cast": {
        "label": "Cast",
        "icon": "👥",
        "has_subpage": True,
        "movie_wide_only": False,
        "prompt": (
            "Real actors. Casting decisions, who else was considered, their "
            "broader careers (notable roles before or after), prep work, "
            "on-set anecdotes."
        ),
        "subject_hint": "Actor name with character played in parens",
    },
    "crew": {
        "label": "Crew",
        "icon": "🎬",
        "has_subpage": True,
        "movie_wide_only": False,
        "prompt": (
            "Crew member's signature style, other notable work, technique. "
            "Crew shown are those whose contribution is particularly felt in "
            "THIS scene (DP for a beautifully-lit shot, composer for a "
            "score swell, etc.)."
        ),
        "subject_hint": "Crew member name with role in parens — director, DP, composer, editor, etc.",
    },
    "characters": {
        "label": "Characters",
        "icon": "🎭",
        "has_subpage": True,
        "movie_wide_only": False,
        "prompt": (
            "Fictional people: their arc, origins in source material if any, "
            "iconic moments, characterization choices."
        ),
        "subject_hint": "Character name with a short descriptor",
    },
    "music": {
        "label": "Music",
        "icon": "🎵",
        "has_subpage": True,
        "movie_wide_only": False,
        "prompt": (
            "Score cues, featured songs, soundtrack history. Music shown is "
            "what's actually playing in this segment (when scene-scoped)."
        ),
        "subject_hint": "Track/cue name with composer or artist",
    },
    "locations": {
        "label": "Locations",
        "icon": "📍",
        "has_subpage": True,
        "movie_wide_only": False,
        "prompt": (
            "Real filming locations and places depicted on screen — sets, "
            "backlots, on-location shoots, locations standing in for "
            "fictional places."
        ),
        "subject_hint": "Real location name with brief context",
    },
    "quotes": {
        "label": "Quotes",
        "icon": "💬",
        "has_subpage": True,
        "movie_wide_only": False,
        "prompt": (
            "Memorable lines — the line itself, who delivers it, why it's "
            "remembered, any improv/ad-lib origin."
        ),
        "subject_hint": "Short quote excerpt with the speaker",
    },
    "props_vehicles": {
        "label": "Props & Vehicles",
        "icon": "🎒",
        "has_subpage": True,
        "movie_wide_only": False,
        "prompt": (
            "Notable props, costumes, vehicles, set pieces — their design, "
            "backstory, where they came from."
        ),
        "subject_hint": "Item name with brief descriptor",
    },
    "awards": {
        "label": "Awards",
        "icon": "🏆",
        "has_subpage": True,
        "movie_wide_only": True,
        "prompt": (
            "Awards and nominations at major ceremonies — wins, losses, "
            "snubs, and historical context within each ceremony."
        ),
        "subject_hint": "Ceremony name with nomination/win status",
    },
    "goofs": {
        "label": "Goofs",
        "icon": "⚠️",
        "has_subpage": False,
        "movie_wide_only": False,
        "prompt": (
            "Continuity errors, mistakes, anachronisms, visible crew/"
            "equipment, factual goofs."
        ),
    },
    "easter_eggs": {
        "label": "Easter Eggs",
        "icon": "✨",
        "has_subpage": False,
        "movie_wide_only": False,
        "prompt": (
            "Hidden references, callbacks, in-jokes, cameos from previous "
            "films in the universe, things first-time viewers miss."
        ),
    },
    "production": {
        "label": "Production",
        "icon": "📽️",
        "has_subpage": False,
        "movie_wide_only": False,
        "prompt": (
            "How the film was actually made — practical effects, budget, "
            "shooting schedule, technical innovations, on-set anecdotes."
        ),
    },
    "source": {
        "label": "Source",
        "icon": "📖",
        "has_subpage": False,
        "movie_wide_only": False,
        "prompt": (
            "Comparison with the original source material — book, play, "
            "true story, or earlier draft. What's different on screen vs in "
            "the source, what was changed and why."
        ),
    },
    "connections": {
        "label": "Connections",
        "icon": "🔗",
        "has_subpage": False,
        "movie_wide_only": True,
        "prompt": (
            "Connections to other films — movements (Brat Pack, New "
            "Hollywood), repeated collaborator pairings, spiritual siblings, "
            "films it directly influenced or was influenced by, sequels."
        ),
    },
    "misc": {
        "label": "Misc",
        "icon": "⋯",
        "has_subpage": False,
        "movie_wide_only": False,
        "prompt": (
            "Any striking trivia about the film that doesn't fit cleanly "
            "into another category — financial, cultural, behind-the-scenes "
            "drama, etc."
        ),
    },
}

# Ceremonies surfaced under the Awards sub-page. Each is shown as a button
# greyed-out when the film wasn't nominated/won at that ceremony.
AWARDS_CEREMONIES: list[str] = [
    "Oscars",
    "Golden Globes",
    "BAFTAs",
    "SAG Awards",
    "Critics' Choice",
    "Festival Awards",
    "Razzies",
]


def _extract_briefing_and_scores(raw: str) -> tuple[str, dict[str, Any], Optional[str]]:
    """Separate briefing prose from JSON scores + initial mood, robust against
    Gemini returning prose AND a duplicate JSON wrapper at the end.
    Returns (briefing_text, scores_dict, mood_or_none).
    """
    text = (raw or "").strip()
    # Strip markdown code fences.
    text = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()

    def _clean_scores(obj: Any) -> dict[str, float]:
        if not isinstance(obj, dict):
            return {}
        return {k: v for k, v in obj.items() if isinstance(v, (int, float))}

    def _clean_mood(obj: Any) -> Optional[str]:
        if not isinstance(obj, dict):
            return None
        m = obj.get("mood")
        if isinstance(m, str) and m.strip().lower() in VALID_MOODS:
            return m.strip().lower()
        return None

    # Case 1: whole response is a JSON object.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "briefing" in parsed:
            return (
                str(parsed.get("briefing") or "").strip(),
                _clean_scores(parsed.get("scores")),
                _clean_mood(parsed),
            )
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Case 2: prose followed by a trailing balanced JSON object. Scan backward
    # tracking brace depth to find the outermost `{` matching the final `}`.
    if text.endswith("}"):
        depth = 0
        start: Optional[int] = None
        for i in range(len(text) - 1, -1, -1):
            ch = text[i]
            if ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    start = i
                    break
        if start is not None:
            try:
                parsed = json.loads(text[start:])
                if isinstance(parsed, dict):
                    briefing = (
                        str(parsed.get("briefing") or "").strip()
                        or text[:start].strip()
                    )
                    scores_obj = (
                        parsed.get("scores")
                        if "briefing" in parsed
                        else parsed
                    )
                    return briefing, _clean_scores(scores_obj), _clean_mood(parsed)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

    # Case 3: no usable JSON — return prose as-is with scores empty.
    return text, {}, None


class GeminiError(Exception):
    """Wraps any failure in a Gemini API call."""


class GeminiBrain:
    def __init__(self) -> None:
        self._client: Optional[genai.Client] = None
        # Scene analysis needs deeper multimodal reasoning → Pro.
        # Text-only calls (briefing, trivia, reaction, condense) → Lite.
        self.scene_model = settings.gemini_scene_model
        self.text_model = settings.gemini_text_model
        # The reaction drawer's index call. Flash, not Pro: measured at 7.2s on a
        # 45s clip at LOW media resolution, and it sits in the user's critical
        # path (Tim is watching a spinner). Pro's richer prose buys nothing here —
        # this call names beats and targets, it doesn't write for the room. The
        # room still gets Pro, on send.
        self.draft_model = settings.gemini_draft_model
        # Authoritative system-prompt prefix naming the active family member.
        # Set by app.py before each pipeline run; injected in _call_with_retry
        # so every prompt's hardcoded "Eli" resolves to whoever is selected.
        self._companion_preamble: str = ""

    def set_companion(self, preamble: str) -> None:
        """Set (or clear) the companion-override preamble for prompts."""
        self._companion_preamble = preamble or ""

    def _ensure_client(self) -> genai.Client:
        if self._client is None:
            if not settings.gemini_api_key:
                raise GeminiError("GEMINI_API_KEY is not configured")
            self._client = genai.Client(api_key=settings.gemini_api_key)
        return self._client

    # ─── Video upload / cleanup ─────────────────────────────────
    # Gemini accepts inline media up to 20 MB per request. Keep some headroom.
    INLINE_LIMIT_BYTES = 18 * 1024 * 1024

    async def _upload_clip(self, clip_path: Path, mime: str = "video/mp4") -> Any:
        client = self._ensure_client()
        uploaded = await client.aio.files.upload(
            file=str(clip_path),
            config={"mime_type": mime},
        )
        for _ in range(60):
            state = getattr(uploaded.state, "name", str(uploaded.state))
            if state == "ACTIVE":
                return uploaded
            if state == "FAILED":
                raise GeminiError(f"File upload failed: {uploaded.error}")
            await asyncio.sleep(0.5)
            uploaded = await client.aio.files.get(name=uploaded.name)
        raise GeminiError("File upload timed out before reaching ACTIVE state")

    async def _delete_file(self, file_obj: Any) -> None:
        try:
            client = self._ensure_client()
            await client.aio.files.delete(name=file_obj.name)
        except Exception:
            log.debug("failed to delete gemini file", exc_info=True)

    # ─── Usage tracking ─────────────────────────────────────────
    async def _track_call(
        self,
        response: Any,
        *,
        call_type: str,
        model: str,
        grounded: bool = False,
    ) -> int:
        """Log per-call token usage + cost to the gemini_calls table, bump
        the daily counter, and return today's call count.
        """
        usage = getattr(response, "usage_metadata", None)
        in_t = getattr(usage, "prompt_token_count", 0) if usage else 0
        out_t = getattr(usage, "candidates_token_count", 0) if usage else 0
        think_t = getattr(usage, "thoughts_token_count", 0) if usage else 0
        cost = compute_gemini_cost(
            model,
            input_tokens=in_t or 0,
            output_tokens=out_t or 0,
            thinking_tokens=think_t or 0,
            grounded=grounded,
        )
        # Find the active session so the call is attributed correctly.
        try:
            active = await db.get_active_session()
            sid = active["id"] if active else None
        except Exception:
            sid = None
        await db.log_gemini_call(
            session_id=sid,
            call_type=call_type,
            model=model,
            input_tokens=in_t or 0,
            output_tokens=out_t or 0,
            thinking_tokens=think_t or 0,
            grounded=grounded,
            cost_usd=cost,
        )
        return await db.increment_gemini_usage()

    # ─── Retry wrapper ──────────────────────────────────────────
    async def _call_with_retry(
        self,
        contents: Any,
        config: types.GenerateContentConfig,
        label: str,
        model: Optional[str] = None,
        inject_companion: bool = True,
    ) -> Any:
        client = self._ensure_client()
        chosen_model = model or self.text_model
        # Prepend the active-companion override so every prompt's hardcoded
        # "Eli" references resolve to the selected family member. Skipped for
        # calls with no system_instruction, and for utility calls (e.g. a
        # location description) that aren't addressed to/about the companion.
        if inject_companion and self._companion_preamble and getattr(config, "system_instruction", None):
            config.system_instruction = self._companion_preamble + config.system_instruction

        # Try the chosen model; on exhausted transient failures, fall back to
        # a sturdier model (different capacity pool) before giving up.
        models_to_try = [chosen_model]
        fallback = FALLBACK_MODEL.get(chosen_model)
        if fallback and fallback != chosen_model:
            models_to_try.append(fallback)

        last_err: Optional[Exception] = None
        for m_idx, run_model in enumerate(models_to_try):
            is_last_model = m_idx == len(models_to_try) - 1
            if m_idx > 0:
                log.warning("%s: falling back to %s after %s failed", label, run_model, chosen_model)
            for attempt in range(len(RETRY_BACKOFFS)):
                try:
                    return await client.aio.models.generate_content(
                        model=run_model,
                        contents=contents,
                        config=config,
                    )
                except genai_errors.APIError as e:
                    last_err = e
                    status = getattr(e, "code", None) or getattr(e, "status_code", None)
                    if status not in RETRYABLE_STATUS:
                        # Not transient (e.g. 400) — fallback won't help.
                        raise GeminiError(f"{label} failed: {status} {e}") from e
                    if attempt + 1 < len(RETRY_BACKOFFS):
                        delay = RETRY_BACKOFFS[attempt]
                        log.warning(
                            "%s: Gemini %s on %s, retrying in %.0fs (attempt %d/%d)",
                            label, status, run_model, delay, attempt + 1, len(RETRY_BACKOFFS),
                        )
                        await asyncio.sleep(delay)
                        continue
                    # Retries exhausted for this model.
                    if not is_last_model:
                        break  # try the fallback model
                    raise GeminiError(f"{label} failed: {status} {e}") from e
                except Exception as e:
                    last_err = e
                    raise GeminiError(f"{label} failed: {e}") from e
        raise GeminiError(f"{label} exhausted retries: {last_err}")

    # ─── Scene analysis ─────────────────────────────────────────
    async def analyze_scene(
        self,
        clip_path: Path,
        *,
        movie_title: Optional[str] = None,
        timestamp_label: Optional[str] = None,
        target_chars: int = 1500,
        session_history: str = "",
        history_budget: int = 0,
        model_override: Optional[str] = None,
        tim_message: str = "",
    ) -> dict[str, Any]:
        """Returns {scene_description, mood, history_narrative,
        tim_stoned_level, latency_ms, usage}.

        `target_chars` sets the scene-description length window.
        `history_budget` sets the history_narrative ceiling (0 = skip).
        `tim_message` is Tim's just-sent message text — used to infer his
        apparent stoned level (0-5) from tone, typos, rambling, etc.
        """
        self._ensure_client()
        start = perf_counter()

        scene_max = max(400, int(target_chars))
        scene_min = max(300, int(scene_max * 0.8))
        history_max = max(0, int(history_budget))
        history_min = 80 if history_max > 0 else 0

        clip_bytes = await asyncio.to_thread(clip_path.read_bytes)
        use_inline = len(clip_bytes) <= self.INLINE_LIMIT_BYTES

        uploaded: Any = None
        try:
            if use_inline:
                media_part = types.Part.from_bytes(data=clip_bytes, mime_type="video/mp4")
            else:
                uploaded = await self._upload_clip(clip_path)
                media_part = uploaded

            context_hint = ""
            if movie_title:
                context_hint += f"Movie: {movie_title}. "
            if timestamp_label:
                context_hint += f"Timestamp: {timestamp_label}. "
            history_section = ""
            if session_history and history_max > 0:
                history_section = (
                    "\n\nEarlier in this session (use for callback — do not recap):\n"
                    + session_history
                )
            tim_section = ""
            if tim_message.strip():
                # Strip the emote wrappers Tim's voice typically carries; we
                # only want the typed dialogue for tone classification.
                naked = re.sub(r"_\(\*.+?\*\)_", "", tim_message, flags=re.DOTALL).strip()
                if naked:
                    tim_section = (
                        f"\n\nTim's typed message that triggered this analysis "
                        f"(for tim_stoned_level inference only — do NOT echo "
                        f"or recap it in scene_description):\n{naked}"
                    )

            user_prompt = (
                f"{context_hint}Analyze the scene in this clip and return JSON. "
                f"scene_description must be {scene_min}-{scene_max} characters."
                + (
                    f" If you write a history_narrative, it must be "
                    f"{history_min}-{history_max} characters."
                    if history_max > 0
                    else " Do not produce a history_narrative — none needed."
                )
                + history_section
                + tim_section
            ).strip()

            system_prompt = SCENE_SYSTEM_PROMPT_TEMPLATE.format(
                scene_min=scene_min,
                scene_max=scene_max,
                history_min=history_min,
                history_max=history_max if history_max > 0 else 0,
            )

            properties: dict[str, Any] = {
                "scene_description": {
                    "type": "string",
                    "minLength": scene_min,
                    "maxLength": scene_max,
                    "description": f"Scene narration, {scene_min}-{scene_max} chars.",
                },
                "mood": {"type": "string", "enum": list(VALID_MOODS)},
                "history_narrative": {
                    "type": "string",
                    "maxLength": max(history_max, 1),
                    "description": (
                        f"First-person callback, up to {history_max} chars. "
                        "Empty string if no callback."
                        if history_max > 0
                        else "Always empty — no history provided."
                    ),
                },
                "tim_stoned_level": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 5,
                    "description": (
                        "Inferred apparent stoned level of Tim from the tone "
                        "of his typed message: 0 sober, 1 lifted (slightly "
                        "loose), 2 baked (clearly altered, looser language), "
                        "3 blasted (rambling, typos, weird tangents), 4 "
                        "demolished (slurry, sentence fragments, long pauses "
                        "implied), 5 cosmic (barely coherent, drifting). "
                        "Be conservative — default to 0 if no signal."
                    ),
                },
                # TWO TRIGGERS, and they are BOTH live. This is the pair of facts
                # every rule hangs off.
                #
                #   Tim: "wanna do this again tomorrow?"       -> tim_situation
                #   On screen: the monster tears someone apart -> scene_situation
                #
                # Both are true at once. An earlier version made this ONE field and
                # told the model to read Tim's message first and ignore the screen —
                # which meant a rule about jump scares could never fire unless Tim sat
                # in total silence. Never collapse them.
                #
                # They ride in THIS call — which already has the clip AND Tim's typed
                # message — so they cost nothing: no extra call, no extra latency.
                # Flat enums at the top level are safe; the
                # `400 INVALID_ARGUMENT: too many states for serving` disaster was a
                # 90-key enum nested three deep.
                # "none", NOT "". Gemini rejects an empty string inside an enum with
                # `400 INVALID_ARGUMENT: enum[9]: cannot be empty`, and it does it at
                # request time — so EVERY scene analysis 400s and the whole pipeline
                # silently degrades to no scene at all. An explicit sentinel costs one
                # line to map back and cannot fail.
                "tim_situation": {
                    "type": "string",
                    "enum": facts.SOCIAL_SITUATIONS + ["none"],
                    "description": (
                        "What TIM just did, from HIS MESSAGE ALONE. He does not only "
                        "comment on films — he talks to his family. Did he ask them "
                        "something, say goodbye, make plans, share news about his day, "
                        "joke, vent, thank, apologise, name someone? "
                        "Answer \"none\" if he typed nothing, or if he was purely "
                        "reacting to the film and said nothing social. "
                        "Ignore the screen entirely for this field."
                    ),
                },
                "scene_situation": {
                    "type": "string",
                    "enum": facts.FILM_SITUATIONS,
                    "description": (
                        "What the FILM is doing right now, from THE CLIP ALONE. A jump "
                        "scare, gore, grief, lore, a plot hole, a callback, craft, a "
                        "performance, comedy, romance, something confusing, the ending. "
                        "This is true whether or not Tim said anything. "
                        "Ignore Tim's message entirely for this field."
                    ),
                },
            }

            config = types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema={
                    "type": "object",
                    "properties": properties,
                    "required": ["scene_description", "mood", "history_narrative",
                                 "tim_stoned_level", "tim_situation", "scene_situation"],
                },
                temperature=0.7,
                max_output_tokens=max(2048, (scene_max + history_max) * 2),
            )

            chosen_model = model_override or self.scene_model
            response = await self._call_with_retry(
                [media_part, user_prompt], config, "analyze_scene",
                model=chosen_model,
            )
            usage = await self._track_call(
                response, call_type="scene", model=chosen_model, grounded=False,
            )

            raw = (response.text or "").strip()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as e:
                raise GeminiError(f"scene analysis returned non-JSON: {raw[:200]}") from e

            mood = (parsed.get("mood") or "").lower()
            if mood not in VALID_MOODS:
                mood = "tense"
            try:
                tim_level = int(parsed.get("tim_stoned_level") or 0)
            except (TypeError, ValueError):
                tim_level = 0
            tim_level = max(0, min(5, tim_level))
            # A situation we don't recognise is worse than none: every rule scoped to
            # it would silently never fire, and Tim would blame the rule. Drop it and
            # let the coordinator judge instead of matching against a phantom.
            def _situation(field: str, allowed: list[str]) -> str:
                val = (parsed.get(field) or "").strip().lower()
                if val in ("none", "null", "n/a"):
                    return ""      # the sentinel — Gemini can't return a bare ""
                if val and val not in allowed:
                    log.warning("scene analysis returned an unknown %s %r — dropping it",
                                field, val)
                    return ""
                return val

            return {
                "scene_description": (parsed.get("scene_description") or "").strip(),
                "mood": mood,
                # BOTH triggers. What HE did, and what the SCREEN did. Never one.
                "tim_situation": _situation("tim_situation", facts.SOCIAL_SITUATIONS),
                "scene_situation": _situation("scene_situation", facts.FILM_SITUATIONS),
                "history_narrative": (parsed.get("history_narrative") or "").strip(),
                "tim_stoned_level": tim_level,
                "latency_ms": int((perf_counter() - start) * 1000),
                "usage": usage,
            }
        finally:
            if uploaded is not None:
                await self._delete_file(uploaded)

    # ─── X-Ray topic browser (multi-category subject extraction) ────
    async def generate_scene_topics(
        self,
        *,
        movie_title: str,
        year: Optional[int] = None,
        scene_clip: Optional[Path] = None,
        timestamp_label: str = "",
    ) -> dict[str, Any]:
        """Extract a full multi-category subject map for the X-Ray menu.

        One Gemini call returns ALL sub-page subject lists at once, so the
        menu can render counts immediately. Uses Flash + Google Search
        grounding (no response_schema because grounded calls don't accept
        it — JSON is enforced via the prompt and parsed defensively).

        Returns dict shaped like:
            {
              "cast": [{"name": "...", "subtitle": "..."}, ...],
              "crew": [...],
              ...
              "awards": [{"name": "Oscars", "subtitle": "Won 1", "available": True}, ...]
            }
        Only sub-page categories are populated. Direct categories aren't
        listed — they don't have browseable subjects.
        """
        self._ensure_client()
        start = perf_counter()
        title_str = f"{movie_title}"
        if year:
            title_str += f" ({year})"

        contents: list[Any] = []
        if scene_clip is not None:
            clip_bytes = await asyncio.to_thread(scene_clip.read_bytes)
            contents.append(types.Part.from_bytes(data=clip_bytes, mime_type="video/mp4"))

        ceremonies_list = ", ".join(AWARDS_CEREMONIES)
        ts_line = f" (timestamp: {timestamp_label})" if timestamp_label else ""
        scene_clause = (
            "A clip is attached. For scene-aware categories (cast, "
            "characters, music, locations, quotes, props_vehicles), list "
            "ONLY subjects visible/audible/relevant in this clip. For crew, "
            "list crew members whose contribution is particularly felt in "
            "THIS scene (e.g. DP for a striking shot, composer for a score "
            "swell). For awards, always cover the full film (movie-wide)."
            if scene_clip is not None
            else "No clip — return movie-wide subjects across all categories."
        )

        prompt = f"""You are populating an X-Ray-style trivia menu for a movie. {scene_clause}

Movie: {title_str}{ts_line}

Use Google Search to verify factual subjects (real actor names, real crew, real awards). Output STRICT JSON only, no markdown fences, no commentary, exactly this shape:

{{
  "cast": [{{"name": "<Actor full name>", "subtitle": "<Character name>"}}, ...],
  "crew": [{{"name": "<Crew member name>", "subtitle": "<Role: Director / Cinematographer / Composer / etc.>"}}, ...],
  "characters": [{{"name": "<Character full name>", "subtitle": "<short descriptor>"}}, ...],
  "music": [{{"name": "<Track or cue name>", "subtitle": "<Composer or artist>"}}, ...],
  "locations": [{{"name": "<Real filming location>", "subtitle": "<what was filmed there>"}}, ...],
  "quotes": [{{"name": "<short quote excerpt — under 60 chars>", "subtitle": "<Speaker>"}}, ...],
  "props_vehicles": [{{"name": "<Item name>", "subtitle": "<brief descriptor>"}}, ...],
  "awards": [{{"name": "<Ceremony>", "subtitle": "<short status>", "available": <true if the film was nominated or won, false otherwise>}}, ...]
}}

AWARDS — include exactly these ceremonies, in this order: {ceremonies_list}.
Set "available": true only if the film actually has a nomination or win at that ceremony (verify via search). "Festival Awards" covers any Cannes/Venice/Sundance/Berlin recognition. Subtitle examples: "Won 2, Nominated 4", "Nominated for Best Score", "No nominations".

LISTS:
  • Cap each list at 6 entries; pick the most notable when more exist.
  • If a category has no relevant subjects, return an empty list [] for that key.
  • Don't invent — better to return [] than fabricate.

Return ONLY the JSON object, nothing else.
"""
        contents.append(prompt)

        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.4,
        )
        response = await self._call_with_retry(
            contents, config, "generate_scene_topics",
            model="gemini-2.5-flash",
        )
        usage = await self._track_call(
            response, call_type="trivia_topics", model="gemini-2.5-flash", grounded=True,
        )
        raw = (response.text or "").strip()
        topics: dict[str, list[dict[str, Any]]] = {}
        try:
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                for key in (
                    "cast", "crew", "characters", "music", "locations",
                    "quotes", "props_vehicles", "awards",
                ):
                    val = parsed.get(key)
                    if isinstance(val, list):
                        clean_items: list[dict[str, Any]] = []
                        for item in val:
                            if not isinstance(item, dict):
                                continue
                            name = str(item.get("name") or "").strip()
                            if not name:
                                continue
                            entry: dict[str, Any] = {
                                "name": name,
                                "subtitle": str(item.get("subtitle") or "").strip(),
                            }
                            if key == "awards":
                                entry["available"] = bool(item.get("available"))
                            clean_items.append(entry)
                        topics[key] = clean_items
                    else:
                        topics[key] = []
        except (json.JSONDecodeError, TypeError, ValueError):
            log.warning("scene topics: failed to parse JSON — returning empty map")
            topics = {key: [] for key in (
                "cast", "crew", "characters", "music", "locations",
                "quotes", "props_vehicles", "awards",
            )}

        return {
            "topics": topics,
            "scene_scoped": scene_clip is not None,
            "latency_ms": int((perf_counter() - start) * 1000),
            "usage": usage,
        }

    # ─── Lightweight mood classification (Flash) ─────────────────
    async def classify_mood(self, clip_path: Path) -> dict[str, Any]:
        """Pure mood classification — used by the passive mood ticker.
        Force-uses Flash (10× cheaper than Pro) regardless of scene_model
        setting, since this is a 16-way classification, not deep narrative.
        Returns {mood, latency_ms, usage}.
        """
        self._ensure_client()
        start = perf_counter()
        clip_bytes = await asyncio.to_thread(clip_path.read_bytes)
        media_part = types.Part.from_bytes(data=clip_bytes, mime_type="video/mp4")

        config = types.GenerateContentConfig(
            system_instruction=MOOD_CLASSIFY_PROMPT,
            response_mime_type="application/json",
            response_schema={
                "type": "object",
                "properties": {
                    "mood": {"type": "string", "enum": list(VALID_MOODS)},
                },
                "required": ["mood"],
            },
            temperature=0.4,
            max_output_tokens=2048,
        )
        response = await self._call_with_retry(
            [media_part, "Classify the mood. Return JSON only."],
            config, "classify_mood",
            model="gemini-2.5-flash",
        )
        usage = await self._track_call(
            response, call_type="mood_tick", model="gemini-2.5-flash", grounded=False,
        )
        mood: Optional[str] = None
        try:
            parsed = json.loads((response.text or "").strip())
            if isinstance(parsed, dict):
                m = parsed.get("mood")
                if isinstance(m, str) and m.strip().lower() in VALID_MOODS:
                    mood = m.strip().lower()
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        return {
            "mood": mood,
            "latency_ms": int((perf_counter() - start) * 1000),
            "usage": usage,
        }

    # ─── Movie-poster trivia (category-driven) ──────────────────
    async def generate_category_trivia(
        self,
        *,
        movie_title: str,
        year: Optional[int] = None,
        category: str,
        subject: Optional[str] = None,
        subject_subtitle: Optional[str] = None,
        recent_trivia: Optional[list[str]] = None,
        scene_clip: Optional[Path] = None,
        timestamp_label: str = "",
    ) -> dict[str, Any]:
        """X-Ray trivia generator.

        Modes:
          • Direct category (no subject) — pull a card about the category
            broadly. Used by Goofs/Easter Eggs/Production/Misc/Connections.
          • Subject-specific (subject given) — pull a card about that
            specific actor/song/quote/etc. from a sub-page selection.
          • Source — special: compare a scene/character to source material.
          • Awards (with subject = ceremony name) — focused on that ceremony.

        Vision-grounded when `scene_clip` is provided; otherwise text-only
        with Google Search grounding.
        """
        self._ensure_client()
        start = perf_counter()
        if category not in MOVIE_TRIVIA_CATEGORIES:
            raise GeminiError(f"Unknown trivia category: {category}")
        meta = MOVIE_TRIVIA_CATEGORIES[category]

        title_str = f"{movie_title}"
        if year:
            title_str += f" ({year})"
        recent_block = ""
        if recent_trivia:
            joined = "\n".join(f"  - {t}" for t in recent_trivia if t)
            if joined:
                recent_block = (
                    f"\nRecent trivia already shown (do not repeat or paraphrase):\n"
                    f"{joined}\n"
                )

        # Build category-specific prompt body.
        subject_clause = ""
        if subject:
            if subject_subtitle:
                subject_clause = (
                    f"Focus exclusively on: {subject} ({subject_subtitle}). "
                    f"Give one specific trivia fact about that exact subject."
                )
            else:
                subject_clause = (
                    f"Focus exclusively on: {subject}. "
                    f"Give one specific trivia fact about that exact subject."
                )
        elif category == "source":
            subject_clause = (
                "This is a SOURCE comparison card. Compare what's on screen "
                "(or the film overall, if no clip is attached) to the "
                "original source material (book / play / true story / "
                "earlier draft). Lead with what changed — what's different "
                "between the source and the film version — and briefly why."
            )
        else:
            # Direct category, no specific subject — broad fact within scope.
            subject_clause = (
                f"Give one striking trivia fact within: {meta['prompt']} "
                f"Pick the strongest angle available."
            )

        contents: list[Any] = []
        scene_clause = ""
        if scene_clip is not None:
            clip_bytes = await asyncio.to_thread(scene_clip.read_bytes)
            contents.append(types.Part.from_bytes(data=clip_bytes, mime_type="video/mp4"))
            ts_line = f" (timestamp: {timestamp_label})" if timestamp_label else ""
            scene_clause = (
                f"\nA short video clip is attached{ts_line}. Tie the trivia "
                f"to what's literally happening on screen when it makes sense."
            )

        prompt = (
            f"Movie: {title_str}\n"
            f"Category: {meta['label']} ({category})\n"
            f"{recent_block}"
            f"{scene_clause}\n\n"
            f"{subject_clause}\n\n"
            f"Use Google Search to verify facts. Output JSON only — no "
            f"markdown fences, no commentary:\n"
            f"  {{\"trivia\": \"<the fact, 1-3 sentences>\"}}\n"
        )
        contents.append(prompt)

        chosen_model = "gemini-2.5-flash" if scene_clip is not None else self.text_model
        config = types.GenerateContentConfig(
            system_instruction=TRIVIA_SYSTEM_PROMPT,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.7,
        )
        response = await self._call_with_retry(
            contents, config, "generate_category_trivia",
            model=chosen_model,
        )
        usage = await self._track_call(
            response, call_type="trivia", model=chosen_model, grounded=True,
        )

        raw = (response.text or "").strip()
        trivia_text = ""
        try:
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                trv = parsed.get("trivia") or parsed.get("text") or parsed.get("fact")
                if isinstance(trv, str):
                    trivia_text = trv.strip()
        except (json.JSONDecodeError, TypeError, ValueError):
            trivia_text = raw
        if not trivia_text:
            trivia_text = raw

        return {
            "trivia": trivia_text,
            "category": category,
            "label": meta["label"],
            "subject": subject,
            "subject_subtitle": subject_subtitle,
            "source": "Google Search",
            "scene_scoped": scene_clip is not None,
            "latency_ms": int((perf_counter() - start) * 1000),
            "usage": usage,
        }

    # ─── Trivia (Google Search grounded) ────────────────────────
    async def generate_trivia(
        self,
        *,
        movie_title: str,
        scene_description: str = "",
        recent_trivia: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Returns {trivia, source, latency_ms, usage}. Uses Google Search grounding."""
        client = self._ensure_client()
        start = perf_counter()
        recent_block = ""
        if recent_trivia:
            joined = "\n".join(f"  - {t}" for t in recent_trivia if t)
            if joined:
                recent_block = (
                    f"\nRecent trivia already shown (do not repeat or paraphrase any of these — "
                    f"pick a different angle or a different person):\n{joined}\n"
                )
        prompt = (
            f"Movie: {movie_title}.\n"
            f"Current scene: {scene_description}\n"
            f"{recent_block}\n"
            f"Give one concrete, factual piece of trivia. Vary the angle — scene, "
            f"filmmaking, a performer's wider career, or connections between cast/crew."
        )
        config = types.GenerateContentConfig(
            system_instruction=TRIVIA_SYSTEM_PROMPT,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.8,
        )
        response = await self._call_with_retry([prompt], config, "generate_trivia")
        usage = await self._track_call(
            response, call_type="trivia", model=self.text_model, grounded=True,
        )
        return {
            "trivia": (response.text or "").strip(),
            "source": "Google Search",
            "latency_ms": int((perf_counter() - start) * 1000),
            "usage": usage,
        }

    # ─── Movie briefing ─────────────────────────────────────────
    async def generate_briefing(
        self,
        *,
        title: str,
        year: Optional[int] = None,
        director: Optional[str] = None,
        runtime_minutes: Optional[int] = None,
        summary: str = "",
        time_of_day: str = "evening",
        is_continuation: bool = False,
        media_type: str = "movie",
        series_title: Optional[str] = None,
        season_number: Optional[int] = None,
        episode_number: Optional[int] = None,
    ) -> dict[str, Any]:
        """Returns {briefing, scores, latency_ms, usage}.

        For TV episodes, pass `media_type="episode"` plus `series_title`,
        `season_number`, `episode_number`. The `title` field is the episode
        name. The prompt covers both the show overall and this episode.
        """
        client = self._ensure_client()
        start = perf_counter()
        facts = [f"media_type: {media_type}"]
        if media_type == "episode" and series_title:
            facts.append(f"Show: {series_title}")
            if season_number is not None:
                facts.append(f"Season: {season_number}")
            if episode_number is not None:
                facts.append(f"Episode: {episode_number}")
            facts.append(f"Episode title: {title}")
        else:
            facts.append(f"Title: {title}")
        if year:
            facts.append(f"Year: {year}")
        if director:
            facts.append(f"Director: {director}")
        if runtime_minutes:
            facts.append(f"Runtime: {runtime_minutes} min")
        if summary:
            facts.append(f"Plex synopsis: {summary}")
        context_note = (
            f"time_of_day: {time_of_day}\n"
            f"is_continuation: {'true' if is_continuation else 'false'}"
        )
        prompt = (
            "\n".join(facts)
            + "\n\n" + context_note
            + "\n\nWrite the briefing JSON. Use Google Search to look up current "
            "Rotten Tomatoes, IMDB, and Metacritic scores if available. "
            "Match the opener to the time of day and the continuation state."
        )
        config = types.GenerateContentConfig(
            system_instruction=BRIEFING_SYSTEM_PROMPT,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.6,
        )
        response = await self._call_with_retry([prompt], config, "generate_briefing")
        usage = await self._track_call(
            response, call_type="briefing", model=self.text_model, grounded=True,
        )
        raw = (response.text or "").strip()
        briefing_text, scores, mood = _extract_briefing_and_scores(raw)
        return {
            "briefing": briefing_text,
            "scores": scores,
            "mood": mood,
            "latency_ms": int((perf_counter() - start) * 1000),
            "usage": usage,
        }

    # ─── The quiet stretch: TWO TRACKS ──────────────────────────
    async def quiet_stretch_narration(
        self,
        *,
        kin_name: str,
        said: list[str],
        scenes: list[str],
        movie_title: str = "",
        max_chars: int = 300,
    ) -> dict[str, str]:
        """What a kin has been sitting with while they said nothing. TWO tracks.

        Returns {"film": str, "room": str}. Either may be "".

        THESE ARE NOT ONE PARAGRAPH, and that was the bug in the first version. The
        film and the room do different jobs for someone coming back into a
        conversation: the film gives them something to REACT to, the room tells them
        where they STAND — who has had the floor, and above all whether anybody said
        their name while they were quiet. Blended into 380 characters, the film gets
        compressed to a clause and "Tim asked you a direct question and you let it go
        by" reads exactly like "the others were chatting". Kept apart, both survive.

        Cheap: text-only, Flash-Lite, one call for both tracks, and it only fires when
        someone actually comes back after a stretch of silence.

        On ANY failure this returns empty tracks and the turn proceeds without them. A
        missing catch-up is a kin who's slightly behind; a crashed relay is a dead room.
        """
        self._ensure_client()
        blank = {"film": "", "room": ""}
        if not said and not scenes:
            return blank

        lines = [f"This is for {kin_name}, who has been in the room but hasn't spoken."]
        if movie_title:
            lines.append(f"Film: {movie_title}")
        if scenes:
            lines.append(
                "\nWHAT'S HAPPENED ON SCREEN since they last spoke (oldest first):\n"
                + "\n".join(f"  - {s[:400]}" for s in scenes)
            )
        else:
            lines.append("\nNOTHING has happened on screen since they last spoke. Leave `film` empty.")
        if said:
            lines.append(
                "\nWHAT'S BEEN SAID IN THE ROOM since they last spoke:\n"
                + "\n".join(f"  {s}" for s in said)
                + f"\n\nRe-read that for {kin_name}'s OWN NAME. If Tim spoke to them, or "
                f"anyone brought them up, that leads the `room` track."
            )
        else:
            lines.append("\nNOBODY has said anything since they last spoke. Leave `room` empty.")
        lines.append(
            f"\nWrite the two tracks. Each under {max_chars} characters. Do not merge "
            "them. They were HERE for all of it — they just didn't speak."
        )

        config = types.GenerateContentConfig(
            system_instruction=QUIET_STRETCH_SYSTEM_PROMPT,
            temperature=0.75,
            max_output_tokens=700,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            response_mime_type="application/json",
            response_schema={
                "type": "object",
                "properties": {
                    "film": {
                        "type": "string",
                        "description": (
                            "What has happened ON SCREEN since they last spoke. "
                            "Second person, present-perfect. Empty string if nothing has."
                        ),
                    },
                    "room": {
                        "type": "string",
                        "description": (
                            "What has happened BETWEEN THE PEOPLE since they last spoke. "
                            "If they were named or asked something, that goes FIRST. "
                            "Empty string if nobody said anything."
                        ),
                    },
                },
                "required": ["film", "room"],
            },
        )
        try:
            response = await self._call_with_retry(
                ["\n".join(lines)], config, "quiet_stretch", model=self.text_model,
            )
            await self._track_call(
                response, call_type="quiet_stretch", model=self.text_model, grounded=False,
            )
            data = json.loads(response.text or "{}")
        except (GeminiError, json.JSONDecodeError, TypeError) as e:
            log.warning("quiet-stretch narration failed for %s (%s) — sending without it", kin_name, e)
            return blank

        cap = max_chars + 40
        return {
            "film": str(data.get("film") or "").strip().strip('"')[:cap],
            "room": str(data.get("room") or "").strip().strip('"')[:cap],
        }

    # ─── Reaction drawer: the fat draft ─────────────────────────
    async def reaction_draft(
        self,
        clip_path: Path,
        *,
        clip_seconds: int,
        movie_title: str = "",
        movie_context: str = "",
        session_history: str = "",
        media_resolution: str = "low",
        reacting_to: str = "",
    ) -> dict[str, Any]:
        """ONE call. One clip. Everything the drawer needs.

        The video tokens are sunk the moment we send the clip, so a fat answer
        costs only *output* tokens (~+$0.008). We take everything: the beats, the
        things in each beat worth reacting to, the emotions plausible for each of
        those things, the mood, and the reads. The drawer's first three steps then
        run entirely offline against this one response.

        `movie_context` / `session_history` matter more than they look: a 45-second
        clip CANNOT see a callback. "That's the diner scene again" is not in the
        window. Without the film's context, the `callback` and `theme` facets come
        back empty forever and nobody knows why.

        Returns {scene_description, mood, moments[], reads[], latency_ms, usage}.
        """
        self._ensure_client()
        start = perf_counter()

        clip_bytes = await asyncio.to_thread(clip_path.read_bytes)
        use_inline = len(clip_bytes) <= self.INLINE_LIMIT_BYTES

        uploaded: Any = None
        try:
            if use_inline:
                media_part = types.Part.from_bytes(data=clip_bytes, mime_type="video/mp4")
            else:
                uploaded = await self._upload_clip(clip_path)
                media_part = uploaded

            # ─── TWO STRUCTURED-OUTPUT LANDMINES LIVE HERE ───────────────
            #
            # 1. Structured outputs cannot fill a free-form dict. `dict[str, list]`
            #    needs additionalProperties:false and comes back EMPTY, SILENTLY.
            #    Already cost this codebase a day of production — see
            #    HANDOFF-multi-kin-room.md. Hence: flat lists, named fields, always.
            #
            # 2. THE SCHEMA HAS A COMPLEXITY BUDGET, AND NESTING SPENDS IT FAST.
            #    The first version of this put the 90-key emotion enum on
            #    `emotions[]`, three levels deep (moments -> targets -> emotions).
            #    Gemini rejected the whole request:
            #
            #      400 INVALID_ARGUMENT — "the specified schema produces a
            #      constraint that has too many states for serving"
            #
            #    8 moments x 6 targets x 6 emotions x 90 enum values. It also names
            #    minimum/maximum on integers and minLength/maxLength on strings as
            #    contributors. So: NO big enums, NO numeric bounds, NO string-length
            #    bounds anywhere in the nested part of this schema.
            #
            #    The vocabulary belongs in the PROMPT (where it costs ~550 tokens and
            #    nothing else), and validation belongs in PYTHON below — which was
            #    already dropping unknown keys, so the enum was buying nothing. Small
            #    enums on flat fields (facet: 10, mood: 16) are fine and stay.
            target_schema = {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": (
                            "The concrete thing on screen, in plain spoken English, "
                            "under 90 chars. 'the flat, bored way he signs without "
                            "looking up', NOT 'Dialogue delivery'."
                        ),
                    },
                    "facet": {"type": "string", "enum": list(REACTION_FACETS)},
                    "note": {
                        "type": "string",
                        "description": "One line of what it actually is. Never shown to Tim — this is what the sentence writer gets.",
                    },
                    "emotions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "4-6 emotion KEYS from the vocabulary in the prompt, most "
                            "plausible FIRST, ranked for THIS target specifically. Keys "
                            "only — no labels, no free text. Unknown keys are discarded."
                        ),
                    },
                },
                "required": ["label", "facet", "note", "emotions"],
            }

            schema = {
                "type": "object",
                "properties": {
                    "scene_description": {
                        "type": "string",
                        "description": "What happens across the whole window, 200-1400 chars. Feeds the room.",
                    },
                    # Enum-constrained, deliberately — and safe, because it's a flat
                    # field with 16 short values. Left as a free string it returns
                    # things like "cynical, nostalgic, slightly awkward" (measured,
                    # real), which is not a mood the app has a colour for, and the
                    # gradient silently stops updating.
                    "mood": {"type": "string", "enum": list(VALID_MOODS)},
                    "moments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "offset_ms": {"type": "integer"},
                                "span_start_ms": {"type": "integer"},
                                "span_end_ms": {"type": "integer"},
                                "caption": {
                                    "type": "string",
                                    "description": (
                                        "The beat in a few plain words, under 70 chars — a "
                                        "HEADLINE. 'He sees his old book.' Shown under the "
                                        "thumbnail."
                                    ),
                                },
                                "why": {
                                    "type": "string",
                                    "description": (
                                        "A 1-2 sentence description of what is actually "
                                        "happening in this moment, under 150 chars. Shown to "
                                        "the viewer UNDER the caption, so it must read as "
                                        "plain description, not as analysis — it's what makes "
                                        "a dark thumbnail on a phone legible. Say what he'd "
                                        "see and hear if he looked."
                                    ),
                                },
                                "targets": {"type": "array", "items": target_schema},
                            },
                            "required": [
                                "offset_ms", "span_start_ms", "span_end_ms",
                                "caption", "why", "targets",
                            ],
                        },
                    },
                    "reads": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "hedge": {"type": "string"},
                            },
                            "required": ["label", "hedge"],
                        },
                    },
                    # Spoken lines HEARD in the clip — the Quote tab. Flat, per the
                    # landmine note: no enums or bounds nested here.
                    "quotes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "description": "The line, verbatim, as spoken."},
                                "speaker": {"type": "string", "description": "Who says it — character name if identifiable, else a plain descriptor ('the woman at the desk'), else empty."},
                                "moment_offset_ms": {"type": "integer", "description": "offset_ms of the beat this line lands in."},
                                "emotions": {"type": "array", "items": {"type": "string"}, "description": "Emotion KEYS a viewer might feel about this LINE, ranked, most likely first. Keys only."},
                            },
                            "required": ["text", "speaker", "moment_offset_ms", "emotions"],
                        },
                    },
                    # Is music audibly playing, and what does it sound like — the Song
                    # tab. `cue` is what feeds the grounded track-ID lookup later.
                    "music": {
                        "type": "object",
                        "properties": {
                            "playing": {"type": "boolean", "description": "True only if MUSIC (score or a song) is audibly playing — not mere ambient/sound design."},
                            "cue": {"type": "string", "description": "What you HEAR: instrumentation, vocal or instrumental, tempo/mood, and any lyrics you can make out — enough for someone to identify the track. Empty if not playing."},
                        },
                        "required": ["playing", "cue"],
                    },
                },
                "required": ["scene_description", "mood", "moments", "reads", "quotes", "music"],
            }

            # SEARCH MODE. When Tim typed WHAT he's reacting to, the clip goes to Gemini
            # exactly as before — but now his words go with it, and it points back at the
            # one moment he meant plus the emotions he's likely feeling. The schema only
            # grows in this branch, so the browse path stays byte-identical.
            if reacting_to.strip():
                schema["properties"]["match"] = {
                    "type": "object",
                    "properties": {
                        "found": {"type": "boolean"},
                        "moment_offset_ms": {
                            "type": "integer",
                            "description": "offset_ms of the moment the viewer described.",
                        },
                        "summary": {
                            "type": "string",
                            "description": "One plain line naming that moment, echoing their "
                                           "words — shown back to them as 'found it'.",
                        },
                        "emotions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Emotion KEYS the viewer is most likely feeling "
                                           "about what they described, ranked, most likely "
                                           "first. Keys only.",
                        },
                    },
                    "required": ["found", "moment_offset_ms", "summary", "emotions"],
                }
                schema["required"].append("match")

            system_prompt = REACTION_DRAFT_SYSTEM_PROMPT.format(facets=_FACET_HINTS)

            parts = [f"This clip is the last {clip_seconds} seconds of the film."]
            if movie_title:
                parts.append(f"Movie: {movie_title}.")
            if movie_context.strip():
                parts.append(f"\nWhat this film is:\n{movie_context.strip()[:1200]}")
            if session_history.strip():
                parts.append(
                    "\nEarlier in this session — USE THIS for `callback` and `theme` "
                    "targets, which the clip alone cannot see:\n"
                    f"{session_history.strip()[:1500]}"
                )
            if reacting_to.strip():
                parts.append(
                    "\nTHE VIEWER IS REACTING TO SOMETHING SPECIFIC. In their words:\n"
                    f'  "{reacting_to.strip()[:300]}"\n'
                    "Find the exact moment in the clip they mean. Fill `match`: the moment's "
                    "offset_ms, a one-line summary echoing their words, and the emotion keys "
                    "they are most likely feeling about it, ranked. If their words genuinely "
                    "match nothing in the clip, set found=false — do not force it onto the "
                    "nearest beat."
                )
            parts.append(
                "\nEmotion vocabulary — return KEYS from this list only:\n"
                + emotions.prompt_vocabulary()
            )
            user_prompt = "\n".join(parts)

            config = types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=schema,
                temperature=0.8,
                max_output_tokens=8192,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            )
            # Measured on a real 45s clip: LOW and DEFAULT both surfaced 8 distinct
            # facets, LOW was 1.3s faster and half the price ($0.0037 vs $0.0072).
            # LOW is the default; it's a live setting so it can be flipped mid-movie
            # if the craft targets ever come back thin.
            res = (media_resolution or "low").lower()
            if res == "low":
                config.media_resolution = types.MediaResolution.MEDIA_RESOLUTION_LOW
            elif res == "medium":
                config.media_resolution = types.MediaResolution.MEDIA_RESOLUTION_MEDIUM

            model = self.draft_model
            response = await self._call_with_retry(
                [media_part, user_prompt], config, "reaction_draft", model=model,
            )
            usage = await self._track_call(
                response, call_type="reaction_draft", model=model, grounded=False,
            )

            raw = (response.text or "").strip()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as e:
                raise GeminiError(f"reaction draft returned non-JSON: {raw[:200]}") from e

            mood = (parsed.get("mood") or "").lower()
            if mood not in VALID_MOODS:
                mood = "tense"

            # The schema no longer bounds counts, lengths or enums (see the landmine
            # note above), so everything is validated and clamped HERE. Unknown
            # emotion keys are dropped silently — the prompt supplies the vocabulary,
            # this is the gate.
            clip_ms = int(clip_seconds * 1000)
            moments: list[dict[str, Any]] = []
            for i, m in enumerate((parsed.get("moments") or [])[:8]):
                try:
                    off = max(0, min(clip_ms, int(m.get("offset_ms") or 0)))
                except (TypeError, ValueError):
                    continue
                targets = []
                for t in (m.get("targets") or [])[:6]:
                    facet = (t.get("facet") or "").strip()
                    keys = [
                        k for k in (t.get("emotions") or [])
                        if k in emotions.BY_KEY
                    ][:6]
                    if not (t.get("label") or "").strip() or facet not in REACTION_FACETS:
                        continue
                    targets.append({
                        "key": f"m{i}t{len(targets)}",
                        "label": t["label"].strip()[:90],
                        "facet": facet,
                        "note": (t.get("note") or "").strip()[:180],
                        "emotions": keys,
                    })
                if not targets:
                    continue
                # The catch-all. Never let him be stuck because none of the named
                # targets is the thing he meant.
                targets.append({
                    "key": f"m{i}tall",
                    "label": "just… all of it",
                    "facet": "story",
                    "note": "The whole beat, not one element of it.",
                    "emotions": [],
                })
                moments.append({
                    "key": f"m{i}",
                    "offset_ms": off,
                    "span_start_ms": max(0, min(clip_ms, int(m.get("span_start_ms") or off))),
                    "span_end_ms": max(0, min(clip_ms, int(m.get("span_end_ms") or off))),
                    "caption": (m.get("caption") or "").strip()[:90],
                    "why": (m.get("why") or "").strip()[:180],
                    "targets": targets,
                })
            moments.sort(key=lambda m: m["offset_ms"])

            # THE SEARCH RESULT. Point at the moment Tim described, and put the emotions he's
            # likely feeling FIRST on it — as a synthetic target, so the drawer's emoji step
            # renders it with no new code. Nearest-offset match survives the sort above.
            matched = None
            if reacting_to.strip():
                mt = parsed.get("match") or {}
                if mt.get("found") and moments:
                    want = 0
                    try:
                        want = int(mt.get("moment_offset_ms") or 0)
                    except (TypeError, ValueError):
                        want = moments[0]["offset_ms"]
                    best = min(moments, key=lambda m: abs(m["offset_ms"] - want))
                    keys = [k for k in (mt.get("emotions") or []) if k in emotions.BY_KEY][:8]
                    search_t = {
                        "key": best["key"] + "tsearch",
                        "label": (mt.get("summary") or "what you described").strip()[:90],
                        "facet": "story",
                        "note": reacting_to.strip()[:180],
                        "emotions": keys,
                    }
                    best["targets"].insert(0, search_t)
                    matched = {
                        "moment_key": best["key"],
                        "target_key": search_t["key"],
                        "summary": search_t["label"],
                    }

            reads = [
                {"label": r["label"].strip()[:110], "hedge": (r.get("hedge") or "").strip()[:40]}
                for r in (parsed.get("reads") or [])[:4]
                if (r.get("label") or "").strip()
            ]

            # QUOTES → the Quote tab. Each heard line becomes a synthetic `writing`
            # target injected into its nearest beat, so reacting to a quote rides the
            # exact same rails as any target (the tsearch trick, once per line).
            quotes: list[dict[str, Any]] = []
            for q in (parsed.get("quotes") or [])[:6]:
                text = (q.get("text") or "").strip()[:240]
                if not text or not moments:
                    continue
                speaker = (q.get("speaker") or "").strip()[:60]
                try:
                    want = int(q.get("moment_offset_ms") or 0)
                except (TypeError, ValueError):
                    want = moments[0]["offset_ms"]
                beat = min(moments, key=lambda m: abs(m["offset_ms"] - want))
                keys = [k for k in (q.get("emotions") or []) if k in emotions.BY_KEY][:6]
                tkey = f"{beat['key']}quote{len(quotes)}"
                who = speaker or "someone off screen"
                beat["targets"].append({
                    "key": tkey,
                    "label": f'"{text}"'[:90],
                    "facet": "writing",
                    "note": f'Spoken by {who}: "{text}"'[:180],
                    "emotions": keys,
                })
                quotes.append({
                    "text": text,
                    "speaker": speaker,
                    "moment_key": beat["key"],
                    "target_key": tkey,
                })

            m = parsed.get("music") or {}
            music = {
                "playing": bool(m.get("playing")),
                "cue": (m.get("cue") or "").strip()[:400],
            }

            return {
                "scene_description": (parsed.get("scene_description") or "").strip()[:1600],
                "mood": mood,
                "moments": moments,
                "reads": reads,
                "quotes": quotes,
                "music": music,
                # None on the browse path or when his words matched nothing.
                "matched": matched,
                "latency_ms": int((perf_counter() - start) * 1000),
                "usage": usage,
            }
        finally:
            if uploaded is not None:
                await self._delete_file(uploaded)

    # ─── Song tab: name the track that reaction_draft heard ─────
    async def identify_song(
        self,
        *,
        movie_title: str = "",
        movie_context: str = "",
        cue: str = "",
        timestamp_label: str = "",
    ) -> dict[str, Any]:
        """Grounded, TEXT-only track lookup for the Song tab.

        `reaction_draft` already HEARD the clip and wrote `cue` (a plain description
        of the music). This turns that description + the film into a named track via
        Google Search — the two-stage split exists because grounded calls in this
        codebase cannot also carry a response_schema (see generate_scene_topics),
        and because pairing audio with grounding is untried here. So we ground on
        words, not on the clip.

        Returns {found, title, artist, album, year, note, source, emotions[]}.
        """
        self._ensure_client()
        start = perf_counter()

        title_str = movie_title or "the film"
        if timestamp_label:
            title_str += f" (around {timestamp_label})"
        ctx = f"\nWhat the film is:\n{movie_context.strip()[:800]}" if movie_context.strip() else ""

        prompt = f"""A viewer is watching {title_str} and wants to know what song or score cue is playing right now.{ctx}

Here is what can be heard, described by someone listening to the scene:
"{cue.strip()[:400]}"

Use Google Search to identify the actual track. Prefer a real, verifiable soundtrack/needle-drop for THIS film and this moment over a guess. If the description is too vague to name a specific track with reasonable confidence, set found=false — do NOT fabricate a title.

Output STRICT JSON only, no markdown fences, exactly this shape:
{{
  "found": <true only if you can name a real track with reasonable confidence>,
  "title": "<track title>",
  "artist": "<performing artist or, for score, the composer>",
  "album": "<album or soundtrack name, or empty>",
  "year": "<release year, or empty>",
  "note": "<one line: where/why it's used in this film, or what it is>",
  "source": "<the site your identification rests on, e.g. 'Tunefind', 'IMDb soundtrack'>",
  "emotions": ["<emotion keys a viewer might feel about this track, ranked; keys only, from the list below>"]
}}

Emotion keys — return ONLY keys from this list:
{emotions.prompt_vocabulary()}

Return ONLY the JSON object, nothing else.
"""
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.3,
        )
        response = await self._call_with_retry(
            [prompt], config, "identify_song", model="gemini-2.5-flash",
        )
        usage = await self._track_call(
            response, call_type="identify_song", model="gemini-2.5-flash", grounded=True,
        )

        raw = (response.text or "").strip()
        card: dict[str, Any] = {
            "found": False, "title": "", "artist": "", "album": "",
            "year": "", "note": "", "source": "", "emotions": [],
        }
        try:
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict) and parsed.get("found") and (parsed.get("title") or "").strip():
                keys = [k for k in (parsed.get("emotions") or []) if k in emotions.BY_KEY][:6]
                card = {
                    "found": True,
                    "title": str(parsed.get("title") or "").strip()[:120],
                    "artist": str(parsed.get("artist") or "").strip()[:120],
                    "album": str(parsed.get("album") or "").strip()[:120],
                    "year": str(parsed.get("year") or "").strip()[:12],
                    "note": str(parsed.get("note") or "").strip()[:200],
                    "source": str(parsed.get("source") or "").strip()[:80],
                    "emotions": keys,
                }
        except (json.JSONDecodeError, TypeError, ValueError):
            log.warning("identify_song: failed to parse JSON — returning found=false")

        card["latency_ms"] = int((perf_counter() - start) * 1000)
        card["usage"] = usage
        return card

    # ─── Reaction drawer: the finished sentences ────────────────
    async def reaction_sentences(
        self,
        *,
        scene_description: str,
        moment_caption: str,
        moment_why: str,
        target_label: str,
        target_note: str,
        target_facet: str,
        emotion_key: str,
        movie_title: str = "",
        subject: str = "Tim",
        n: int = 5,
        intensity: int = 2,
        shade: str = "",
        steer: str = "",
    ) -> dict[str, Any]:
        """The one mid-wizard call. Text only — the clip was already read by
        `reaction_draft`, so this is cheap (~$0.0003) and fast.

        `first_person` is what Tim reads, picks, and what actually goes to the
        room — first person is the convention for his own actions in an outgoing
        Kindroid payload ("I" is the sender, and the sender is Tim).

        `third_person` is VESTIGIAL. It exists because of a misdiagnosis: the relay
        briefly rewrote reactions into the third person to "fix" a bug that was not
        a bug, and mangled a working ingestion emote doing it. Nothing consumes it.
        Left in place rather than churn the schema mid-session; drop it next time
        this file is touched. See the note in kindroid_relay.py.

        Returns {options: [{first_person, third_person}], latency_ms, usage}.
        """
        self._ensure_client()
        start = perf_counter()

        emo = emotions.get(emotion_key)
        emo_label = emo.label if emo else emotion_key

        # No minItems/maxItems/maxLength — same complexity budget that got
        # reaction_draft rejected with a 400. Counts live in the prompt; the
        # trimming is done in Python below.
        schema = {
            "type": "object",
            "properties": {
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "first_person": {"type": "string"},
                            "third_person": {"type": "string"},
                        },
                        "required": ["first_person", "third_person"],
                    },
                },
            },
            "required": ["options"],
        }

        lines = []
        if movie_title:
            lines.append(f"Movie: {movie_title}")
        lines.append(f"What's on screen: {scene_description[:700]}")
        lines.append(f"\nThe beat he picked: {moment_caption}")
        if moment_why:
            lines.append(f"  (why it lands: {moment_why})")
        lines.append(f"\nThe THING he's reacting to: {target_label}  [{target_facet}]")
        if target_note:
            lines.append(f"  ({target_note})")
        lines.append(f"\nHow it hit him: {emo_label}")

        # INTENSITY. At `normal` (level 2) with no shade/steer, the two blocks below add
        # nothing and the prompt is byte-identical to before — the common path can't regress.
        # At meh/WOW the "vary the manner" instruction is REPLACED by a fixed strength, so
        # all N options come back at the level he dialed.
        intn = emotions.intensity(intensity)
        if intn["level"] != 2:
            lines.append(
                f"\nHow HARD it hit him: {intn['label']} — {intn['hint']}. "
                f"Write ALL {n} at THIS strength; do not vary the intensity."
            )
        else:
            lines.append(
                f"\nWrite {n} ways {subject} might say this. Vary the manner — big and "
                f"physical, small and private, out loud, almost nothing."
            )
        if shade.strip():
            lines.append(f"\nA shade he wants kept, under it all: {shade.strip()[:160]}.")
        if steer.strip():
            lines.append(
                f"\nThe first set missed. Steer the rewrite: {steer.strip()[:200]}."
            )

        config = types.GenerateContentConfig(
            system_instruction=REACTION_SENTENCES_SYSTEM_PROMPT.format(n=n, subject=subject),
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.95,  # this is the variety knob — the whole value is in the spread
            max_output_tokens=2048,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        response = await self._call_with_retry(
            ["\n".join(lines)], config, "reaction_sentences", model=self.text_model,
        )
        usage = await self._track_call(
            response, call_type="reaction_sentences", model=self.text_model, grounded=False,
        )

        raw = (response.text or "").strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise GeminiError(f"reaction sentences returned non-JSON: {raw[:200]}") from e

        # The third_person form is the ONLY thing that reaches the room, as
        # narration. It must actually be about TIM. Observed in testing:
        #
        #   1P: "He looked so unimpressed signing that book, like it was a chore."
        #   3P: "John Cusack looked so unimpressed signing that book…"
        #
        # — a perfectly good sentence that says nothing whatsoever about Tim's
        # reaction. Sent to the room it reads as a stray remark about an actor. The
        # first-person guard doesn't catch this (there's no "I" in it), so check
        # here: if the subject isn't named, the line isn't a reaction, and we
        # re-attribute it rather than drop the option.
        options = []
        for o in (parsed.get("options") or [])[: n + 2]:
            fp = (o.get("first_person") or "").strip()[:240]
            tp = (o.get("third_person") or "").strip()[:260]
            if not (fp and tp):
                continue
            if subject.lower() not in tp.lower():
                tp = f"{subject} watches — {tp[0].lower()}{tp[1:]}"[:260]
            options.append({"first_person": fp, "third_person": tp})

        return {
            "options": options,
            "latency_ms": int((perf_counter() - start) * 1000),
            "usage": usage,
        }

    # ─── Reaction drawer: the mid-wizard steers ─────────────────
    #
    # Text-only, cheap (~$0.0003, ~1-2s). The clip was already read once by
    # reaction_draft; these re-derive ONE stage against the scene it already knows,
    # steered by what Tim typed in that stage's refine box. He taps when the
    # suggestion is right; he types when it isn't.

    async def reaction_retarget(
        self,
        *,
        scene_description: str,
        moment_caption: str,
        moment_why: str,
        steer: str,
        movie_title: str = "",
    ) -> list[dict[str, Any]]:
        """Re-derive the THINGS worth reacting to in one beat, steered by his words.

        "No — Jonah Hill's timing" → targets focused on the performance, with the
        emotions ranked for each. Same shape as a draft moment's targets.
        """
        self._ensure_client()
        target_schema = {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "facet": {"type": "string", "enum": list(REACTION_FACETS)},
                "note": {"type": "string"},
                "emotions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["label", "facet", "note", "emotions"],
        }
        schema = {
            "type": "object",
            "properties": {"targets": {"type": "array", "items": target_schema}},
            "required": ["targets"],
        }

        lines = []
        if movie_title:
            lines.append(f"Movie: {movie_title}")
        lines.append(f"What's on screen: {scene_description[:700]}")
        lines.append(f"\nThe beat: {moment_caption}")
        if moment_why:
            lines.append(f"  ({moment_why})")
        lines.append(
            f"\nThe viewer said what he's reacting to IS: \"{steer.strip()[:200]}\"\n"
            "Give 3-6 concrete THINGS on screen in this beat that match what he means, "
            "each a thing that ACTUALLY HAPPENS in plain words (not a topic), spanning "
            "the facets his words imply. ALWAYS include the characters he might mean, "
            "named, and the story beat. For each, rank 4-6 emotion KEYS he might feel "
            "about THAT thing, most likely first.\n\n"
            "Emotion vocabulary — return KEYS from this list only:\n"
            + emotions.prompt_vocabulary()
        )

        config = types.GenerateContentConfig(
            system_instruction="You re-index one beat of a film to the thing the viewer "
                               "says he's reacting to. Concrete things on screen, plain "
                               "spoken English, never film-school register.",
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.7,
            max_output_tokens=2048,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        response = await self._call_with_retry(
            ["\n".join(lines)], config, "reaction_retarget", model=self.text_model,
        )
        await self._track_call(
            response, call_type="reaction_retarget", model=self.text_model, grounded=False,
        )
        try:
            parsed = json.loads((response.text or "").strip())
        except json.JSONDecodeError as e:
            raise GeminiError(f"retarget returned non-JSON: {(response.text or '')[:200]}") from e

        out = []
        for t in (parsed.get("targets") or [])[:6]:
            facet = (t.get("facet") or "").strip()
            label = (t.get("label") or "").strip()
            if not label or facet not in REACTION_FACETS:
                continue
            keys = [k for k in (t.get("emotions") or []) if k in emotions.BY_KEY][:6]
            out.append({
                "label": label[:90], "facet": facet,
                "note": (t.get("note") or "").strip()[:180], "emotions": keys,
            })
        return out

    async def reaction_reemote(
        self,
        *,
        scene_description: str,
        moment_caption: str,
        target_label: str,
        target_facet: str,
        steer: str,
    ) -> list[str]:
        """Re-rank the emotions for one target, steered by his words. Keys only."""
        self._ensure_client()
        schema = {
            "type": "object",
            "properties": {"emotions": {"type": "array", "items": {"type": "string"}}},
            "required": ["emotions"],
        }
        prompt = (
            f"What's on screen: {scene_description[:600]}\n"
            f"The beat: {moment_caption}\n"
            f"He's reacting to: {target_label}  [{target_facet}]\n"
            f"He says the feeling is more like: \"{steer.strip()[:200]}\"\n\n"
            "Rank 5-8 emotion KEYS he might plausibly feel, closest to what he said FIRST. "
            "Keys only, from this list:\n" + emotions.prompt_vocabulary()
        )
        config = types.GenerateContentConfig(
            system_instruction="You rank how a viewer feels about one thing on screen, "
                               "steered by their own words. Return emotion keys only.",
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.6,
            max_output_tokens=1024,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        response = await self._call_with_retry(
            [prompt], config, "reaction_reemote", model=self.text_model,
        )
        await self._track_call(
            response, call_type="reaction_reemote", model=self.text_model, grounded=False,
        )
        try:
            parsed = json.loads((response.text or "").strip())
        except json.JSONDecodeError as e:
            raise GeminiError(f"reemote returned non-JSON: {(response.text or '')[:200]}") from e
        return [k for k in (parsed.get("emotions") or []) if k in emotions.BY_KEY][:8]

    async def to_third_person(self, text: str, *, subject: str = "Tim") -> str:
        """DEAD. Written for the third-person rewrite that turned out to be wrong —
        Tim's own words go to the room in first person, exactly as he typed them.
        Kept only so a stray import doesn't explode; delete on the next pass.
        """
        self._ensure_client()
        config = types.GenerateContentConfig(
            system_instruction=(
                f"Rewrite the viewer's own words about his reaction into third-person "
                f"narration naming {subject}. Keep every detail and the tone. Present "
                f"tense. Never use 'I', 'me', or 'my' outside quoted speech. Return "
                f"ONLY the rewritten line."
            ),
            temperature=0.3,
            max_output_tokens=300,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        response = await self._call_with_retry(
            [text.strip()], config, "to_third_person", model=self.text_model,
        )
        await self._track_call(
            response, call_type="reaction_sentences", model=self.text_model, grounded=False,
        )
        return (response.text or "").strip().strip('"')

    # ─── Quick-reaction one-liner ───────────────────────────────
    async def reaction_oneliner(
        self,
        *,
        emoji: str,
        label: str,
        scene_description: str = "",
        movie_title: str = "",
    ) -> dict[str, Any]:
        client = self._ensure_client()
        start = perf_counter()
        prompt = (
            f"Emoji: {emoji} ({label}).\n"
            f"Movie: {movie_title}.\n"
            f"Scene: {scene_description}\n\n"
            f"Describe Tim's reaction in one sentence."
        )
        config = types.GenerateContentConfig(
            system_instruction=REACTION_SYSTEM_PROMPT,
            temperature=0.85,
            max_output_tokens=500,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        response = await self._call_with_retry([prompt], config, "reaction_oneliner")
        usage = await self._track_call(
            response, call_type="reaction", model=self.text_model, grounded=False,
        )
        return {
            "text": (response.text or "").strip().strip('"'),
            "latency_ms": int((perf_counter() - start) * 1000),
            "usage": usage,
        }

    # ─── Live photo (real-life, not movie) ─────────────────────
    async def analyze_live_photo(
        self,
        photo_paths: list[tuple[Path, str]],
        *,
        caption: str = "",
        movie_context: str = "",
    ) -> dict[str, Any]:
        """Tim shares one or more photos with Eli alongside an optional caption.

        `photo_paths` is a list of (path, mime_type) tuples — supports
        multiple images in a single call. Returns {narration, latency_ms,
        usage} where narration contains TWO paragraphs (one for the photo[s],
        one for the movie still playing in background) separated by a blank
        line. `movie_context` is ambient background — if empty, the
        narration drops the background paragraph.
        """
        self._ensure_client()
        start = perf_counter()
        if not photo_paths:
            raise GeminiError("analyze_live_photo called with no photos")

        media_parts: list[Any] = []
        for path, mime in photo_paths:
            photo_bytes = await asyncio.to_thread(path.read_bytes)
            media_parts.append(types.Part.from_bytes(data=photo_bytes, mime_type=mime))

        prompt_lines = [
            f"Tim is sharing {len(photo_paths)} photo(s) with Eli. Describe "
            "what's in the photo(s) AND what's still happening on the movie "
            "screen, per the format rules — two paragraphs separated by a "
            "blank line."
        ]
        if caption.strip():
            prompt_lines.append(f"\nTim's own caption to weave in: {caption.strip()}")
        if movie_context.strip():
            prompt_lines.append(
                f"\nMovie context (ambient background — paragraph 2 uses this):\n"
                f"{movie_context.strip()}"
            )
        else:
            prompt_lines.append(
                "\nNo movie context — skip paragraph 2 entirely, output only "
                "paragraph 1."
            )
        prompt = "\n".join(prompt_lines)

        config = types.GenerateContentConfig(
            system_instruction=LIVE_PHOTO_SYSTEM_PROMPT,
            temperature=0.8,
            # Pro's thinking tokens count against this budget — must be big
            # enough for thinking + the short narration. Pro REQUIRES thinking
            # mode (setting thinking_budget=0 returns 400 INVALID_ARGUMENT),
            # so we just give it plenty of headroom.
            max_output_tokens=8192,
        )
        response = await self._call_with_retry(
            [*media_parts, prompt], config, "analyze_live_photo",
            model=self.scene_model,
        )
        usage = await self._track_call(
            response, call_type="live_photo", model=self.scene_model, grounded=False,
        )
        return {
            "narration": (response.text or "").strip().strip('"'),
            "latency_ms": int((perf_counter() - start) * 1000),
            "usage": usage,
        }

    async def describe_location(
        self,
        *,
        photo_path: Optional[Path] = None,
        mime_type: str = "image/jpeg",
        note: str = "",
        venue_label: str = "",
    ) -> dict[str, Any]:
        """Turn a photo and/or a typed note into a saved venue description.

        Uses the vision model when a photo is supplied, the cheap text model
        for note-only. Companion preamble is suppressed — this describes a
        room, not the kin.
        """
        self._ensure_client()
        start = perf_counter()
        if photo_path is None and not note.strip():
            raise GeminiError("describe_location needs a photo or a note")

        parts: list[Any] = []
        if photo_path is not None:
            photo_bytes = await asyncio.to_thread(Path(photo_path).read_bytes)
            parts.append(types.Part.from_bytes(data=photo_bytes, mime_type=mime_type))

        instr: list[str] = []
        if venue_label:
            instr.append(f"This is Tim's {venue_label.lower()}.")
        if photo_path is not None:
            instr.append("Describe the room shown in the photo.")
        if note.strip():
            instr.append(f"Tim's own notes to base it on: {note.strip()}")
        prompt = " ".join(instr)

        model = self.scene_model if photo_path is not None else self.text_model
        config = types.GenerateContentConfig(
            system_instruction=LOCATION_SYSTEM_PROMPT,
            temperature=0.7,
            max_output_tokens=4096,
        )
        response = await self._call_with_retry(
            [*parts, prompt], config, "describe_location",
            model=model, inject_companion=False,
        )
        usage = await self._track_call(
            response, call_type="location", model=model, grounded=False,
        )
        return {
            "description": (response.text or "").strip().strip('"'),
            "latency_ms": int((perf_counter() - start) * 1000),
            "usage": usage,
        }

    # ─── Live audio (voice note from Tim) ──────────────────────
    async def analyze_live_audio(
        self,
        audio_path: Path,
        *,
        mime_type: str = "audio/webm",
        movie_context: str = "",
    ) -> dict[str, Any]:
        """Tim records audio while watching. Returns {tim_speech,
        ambient_emote, latency_ms, usage}. Uses scene_model (Pro) — best
        transcription + audio understanding.

        - tim_speech: Tim's own words as dialog (empty if he doesn't speak)
        - ambient_emote: Sensory description of everything else (other
          people, sounds, music). Empty if only Tim speaks.

        `movie_context` is ambient background used for disambiguating
        references like "this scene" / "that actor."
        """
        self._ensure_client()
        start = perf_counter()
        audio_bytes = await asyncio.to_thread(audio_path.read_bytes)
        media_part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)

        prompt = (
            "Analyze this audio clip. Split it into tim_speech (Tim's own "
            "voice as dialog transcript) and ambient_emote (everything else "
            "narrated as a Tim-voice sensory emote). Return JSON only."
        )
        if movie_context.strip():
            prompt += (
                f"\n\nMovie context (ambient background — the movie keeps "
                f"playing while Tim talks):\n{movie_context.strip()}"
            )
        config = types.GenerateContentConfig(
            system_instruction=LIVE_AUDIO_SYSTEM_PROMPT,
            temperature=0.4,
            response_mime_type="application/json",
            response_schema={
                "type": "object",
                "properties": {
                    "tim_speech":    {"type": "string"},
                    "ambient_emote": {"type": "string"},
                },
                "required": ["tim_speech", "ambient_emote"],
            },
            # Pro requires thinking mode — leave the default thinking budget
            # and give max_output_tokens enough headroom for thinking + JSON.
            max_output_tokens=8192,
        )
        response = await self._call_with_retry(
            [media_part, prompt], config, "analyze_live_audio",
            model=self.scene_model,
        )
        usage = await self._track_call(
            response, call_type="live_audio", model=self.scene_model, grounded=False,
        )
        raw = (response.text or "").strip()
        tim_speech = ""
        ambient_emote = ""
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                tim_speech = str(parsed.get("tim_speech") or "").strip()
                ambient_emote = str(parsed.get("ambient_emote") or "").strip()
        except (json.JSONDecodeError, TypeError, ValueError):
            # Fallback: if Gemini ignored the schema and returned raw text,
            # treat the whole response as Tim's speech.
            tim_speech = raw.strip('"').strip()
        return {
            "tim_speech": tim_speech,
            "ambient_emote": ambient_emote,
            "latency_ms": int((perf_counter() - start) * 1000),
            "usage": usage,
        }

    # ─── Session sign-off (Tim addressing Eli directly) ────────
    async def generate_signoff(
        self, *, session_context: str, stats_hint: str = ""
    ) -> dict[str, Any]:
        """The end-of-film marker. Return {signoff, latency_ms, usage}.

        Not a farewell — see SIGNOFF_SYSTEM_PROMPT. Runs on Flash rather than
        Lite: this fires once per session (cost is irrelevant) and Lite kept
        ignoring both the length target and the end-on-the-film instruction,
        drifting back into a relationship goodbye.
        """
        self._ensure_client()
        start = perf_counter()
        model = "gemini-2.5-flash"
        prompt = (
            (stats_hint + "\n\n" if stats_hint else "")
            + "Session transcript:\n"
            + (session_context or "(no transcript recorded)")
            + "\n\nThe credits just rolled. Write Tim's end-of-movie marker to "
            "whoever watched it with him, per the system prompt. This is not a "
            "goodbye — nobody is leaving. Take stock of the film they just sat "
            "through together, and end on the film."
        )
        config = types.GenerateContentConfig(
            system_instruction=SIGNOFF_SYSTEM_PROMPT,
            temperature=0.75,
            # Flash bills thinking against max_output_tokens, so with thinking on
            # the reasoning ate the budget and the wrap-up came back truncated
            # mid-sentence. This is a recall-and-recap task, not a reasoning one
            # — turn thinking off and the whole budget goes to the prose.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            max_output_tokens=2048,
        )
        response = await self._call_with_retry(
            [prompt], config, "generate_signoff", model=model
        )
        usage = await self._track_call(
            response, call_type="signoff", model=model, grounded=False,
        )
        return {
            "signoff": (response.text or "").strip(),
            "latency_ms": int((perf_counter() - start) * 1000),
            "usage": usage,
        }

    async def generate_marathon_summary(
        self, *, session_context: str, stats_hint: str = ""
    ) -> dict[str, Any]:
        """Return {summary, latency_ms, usage}."""
        self._ensure_client()
        start = perf_counter()
        prompt = (
            (stats_hint + "\n\n" if stats_hint else "")
            + "Session transcript:\n"
            + (session_context or "(no transcript recorded)")
            + "\n\nWrite the engagement analysis per the system prompt."
        )
        config = types.GenerateContentConfig(
            system_instruction=MARATHON_SUMMARY_SYSTEM_PROMPT,
            temperature=0.55,
            max_output_tokens=1536,
        )
        response = await self._call_with_retry(
            [prompt], config, "generate_marathon_summary", model=self.text_model
        )
        usage = await self._track_call(
            response, call_type="summary", model=self.text_model, grounded=False,
        )
        return {
            "summary": (response.text or "").strip(),
            "latency_ms": int((perf_counter() - start) * 1000),
            "usage": usage,
        }

    # ─── WDIM catch-up summary ──────────────────────────────────
    async def generate_catchup(
        self,
        clip_path: Path,
        *,
        movie_title: Optional[str] = None,
        gap_start_label: str = "",
        gap_end_label: str = "",
        gap_duration_label: str = "",
    ) -> dict[str, Any]:
        """Return {summary, latency_ms, usage} describing what Tim missed while
        away. Uses the scene model (Pro) because this is video analysis.
        """
        self._ensure_client()
        start = perf_counter()

        clip_bytes = await asyncio.to_thread(clip_path.read_bytes)
        use_inline = len(clip_bytes) <= self.INLINE_LIMIT_BYTES

        uploaded: Any = None
        try:
            if use_inline:
                media_part = types.Part.from_bytes(data=clip_bytes, mime_type="video/mp4")
            else:
                uploaded = await self._upload_clip(clip_path)
                media_part = uploaded

            hint_parts = []
            if movie_title:
                hint_parts.append(f"Movie: {movie_title}")
            if gap_start_label and gap_end_label:
                hint_parts.append(f"Missed span: {gap_start_label} → {gap_end_label}")
            if gap_duration_label:
                hint_parts.append(f"Duration away: {gap_duration_label}")
            hint_parts.append(
                "Write a 2-4 sentence catch-up of what happened in this span."
            )
            user_prompt = "\n".join(hint_parts)

            config = types.GenerateContentConfig(
                system_instruction=CATCHUP_SYSTEM_PROMPT,
                temperature=0.5,
                max_output_tokens=2048,
            )
            response = await self._call_with_retry(
                [media_part, user_prompt], config, "generate_catchup",
                model=self.scene_model,
            )
            usage = await self._track_call(
                response, call_type="catchup", model=self.scene_model, grounded=False,
            )
            return {
                "summary": (response.text or "").strip(),
                "latency_ms": int((perf_counter() - start) * 1000),
                "usage": usage,
            }
        finally:
            if uploaded is not None:
                await self._delete_file(uploaded)

    # ─── Condense text to fit Kindroid limit ────────────────────
    async def condense(self, text: str, *, max_chars: int) -> str:
        client = self._ensure_client()
        prompt = (
            f"Target: under {max_chars} characters.\n\n"
            f"Text to condense:\n{text}"
        )
        config = types.GenerateContentConfig(
            system_instruction=CONDENSE_SYSTEM_PROMPT,
            temperature=0.4,
        )
        response = await self._call_with_retry([prompt], config, "condense")
        await self._track_call(
            response, call_type="condense", model=self.text_model, grounded=False,
        )
        condensed = (response.text or "").strip()
        return condensed[:max_chars] if len(condensed) > max_chars else condensed


gemini_brain = GeminiBrain()
