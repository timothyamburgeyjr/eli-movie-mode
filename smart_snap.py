"""FFmpeg-based clip and frame extraction from remote Plex stream URLs."""
import asyncio
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from config import settings

log = logging.getLogger(__name__)


def _resolve_ffmpeg() -> Optional[str]:
    """Find the ffmpeg binary. PATH first, then common Windows install locations."""
    override = os.environ.get("FFMPEG_PATH")
    if override and Path(override).is_file():
        return override
    found = shutil.which("ffmpeg")
    if found:
        return found
    # Windows fallbacks — winget, chocolatey, manual install
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe",
        Path("C:/ProgramData/chocolatey/bin/ffmpeg.exe"),
        Path("C:/ffmpeg/bin/ffmpeg.exe"),
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


class FFmpegError(Exception):
    """FFmpeg returned non-zero or timed out."""


class FFmpegNotFound(FFmpegError):
    """ffmpeg binary not found on PATH."""


def build_stream_url(part_key: str, token: Optional[str] = None) -> str:
    """Construct a direct-play Plex stream URL for FFmpeg."""
    tok = token or settings.plex_token
    base = settings.plex_url.rstrip("/")
    sep = "&" if "?" in part_key else "?"
    return f"{base}{part_key}{sep}X-Plex-Token={quote(tok, safe='')}"


def build_thumb_url(thumb_key: str, token: Optional[str] = None) -> str:
    tok = token or settings.plex_token
    base = settings.plex_url.rstrip("/")
    sep = "&" if "?" in thumb_key else "?"
    return f"{base}{thumb_key}{sep}X-Plex-Token={quote(tok, safe='')}"


async def _run_ffmpeg(args: list[str], timeout: float = 90.0) -> None:
    """Run ffmpeg in a worker thread.

    We intentionally avoid asyncio.create_subprocess_exec here — on Windows the
    SelectorEventLoop that uvicorn uses doesn't implement subprocess support
    (raises NotImplementedError). asyncio.to_thread + subprocess.run works on
    every platform and every loop.
    """
    import subprocess

    binary = _resolve_ffmpeg()
    if not binary:
        raise FFmpegNotFound("ffmpeg binary not found on PATH or known locations")
    cmd = [binary, "-hide_banner", "-loglevel", "error", *args]

    def _run() -> tuple[int, bytes]:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
            )
        except FileNotFoundError as e:
            raise FFmpegNotFound(f"ffmpeg not executable at {binary}") from e
        except subprocess.TimeoutExpired as e:
            raise FFmpegError(f"ffmpeg timed out after {timeout}s") from e
        return result.returncode, result.stderr or b""

    returncode, stderr = await asyncio.to_thread(_run)
    if returncode != 0:
        err = stderr.decode("utf-8", errors="ignore").strip()
        raise FFmpegError(f"ffmpeg exited {returncode}: {err[:500]}")


def _output_path(prefix: str, ext: str, out_dir: Optional[Path] = None) -> Path:
    out_dir = out_dir or Path(settings.frames_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return out_dir / f"{prefix}-{stamp}.{ext}"


async def extract_clip(
    stream_url: str,
    end_timestamp_seconds: float,
    duration_seconds: int = 30,
    out_dir: Optional[Path] = None,
) -> Path:
    """Extract a clip ending at `end_timestamp_seconds` from the remote stream.

    Fast-seeks before `-i`, then downscales + re-encodes to a small MP4.
    A full-bitrate 4K stream-copy would produce ~80 MB for 30 s and crush
    upload latency — 480p ultrafast H.264 lands the clip at ~3-5 MB while
    preserving enough visual detail for Gemini's scene analysis.
    """
    start = max(0.0, float(end_timestamp_seconds) - float(duration_seconds))
    out = _output_path("clip", "mp4", out_dir)
    args = [
        "-y",
        "-ss", f"{start:.3f}",
        "-i", stream_url,
        "-t", str(int(duration_seconds)),
        "-vf", "scale=-2:360,fps=12",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "30",
        "-c:a", "aac",
        "-b:a", "96k",
        "-ac", "2",
        "-movflags", "+faststart",
        str(out),
    ]
    await _run_ffmpeg(args, timeout=float(duration_seconds) * 3 + 30)
    return out


async def extract_frame(
    stream_url: str,
    at_timestamp_seconds: float,
    out_dir: Optional[Path] = None,
) -> Path:
    """Grab a single keyframe at the given timestamp."""
    out = _output_path("frame", "jpg", out_dir)
    args = [
        "-y",
        "-ss", f"{float(at_timestamp_seconds):.3f}",
        "-i", stream_url,
        "-frames:v", "1",
        "-q:v", "2",
        str(out),
    ]
    await _run_ffmpeg(args, timeout=30.0)
    return out


async def ffmpeg_available() -> bool:
    """Probe ffmpeg via a thread so we don't trip over Windows event-loop limits."""
    import subprocess

    binary = _resolve_ffmpeg()
    if not binary:
        return False

    def _check() -> bool:
        try:
            result = subprocess.run(
                [binary, "-version"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    return await asyncio.to_thread(_check)
