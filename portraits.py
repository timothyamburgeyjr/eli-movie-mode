"""Portrait thumbnails — the faces on the roster picker and on every chat bubble.

The vault's bust shots are 2-3 MB PNGs apiece; the roster is ~18 MB of them.
Fine as reference art, absurd to drop into a dropdown. So we downscale each one
once into `static/portraits/{key}.jpg` and serve from the static mount we
already have.

ffmpeg does the resizing — it's already in the Docker image and `smart_snap`
already shells out to it, so this costs no new dependency (no Pillow).

Thumbnails are regenerated only when the source changes: we stamp the source's
mtime+size into a sidecar and compare on boot. Editing a bust in the vault and
restarting is enough to refresh the face.
"""
import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from characters import Character, portrait_source, selectable
from config import settings
from smart_snap import FFmpegError, _run_ffmpeg

log = logging.getLogger(__name__)

THUMB_PX = 160  # 2x the ~80px the picker renders at, so it stays crisp on retina
_STAMP_FILE = ".sources.json"


def _thumb_dir() -> Path:
    return Path(settings.portraits_dir)


def thumb_path(key: str) -> Path:
    return _thumb_dir() / f"{key}.jpg"


def portrait_url(key: str) -> Optional[str]:
    """Public URL for a kin's thumbnail, or None if they have no art.

    Path-based, not a route — `static/` is already mounted, so this is just a
    file the browser fetches. Callers fall back to initials when it's None.
    """
    return f"/static/portraits/{key}.jpg" if thumb_path(key).is_file() else None


def _source_stamp(src: Path) -> str:
    st = src.stat()
    return f"{int(st.st_mtime)}:{st.st_size}"


def _load_stamps() -> dict[str, str]:
    try:
        return json.loads((_thumb_dir() / _STAMP_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_stamps(stamps: dict[str, str]) -> None:
    d = _thumb_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / _STAMP_FILE).write_text(json.dumps(stamps, indent=2), encoding="utf-8")


async def _make_thumb(src: Path, dest: Path) -> None:
    """Downscale one bust to a square-ish thumbnail.

    Scales the short edge to THUMB_PX then centre-crops to a square, so faces
    stay centred whatever the source aspect ratio is.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    await _run_ffmpeg(
        [
            "-y",
            "-i", str(src),
            "-vf", f"scale={THUMB_PX}:{THUMB_PX}:force_original_aspect_ratio=increase,"
                   f"crop={THUMB_PX}:{THUMB_PX}",
            "-frames:v", "1",
            "-q:v", "3",
            str(dest),
        ],
        timeout=30.0,
    )


async def refresh_portraits(*, force: bool = False) -> dict[str, Optional[str]]:
    """Ensure every selectable kin has a current thumbnail. Returns key -> url.

    Called at startup. Never raises — a kin with no art (or a bad source) just
    gets a None and the UI falls back to initials. A missing face should not
    keep the app from booting.
    """
    stamps = _load_stamps()
    result: dict[str, Optional[str]] = {}
    rebuilt: list[str] = []

    for char in selectable():
        src = portrait_source(char)
        if src is None:
            result[char.key] = None
            continue

        dest = thumb_path(char.key)
        try:
            stamp = _source_stamp(src)
        except OSError:
            result[char.key] = None
            continue

        fresh = dest.is_file() and stamps.get(char.key) == stamp
        if fresh and not force:
            result[char.key] = portrait_url(char.key)
            continue

        try:
            await _make_thumb(src, dest)
        except (FFmpegError, OSError) as e:
            log.warning("portraits: %s failed (%s) — falling back to initials", char.key, e)
            result[char.key] = None
            continue

        stamps[char.key] = stamp
        rebuilt.append(char.key)
        result[char.key] = portrait_url(char.key)

    if rebuilt:
        _save_stamps(stamps)
        log.info("portraits: rebuilt %d (%s)", len(rebuilt), ", ".join(rebuilt))

    missing = [k for k, v in result.items() if v is None]
    if missing:
        log.info("portraits: no art for %s — will render as initials", ", ".join(missing))
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    urls = asyncio.run(refresh_portraits(force=True))
    for key, url in urls.items():
        size = thumb_path(key).stat().st_size if url else 0
        print(f"  {key:8} {url or 'initials fallback':40} {size / 1024:6.1f} KB" if url else f"  {key:8} initials fallback")
