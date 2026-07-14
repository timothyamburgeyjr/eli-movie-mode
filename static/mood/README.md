# Movie Mode button art

Drop a file here and it shows up on the button. No rebuild, no registration — the directory is
bind-mounted and `/api/mood-art` reads it live. Reload the page.

## Keys — one per MOOD

Each mood gets its own face. `foreboding` and `horror` are the same family and the same hue,
but they are not the same picture and not the same colour: **hue says what kind of feeling it
is, intensity says how far into it you are.** So the redder it gets, the angrier it gets.

The **frame colour is drawn by the app** from the table below; the art inside is yours.

| key | family | frame | text tint | heat | what it means |
|---|---|---|---|---|---|
| `tender` | desire | `#b62d68` | `#d79ab5` | 32% | gentle affection, care |
| `romance` | desire | `#cd2b71` | `#e0a7c0` | 62% | love, intimacy, falling |
| `desire` | desire | `#dc337c` | `#e4aac3` | 94% | heat, wanting, charged |
| `foreboding` | fear | `#57161f` | `#b44857` | 28% | quiet menace, the calm before |
| `dread` | fear | `#701622` | `#c35261` | 57% | slow-burn doom, weight closing in |
| `paranoia` | fear | `#7c1523` | `#c95867` | 72% | watched, hunted, trusting no one |
| `horror` | fear | `#961324` | `#d36473` | 100% | visceral terror, the thing is HERE |
| `anger` | fury | `#c92a19` | `#e09991` | 45% | seething, wronged, holding it in |
| `rage` | fury | `#ef2c17` | `#eaaba4` | 95% | violence, wrath, coming apart |
| `adrenaline` | kinetic | `#cb621c` | `#e0b496` | 62% | directed high-energy action |
| `chaos` | kinetic | `#e46a18` | `#e7bfa4` | 95% | frantic disorientation |
| `whimsy` | levity | `#bd9525` | `#dbc997` | 32% | playful, quirky, light on its feet |
| `humor` | levity | `#dba920` | `#e4d3a6` | 66% | comedy, laughter, irony |
| `absurd` | levity | `#e7b426` | `#e7d6a6` | 92% | gleefully ridiculous, off the rails |
| `distaste` | revulsion | `#747f2b` | `#b6c079` | 38% | grubby, off, faintly wrong |
| `disgust` | revulsion | `#95a629` | `#c8d28c` | 90% | revolting, physically repellent |
| `serene` | serenity | `#317e62` | `#81bda7` | 32% | peaceful, contemplative, still |
| `cozy` | serenity | `#329370` | `#8cc8b2` | 62% | warm, safe, held |
| `unease` | tension | `#2f6c8a` | `#83afc4` | 30% | something is off, can't name it |
| `mystery` | tension | `#2f7497` | `#8ab5ca` | 48% | intrigue, puzzles, investigation |
| `awkward` | tension | `#2f799e` | `#8eb8ce` | 57% | social cringe, want to look away |
| `tense` | tension | `#2e85b0` | `#98c1d5` | 82% | held-breath suspense |
| `longing` | sorrow | `#283e7b` | `#7488be` | 30% | yearning, wanting what isn't there |
| `bittersweet` | sorrow | `#29438a` | `#7d90c6` | 46% | joy and sorrow layered together |
| `melancholy` | sorrow | `#29489a` | `#8699cd` | 62% | sadness, wistfulness, loss |
| `grief` | sorrow | `#2851c3` | `#9daedc` | 100% | devastation, the floor gone |
| `hope` | wonder | `#7432ad` | `#b99ad4` | 36% | light ahead, something might hold |
| `triumph` | wonder | `#8330cb` | `#c7abdf` | 70% | victory, exhilaration, earned it |
| `awe` | wonder | `#8c36d6` | `#c9ace2` | 92% | wonder, spectacle, breathtaking craft |

Plus the button's own states, which are grey and equally worth drawing:
`nofilm` · `paused` · `standby` · `idle` · `away` · `ended` · `none` (rolling, unclassified).

Nothing is required. **A mood with no face falls back to live text**, so you can ship one
piece of art or twenty-nine and everything in between works.

## The face — this is the slot to use

**`<key>.face.full.png`** is one image that *is* the entire inside of the button: title, label,
any embellishment, composed exactly as you want them. It supersedes every other slot. The app
draws only the frame, the glow and the pulse, because those track the room in real time and
have to animate.

