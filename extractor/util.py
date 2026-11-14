"""Shared helpers: time formatting, video duration, building the transcript."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# Each transcript line covers ~30 seconds, the same as workflow 1, so the agent's tools
# work the same way on YouTube videos and on local files.
LINE_SECONDS = 30


def hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def probe_duration(video: Path) -> int:
    """Video length in seconds, read with ffprobe."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(video)],
        check=True, capture_output=True, text=True,
    )
    return int(round(float(r.stdout.strip())))


def build_transcript(segments: list[dict], line_seconds: int = LINE_SECONDS) -> str:
    """[{start, text}] -> lines of '[hh:mm:ss] text', each line covering ~30 seconds."""
    lines: list[tuple[float, list[str]]] = []
    for seg in segments:
        text = re.sub(r"\s+", " ", str(seg.get("text") or "")).strip()
        if not text:
            continue
        start = float(seg.get("start") or 0)
        if not lines or start - lines[-1][0] >= line_seconds:
            lines.append((start, [text]))
        else:
            lines[-1][1].append(text)
    return "\n".join(f"[{hms(start)}] {' '.join(parts)}" for start, parts in lines)


def split_into_sections(transcript: str, section_seconds: int) -> list[dict]:
    """Split a transcript (lines stamped [hh:mm:ss]) into sections of section_seconds."""
    groups: dict[int, list[str]] = {}
    for line in transcript.splitlines():
        m = re.match(r"\[(\d{2}):(\d{2}):(\d{2})\]", line)
        if not m:
            continue
        sec = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
        groups.setdefault(sec // section_seconds, []).append(line)
    return [
        {
            "section": i,
            "start_sec": k * section_seconds,
            "end_sec": (k + 1) * section_seconds,
            "content": "\n".join(groups[k]),
        }
        for i, k in enumerate(sorted(groups), 1)
    ]
