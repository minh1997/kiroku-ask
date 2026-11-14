"""Extract frames, drop near-duplicates, OCR the on-screen text and pick out URLs."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path

import numpy as np
from PIL import Image

# Hash size: the image is shrunk to (HASH_SIZE+1) x HASH_SIZE -> HASH_SIZE*HASH_SIZE bits
HASH_SIZE = 16
# Minimum number of differing bits for a frame to count as changed.
# Kept low (keeps more frames) because the text grouping afterwards decides the final result.
HASH_THRESHOLD = 6
# Even if the picture does not change, OCR again after this many seconds so nothing is missed
FORCE_KEEP_SECONDS = 30


def _dhash(path: Path) -> np.ndarray:
    """dHash: compare the brightness of horizontally adjacent pixels."""
    with Image.open(path) as im:
        g = im.convert("L").resize((HASH_SIZE + 1, HASH_SIZE), Image.LANCZOS)
        a = np.asarray(g, dtype=np.int16)
    return (a[:, 1:] > a[:, :-1]).flatten()


def extract_frames(video: Path, out_dir: Path, fps: float = 1.0, height: int = 720) -> list[tuple[int, Path]]:
    """Extract frames with ffmpeg. Return [(second, image path)]."""
    out_dir.mkdir(parents=True, exist_ok=True)
    step = 1.0 / fps
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video),
            # Keep enough height for OCR to read small text; -2 keeps the width even
            "-vf", f"fps={fps},scale=-2:'min({height},ih)'",
            "-q:v", "2",
            str(out_dir / "%08d.jpg"),
        ],
        check=True,
    )
    images = sorted(out_dir.glob("*.jpg"))
    # Frame n (numbered from 1) corresponds to time (n-1)/fps
    return [(int(round(i * step)), p) for i, p in enumerate(images)]


def drop_duplicate_frames(frames: list[tuple[int, Path]], threshold: int = HASH_THRESHOLD) -> list[tuple[int, Path]]:
    """Drop frames that look almost the same as the previously kept frame."""
    kept: list[tuple[int, Path]] = []
    prev_hash = None
    prev_sec = None
    for sec, path in frames:
        h = _dhash(path)
        changed = (
            prev_hash is None
            or int(np.count_nonzero(h != prev_hash)) >= threshold
            or sec - prev_sec >= FORCE_KEEP_SECONDS
        )
        if changed:
            kept.append((sec, path))
            prev_hash = h
            prev_sec = sec
    return kept


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _fold(s: str) -> str:
    """Lower-case and strip diacritics, for fuzzy comparison only."""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower()


def _similar(a: str, b: str) -> bool:
    """Two pieces of text count as the same if most of their words overlap."""
    ta, tb = set(_fold(a).split()), set(_fold(b).split())
    if not ta and not tb:
        return True
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.85


def group_by_text(
    results: list[tuple[int, str]], last_end_sec: int, min_chars: int = 3
) -> list[dict]:
    """Merge consecutive frames with the same text into one row."""
    rows: list[dict] = []
    for sec, text in results:
        text = _normalize(text)
        if len(text) < min_chars:
            continue
        if rows and _similar(rows[-1]["text"], text):
            rows[-1]["end_sec"] = sec
            # Keep the longer reading: OCR sometimes misses words on some frames
            if len(text) > len(rows[-1]["text"]):
                rows[-1]["text"] = text
            continue
        if rows:
            rows[-1]["end_sec"] = max(rows[-1]["end_sec"], sec)
        rows.append({"start_sec": sec, "end_sec": sec, "text": text})
    if rows:
        rows[-1]["end_sec"] = max(rows[-1]["end_sec"], last_end_sec)
    return rows


# Common top-level domains. A fixed list instead of \w+ cuts false positives from noisy OCR.
_TLD = (
    "com|vn|net|org|io|co|me|shop|store|edu|gov|info|biz|app|dev|xyz|top|site|"
    "online|live|tv|cc|asia|id|ph|th|my|sg|us|uk|jp|kr|cn|ru|de|fr|it|es|au|ca"
)
_URL_RE = re.compile(
    r"(?:(?:https?://|www\.)[^\s<>\"']+"
    r"|(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:" + _TLD + r")(?![a-z])(?:/[^\s<>\"']*)?)",
    re.IGNORECASE,
)


def find_urls(text: str) -> list[str]:
    """Return the URLs / domain names found in a piece of text."""
    out: list[str] = []
    for m in _URL_RE.finditer(text.replace("，", ",")):
        u = m.group(0).rstrip(".,;:!?)]}’\"'")
        # Skip strings made only of digits and dots (usually a price such as 990.000)
        if re.fullmatch(r"[\d.,/]+", u):
            continue
        if u.lower() not in [x.lower() for x in out]:
            out.append(u)
    return out


def ocr_frames(
    frames: list[tuple[int, Path]], engine: str = "auto", language: str = "vi"
) -> list[tuple[int, str]]:
    """OCR each frame. Return [(second, text)]."""
    read = _make_ocr(engine, language)
    out = []
    for i, (sec, path) in enumerate(frames, 1):
        try:
            out.append((sec, read(path)))
        except Exception as e:  # one bad frame should not stop the whole video
            print(f"  ! OCR failed at second {sec}: {e}")
        if i % 50 == 0 or i == len(frames):
            print(f"  OCR {i}/{len(frames)} frames")
    return out


def _make_ocr(engine: str, language: str):
    """Return a function that takes an image path and returns its text."""
    if engine in ("auto", "paddleocr"):
        try:
            from paddleocr import PaddleOCR  # type: ignore

            ocr = PaddleOCR(lang=language)
            print("OCR: PaddleOCR")

            def _read(path: Path) -> str:
                result = ocr.predict(str(path))
                parts = []
                for page in result or []:
                    # PP-OCRv5 returns a dict with rec_texts; older versions return [[box, (text, score)]]
                    if isinstance(page, dict):
                        parts += list(page.get("rec_texts") or [])
                    else:
                        for d in page or []:
                            if isinstance(d, (list, tuple)) and len(d) >= 2:
                                t = d[1]
                                parts.append(t[0] if isinstance(t, (list, tuple)) else str(t))
                return " ".join(x for x in parts if x)

            return _read
        except Exception as e:
            if engine == "paddleocr":
                raise
            print(f"PaddleOCR not available ({e}); falling back to EasyOCR.")

    if engine in ("auto", "easyocr"):
        import easyocr  # type: ignore

        reader = easyocr.Reader([language], gpu=True)
        print("OCR: EasyOCR")

        def _read(path: Path) -> str:
            return " ".join(reader.readtext(str(path), detail=0) or [])

        return _read

    raise ValueError(f"Invalid OCR engine: {engine}")


def process(
    video: Path, end_sec: int, engine: str = "auto", language: str = "vi",
    fps: float = 1.0, threshold: int = HASH_THRESHOLD,
) -> tuple[list[dict], list[dict]]:
    """Run the whole chain: frames -> de-duplicate -> OCR -> group -> find URLs."""
    tmp = Path(tempfile.mkdtemp(prefix="frames_"))
    try:
        frames = extract_frames(video, tmp, fps=fps)
        print(f"Extracted {len(frames)} frames.")
        kept = drop_duplicate_frames(frames, threshold)
        print(f"{len(kept)} frames left after dropping near-duplicates.")
        results = ocr_frames(kept, engine=engine, language=language)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    rows = group_by_text(results, end_sec)

    links: list[dict] = []
    last_seen: dict[str, int] = {}
    for row in rows:
        for u in find_urls(row["text"]):
            key = u.lower()
            if key in last_seen and row["start_sec"] - last_seen[key] < 60:
                continue  # same link shown continuously: record it once
            last_seen[key] = row["start_sec"]
            links.append({
                "at_sec": row["start_sec"],
                "url": u,
                "source": "screen",
                "context": row["text"][:300],
            })
    return rows, links
