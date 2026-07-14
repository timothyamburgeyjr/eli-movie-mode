"""Generate the whole prompt sheet from the mood table.

Every hex in it comes from emotions.py, so the sheet cannot disagree with the app. The only
hand-authored parts are the two things a computer has no business choosing: how the type
should FEEL for each family, and the single embellishment gesture for each mood.
"""
import colorsys
import io
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import emotions as e  # noqa: E402


def hexf(h, s, l):
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360, l / 100, s / 100)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


# THE ACCENT IS A CONTRAST RULE, NOT A MEANING RULE.
#
# The accent is derived as the family's hue +150 degrees — a split-complement, so it brings a
# second colour without the vibration a true complement (180) causes. Rendered at real button
# size that works beautifully wherever the accent should OPPOSE the mood: cyan in gold (humor)
# pops, sickly green in oxblood (dread) is pure horror, a warm ember-orange in cold blue
# (melancholy, grief) is exactly right.
#
# It fails wherever the accent should BELONG to the mood rather than fight it. The formula
# gave `desire` a GREEN label inside a rose button, `cozy` a MAGENTA one inside green, and
# `awe` an acidic chartreuse. All three are correct split-complements and all three are
# semantically wrong: green does not say wanting, and magenta is far too loud for safe.
#
# So the three warm/expansive families get a hand-chosen warm accent instead. Warmth wants
# warmth. This is the one place in the colour system where taste beats the formula, and it is
# worth saying out loud rather than quietly fudging the hue and pretending the wheel is intact.
ACCENT_OVERRIDE = {
    "wonder":   ("#e8c15a", "gold — starlight against the violet, not an acid chartreuse"),
    "serenity": ("#e8c98a", "warm firelight against the green — a hearth, not a klaxon"),
    "desire":   ("#f0cf9e", "candlelit gold against the rose — warm, not a traffic light"),
}

# How the TYPE should feel. One per family — a family is a register, and register is
# exactly what typography carries.
FONT = {
    "fear":      "the same heavy slab serif, but the strokes subtly gnawed and eroded at the edges, as if something has been at them. Still solid, still perfectly legible",
    "fury":      "the same heavy slab serif, leaning forward slightly, with hard aggressive corners. Struck rather than set",
    "kinetic":   "the same heavy slab serif, italicised and leaning hard forward, as if moving",
    "levity":    "the same heavy slab serif, but rounder and bouncier, sitting on a gently uneven baseline",
    "revulsion": "the same heavy slab serif, with the strokes sagging and thickening at the bottom, as if softening",
    "serenity":  "the same heavy slab serif, softened — rounded corners, even weight, perfectly level and still",
    "tension":   "the same heavy slab serif, rigid and upright, tightly spaced, holding itself very still",
    "sorrow":    "the same heavy slab serif, weighted and heavy at the base, as if it is hard to hold up",
    "wonder":    "the same heavy slab serif, lifted and engraved, with a slight upward optical rise",
    "desire":    "the same heavy slab serif, softened and warm, with generous rounded curves",
}

# ONE gesture each. This is the whole budget at 180px wide — the compass rose died because
# it had more features than it had pixels.
GESTURE = {
    "foreboding":  "a single long shadow stretching away from the letters, far longer than it should be",
    "dread":       "a low, thick bank of dark mist swallowing the bottom third of the letters",
    "paranoia":    "one large simple eye, wide open, peering out from behind the letters",
    "horror":      "bold jagged cracks splitting DOWNWARD through the letters, with one heavy drip "
                   "running down from the M. The cracks must run vertically, never as a horizontal "
                   "line across the title — that reads as a strikethrough",
    "anger":       "a hard jagged fracture splitting VERTICALLY down through one letter. "
                   "It must NOT run horizontally across the title — a horizontal line through the "
                   "words reads as a strikethrough, as if they were cancelled",
    "rage":        "the letters violently shattered, with a few big shards flung outward",
    "adrenaline":  "bold motion streaks flying off the right edge of the letters",
    "chaos":       "the letters knocked askew at wild angles, jumbled but still readable",
    "whimsy":      "two or three simple balloons floating up behind the letters",
    "humor":       "the whole title tilted at a jaunty angle, with a couple of bold sparkle dashes",
    "absurd":      "the letters at gleefully mismatched sizes, one of them clearly upside down",
    "distaste":    "a single grubby smear dragged across the BASE of the canvas, below the label",
    "disgust":     "thick heavy drips oozing down from the bottom of every letter",
    "serene":      "one clean horizontal line at the very BASE of the canvas, below the label, "
                   "perfectly level, like still water",
    "cozy":        "a soft thick blanket fold curving behind the words",
    "unease":      "one single letter tilted very slightly out of true — subtle, but clearly wrong",
    "mystery":     "the letters half-obscured by a low bank of fog, partially hiding two of them",
    "awkward":     "one letter sunk awkwardly below the baseline, out of step with the rest",
    "tense":       "a single taut wire stretched tight and anchored at both edges of the canvas, "
                   "passing BEHIND the letters — visible only where it emerges either side of them. "
                   "It must NOT cross the faces of the letters: a horizontal line through a title "
                   "reads as a strikethrough, as if the words were cancelled",
    "longing":     "a faint, faded second copy of the words, offset far behind the solid ones",
    "bittersweet": "the words lit from one side only — half in warm light, half in shadow",
    "melancholy":  "slow heavy drips running down from the letters, like rain on glass",
    "grief":       "the words sinking, the baseline collapsing beneath them, with jagged cracks "
                   "running DOWNWARD through the letters. No horizontal line across the title — "
                   "that reads as a strikethrough",
    "hope":        "a single shaft of light breaking from behind the words",
    "triumph":     "bold rays radiating outward from behind the words",
    "awe":         "a scatter of large four-point stars above and around the words",
    "tender":      "one small simple heart resting beside the words, and a soft glow",
    "romance":     "a few large rose petals drifting down past the letters",
    "desire":      "visible heat shimmer rising off the letters, as if they are hot",
}

