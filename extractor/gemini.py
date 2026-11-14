"""Gemini API calls: file upload, scene log, transcription, summaries."""

from __future__ import annotations

import json
import mimetypes
import os
import time
from pathlib import Path

import requests

BASE_URL = "https://generativelanguage.googleapis.com"
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
# Language Gemini writes descriptions, keywords and summaries in. The n8n agent searches
# this text with keywords in the same language, so keep it consistent across videos.
OUTPUT_LANGUAGE = "Vietnamese"


def _key() -> str:
    k = os.environ.get("GEMINI_API_KEY")
    if not k:
        raise RuntimeError("GEMINI_API_KEY environment variable is not set.")
    return k


# --------------------------------------------------------------- File API
# mimetypes guesses .m4a as "audio/mp4a-latm", which Gemini rejects, so pin common extensions
_MIME = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".webm": "video/webm",
    ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav",
}


def upload(path: Path) -> tuple[str, str]:
    """Resumable upload. Return (fileUri, mimeType) for use in generateContent."""
    size = path.stat().st_size
    mime = _MIME.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "video/mp4"

    r = requests.post(
        f"{BASE_URL}/upload/v1beta/files",
        headers={
            "x-goog-api-key": _key(),
            "Content-Type": "application/json",
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": mime,
        },
        json={"file": {"display_name": path.name}},
        timeout=120,
    )
    r.raise_for_status()
    url = r.headers.get("x-goog-upload-url") or r.headers.get("X-Goog-Upload-URL")
    if not url:
        raise RuntimeError("Gemini did not return an upload URL.")

    print(f"  uploading {size / 1e6:.0f} MB...")
    with path.open("rb") as f:
        r = requests.post(
            url,
            headers={
                "Content-Length": str(size),
                "X-Goog-Upload-Offset": "0",
                "X-Goog-Upload-Command": "upload, finalize",
            },
            data=f,
            timeout=3600,
        )
    r.raise_for_status()
    info = r.json().get("file", {})
    name, uri = info.get("name"), info.get("uri")
    if not uri:
        raise RuntimeError(f"Upload returned no uri: {r.text[:300]}")

    # Gemini has to process the file before it can be used
    for _ in range(360):
        if info.get("state") == "ACTIVE":
            print("  file is ready.")
            return uri, mime
        if info.get("state") == "FAILED":
            raise RuntimeError(f"Gemini failed to process the file: {info.get('error')}")
        time.sleep(5)
        g = requests.get(f"{BASE_URL}/v1beta/{name}", headers={"x-goog-api-key": _key()}, timeout=60)
        g.raise_for_status()
        info = g.json()
    raise RuntimeError("File still PROCESSING after 30 minutes.")


def delete_file(uri: str) -> None:
    """Delete an uploaded file (Gemini also deletes it automatically after 48 hours)."""
    try:
        name = "files/" + uri.rstrip("/").split("/")[-1]
        requests.delete(f"{BASE_URL}/v1beta/{name}", headers={"x-goog-api-key": _key()}, timeout=60)
    except Exception:
        pass


# --------------------------------------------------------------- generateContent
def call(body: dict, attempts: int = 3) -> dict:
    error = None
    for attempt in range(attempts):
        try:
            r = requests.post(
                f"{BASE_URL}/v1beta/models/{MODEL}:generateContent",
                headers={"x-goog-api-key": _key(), "Content-Type": "application/json"},
                json=body, timeout=900,
            )
            if r.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            r.raise_for_status()
            return r.json()
        except Exception as e:
            error = e
            if attempt < attempts - 1:
                wait = 5 * (attempt + 1)
                print(f"  ! error ({e}); retrying in {wait}s")
                time.sleep(wait)
    raise RuntimeError(f"Gemini call failed: {error}")


def _response_text(res: dict, label: str) -> str:
    cand = (res.get("candidates") or [{}])[0]
    parts = ((cand.get("content") or {}).get("parts") or [])
    # Skip "thought" parts, otherwise JSON parsing fails
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text.strip():
        raise RuntimeError(
            f"{label}: Gemini returned nothing "
            f"(finishReason={cand.get('finishReason')}, {res.get('promptFeedback')})"
        )
    return text


def _response_json(res: dict, label: str) -> dict:
    text = _response_text(res, label)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        cand = (res.get("candidates") or [{}])[0]
        raise RuntimeError(
            f"{label}: invalid JSON (finishReason={cand.get('finishReason')}). "
            "If it is MAX_TOKENS, lower --scene-chunk-seconds."
        )


def _video_part(uri: str, mime: str, start: int, end: int, fps: float) -> dict:
    return {
        "fileData": {"fileUri": uri, "mimeType": mime},
        "videoMetadata": {"startOffset": f"{start}s", "endOffset": f"{end}s", "fps": fps},
    }


