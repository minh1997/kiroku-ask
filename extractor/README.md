# kiroku-ask extractor: ingest video files on your machine

Runs **on your own machine** (PC with an NVIDIA GPU or Apple Silicon Mac), not in Docker, so it can use the GPU. It reads a video file and writes the results as text to the Postgres of the n8n stack — usually on a VPS, reached through an SSH tunnel. The video file never passes through the VPS.

Each video becomes 4 kinds of data, all timestamped:

| Step | How | Stored in |
|---|---|---|
| Transcript | Local Whisper, or Gemini | `livestreams.transcript` |
| Summary | Gemini, 20-minute sections then merged | `livestreams.summary`, `section_summaries` |
| On-screen text | ffmpeg + local OCR | `screen_text`, `video_links` |
| Scene log | Gemini watches the uploaded file, 10-minute chunks | `video_scenes`, `video_links` |

## 1. Prepare the database

If Postgres was already running before, add the new tables (run once):

```bash
cd ..   # back to the kiroku-ask folder
docker compose exec postgres sh -c \
  'psql -U "$POSTGRES_USER" -d livestream -f /docker-entrypoint-initdb.d/03-local-video.sql'
```

## 2. Install

You need **ffmpeg** and **Python 3.10+**.

**PC with an NVIDIA GPU**

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-nvidia.txt
```

PaddleOCR needs `paddlepaddle` or `paddlepaddle-gpu` installed separately to match your CUDA version — see https://www.paddlepaddle.org.cn/install/quick. If it will not install, use EasyOCR: `pip install easyocr` and run with `--ocr easyocr`.

**Apple Silicon Mac**

```bash
brew install ffmpeg
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-mac.txt
```

Libraries are imported lazily: you only need the ones for the backends you actually use.

## 3. Connect to Postgres and set environment variables

**Postgres on a VPS** (the usual case): open an SSH tunnel in a separate terminal and leave it running while you ingest:

```bash
ssh -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes \
    -L 5433:127.0.0.1:5432 user@VPS_IP
```

Then, in the terminal where you run the tool:

```bash
export GEMINI_API_KEY='...'
export PG_DSN='postgresql://livestream_app:PASSWORD@127.0.0.1:5433/livestream'
# gemini-3.8-flash is the default; change the model if needed:
# export GEMINI_MODEL='gemini-3.8-flash'
```

- `PASSWORD` is `LIVESTREAM_APP_PASSWORD` from the `.env` file on the VPS. If it contains special characters (`@`, `:`, `/`, `#`…) they must be URL-encoded, e.g. `@` → `%40`.
- **Postgres on this same machine** (local docker compose): use port `5432` instead of `5433`, no tunnel needed.
- Other options (Tailscale/WireGuard, an exposed port with an allowlist): see *Running on a VPS* in the main README.

The tool adds TCP keepalive settings to `PG_DSN` (`keepalives`, `keepalives_idle=30`, `keepalives_interval=10`, `keepalives_count=3`, `connect_timeout=15`) so a dead connection is detected in about a minute instead of hanging. Parameters you set yourself in `PG_DSN` are left untouched.

**Tunnel dropped halfway?** Each step (transcript, OCR, scene log) finishes completely before it opens a Postgres connection, writes in a single transaction, commits and closes — no connection is held while OCR or an upload runs for tens of minutes. If the connection is lost while writing, the tool retries 3 times (waiting 10 s, then 20 s); just reopen the tunnel. Steps that were already written stay as they are; to run a missing step, use the `none` flags in section 4.

## 4. Run

```bash
# PC with an NVIDIA GPU
python ingest_video.py ~/videos/live-10-11.mp4 --date 2026-11-10 --title "Live 10/11"

# Apple Silicon Mac (mlx-whisper is picked automatically)
python ingest_video.py ~/videos/live-10-11.mp4

# Whisper will not install: let Gemini transcribe
python ingest_video.py video.mp4 --asr gemini
```

Without `--date` the file's modification date is used. Without `--title` the file name is used.

Running the same file again **updates** the existing row instead of creating a duplicate. You can also re-run a single step:

