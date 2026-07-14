# The 35 button faces — copy-paste prompts

**Generated from `emotions.py`.** Every hex here is the app's own, so the art and the
frame can't disagree. Attach the baseline `Movie Mode (no film)` render to *every* prompt
as the style reference — that is what keeps thirty-five images looking like one set
instead of thirty-five separate attempts.

**Nothing here is required.** A mood with no face falls back to live text. Ship one, ship
all of them, stop anywhere in between.

## Do these six first

They cover **78% of every scene you have ever watched**: `tense` · `humor` · `dread` · `foreboding` · `melancholy` · `adrenaline`.

## After keying each one

```
python tools/cutout.py temp/<file>.png work.png
python tools/make_mask.py work.png <key> --face --full
```

---

## `tense` — Tense

*held-breath suspense · tension family · frame `#2e85b0` · 22.7% of scenes*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(tense)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, rigid and upright, tightly spaced, holding itself very still. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#d6e4eb**, as solid metal with a subtle bevel.
> - Label in **#cc5c6f**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #cc5c6f:** a single taut wire stretched tight and anchored at both edges of the canvas, passing BEHIND the letters — visible only where it emerges either side of them. It must NOT cross the faces of the letters: a horizontal line through a title reads as a strikethrough, as if the words were cancelled.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `humor` — Humor

*comedy, laughter, irony · levity family · frame `#dba920` · 17.9% of scenes*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(humor)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, but rounder and bouncier, sitting on a gently uneven baseline. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#ebe5d6**, as solid metal with a subtle bevel.
> - Label in **#5cb2cc**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #5cb2cc:** the whole title tilted at a jaunty angle, with a couple of bold sparkle dashes.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `dread` — Dread

*slow-burn doom, weight closing in · fear family · frame `#701622` · 15.3% of scenes*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(dread)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, but the strokes subtly gnawed and eroded at the edges, as if something has been at them. Still solid, still perfectly legible. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#ebd6d9**, as solid metal with a subtle bevel.
> - Label in **#5ccc85**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #5ccc85:** a low, thick bank of dark mist swallowing the bottom third of the letters.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `foreboding` — Foreboding

*quiet menace, the calm before · fear family · frame `#57161f` · 9.7% of scenes*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(foreboding)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, but the strokes subtly gnawed and eroded at the edges, as if something has been at them. Still solid, still perfectly legible. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#ebd6d9**, as solid metal with a subtle bevel.
> - Label in **#5ccc85**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #5ccc85:** a single long shadow stretching away from the letters, far longer than it should be.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `melancholy` — Melancholy

*sadness, wistfulness, loss · sorrow family · frame `#29489a` · 7.4% of scenes*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(melancholy)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, weighted and heavy at the base, as if it is hard to hold up. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#d6dceb**, as solid metal with a subtle bevel.
> - Label in **#cc765c**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #cc765c:** slow heavy drips running down from the letters, like rain on glass.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `adrenaline` — Adrenaline

*directed high-energy action · kinetic family · frame `#cb621c` · 5.1% of scenes*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(adrenaline)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, italicised and leaning hard forward, as if moving. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#ebded6**, as solid metal with a subtle bevel.
> - Label in **#5cccc0**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #5cccc0:** bold motion streaks flying off the right edge of the letters.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `cozy` — Cozy

*warm, safe, held · serenity family · frame `#329370` · 4.4% of scenes*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(cozy)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, softened — rounded corners, even weight, perfectly level and still. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#d6ebe3**, as solid metal with a subtle bevel.
> - Label in **#e8c98a**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #e8c98a:** a soft thick blanket fold curving behind the words.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `awe` — Awe

*wonder, spectacle, breathtaking craft · wonder family · frame `#8c36d6` · 3.6% of scenes*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(awe)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, lifted and engraved, with a slight upward optical rise. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#e1d6eb**, as solid metal with a subtle bevel.
> - Label in **#e8c15a**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #e8c15a:** a scatter of large four-point stars above and around the words.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `bittersweet` — Bittersweet

*joy and sorrow layered together · sorrow family · frame `#29438a` · 3.3% of scenes*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(bittersweet)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, weighted and heavy at the base, as if it is hard to hold up. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#d6dceb**, as solid metal with a subtle bevel.
> - Label in **#cc765c**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #cc765c:** the words lit from one side only — half in warm light, half in shadow.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `chaos` — Chaos

*frantic disorientation · kinetic family · frame `#e46a18` · 3.2% of scenes*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(chaos)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, italicised and leaning hard forward, as if moving. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#ebded6**, as solid metal with a subtle bevel.
> - Label in **#5cccc0**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #5cccc0:** the letters knocked askew at wild angles, jumbled but still readable.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `horror` — Horror