# --------------------------------------------------------------- Scene log
SCENE_SCHEMA = {
    "type": "object",
    "properties": {
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_sec": {"type": "integer"},
                    "end_sec": {"type": "integer"},
                    "description": {"type": "string"},
                    "on_screen_text": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["start_sec", "end_sec", "description", "on_screen_text", "keywords"],
            },
        },
        "links": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "at_sec": {"type": "integer"},
                    "url": {"type": "string"},
                    "source": {"type": "string", "enum": ["screen", "speech"]},
                    "context": {"type": "string"},
                },
                "required": ["at_sec", "url", "source", "context"],
            },
        },
    },
    "required": ["scenes", "links"],
}


def _scene_prompt(length: int, title: str) -> str:
    return f"""You are watching ONE CHUNK, {length} seconds long, of the video "{title}".
Task: write a detailed visual log so that questions like "when does the red shirt appear", "what product is she holding", "where is this filmed", "how many people are there" or "when does the pricing slide show up" can be answered later.

TIME: every timestamp is the NUMBER OF SECONDS FROM THE START OF THIS CHUNK (start of chunk = 0, end of chunk = {length}).

1) scenes: split the chunk into consecutive scenes that cover 0 to {length} without gaps.
   Start a new scene on every visible change: slide change, a web page opened or switched, a product picked up or put down, a change of clothes, a new person, a camera angle change, screen sharing starting or stopping.
   Merge long static stretches into one scene, but no scene may be longer than 60 seconds.
   - description: in {OUTPUT_LANGUAGE}, specific: how many people and who (gender, appearance), clothing WITH COLOURS, objects/products being held with colours, actions, surroundings, what the shared screen shows.
   - on_screen_text: copy legible text verbatim (slide titles, prices, discount codes, product names, URL in the address bar). Empty string if none.
   - keywords: 3–10 short phrases in {OUTPUT_LANGUAGE}, e.g. the {OUTPUT_LANGUAGE} words for "red shirt", "price list", "slide", "browser", "phone".

2) links: every link, URL or domain name:
   - source "screen": seen in the browser address bar, on a slide, in an image, in a pinned comment. Add one entry each time a NEW web page is opened or navigated to.
   - source "speech": the speaker reads out a link or domain name.
   - url: copy exactly as seen/heard; if it is not fully legible, write the legible part.
   - context: what the page is and why it was opened.

Only record what is actually seen or heard. Do not guess, do not make things up."""


def scene_log(
    uri: str, mime: str, total_seconds: int, title: str,
    chunk_seconds: int = 600, resolution: str = "MEDIA_RESOLUTION_HIGH", fps: float = 1.0,
) -> tuple[list[dict], list[dict]]:
    """Have Gemini watch the video chunk by chunk. Return (scenes, links)."""
    scenes: list[dict] = []
    links: list[dict] = []
    starts = list(range(0, total_seconds, chunk_seconds))

    for i, start in enumerate(starts, 1):
        end = min(start + chunk_seconds, total_seconds)
        print(f"  chunk {i}/{len(starts)} ({start}s–{end}s)")
        res = call({
            "contents": [{"role": "user", "parts": [
                _video_part(uri, mime, start, end, fps),
                {"text": _scene_prompt(end - start, title)},
            ]}],
            "generationConfig": {
                "mediaResolution": resolution,
                "responseMimeType": "application/json",
                "responseJsonSchema": SCENE_SCHEMA,
                "thinkingConfig": {"thinkingLevel": "LOW"},
                "maxOutputTokens": 32768,
            },
        })
        data = _response_json(res, f"Chunk {i}")
        s, l = _to_video_time(data, start, end)
        scenes += s
        links += l

    scenes.sort(key=lambda x: x["start_sec"])
    links.sort(key=lambda x: x["at_sec"])
    return scenes, links


def _to_video_time(data: dict, start: int, end: int) -> tuple[list[dict], list[dict]]:
    """Convert chunk-relative seconds to seconds in the original video."""
    raw_scenes = data.get("scenes") or []
    raw_links = data.get("links") or []

    def _int(v):
        try:
            return int(round(float(v)))
        except (TypeError, ValueError):
            return None

    # The prompt asks for chunk-relative times; if the model used video time anyway, don't add the offset
    length = end - start
    stamps = [_int(s.get("start_sec")) for s in raw_scenes] + [_int(s.get("end_sec")) for s in raw_scenes]
    stamps += [_int(l.get("at_sec")) for l in raw_links]
    stamps = [t for t in stamps if t is not None]
    offset = start
    if start > 0 and stamps and max(stamps) > length + 5 and min(stamps) >= start - 5:
        offset = 0

    def convert(v):
        return min(end, max(start, (_int(v) or 0) + offset))

    scenes = []
    for s in raw_scenes:
        description = str(s.get("description") or "").strip()
        if not description:
            continue
        a = convert(s.get("start_sec"))
        scenes.append({
            "start_sec": a,
            "end_sec": max(a, convert(s.get("end_sec"))),
            "description": description,
            "on_screen_text": str(s.get("on_screen_text") or "").strip(),
            "keywords": ", ".join(
                x for x in (str(y).strip() for y in (s.get("keywords") or [])) if x
            ),
        })

    links = []
    for l in raw_links:
        u = str(l.get("url") or "").strip()
        if not u:
            continue
        links.append({
            "at_sec": convert(l.get("at_sec")),
            "url": u,
            "source": "speech" if l.get("source") == "speech" else "screen",
            "context": str(l.get("context") or "").strip(),
        })
    return scenes, links


