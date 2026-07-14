"""Cut a generated image out of its background.

    python tools/cutout.py in.png out.png              # auto: chroma key, else flood fill
    python tools/cutout.py in.png out.png --key ff00ff # force a chroma key colour
    python tools/cutout.py in.png out.png --flood      # force the flood fill

The image generator will not give you an alpha channel. It will hand back an opaque PNG,
often with a transparency checkerboard *drawn into the picture* as real pixels. This turns
that into a genuine cutout.

TWO STRATEGIES, and the difference matters:

  CHROMA KEY (preferred). The art was generated on a flat magenta field. Magenta appears
  nowhere in engraved pewter or crimson, so the key is exact — every pixel is either the
  key colour or it is art, with no judgement call in between. Nothing can be mangled.

  FLOOD FILL (rescue). No key colour, so we grow a region inwards from the border, taking
  pixels that resemble their neighbours. This is used instead of a brightness threshold
  because a threshold would delete every bright HIGHLIGHT inside the letterforms — the
  bevel, the polish, the glint in the metal — since those are the same value as the
  background. A flood fill can't reach them: they're walled in by dark strokes. It also
  copes with the drawn checkerboard, whose squares are all mutually similar and all
  connected to the border.

Neither is magic. Check the reported coverage, and look at the result.
"""
from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

from PIL import Image

FLOOD_TOL = 34      # how far a pixel may sit from its spreading neighbour and still be bg
KEY_TOL = 60        # chroma key: distance from the key colour
FEATHER = 20        # soften the rim over this much extra distance, to kill halos


def _d(a, b) -> float:
    return (abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])) / 3


def detect_key(im: Image.Image):
    """Is this sitting on a flat, saturated key field? Only say yes if the border agrees."""
    w, h = im.size
    px = im.load()
    edge = [px[x, 0] for x in range(0, w, 7)] + [px[x, h - 1] for x in range(0, w, 7)] \
         + [px[0, y] for y in range(0, h, 7)] + [px[w - 1, y] for y in range(0, h, 7)]
    ref = max(set(edge), key=edge.count)
    if sum(1 for c in edge if _d(c, ref) <= KEY_TOL) < len(edge) * 0.9:
        return None                       # the border isn't one colour: not a key field
    r, g, b = ref[:3]
    # Saturated and not grey — a real key, not just a dark or white card.
    if max(r, g, b) - min(r, g, b) < 70:
        return None
    return (r, g, b)


def chroma(im: Image.Image, key) -> Image.Image:
    px = im.load()
    w, h = im.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            d = _d((r, g, b), key)
            if d <= KEY_TOL:
                px[x, y] = (r, g, b, 0)
                continue
            if d <= KEY_TOL + FEATHER:
                a = int(a * (d - KEY_TOL) / FEATHER)
            px[x, y] = despill(r, g, b, a)
    return im


def despill(r, g, b, a):
    """Kill the key's colour cast in the art itself.

    A hairline one pixel wide is mostly ANTI-ALIASING — it is a blend of the ink and the
    magenta behind it. Keying only decides which pixels to drop; it leaves that blend
    behind, so every fine line in the engraving came out violet. Green is the channel
    magenta has none of, so where red and blue both run ahead of green, pull them back to
    it. Neutral metal is unchanged (its channels already agree); a violet hairline turns
    grey, which is what it was always meant to be.

    This is only right because the palette is greys. Art with genuinely warm or cool tones
    would need the spill suppressed relative to its own hue, not to green.
    """
    if r > g and b > g:
        spill = min(r - g, b - g)
        r -= spill
        b -= spill
    return (r, g, b, a)


def flood(im: Image.Image) -> Image.Image:
    """Grow inwards from the border. Highlights INSIDE the letters are walled off by the
    dark strokes around them, so they survive — which is the entire reason not to threshold."""
    px = im.load()
    w, h = im.size
    seen = bytearray(w * h)
    q = deque()
    for x in range(w):
        for y in (0, h - 1):
            q.append((x, y, px[x, y][:3]))
    for y in range(h):
        for x in (0, w - 1):
            q.append((x, y, px[x, y][:3]))

    while q:
        x, y, ref = q.popleft()
        i = y * w + x
        if seen[i]:
            continue
        cur = px[x, y][:3]
        if _d(cur, ref) > FLOOD_TOL:
            continue
        seen[i] = 1
        px[x, y] = (cur[0], cur[1], cur[2], 0)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and not seen[ny * w + nx]:
                q.append((nx, ny, cur))    # spread from the CURRENT pixel: tolerates gradients
    return im


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    if not src.is_file():
        print(f"No such file: {src}")
        return 2

    im = Image.open(src).convert("RGBA")
    before = sum(1 for p in im.getchannel("A").getdata() if p > 8)

    forced_key = None
    if "--key" in sys.argv:
        hexv = sys.argv[sys.argv.index("--key") + 1].lstrip("#")
        forced_key = tuple(int(hexv[i:i + 2], 16) for i in (0, 2, 4))

    key = forced_key or (None if "--flood" in sys.argv else detect_key(im))
    if key:
        print(f"chroma key #{key[0]:02x}{key[1]:02x}{key[2]:02x} — exact, nothing to mangle")
        im = chroma(im, key)
    else:
        print("no key field found — flood filling from the border")
        print("  (highlights inside the letterforms are walled in, so they survive)")
        im = flood(im)

    box = im.getbbox()
    if box:
        im = im.crop(box)
    after = sum(1 for p in im.getchannel("A").getdata() if p > 8)
    total = before or 1
    im.save(dst)
    print(f"wrote {dst}  {im.size[0]}x{im.size[1]}  kept {after / total:.1%} of the pixels")
    if after / total > 0.85:
        print("  ! Almost nothing was removed. The background probably isn't uniform —")
        print("    regenerate on a flat magenta field and pass --key ff00ff.")
    elif after / total < 0.03:
        print("  ! Almost everything was removed. The fill leaked through the art.")
        print("    Regenerate on a flat magenta field and pass --key ff00ff.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
