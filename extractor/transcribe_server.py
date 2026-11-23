#!/usr/bin/env python3
"""kiroku-ask local transcription service, called by n8n workflow 4.

Runs natively on your machine (not in Docker) so Whisper can use the GPU: Metal on an
Apple Silicon Mac via mlx-whisper, CUDA on an NVIDIA PC via faster-whisper. n8n, inside
Docker, reaches it at http://host.docker.internal:8765.

    python transcribe_server.py                  # backend picked automatically
    python transcribe_server.py --whisper-model mlx-community/whisper-large-v3-turbo

API (JSON):
    GET  /health      -> {"status": "ok", "backend": ..., "model": ..., "queued": n}
    POST /jobs        {"source": "<YouTube URL or file path>", "language": "vi"}
                      -> 202 {"job_id": ..., "status": "queued"}
    GET  /jobs/<id>   -> {"job_id", "status": queued|active|completed|failed, "error",
                          "lang", "content": [{"text", "offset", "duration"}], "video": {...}}

"content" uses the same shape as the Supadata transcript API (offset/duration in
milliseconds), so the workflow handles both sources with the same code. Jobs run one at a
time (a single GPU) and live in memory: restarting the service forgets them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import queue
import re
import sys
import tempfile
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import asr
import download
import util

KEEP_FINISHED_JOBS = 50


class JobStore:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.queue: queue.Queue[str] = queue.Queue()

    def add(self, source: str, language: str) -> dict:
        job = {
            "job_id": uuid.uuid4().hex,
            "status": "queued",
            "source": source,
            "language": language,
            "created_at": time.time(),
            "error": None,
        }
        with self.lock:
            self.jobs[job["job_id"]] = job
            self._prune()
        self.queue.put(job["job_id"])
        return job

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return dict(job) if job else None

    def update(self, job_id: str, **fields) -> None:
        with self.lock:
            self.jobs[job_id].update(fields)

    def queued(self) -> int:
        with self.lock:
            return sum(1 for j in self.jobs.values() if j["status"] in ("queued", "active"))

    def _prune(self) -> None:
        finished = sorted(
            (j for j in self.jobs.values() if j["status"] in ("completed", "failed")),
            key=lambda j: j["created_at"],
        )
        for j in finished[:-KEEP_FINISHED_JOBS]:
            del self.jobs[j["job_id"]]


def resolve_source(source: str) -> tuple[str, str]:
    """Validate a job's source. Return ("youtube", url) or ("file", absolute path).

    Raises ValueError with a message meant for the n8n user.
    """
    source = source.strip()
    if re.match(r"^https?://", source, re.I):
        vid = download.youtube_id(source)
        if not vid:
            raise ValueError("Only YouTube links or file paths on this machine are supported: " + source)
        return "youtube", f"https://www.youtube.com/watch?v={vid}"
    path = Path(source).expanduser()
    if not path.is_absolute():
        raise ValueError(f"Use an absolute file path (e.g. ~/Movies/video.mp4), got: {source}")
    if not path.is_file():
        raise ValueError(f"File not found on the machine running the transcription service: {path}")
    return "file", str(path.resolve())


def run_job(job: dict, backend: str, model: str | None) -> dict:
    """Download (if needed), extract audio, transcribe. Return the job result fields."""
    kind, source = resolve_source(job["source"])
    with tempfile.TemporaryDirectory(prefix="kiroku_") as tmp:
        tmp_dir = Path(tmp)
        if kind == "youtube":
            print(f"[{job['job_id'][:8]}] downloading audio: {source}", flush=True)
            media, video = download.fetch_audio(source, tmp_dir)
            video.update({"source": "youtube", "file_path": None})
        else:
            media = Path(source)
            mtime = dt.datetime.fromtimestamp(media.stat().st_mtime)
            video = {
                "video_id": util.make_video_id(media),
                "url": None,
                "title": media.stem,
                "started_at": None,
                "upload_date": mtime.strftime("%Y-%m-%d"),
                "duration_seconds": None,
                "source": "file",
                "file_path": source,
            }

        if not video.get("duration_seconds"):
            video["duration_seconds"] = util.probe_duration(media)

        print(f"[{job['job_id'][:8]}] transcribing {video['duration_seconds']}s with {backend}", flush=True)
        wav = asr.extract_audio(media, tmp_dir / "audio.wav")
        segments = asr.transcribe(wav, backend, model, job["language"])

    content = []
    for seg in segments:
        text = re.sub(r"\s+", " ", str(seg.get("text") or "")).strip()
        if not text:
            continue
        start = float(seg.get("start") or 0)
        end = float(seg.get("end") or start)
        content.append({
            "text": text,
            "offset": int(round(start * 1000)),
            "duration": max(0, int(round((end - start) * 1000))),
        })
    if not content:
        raise ValueError("No speech was transcribed (silent audio or wrong language?).")
    return {"content": content, "lang": job["language"], "video": video,
            "transcriber": f"{backend}:{model or asr.DEFAULT_MODELS.get(backend)}"}


def worker(store: JobStore, backend: str, model: str | None) -> None:
    while True:
        job_id = store.queue.get()
        job = store.get(job_id)
        if not job:
            continue
        store.update(job_id, status="active", started_at=time.time())
        t0 = time.time()
        try:
            result = run_job(job, backend, model)
            store.update(job_id, status="completed", finished_at=time.time(), **result)
            print(f"[{job_id[:8]}] done: {len(result['content'])} segments "
                  f"in {time.time() - t0:.0f}s", flush=True)
        except Exception as e:  # report every failure to n8n instead of crashing the worker
            traceback.print_exc()
            store.update(job_id, status="failed", finished_at=time.time(),
                         error=f"{type(e).__name__}: {e}")


def make_handler(store: JobStore, backend: str, model: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "kiroku-transcribe/1"

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/health":
                return self._send(200, {
                    "status": "ok", "backend": backend,
                    "model": model or asr.DEFAULT_MODELS.get(backend),
                    "queued": store.queued(),
                })
            m = re.fullmatch(r"/jobs/([0-9a-f]{32})", self.path)
            if m:
                job = store.get(m[1])
                if not job:
                    return self._send(404, {"error": "Unknown job (was the service restarted?)"})
                return self._send(200, job)
            self._send(404, {"error": "Not found"})

        def do_POST(self) -> None:
            if self.path != "/jobs":
                return self._send(404, {"error": "Not found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                source = str(body.get("source") or "")
                language = str(body.get("language") or "vi")
                resolve_source(source)  # fail fast on a bad link or a missing file
            except (ValueError, json.JSONDecodeError) as e:
                return self._send(400, {"error": str(e)})
            job = store.add(source, language)
            print(f"[{job['job_id'][:8]}] queued: {source}", flush=True)
            self._send(202, {"job_id": job["job_id"], "status": job["status"]})

        def log_message(self, fmt, *args) -> None:  # keep the console readable while n8n polls
            if not self.path.startswith("/jobs/"):
                sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

    return Handler


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="kiroku-ask local transcription service for n8n")
    p.add_argument("--host", default="127.0.0.1",
                   help="Address to listen on (default 127.0.0.1; on Linux use the Docker bridge IP, "
                        "e.g. 172.17.0.1, so containers can reach it)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--asr", default="auto", choices=["auto", "faster-whisper", "mlx-whisper"],
                   help="auto = mlx-whisper on Apple Silicon, faster-whisper elsewhere")
    p.add_argument("--whisper-model", help="Whisper model name (default: see asr.DEFAULT_MODELS)")
    args = p.parse_args(argv)

    backend = asr.default_backend() if args.asr == "auto" else args.asr
    store = JobStore()
    threading.Thread(target=worker, args=(store, backend, args.whisper_model), daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(store, backend, args.whisper_model))
    print(f"Transcription service on http://{args.host}:{args.port} "
          f"(backend {backend}, model {args.whisper_model or asr.DEFAULT_MODELS.get(backend)})")
    print("n8n (Docker) reaches it at http://host.docker.internal:%d. Ctrl+C to stop." % args.port,
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