*visceral terror, the thing is HERE · fear family · frame `#961324` · 2.2% of scenes*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(horror)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, but the strokes subtly gnawed and eroded at the edges, as if something has been at them. Still solid, still perfectly legible. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#ebd6d9**, as solid metal with a subtle bevel.
> - Label in **#5ccc85**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #5ccc85:** bold jagged cracks splitting DOWNWARD through the letters, with one heavy drip running down from the M. The cracks must run vertically, never as a horizontal line across the title — that reads as a strikethrough.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `whimsy` — Whimsy

*playful, quirky, light on its feet · levity family · frame `#bd9525` · 1.5% of scenes*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(whimsy)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, but rounder and bouncier, sitting on a gently uneven baseline. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#ebe5d6**, as solid metal with a subtle bevel.
> - Label in **#5cb2cc**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #5cb2cc:** two or three simple balloons floating up behind the letters.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `mystery` — Mystery

*intrigue, puzzles, investigation · tension family · frame `#2f7497` · 1.4% of scenes*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(mystery)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, rigid and upright, tightly spaced, holding itself very still. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#d6e4eb**, as solid metal with a subtle bevel.
> - Label in **#cc5c6f**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #cc5c6f:** the letters half-obscured by a low bank of fog, partially hiding two of them.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `triumph` — Triumph

*victory, exhilaration, earned it · wonder family · frame `#8330cb` · 1.1% of scenes*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(triumph)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, lifted and engraved, with a slight upward optical rise. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#e1d6eb**, as solid metal with a subtle bevel.
> - Label in **#e8c15a**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #e8c15a:** bold rays radiating outward from behind the words.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `romance` — Romance

*love, intimacy, falling · desire family · frame `#cd2b71` · 0.8% of scenes*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(romance)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, softened and warm, with generous rounded curves. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#ebd6df**, as solid metal with a subtle bevel.
> - Label in **#f0cf9e**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #f0cf9e:** a few large rose petals drifting down past the letters.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `serene` — Serene

*peaceful, contemplative, still · serenity family · frame `#317e62` · 0.2% of scenes*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(serene)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, softened — rounded corners, even weight, perfectly level and still. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#d6ebe3**, as solid metal with a subtle bevel.
> - Label in **#e8c98a**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #e8c98a:** one clean horizontal line at the very BASE of the canvas, below the label, perfectly level, like still water.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `paranoia` — Paranoia

*watched, hunted, trusting no one · fear family · frame `#7c1523` · new — never yet classified*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(paranoia)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, but the strokes subtly gnawed and eroded at the edges, as if something has been at them. Still solid, still perfectly legible. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#ebd6d9**, as solid metal with a subtle bevel.
> - Label in **#5ccc85**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #5ccc85:** one large simple eye, wide open, peering out from behind the letters.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `anger` — Anger

*seething, wronged, holding it in · fury family · frame `#c92a19` · new — never yet classified*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(anger)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, leaning forward slightly, with hard aggressive corners. Struck rather than set. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#ebd8d6**, as solid metal with a subtle bevel.
> - Label in **#5ccc9f**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #5ccc9f:** a hard jagged fracture splitting VERTICALLY down through one letter. It must NOT run horizontally across the title — a horizontal line through the words reads as a strikethrough, as if they were cancelled.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `rage` — Rage

*violence, wrath, coming apart · fury family · frame `#ef2c17` · new — never yet classified*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(rage)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, leaning forward slightly, with hard aggressive corners. Struck rather than set. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#ebd8d6**, as solid metal with a subtle bevel.
> - Label in **#5ccc9f**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #5ccc9f:** the letters violently shattered, with a few big shards flung outward.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `absurd` — Absurd

*gleefully ridiculous, off the rails · levity family · frame `#e7b426` · new — never yet classified*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(absurd)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, but rounder and bouncier, sitting on a gently uneven baseline. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#ebe5d6**, as solid metal with a subtle bevel.
> - Label in **#5cb2cc**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #5cb2cc:** the letters at gleefully mismatched sizes, one of them clearly upside down.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `distaste` — Distaste

*grubby, off, faintly wrong · revulsion family · frame `#747f2b` · new — never yet classified*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(distaste)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, with the strokes sagging and thickening at the bottom, as if softening. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#e8ebd6**, as solid metal with a subtle bevel.
> - Label in **#5c85cc**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #5c85cc:** a single grubby smear dragged across the BASE of the canvas, below the label.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `disgust` — Disgust

*revolting, physically repellent · revulsion family · frame `#95a629` · new — never yet classified*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(disgust)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, with the strokes sagging and thickening at the bottom, as if softening. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#e8ebd6**, as solid metal with a subtle bevel.
> - Label in **#5c85cc**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #5c85cc:** thick heavy drips oozing down from the bottom of every letter.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `unease` — Unease

*something is off, can't name it · tension family · frame `#2f6c8a` · new — never yet classified*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(unease)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, rigid and upright, tightly spaced, holding itself very still. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#d6e4eb**, as solid metal with a subtle bevel.
> - Label in **#cc5c6f**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #cc5c6f:** one single letter tilted very slightly out of true — subtle, but clearly wrong.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `awkward` — Awkward

