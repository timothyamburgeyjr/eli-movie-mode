"""Take everything in temp/ and turn it into button art.

    python tools/ingest.py                # everything in temp/
    python tools/ingest.py temp/tense.png # just one
    python tools/ingest.py --status       # what's done, what's left

The file's NAME is the key. `tense.png` -> the tense face. `no-film.png` -> `nofilm`
(dashes, underscores and spaces are all forgiven, because they will all happen).

For each file: key out the magenta, kill the colour spill, fit it to the button, and write
it as `<key>.face.full.png`. Anything whose name isn't a mood or a state is skipped loudly
rather than silently — a typo'd filename that quietly does nothing is how you end up
staring at a button wondering why your new art isn't showing.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import emotions  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEMP = ROOT / "temp"
STATES = {"none", "paused", "nofilm", "standby", "idle", "away", "ended"}
VALID = set(emotions.MOODS) | STATES

# The names that will actually come off a download.
#
# `live` is the important one. The internal key for "rolling, but nothing classified yet" is
# `none` — but the button LABEL for that state is "(live)", and the prompt sheet says so, so
# the file that comes back is called live.png every single time. Making the human rename it to
# match an internal key they never see would be the tool serving itself.
ALIASES = {
    "nofilm": "nofilm", "no_film": "nofilm", "no-film": "nofilm", "no film": "nofilm",
    "live": "none", "generic": "none", "unclassified": "none",
}


def key_for(stem: str) -> str | None:
    k = stem.strip().lower().replace(" ", "-")
    k = ALIASES.get(k, k)
    k = ALIASES.get(k.replace("-", ""), k.replace("-", "_"))
    for cand in (k, k.replace("_", ""), k.replace("_", "-")):
        if cand in VALID:
            return cand
    return None


def run(*args) -> tuple[bool, str]:
    r = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True)
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


STATE_LABEL = {"none": "live", "nofilm": "no film", "paused": "paused", "standby": "standby",
               "idle": "idle", "away": "away", "ended": "ended"}


def have(key: str) -> bool:
    out = ROOT / "static" / "mood"
    return (out / f"{key}.face.full.png").exists() or (out / f"{key}.face.png").exists()


def status() -> int:
    """The inventory, read off the disk. Ranked by how often each mood ACTUALLY occurs in
    Tim's own sessions, because six of the twenty-nine cover 78% of every scene he has ever
    watched and the tail is genuinely optional."""
    import sqlite3
    seen: dict[str, int] = {}
    try:
        con = sqlite3.connect(ROOT / "data" / "moviemode.db")
        seen = dict(con.execute("SELECT mood, COUNT(*) FROM messages "
                                "WHERE mood IS NOT NULL GROUP BY mood"))
    except sqlite3.Error:
        pass
    total = sum(seen.values()) or 1

    print("BUTTON STATES")
    for k in ("none", "nofilm", "paused", "standby", "idle", "away", "ended"):
        print(f"  [{'x' if have(k) else ' '}] {STATE_LABEL[k]:12} -> {k}.png")

    print()
    print("MOODS  (ranked by how often they actually happen)")
    cum = 0.0
    for i, k in enumerate(sorted(emotions.MOODS, key=lambda m: -seen.get(m, 0)), 1):
        n = seen.get(k, 0)
        cum += n / total
        freq = f"{n / total:5.1%}" if n else "  new"
        note = "   <-- 78% of all scenes covered by here" if i == 6 else ""
        print(f"  [{'x' if have(k) else ' '}] {k:12} {freq}  running {cum:5.1%}  "
              f"{emotions.MOOD_COLORS[k]}{note}")

    ds = sum(have(k) for k in STATE_LABEL)
    dm = sum(have(k) for k in emotions.MOODS)
    print()
    print(f"{ds}/7 states, {dm}/{len(emotions.MOODS)} moods = {ds + dm}/{7 + len(emotions.MOODS)}.")
    print("Every missing one falls back to live text. Nothing is broken by waiting.")
    return 0


def main() -> int:
    if "--status" in sys.argv:
        return status()
    srcs = [Path(a) for a in sys.argv[1:] if not a.startswith("-")] or sorted(
        p for p in TEMP.glob("*") if p.suffix.lower() in (".png", ".webp", ".jpg", ".jpeg")
    )
    if not srcs:
        print("Nothing in temp/. Drop the renders in, named by key (tense.png, dread.png).")
        return 1

    done, skipped = [], []
    work = Path(tempfile.mkdtemp())
    try:
        for src in srcs:
            key = key_for(src.stem)
            if not key:
                skipped.append((src.name, "not a mood or a state — check the filename"))
                continue

            cut = work / f"{key}.png"
            ok, out = run("tools/cutout.py", str(src), str(cut))
            if not ok:
                skipped.append((src.name, out.strip().splitlines()[-1] if out.strip() else "cutout failed"))
                continue
            # cutout warns rather than fails when the key didn't take. Don't ship that silently.
            if "!" in out:
                skipped.append((src.name, [l.strip() for l in out.splitlines() if "!" in l][0]))
                continue

            ok, out2 = run("tools/make_mask.py", str(cut), key, "--face", "--full")
            if not ok:
                skipped.append((src.name, out2.strip().splitlines()[-1] if out2.strip() else "ingest failed"))
                continue

            keyline = next((l for l in out.splitlines() if "chroma key" in l or "flood" in l), "")
            size = next((l.split("  ")[1] for l in out2.splitlines() if "wrote" in l), "")
            done.append((src.name, key, keyline.strip(), size.strip()))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    for name, key, how, size in done:
        print(f"  OK    {name:16} -> {key:11} {size}")
        print(f"        {how}")
    for name, why in skipped:
        print(f"  SKIP  {name:16} {why}")

    print(f"\n{len(done)} in, {len(skipped)} skipped. Reload the dashboard — no rebuild needed.")
    return 1 if skipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
