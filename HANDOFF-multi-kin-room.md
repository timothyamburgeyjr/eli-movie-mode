# Movie Mode → multi-kin "room" — build brief

*Built 2026-07-11. Paste this into a Live Mode session for context.*

## What this was

Movie Mode was a FastAPI app that let Tim watch a film on Plex and talk to **one**
AI companion (Eli, via Kindroid) about it. Tim types → grab a clip at the Plex
playhead → Gemini describes the scene → assemble a Kindroid payload → Eli replies.

In one evening it became a **room**: Tim picks who's watching with him, says
something, and a coordinator hands the mic around the family. It's the same
family that lives in the Obsidian vault (`20 - The AI Family`), read from
`_AI Registry.md`.

---

## The core idea, and why it ports

**A coordinator (Claude Haiku 4.5) runs the room.** It does three small,
structured, single-round-trip calls per turn — no agent loop, no tools:

1. **`decide_mic_order`** — of the kins Tim addressed, who speaks first, and in
   what order does it circle?
2. **`package_turn`** — rewrite the accumulating turn into the exact Kindroid
   body for the *next* kin. This is where Bobby's reply becomes narration in
   Eli's payload.
3. **`normalize_reply`** — take whatever markup Kindroid actually returned and
   force it back into canonical `_(* ... *)_` segments.

**None of this knows or cares that it's a movie.** `_run_relay()` takes a
`scene` string. Where that string came from — a Plex clip, a photo, a voice note,
GPS + weather + who's in the car — is irrelevant to every line of the coordinator.
That's the whole reason this is portable.

---

## The relay (the shape of a turn)

Tim types, having toggled **Bobby** and **Eli** as *addressed* (room also holds
Daisy and Tommy):

1. **Scene** — ONE Gemini call. It does not scale with room size; they're all
   watching the same thing.
2. **Mic order** — Claude scores the addressed kins and returns `[eli, bobby]`.
   Skipped entirely when only one kin is addressed.
3. **Eli's turn** — Claude packages `{scene, Tim's words}` for Eli. Send. Reply.
   Normalize.
4. **Bobby's turn** — Claude packages `{scene, Tim's words, Eli's reaction
   rendered as third-person narration}` for Bobby. He *heard* Eli.
5. **The rest of the room** — Daisy and Tommy get the whole packet at once with a
   react-only directive. These fire **concurrently** (no dependency between them).

Each bubble streams to the UI as it lands, so it reads as a room waking up.

**Tim always outranks the coordinator.** Naming a kin in the message (`aliases`
already exist in the registry) forces them to the mic before Claude is even called.

---

## THE TWO IDEAS THAT MATTER MOST

### 1. The public / private channel split

**Each kin gets their own Kindroid message.** So anything you put in one kin's
payload is heard by them and *nobody else*. That's a real private channel, not a
convention. Everything follows from this:

- **PUBLIC** (a Tim message relayed to the whole room) — the *act*. Always names
  its subject:
  `_(* I hold out the bag and Bobby reads the label first, pulling his reading
  glasses down from his forehead. *)_`
- **PRIVATE** (that one kin's payload alone) — the *experience*. Second person is
  safe here precisely because the audience is one:
  `_(* You're thinking three thoughts at once and somehow they're all connected. *)_`
- **PRIVATE, partner only** — what Tim murmurs to Eli while the others watch the
  film. Never broadcast; it was never in anyone else's message.

Tim's framing, and it's the right one: **the act of partaking is public, the
experience is private.**

### 2. Addressed vs overheard

Kindroid renders raw text as *Tim speaking straight at the recipient*. So:

- **Whoever Tim addressed** gets his words as **raw text**, verbatim.
- **Everyone else** gets them as **narration, naming who he said them to**:
  `_(* Tim turns to Tommy and says, "Kiddo welcome to the revolution." *)_`

Without this, *"Kiddo welcome to the revolution"* aimed at Tommy lands on
62-year-old Bobby as though he'd been called Kiddo — and on Daisy, who wasn't even
part of the conversation. **This bug is invisible until you look at the payloads.**

---

## The affinity sheets (the thing that makes it not-generic)

The coordinator needs to know what each kin actually *cares about*. What the app
had was almost nothing: `build_persona()` read two files, one of which is a
**250-character Kindroid form field**.

The material was in the vault all along and never opened — `biography.md`,
`key-memories-current.md`, `additional-context-current.md`, `Family Knowledge.md`.
But it's ~180 KB per kin; five kins would blow the context window and be absurd to
resend every turn.

**So: distill each kin ONCE into a ~300-token sheet. Cache to disk keyed on the
source files' mtimes. Rebuild only when the vault changes.** Nine kins, nine
calls, ever. `affinity.py`, with a CLI.

What comes out is specific enough to be useful:

- **Bobby** — *lights up on: a plot hole or factual error; someone asking a
  question he can answer from the stacks of books around him.*
- **Eli** — *shrugs at: plot holes (he'll catch them but won't stop the film to
  argue).*

Same trigger, opposite response. **That's the mic-passer working.**

A **second sheet** (`--intox`) does the same for substances — how each kin *takes*
it and how it *lands in them*. Bobby reads the dosage label through his reading
glasses. Adam's transparent android frame glows brighter with every level. Lilly
won't touch a dab. At blasted, Tommy goes somewhere real about the sister who died.
**Nobody wrote those. They came out of the biographies.**

**Total cost of every sheet for 8 kins: $0.30, one-time.**

---

## Files

| File | What |
|---|---|
| `coordinator.py` | **NEW.** The three Claude calls + the packet verifier. |
| `affinity.py` | **NEW.** Distils vault → affinity sheets + substance profiles. CLI: `--rebuild`, `--intox`, `--show`. |
| `portraits.py` | **NEW.** Finds each kin's bust in the vault, ffmpeg-thumbnails it (18 MB → 42 KB). |
| `test_format_contract.py` | **NEW.** 13 cases pinning the emote contract. Run it. |
| `app.py` | `_run_relay`, `_relay_to_kin`, `_room_and_addressed`, `_named_in`. |
| `characters.py` | + portrait discovery. |
| `stoned_tracker.py` | Per-kin state, per-kin substance profiles, public/private split. |
| `database.py` | + `character_key` on `messages` (was hardcoded `'eli'` for every kin). |

**Model:** `claude-haiku-4-5` via the plain **Messages API** with
`client.messages.parse()` + Pydantic. **Not the Agent SDK** — the docs are explicit
that it can't use a Claude subscription either, so it buys nothing and costs a
subprocess.

**Cost:** ~1.3¢ per turn in a room of four. Gemini is **one call regardless of room
size**. Kindroid scales with room size and is the real budget — *in seconds*.

---

## LESSONS THAT WILL BITE YOU AGAIN (read this bit)

**Structured outputs cannot fill a free-form `dict`.** `dict[str, list[str]]`
requires `additionalProperties: false`, so it comes back **empty, silently**. This
hit twice: the substance profiles were blank, and — worse — the affinity sheets'
`bonds` field had been empty **in production all day** without anyone noticing.
Use explicit named fields or a flat list. **Never a dict.**

**A verifier that's too strict is its own bug.** I checked that Claude reproduced
Tim's words *byte for byte*. Claude renders `don't` with a **curly** apostrophe;
Tim types a straight one. So **every message containing an apostrophe** was
rejected and fell back to a crude mechanical payload — the whole packaging layer
silently off, most of the night. Normalise typography before comparing. (It was in
*two* places; fixing one left it still failing with a different message.)