# --------------------------------------------------------------- Transcription
TRANSCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "at_sec": {"type": "number"},
                    "text": {"type": "string"},
                },
                "required": ["at_sec", "text"],
            },
        }
    },
    "required": ["segments"],
}


def transcribe(audio_chunks: list[tuple[int, Path]], language: str = OUTPUT_LANGUAGE) -> list[dict]:
    """Transcribe each audio chunk. Return [{start, text}] in seconds of the original file."""
    out: list[dict] = []
    for i, (start, f) in enumerate(audio_chunks, 1):
        print(f"  chunk {i}/{len(audio_chunks)} (from {start}s)")
        uri, mime = upload(f)
        try:
            res = call({
                "contents": [{"role": "user", "parts": [
                    {"fileData": {"fileUri": uri, "mimeType": mime}},
                    {"text": f"Transcribe the speech in this audio verbatim ({language}). "
                             "Split it into short segments of 10–20 seconds, each with its start time in "
                             "seconds COUNTED FROM THE START OF THIS AUDIO CLIP. Keep the speaker's exact "
                             "words and add punctuation for readability. Do not add commentary or anything "
                             "not in the audio. Skip silence and music-only stretches."},
                ]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseJsonSchema": TRANSCRIPT_SCHEMA,
                    "thinkingConfig": {"thinkingLevel": "LOW"},
                    "maxOutputTokens": 32768,
                },
            })
            data = _response_json(res, f"Audio chunk {i}")
        finally:
            delete_file(uri)
        for seg in data.get("segments") or []:
            t = str(seg.get("text") or "").strip()
            if t:
                out.append({"start": start + float(seg.get("at_sec") or 0), "text": t})
    out.sort(key=lambda x: x["start"])
    return out


# --------------------------------------------------------------- Summaries
NO_CONTENT = "(No significant content)"


def _generate_text(prompt: str, label: str) -> str:
    res = call({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"thinkingConfig": {"thinkingLevel": "LOW"}},
    })
    return _response_text(res, label).strip()


def summarize(sections: list[dict], title: str, stream_date: str, total_seconds: int) -> tuple[str, list[dict]]:
    """Summarize each transcript section, then merge. Return (overall summary, section summaries)."""
    from util import hms

    section_summaries = []
    for s in sections:
        print(f"  summarizing section {s['section']}/{len(sections)}")
        prompt = f"""You are summarizing SECTION {s['section']}/{len(sections)} (from {hms(s['start_sec'])} to {hms(s['end_sec'])}) of the video "{title}" from {stream_date}.

Requirements:
- Write in {OUTPUT_LANGUAGE}, as bullet points.
- Keep only valuable content: knowledge, facts, numbers, prices, decisions, announcements, important questions and their answers.
- Skip greetings, reading out viewer names, asking for likes/shares, waiting, repetition.
- Give each point the nearest [hh:mm:ss] timestamp from the transcript.
- The transcript was produced by AI, so spelling and proper names may be wrong: interpret from context, do not invent anything.
- If this section has no significant content, write only: "{NO_CONTENT}".

Transcript:
{s['content']}"""
        section_summaries.append({
            "section": s["section"],
            "start": hms(s["start_sec"]),
            "end": hms(s["end_sec"]),
            "summary": _generate_text(prompt, f"Section summary {s['section']}"),
        })

    sections_text = "\n\n".join(
        f"### Section {d['section']} ({d['start']} – {d['end']})\n{d['summary']}" for d in section_summaries
    )
    print("  overall summary")
    overall = _generate_text(
        f"""Below are the section summaries of the video "{title}" from {stream_date} (about {round(total_seconds / 60)} minutes long).

Write an overall summary in {OUTPUT_LANGUAGE} with this structure:
1. **Overview** (2–4 sentences): what the video is about.
2. **Main topics**: grouped by topic, each point keeps its [hh:mm:ss] timestamp.
3. **Key information**: numbers, prices, dates, announcements, commitments, links mentioned (if any).
4. **Notable Q&A** (if any).

Do not add anything that is not in the section summaries. Ignore sections marked "{NO_CONTENT}".
Translate the headings above into {OUTPUT_LANGUAGE}.

{sections_text}""",
        "Overall summary",
    )
    return overall, section_summaries
