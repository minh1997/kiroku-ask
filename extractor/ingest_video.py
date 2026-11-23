#!/usr/bin/env python3
"""kiroku-ask: ingest a video file into the livestream database so the n8n agent can answer questions about it.

Examples:
  export GEMINI_API_KEY=...
  export PG_DSN='postgresql://livestream_app:PASSWORD@127.0.0.1:5433/livestream'

  # PC with an NVIDIA GPU
  python ingest_video.py video.mp4 --date 2026-11-10 --title "Live 10/11"

  # Apple Silicon Mac (mlx-whisper is picked automatically)
  python ingest_video.py video.mp4

  # Re-run a single step only (the other steps stay as they are in the database)
  python ingest_video.py video.mp4 --asr none --ocr none --visual gemini
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import tempfile
from pathlib import Path

import asr
import db
import frames
import gemini
import util


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="kiroku-ask: ingest a video file for the n8n Q&A agent")
    p.add_argument("video", type=Path, help="Path to the video file")
    p.add_argument("--date", help="Date of the video, YYYY-MM-DD (default: file modification date)")
    p.add_argument("--title", help="Title (default: file name)")
    p.add_argument("--video-id", help="ID in the database (default: derived from the file name)")
    p.add_argument("--language", default="vi", help="Language code for ASR and OCR (default: vi)")

    p.add_argument("--asr", default="auto",
                   choices=["auto", "faster-whisper", "mlx-whisper", "gemini", "none"],
                   help="Speech-to-text backend. auto = mlx-whisper on Apple Silicon, "
                        "faster-whisper elsewhere; none to skip.")
    p.add_argument("--whisper-model", help="Whisper model name (default: see asr.DEFAULT_MODELS)")
    p.add_argument("--ocr", default="auto", choices=["auto", "paddleocr", "easyocr", "none"],
                   help="OCR engine for on-screen text")
    p.add_argument("--visual", default="gemini", choices=["gemini", "none"],
                   help="Write the visual scene log with Gemini")
    p.add_argument("--no-summary", dest="summary", action="store_false",
                   help="Skip summarizing the transcript with Gemini")

    p.add_argument("--fps", type=float, default=1.0, help="Frames per second to OCR")
    p.add_argument("--hash-threshold", type=int, default=frames.HASH_THRESHOLD,
                   help="Bits that must differ for a frame to count as changed (lower = keep more frames)")
    p.add_argument("--scene-chunk-seconds", type=int, default=600,
                   help="Length of each chunk sent to Gemini, in seconds. Lower it on MAX_TOKENS errors.")
    p.add_argument("--resolution", default="high", choices=["high", "low"],
                   help="Resolution Gemini watches the video at. 'high' reads small text and links.")
    args = p.parse_args(argv)

    video: Path = args.video.expanduser()
    if not video.is_file():
        print(f"File not found: {video}", file=sys.stderr)
        return 1

    if args.asr == "auto":
        args.asr = asr.default_backend()

    video_id = args.video_id or util.make_video_id(video)
    stream_date = args.date or dt.datetime.fromtimestamp(video.stat().st_mtime).strftime("%Y-%m-%d")
    try:
        dt.date.fromisoformat(stream_date)
    except ValueError:
        print(f"Invalid date: {stream_date} (expected YYYY-MM-DD)", file=sys.stderr)
        return 1
    title = args.title or video.stem
    total_seconds = util.probe_duration(video)

    print(f"Video    : {video}")
    print(f"ID       : {video_id}")
    print(f"Date     : {stream_date}")
    print(f"Duration : {total_seconds}s (~{round(total_seconds / 60)} min)\n")

    # Each step: do the work first (may take tens of minutes), then open a Postgres
    # connection, write + commit + close right away. No connection is held over the
    # SSH tunnel while waiting.
    db.write_step("video info", lambda conn: db.upsert_video(conn, {
        "video_id": video_id, "stream_date": stream_date, "title": title,
        "url": None, "file_path": str(video.resolve()),
        "duration_seconds": total_seconds, "language": args.language,
    }))
    print("[1/4] Saved video info.")

    # ---------------------------------------------------- Transcript
    if args.asr != "none":
        print(f"\n[2/4] Transcribing ({args.asr})...")
        with tempfile.TemporaryDirectory(prefix="audio_") as tmp:
            wav = asr.extract_audio(video, Path(tmp) / "audio.wav")
            if args.asr == "gemini":
                chunks = asr.split_audio(wav, Path(tmp) / "chunks", 600, total_seconds)
                segments = gemini.transcribe(chunks)
            else:
                segments = asr.transcribe(wav, args.asr, args.whisper_model, args.language)

        transcript = util.build_transcript(segments)
        if not transcript.strip():
            print("  ! No speech was transcribed.")
        else:
            print(f"  {len(transcript.splitlines())} transcript lines.")
            summary, section_summaries = "", []
            if args.summary:
                print("  Summarizing...")
                summary, section_summaries = gemini.summarize(
                    util.split_into_sections(transcript, 1200), title, stream_date, total_seconds
                )
            db.write_step("transcript", lambda conn: db.save_transcript(
                conn, video_id, transcript, summary, section_summaries))
            print("  Saved transcript" + (" and summary." if args.summary else "."))
    else:
        print("\n[2/4] Skipping transcript.")

    # ---------------------------------------------------- On-screen text (OCR)
    if args.ocr != "none":
        print(f"\n[3/4] Reading on-screen text (OCR: {args.ocr})...")
        text_rows, ocr_links = frames.process(
            video, total_seconds, engine=args.ocr, language=args.language,
            fps=args.fps, threshold=args.hash_threshold,
        )
        db.write_step("on-screen text", lambda conn: db.save_screen_text(
            conn, video_id, text_rows, ocr_links))
        print(f"  Saved {len(text_rows)} text rows and {len(ocr_links)} links.")
    else:
        print("\n[3/4] Skipping OCR.")

    # ---------------------------------------------------- Visual scene log
    if args.visual != "none":
        print("\n[4/4] Writing the visual scene log (Gemini)...")
        uri, mime = gemini.upload(video)
        try:
            scenes, gemini_links = gemini.scene_log(
                uri, mime, total_seconds, title,
                chunk_seconds=args.scene_chunk_seconds,
                resolution=("MEDIA_RESOLUTION_HIGH" if args.resolution == "high"
                            else "MEDIA_RESOLUTION_LOW"),
            )
        finally:
            gemini.delete_file(uri)
        db.write_step("scene log", lambda conn: db.save_scenes(conn, video_id, scenes, gemini_links))
        print(f"  Saved {len(scenes)} scenes and {len(gemini_links)} links.")
    else:
        print("\n[4/4] Skipping visual scene log.")

    print(f"\nDone. Ask the n8n agent about \"the video from {stream_date}\" or \"{title}\".")
    return 0


if __name__ == "__main__":
    sys.exit(main())