```bash
python ingest_video.py video.mp4 --asr none --ocr none --visual gemini   # redo only the scene log
python ingest_video.py video.mp4 --asr none --visual none                # redo only OCR
```

## 5. Notable options

| Option | Default | Notes |
|---|---|---|
| `--asr` | `auto` | `mlx-whisper` on Apple Silicon, `faster-whisper` elsewhere. `gemini` if Whisper will not install, `none` to skip |
| `--whisper-model` | `kiendt/PhoWhisper-large-ct2` (faster-whisper), `mlx-community/whisper-large-v3-mlx` (mlx-whisper) | PhoWhisper is VinAI's Whisper fine-tuned for Vietnamese. `mlx-community/whisper-large-v3-turbo` is several times faster on a Mac. |
| `--ocr` | `auto` | Tries PaddleOCR first, falls back to EasyOCR |
| `--language` | `vi` | Language code for ASR and OCR |
| `--resolution` | `high` | `high` lets Gemini read links and small text; `low` is about 3x cheaper |
| `--fps` | `1` | Frames per second fed to OCR |
| `--hash-threshold` | `6` | Lower = keep more frames = slower OCR but fewer misses |
| `--scene-chunk-seconds` | `600` | Length of each chunk sent to Gemini. Lower to `300` on MAX_TOKENS errors. |
| `--no-summary` | | Skip the summary step |

CTranslate2 (the engine behind faster-whisper) has no Metal backend, so on a Mac faster-whisper only runs on the CPU — that is why `mlx-whisper` exists for Apple Silicon.

## 6. Local transcription service for n8n (workflow 4)

`transcribe_server.py` lets n8n use the Whisper on this machine. n8n runs in Docker, where a Mac's GPU is not available, so the service runs natively and n8n calls it over HTTP.

```bash
source .venv/bin/activate
python transcribe_server.py                    # listens on 127.0.0.1:8765
python transcribe_server.py --whisper-model mlx-community/whisper-large-v3-turbo   # faster on a Mac
```

Keep it running while you use workflow 4. It needs no database access and no API key: it takes a YouTube link (downloads only the audio with yt-dlp) or a file path on this machine, runs Whisper, and returns the segments; n8n does the summary and writes to Postgres. Jobs run one at a time and are kept in memory only, so restarting the service forgets them.

- The first job downloads the Whisper model (about 3 GB for large-v3).
- n8n in Docker Desktop (Mac/Windows) reaches the service at `http://host.docker.internal:8765`. On Linux, `docker-compose.yml` maps that name to the host; start the service with `--host 172.17.0.1` (the Docker bridge address) so containers can reach it, and never with `0.0.0.0` on a machine exposed to the Internet.
- Check it: `curl localhost:8765/health`.

| Option | Default | Notes |
|---|---|---|
| `--asr` | `auto` | `mlx-whisper` on Apple Silicon, `faster-whisper` elsewhere |
| `--whisper-model` | see `--whisper-model` above | |
| `--host`, `--port` | `127.0.0.1`, `8765` | If you change the port, also change `LOCAL_ASR_URL` in the *Prepare input* node of workflow 4 |

## 7. How the OCR step works

A 2-hour video at 1 frame per second gives 7,200 images, but most of them are near-identical. The tool:

1. Extracts frames with ffmpeg.
2. Computes a **dHash** for each frame and drops frames that look almost the same as the previous kept one (while still keeping at least one frame every 30 seconds).
3. OCRs the remaining frames.
4. Merges consecutive frames with the same text into one `start_sec`–`end_sec` row.
5. Extracts URLs with a regex over a list of domain suffixes, skipping strings made only of digits and dots (so a price like `990.000` is not mistaken for a link).

Step 2 only saves time; step 4 decides the final result, so a low `--hash-threshold` still gives correct output, just more slowly.

## 8. Notes

- Gemini can only process **public videos** when given a YouTube link, but an **uploaded file** has no such restriction — which is why the file path works for private videos too.
- Files uploaded to Gemini are kept for 48 hours and then deleted automatically; the tool also deletes them as soon as it is done.
- Very long videos are best split first, since the upload can take a long time.
- Gemini charges for the amount of video watched, paid **once** at ingest time. Q&A afterwards only reads Postgres.
