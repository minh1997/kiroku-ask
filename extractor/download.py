"""Download the audio track of a YouTube video with yt-dlp.

Only the audio is downloaded (much smaller and faster than the video), since this is used
for transcription. To OCR the screen or build the scene log, download the full video
yourself and run ingest_video.py on it.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

YOUTUBE_ID_PATTERNS = [
    re.compile(r"(?:[?&]v=|/live/|/shorts/|/embed/|youtu\.be/)([A-Za-z0-9_-]{11})"),
]


def youtube_id(url: str) -> str | None:
    """The 11-character video ID of a YouTube URL, or None if it is not one."""
    if not re.match(r"^https?://([a-z0-9-]+\.)*(youtube\.com|youtu\.be)/", url.strip(), re.I):
        return None
    for p in YOUTUBE_ID_PATTERNS:
        m = p.search(url)
        if m:
            return m[1]
    return None


def _iso_utc(ts) -> str | None:
    if not ts:
        return None
    return dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc).isoformat()


def fetch_audio(url: str, out_dir: Path) -> tuple[Path, dict]:
    """Download the best audio stream into out_dir. Return (file, metadata)."""
    import yt_dlp  # type: ignore

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get("live_status") in ("is_live", "is_upcoming"):
            raise ValueError("This livestream has not ended yet. Try again once it is over.")
        info = ydl.extract_info(url, download=True)
        downloads = info.get("requested_downloads") or []
        path = Path(downloads[0]["filepath"] if downloads else ydl.prepare_filename(info))

    upload_date = info.get("upload_date")  # YYYYMMDD
    meta = {
        "video_id": info["id"],
        "url": f"https://www.youtube.com/watch?v={info['id']}",
        "title": info.get("title"),
        # Actual start of a livestream, else the upload time
        "started_at": _iso_utc(info.get("release_timestamp") or info.get("timestamp")),
        "upload_date": (f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
                        if upload_date else None),
        "duration_seconds": int(info["duration"]) if info.get("duration") else None,
    }
    return path, meta