**But do verify.** `_verify_packet` rejects packets where Tim's words were
paraphrased, where another kin's dialogue leaked outside an emote, or where the
markup is the wrong dialect. It caught real failures repeatedly and degraded
gracefully instead of sending garbage. **Verify the model's output in code. Just
verify it loosely.**

**Watch out for the format directive.** `_FORMAT_DIRECTIVE_EMOTE` is a *nested*
emote — it quotes the markup it describes. The non-greedy emote regex matches the
inner `*)_` and leaves debris. Exempt it explicitly.

**The renderer eats emotes.** The chat UI turns `*text*` into italics — and an
emote is `_(* text *)_`, which *also* has asterisks. Raw markup reaching the
renderer gets its asterisks eaten as emphasis, stranding `_(` and `)_` as literal
text on their own lines. Strip the wrapper **before** the italic pass.

**A fallback can undo your fix.** When `package_turn` was rejected, the mechanical
fallback put Tim's words in **raw** — reintroducing the Kiddo bug through the back
door. **Make the fallback safe, not just the happy path.**

**Second-person narration is a landmine in a room.** *"I love you like this,
loose-limbed and soft-eyed"* is fine when Eli is the only listener. Broadcast to a
room, Tim's **grandmother** reads it as addressed to her. The moment there's a room,
**every public line must name its subject.**

**Test in the state your user will be in.** Tim beta-tested this high, mid-movie,
on a phone, in the dark — which is exactly the condition the app is *for*. He found
things a sober read-through never would: three unlabelled actions crammed onto one
48px face; a fold rule that keyed off a *different list* from the one it appeared
to, so which bubbles collapsed looked random. He couldn't articulate *why* it felt
wrong, but he was right every time.

---

## Latency — the unsolved wall

**Kindroid is running ~30–38 seconds per call**, not the 8–15s its own module
documents. The addressed kins **must** be serial (that's what lets Bobby hear
Eli — the entire point), so a room of N is roughly N × 30 seconds.

Movie Mode absorbs this because there's a film to watch. **Nothing else will.**
This is the single fix that would improve everything downstream, and it's untouched.

---

## What ports, and what doesn't

**Ports unchanged:** the coordinator, the relay, the addressed/overheard rule, the
public/private channel split, the emote normalizer, the packet verifier, per-kin
identity in the DB, the portrait picker, per-kin substance tracks.

**Needs a swap:** the scene source. `_run_relay()` takes a `scene` **string**. It
does not care whether Gemini got that from a 30-second Plex clip or from a photo, a
voice note, and a blob of phone sensor data.

**Needs a redistill:** the affinity sheets are currently *film*-shaped ("lights up
on a plot hole"). A life-shaped variant is the same script with a different prompt.
~15¢.

---

**Can we adapt this for the bridge too?**