*social cringe, want to look away · tension family · frame `#2f799e` · new — never yet classified*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(awkward)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, rigid and upright, tightly spaced, holding itself very still. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#d6e4eb**, as solid metal with a subtle bevel.
> - Label in **#cc5c6f**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #cc5c6f:** one letter sunk awkwardly below the baseline, out of step with the rest.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `longing` — Longing

*yearning, wanting what isn't there · sorrow family · frame `#283e7b` · new — never yet classified*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(longing)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, weighted and heavy at the base, as if it is hard to hold up. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#d6dceb**, as solid metal with a subtle bevel.
> - Label in **#cc765c**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #cc765c:** a faint, faded second copy of the words, offset far behind the solid ones.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `grief` — Grief

*devastation, the floor gone · sorrow family · frame `#2851c3` · new — never yet classified*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(grief)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, weighted and heavy at the base, as if it is hard to hold up. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#d6dceb**, as solid metal with a subtle bevel.
> - Label in **#cc765c**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #cc765c:** the words sinking, the baseline collapsing beneath them, with jagged cracks running DOWNWARD through the letters. No horizontal line across the title — that reads as a strikethrough.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `hope` — Hope

*light ahead, something might hold · wonder family · frame `#7432ad` · new — never yet classified*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(hope)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, lifted and engraved, with a slight upward optical rise. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#e1d6eb**, as solid metal with a subtle bevel.
> - Label in **#e8c15a**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #e8c15a:** a single shaft of light breaking from behind the words.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `tender` — Tender

*gentle affection, care · desire family · frame `#b62d68` · new — never yet classified*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(tender)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, softened and warm, with generous rounded curves. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#ebd6df**, as solid metal with a subtle bevel.
> - Label in **#f0cf9e**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #f0cf9e:** one small simple heart resting beside the words, and a soft glow.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `desire` — Desire

*heat, wanting, charged · desire family · frame `#dc337c` · new — never yet classified*

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label **`(desire)`** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif, softened and warm, with generous rounded curves. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#ebd6df**, as solid metal with a subtle bevel.
> - Label in **#f0cf9e**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #f0cf9e:** visible heat shimmer rising off the letters, as if they are hot.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

# The button's own states

Grey and unlit — no family, no mood. Title in **`#8a8a99`**, label in **`#5a5a68`**,
the plain baseline typeface, no bevel changes.

## `none`

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label ****(live)**** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif as the reference, unchanged. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#8a8a99**, as solid metal with a subtle bevel.
> - Label in **#5a5a68**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #5a5a68:** none — plain and still. The film is ROLLING but no mood has been classified yet; this is the first few seconds of every movie.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `nofilm`

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label ****(no film)**** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif as the reference, unchanged. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#8a8a99**, as solid metal with a subtle bevel.
> - Label in **#5a5a68**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #5a5a68:** none — this is the resting baseline.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `paused`

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label ****(paused)**** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif as the reference, unchanged. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#8a8a99**, as solid metal with a subtle bevel.
> - Label in **#5a5a68**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #5a5a68:** two bold vertical pause bars set beside the words.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `standby`

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label ****(standby)**** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif as the reference, unchanged. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#8a8a99**, as solid metal with a subtle bevel.
> - Label in **#5a5a68**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #5a5a68:** a single simple shield resting behind the words.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `away`

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label ****(away)**** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif as the reference, unchanged. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#8a8a99**, as solid metal with a subtle bevel.
> - Label in **#5a5a68**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #5a5a68:** an open doorway shape behind the words.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `ended`

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label ****(ended)**** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif as the reference, unchanged. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#8a8a99**, as solid metal with a subtle bevel.
> - Label in **#5a5a68**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #5a5a68:** the words dissolving softly at their right edge.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---

## `idle`

> Using the attached image as the **exact style reference**, design a horizontal **button face**
> for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
> letterforms with a bevel *inside* the stroke, real mass, confident weight.
> 
> This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.
> 
> **Layout — 2:1 landscape, centred:**
> - The title **Movie Mode** large and dominant, filling most of the width.
> - Beneath it, much smaller, the label ****(idle)**** in lowercase, in a thick plain rounded
>   sans-serif, parentheses included.
> - Generous margins. Nothing touching the canvas edges.
> 
> **Typeface:** the same heavy slab serif as the reference, unchanged. **Not narrow, not condensed.**
> 
> **Colour:**
> - Title in **#8a8a99**, as solid metal with a subtle bevel.
> - Label in **#5a5a68**.
> - **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
>   outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.
> 
> **Embellishment — exactly ONE gesture, in the accent colour #5a5a68:** none — plain and still.
> Bold and simple. No fine detail — anything delicate dissolves at this size.
> 
> **Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
> **Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
> they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
> behind the art; the app draws the button's border itself. **Magenta, pink and purple must
> appear nowhere in the artwork.**
> 
> **Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution.

---