- **Design it 2:1.** The button is 74 CSS px tall and 139–184 wide depending on layout, so the
  art is centre-fitted (`contain`, never `cover` — cropping would eat the edge of the word).
- **Export around 1500×750.** At 3× device pixels the button is 222px tall, so that is
  comfortably oversampled. Resolution has never been the problem here; *detail density* has.
- **Do not draw the button's frame into the art.** The app draws the rounded border and the
  mood glow around it. A baked-in frame sits inside the real one and reads as a double rule.

Finer-grained slots still work and are used only when a key has no face: `<key>.png` (the
circle icon), `<key>.word.png` (just the title), `<key>.sub.png` (just the label),
`<key>.lock.png` (title + label together). Every slot falls back to live text or a drawn
vector when it has no art — that is the normal state, not a degraded one.

## Two colour modes

**`.full` — full colour.** The art keeps its own palette. This is right for the face, where the
art *is* the design. Nothing enforces that it agrees with the frame, so generate it in a
palette chosen against the mood's hex above.

**No `.full` — mask.** Only the alpha survives; the room paints the shape in the mood's colour,
so the art *cannot* come out a different colour from the frame. Right for flat silhouettes,
wrong for engraved metal (it would throw the metal away). If both exist, `.full` wins.

## The art's palette

The frame is the mood's hex (drawn by the app). The art brings two more colours, so the app
is not monochrome. **Both are derived from the family's hue, not hand-picked:**

- **The words** — the family's hue at low saturation and high lightness. A light near-neutral
  that still *belongs* to the mood. Legible on the dark button; doesn't fight the frame.
- **The accent** — the family's hue **+150°**. A split-complement, deliberately *not* the true
  complement at 180°: red's complement is green, and a green word inside a red glowing button
  vibrates and gets *harder* to read. 150° brings colour without the fight.

The accent is where the second colour lives **and** where the embellishment lives, so it does
double duty rather than being decoration.

| family | the hue means | the words | the accent (and the embellishment) |
|---|---|---|---|
| `desire` | wanting | `#ebd6df` | `#5ccc64` |
| `fear` | threat, frozen | `#ebd6d9` | `#5ccc85` |
| `fury` | threat, swinging | `#ebd8d6` | `#5ccc9f` |
| `kinetic` | something is HAPPENING | `#ebded6` | `#5cccc0` |
| `levity` | the room is laughing | `#ebe5d6` | `#5cb2cc` |
| `revulsion` | get it away from me | `#e8ebd6` | `#5c85cc` |
| `serenity` | safe, held, still | `#d6ebe3` | `#cc5cbd` |
| `tension` | wound tight | `#d6e4eb` | `#cc5c6f` |
| `sorrow` | aching | `#d6dceb` | `#cc765c` |
| `wonder` | looking up at something bigger | `#e1d6eb` | `#c8cc5c` |
| `nofilm` etc | unlit | `#8a8a99` | — |

Within a family, every mood shares these two — only the *frame* moves with the heat. That is
what keeps four fear faces looking like one family rather than four unrelated pictures.

## Generating

The image generator will not hand you an alpha channel. It hands back an opaque PNG, often with
a transparency checkerboard *drawn into the picture* as real pixels. So:

1. Ask for the art on a **flat solid magenta field (`#FF00FF`), 100% opaque**, and say
   explicitly *not* to use transparency or to draw a checkerboard. Magenta appears nowhere in
   these palettes, so the key is exact and cannot mangle the art.
2. Cut it out and ingest it:

```
python tools/cutout.py temp/button2.png work.png       # keys the magenta, kills the colour spill
python tools/make_mask.py work.png nofilm --face --full
```

`cutout.py` falls back to a border flood-fill when there is no key field. That is deliberately
*not* a brightness threshold: a threshold would delete every bright highlight **inside** the
letterforms, since those are the same value as the background. A flood fill can't reach them —
they're walled in by the dark strokes.

## Two things to insist on in the prompt

**Solid letterforms, not thin bright outlines around dark centres.** At this size the outline is
all that survives and the words dissolve into hairy noise. This killed the first two attempts.

**No fine ornament.** The first face carried the full Oasis compass rose; at 46px it had more
distinct features than it had pixels and came out a grey blob. The whole budget at this size is:
a heavy title, a small plain label, and *one* gesture — webs, a crack, a hat. Anything more is
noise.
