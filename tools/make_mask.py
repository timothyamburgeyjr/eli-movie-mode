"""Turn whatever the image generator hands back into a button-ready asset.

    python tools/make_mask.py ~/Downloads/fear.png fear --lock      # "Movie Mode" + subtitle
    python tools/make_mask.py ~/Downloads/fear.png fear             # the circle icon
    python tools/make_mask.py ~/Downloads/fear.png fear --full      # keep its own colours

The LOCKUP is the main event: one image carrying the title and the emotion beneath it, so
the typographic relationship between the two lines is designed rather than assembled. Keys
are the seven families (fear, tension, kinetic, wonder, levity, warmth, sorrow) plus the
button's own states (paused, nofilm, standby, idle, away, ended) - "PAUSED" is as much worth
drawing as "Afraid" is.

The generator will not give you a clean asset. It will give you a 1024x1024 PNG with a
white or checkerboard background, soft anti-aliased edges, and the subject floating
somewhere near the middle with uneven margins. This makes it usable anyway:

  * lifts a flat background to transparent (whatever colour it happens to be)
  * crops to the actual ink, so every icon ends up optically the same size — otherwise one
    family's art sits noticeably smaller in the circle than the next one's
  * squares it up with a small even margin
  * downscales to 128x128 with a good filter

MASK mode (default) throws the colours away and keeps only the shape. That is not a
limitation, it is the point: the room paints the shape in the mood's colour, so the art
CANNOT come out a different colour from the border, the scrubber and the badge. That
disagreement is the exact bug we just spent an afternoon killing.

FULL mode keeps the colours. Only use it for art generated in that family's own hex.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import emotions  # noqa: E402

OUT = Path("static/mood")
SIZE = 128          # icon: a square
MARGIN = 6          # px of breathing room in the 128 box, per side
WORD_H = 256        # wordmark/face: normalise on HEIGHT, keep the aspect.
                    # The button is 74 CSS px tall; at 3x device pixels that is 222,
                    # so 256 is comfortably oversampled and the small label holds up.
WORD_MAX_W = 1200
BG_TOLERANCE = 26   # how far from the corner colour still counts as background

# The button has three independent slots. Each falls back to text or a drawn vector when it
# has no art, so you can fill in as few or as many as you like.
SLOTS = {
    "face": ".face",    # <key>.face.png   ONE image that IS the whole button. Wins over all.
    "icon": "",         # <key>.png        the circle
    "lock": ".lock",    # <key>.lock.png   "Movie Mode" + the subtitle, as ONE piece of art
    "word": ".word",    # <key>.word.png   just the title  (only if there is no lockup)
    "sub":  ".sub",     # <key>.sub.png    just the subtitle (only if there is no lockup)
}
# Keys are the MOODS (each gets its own face) plus the button's own states. `paused` is as
# much a thing worth drawing as `dread` is.
STATE_KEYS = {"none", "paused", "nofilm", "standby", "idle", "away", "ended"}


def lift_background(im: Image.Image) -> Image.Image:
    """Make a flat background transparent. Samples the corners rather than assuming white —
    the generator likes to return off-white, pale grey, or a faint gradient."""
    im = im.convert("RGBA")
    px = im.load()
    w, h = im.size
    corners = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]
    opaque = [c for c in corners if c[3] > 8]
    if not opaque:
        return im  # already transparent — the generator did the right thing
    # The most common corner. If the corners disagree wildly it's art, not a background.
    bg = max(set(opaque), key=opaque.count)
    if sum(1 for c in opaque if _near(c, bg)) < 3:
        return im

    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            d = _dist((r, g, b, a), bg)
            if d <= BG_TOLERANCE:
                px[x, y] = (r, g, b, 0)
            elif d <= BG_TOLERANCE * 2.5:
                # Feather the anti-aliased rim instead of leaving a hard halo around it.
                px[x, y] = (r, g, b, int(a * (d - BG_TOLERANCE) / (BG_TOLERANCE * 1.5)))
    return im


def _near(c, d) -> bool:
    return _dist(c, d) <= BG_TOLERANCE


def _dist(c, d) -> float:
    return sum(abs(c[i] - d[i]) for i in range(3)) / 3


def fit_icon(im: Image.Image) -> Image.Image:
    """Crop to the ink, then centre it in a square. Without this, one family's icon renders
    visibly smaller than the next's and the button looks like it's twitching between moods."""
    box = im.getbbox()
    if box:
        im = im.crop(box)
    side = max(im.size)
    sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    sq.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
    inner = SIZE - MARGIN * 2
    sq = sq.resize((inner, inner), Image.LANCZOS)
    out = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    out.paste(sq, (MARGIN, MARGIN))
    return out


def fit_word(im: Image.Image) -> Image.Image:
    """A wordmark is WIDE. Squaring it would wreck it, so keep the aspect ratio and normalise
    on height instead — that way every mood's title sits on the same baseline at the same
    size, however long its embellishments trail."""
    box = im.getbbox()
    if box:
        im = im.crop(box)
    h = WORD_H
    w = max(1, round(im.width * (h / im.height)))
    if w > WORD_MAX_W:                     # a very trailing wordmark: fit the width instead
        w = WORD_MAX_W
        h = max(1, round(im.height * (WORD_MAX_W / im.width)))
    return im.resize((w, h), Image.LANCZOS)


def to_mask(im: Image.Image) -> Image.Image:
    """Keep the ALPHA, discard the colour. CSS mask-image reads nothing else."""
    a = im.getchannel("A")
    m = Image.new("RGBA", im.size, (0, 0, 0, 0))
    m.putalpha(a)
    return m


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, family = Path(sys.argv[1]), sys.argv[2].lower()
    full = "--full" in sys.argv
    slot = "icon"
    for s_ in SLOTS:
        if f"--{s_}" in sys.argv:
            slot = s_

    valid = set(emotions.MOODS) | STATE_KEYS
    if family not in valid:
        print(f"'{family}' is not a key. Pick from: {', '.join(sorted(valid))}")
        return 2
    if not src.is_file():
        print(f"No such file: {src}")
        return 2

    im = lift_background(Image.open(src))
    im = fit_icon(im) if slot == "icon" else fit_word(im)   # only the icon gets squared
    # A face is wide art (roughly 2:1) - squaring it would crush the lettering.
    if not full:
        im = to_mask(im)

    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / f"{family}{SLOTS[slot]}{'.full' if full else ''}.png"
    im.save(dest)

    px = im.width * im.height
    ink = sum(1 for p in im.getchannel("A").getdata() if p > 20) / px
    print(f"wrote {dest}  ({slot}, {'full-colour' if full else 'mask'}, "
          f"{im.width}x{im.height}, {ink:.0%} ink)")
    if ink < 0.04:
        print("  ! Almost nothing survived. The background lift probably ate the art —")
        print("    ask for a SOLID subject on a TRANSPARENT background and try again.")
    elif ink > 0.80:
        print("  ! Nearly the whole square is ink. The background did not lift — it is")
        print("    probably a gradient. Ask for a flat or transparent background.")
    if not full:
        print(f"  colour comes from the room ({emotions.MOOD_COLORS.get(family, 'grey')}), not the file.")
    print("  reload the dashboard — /api/mood-art picks it up, no rebuild.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