# The button's own states. Grey, unlit, no family.
STATES = {
    "none":    ("(live)",     "none — plain and still. The film is ROLLING but no mood has been "
                              "classified yet; this is the first few seconds of every movie"),
    "nofilm":  ("(no film)",  "none — this is the resting baseline"),
    "paused":  ("(paused)",   "two bold vertical pause bars set beside the words"),
    "standby": ("(standby)",  "a single simple shield resting behind the words"),
    "away":    ("(away)",     "an open doorway shape behind the words"),
    "ended":   ("(ended)",    "the words dissolving softly at their right edge"),
    "idle":    ("(idle)",     "none — plain and still"),
}

BASE = """Using the attached image as the **exact style reference**, design a horizontal **button face**
for a companion app called **Movie Mode**. Match its craft precisely: heavy solid slab-serif
letterforms with a bevel *inside* the stroke, real mass, confident weight.

This is UI artwork displayed only about 180 pixels wide, so it must be **bold and simple**.

**Layout — 2:1 landscape, centred:**
- The title **Movie Mode** large and dominant, filling most of the width.
- Beneath it, much smaller, the label **{label}** in lowercase, in a thick plain rounded
  sans-serif, parentheses included.
- Generous margins. Nothing touching the canvas edges.

**Typeface:** {font}. **Not narrow, not condensed.**

**Colour:**
- Title in **{words}**, as solid metal with a subtle bevel.
- Label in **{accent}**.
- **Do NOT render the letters as thin bright outlines around dark centres** — at this size the
  outline is all that survives and the words dissolve into noise. Solid mass first, bevel second.

**Embellishment — exactly ONE gesture, in the accent colour {accent}:** {gesture}.
Bold and simple. No fine detail — anything delicate dissolves at this size.

**Background:** a flat solid magenta field, hex **#FF00FF**, 100% opaque, edge to edge.
**Do NOT use transparency. Do NOT draw a transparency checkerboard** — real magenta pixels,
they will be keyed out. No gradient, no vignette, no frame, no plate, no rounded rectangle
behind the art; the app draws the button's border itself. **Magenta, pink and purple must
appear nowhere in the artwork.**

**Constraints:** exactly 2:1 aspect. No other text. No logo, no icon. High resolution."""

con = sqlite3.connect("data/moviemode.db")
seen = dict(con.execute(
    "SELECT mood, COUNT(*) FROM messages WHERE mood IS NOT NULL GROUP BY mood"))
total = sum(seen.values()) or 1

out = [
    "# The 35 button faces — copy-paste prompts",
    "",
    "**Generated from `emotions.py`.** Every hex here is the app's own, so the art and the",
    "frame can't disagree. Attach the baseline `Movie Mode (no film)` render to *every* prompt",
    "as the style reference — that is what keeps thirty-five images looking like one set",
    "instead of thirty-five separate attempts.",
    "",
    "**Nothing here is required.** A mood with no face falls back to live text. Ship one, ship",
    "all of them, stop anywhere in between.",
    "",
    "## Do these six first",
    "",
    "They cover **78% of every scene you have ever watched**: "
    "`tense` · `humor` · `dread` · `foreboding` · `melancholy` · `adrenaline`.",
    "",
    "## After keying each one",
    "",
    "```",
    "python tools/cutout.py temp/<file>.png work.png",
    "python tools/make_mask.py work.png <key> --face --full",
    "```",
    "",
    "---",
    "",
]

ranked = sorted(e.MOODS, key=lambda m: -seen.get(m, 0))
for key in ranked:
    m = e.MOODS[key]
    hue = e.FAMILY_HUES[m.family][0]
    words = hexf(hue, 34, 88)
    accent = ACCENT_OVERRIDE.get(m.family, (hexf(hue + 150, 52, 58), ""))[0]
    n = seen.get(key, 0)
    freq = f"{n / total:.1%} of scenes" if n else "new — never yet classified"
    out += [
        f"## `{key}` — {m.label}",
        "",
        f"*{m.blurb} · {m.family} family · frame `{e.MOOD_COLORS[key]}` · {freq}*",
        "",
        "> " + BASE.format(
            label=f"`({key})`", font=FONT[m.family], words=words, accent=accent,
            gesture=GESTURE[key],
        ).replace("\n", "\n> "),
        "",
        "---",
        "",
    ]

out += ["# The button's own states", "",
        "Grey and unlit — no family, no mood. Title in **`#8a8a99`**, label in **`#5a5a68`**,",
        "the plain baseline typeface, no bevel changes.", ""]
for key, (label, gesture) in STATES.items():
    out += [
        f"## `{key}`",
        "",
        "> " + BASE.format(
            label=f"**{label}**",
            font="the same heavy slab serif as the reference, unchanged",
            words="#8a8a99", accent="#5a5a68", gesture=gesture,
        ).replace("\n", "\n> "),
        "",
        "---",
        "",
    ]

io.open("static/mood/PROMPTS.md", "w", encoding="utf-8", newline="").write("\n".join(out))
print(f"static/mood/PROMPTS.md — {len(e.MOODS)} moods + {len(STATES)} states = "
      f"{len(e.MOODS) + len(STATES)} prompts, ranked by how often each actually occurs")
