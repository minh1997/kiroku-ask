# kiroku-ask

**Ask your video recordings anything.** *Kiroku* (記録) is Japanese for "record": kiroku-ask turns livestreams and video files into a timestamped record — what was said, what was shown on screen, which links were opened — and lets you chat with it.

- **Ask by date or by title:** "What was yesterday's stream about?", "In the lipstick video, when does the red shirt appear?", "What discount code was on the slide?"
- **Answers point to the moment:** every answer carries a `[hh:mm:ss]` timestamp, and YouTube videos get a link that jumps to that second.
- **Four layers per video:** transcript, summary, on-screen text (OCR) and a visual scene log, plus every link seen or spoken.
- **YouTube or local files:** public YouTube videos are ingested inside n8n; private recordings are processed on your own GPU machine, and only text is sent to the server.
- **Plain SQL, no vector database:** everything is stored in Postgres with timestamps; the AI agent reads it through read-only tools.

Built with n8n, Postgres, Gemini, Whisper and OCR, running in Docker.

```
kiroku-ask/
├── docker-compose.yml          # n8n + shared Postgres 16 (+ optional Caddy HTTPS for a VPS)
├── Caddyfile                   # HTTPS reverse proxy when running on a VPS
├── .env.example                # passwords and keys — copy to .env
├── init-db/
│   ├── 01-livestream.sh        # "livestream" database, 2 users, livestreams table
│   ├── 02-visual-log.sql       # video_scenes (scene log) + video_links
│   └── 03-local-video.sql      # screen_text (OCR) + columns for videos ingested from files
├── workflows/
│   ├── 1-ingest-and-summarize.json   # YouTube: link → transcript + summary
│   ├── 2-qa-chat.json                # Q&A chat (8 Postgres tools + segment re-watch)
│   └── 3-visual-analysis.json        # YouTube: Gemini watches the video → scene log + links
└── extractor/                  # RUNS ON YOUR MACHINE: ingest video files (NVIDIA GPU / Apple Silicon)
    ├── ingest_video.py         # one command: transcript + summary + OCR + scene log
    └── README.md               # per-platform setup
```

**Two ways to ingest, one shared database and one shared agent:**

| | Public YouTube video | Local video file |
|---|---|---|
| Tool | Workflows 1 + 3 in n8n | `extractor/ingest_video.py` |
| Transcript | YouTube captions via Supadata | Local Whisper, or Gemini |
| On-screen text | Read by Gemini (may get a few characters wrong) | **Local OCR** (character-exact) |
| Scene log | Gemini reads the YouTube link | Gemini reads the uploaded file |
| Works with private videos | No | Yes |
| Must wait for YouTube processing | Yes (~30–60 minutes after the stream ends) | No |

One Postgres container holds 2 databases:

| Database | Used for | User |
|---|---|---|
| `n8n` | n8n's own workflows, credentials and execution history | `POSTGRES_USER` in .env |
| `livestream` | `livestreams` (summary + transcript), `video_scenes` (scene log), `screen_text` (OCR text), `video_links` (links) | `livestream_app` (write), `livestream_reader` (read-only) |

The Q&A agent only uses the **read-only** user, so even if the model calls a tool wrongly it cannot modify or delete data.

**Split deployment (recommended):**

```
┌──────────── VPS (always on, no GPU) ───────────────┐        ┌─ Personal machine (GPU) ──┐
│  Caddy :443 ──► n8n 127.0.0.1:5678                  │        │  extractor/ingest_video.py│
│                  │                                  │  SSH   │  Whisper · OCR · ffmpeg   │
│                  ▼                                  │ tunnel │  uploads straight to      │
│             Postgres 127.0.0.1:5432  ◄──────────────┼────────┤  Gemini · 127.0.0.1:5433  │
└─────────────────────────────────────────────────────┘  text  └───────────────────────────┘
```

Video files **never pass through the VPS**: the personal machine processes them, uploads the video straight to Gemini (if used), and writes only text (transcript, summary, OCR text, scene log, links) to Postgres on the VPS. See [Running on a VPS](#running-on-a-vps).

The videos and users are Vietnamese, so summaries, scene descriptions and agent answers are produced in Vietnamese; prompts and code are in English.

---

## 1. Start

```bash
git clone https://github.com/minh1997/kiroku-ask.git
cd kiroku-ask
cp .env.example .env
openssl rand -hex 32      # paste the output into N8N_ENCRYPTION_KEY
# change the passwords in .env
docker compose up -d
docker compose logs postgres | grep "Created database livestream"
```

Open http://localhost:5678 and create the owner account. (On a VPS, read [Running on a VPS](#running-on-a-vps) first.)

> The scripts in `init-db/` **only run the first time** (while the Postgres volume is empty). If you already have an older volume, run them by hand:
> ```bash
> docker compose exec postgres bash /docker-entrypoint-initdb.d/01-livestream.sh   # only if the livestream database does not exist yet
> docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d livestream -f /docker-entrypoint-initdb.d/02-visual-log.sql'
> docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d livestream -f /docker-entrypoint-initdb.d/03-local-video.sql'
> ```
> Scripts 02 and 03 are safe to run any number of times.

## Running on a VPS

### a) n8n behind HTTPS

Postgres and n8n both bind to `127.0.0.1` in `docker-compose.yml`, so **none of their ports are exposed to the Internet**. Reach n8n through Caddy (a reverse proxy that obtains a Let's Encrypt certificate automatically):

1. Point the DNS (A record) of a domain such as `n8n.your-domain.com` at the VPS IP.
2. Open only ports 22, 80 and 443 in the VPS firewall. With ufw: `ufw allow OpenSSH && ufw allow 80,443/tcp && ufw enable`.
3. In `.env`, use the VPS block:
   ```
   N8N_HOST=n8n.your-domain.com
   N8N_PROTOCOL=https
   WEBHOOK_URL=https://n8n.your-domain.com/
   N8N_SECURE_COOKIE=true
   N8N_PROXY_HOPS=1
   ```
4. Start with Caddy: `docker compose --profile https up -d`

> Docker writes iptables rules directly and **bypasses ufw**: a port published as `"5678:5678"` is reachable from the Internet even if ufw blocks it. So do not edit the `127.0.0.1:...` lines in `docker-compose.yml`.

If nginx/Caddy is already installed on the VPS, drop `--profile https` and point that proxy at `127.0.0.1:5678` (enable WebSocket — the n8n editor needs it).

### b) Connect the personal machine to Postgres with an SSH tunnel

On the personal machine, open a separate terminal and leave it running while you ingest:

```bash
ssh -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes \
    -L 5433:127.0.0.1:5432 user@VPS_IP
```

Local port `5433` avoids clashing with a Postgres that may already run on the personal machine. Then:

```bash
export PG_DSN='postgresql://livestream_app:PASSWORD@127.0.0.1:5433/livestream'
```

Windows: OpenSSH ships with Windows 10/11; run the same command in PowerShell (`$env:PG_DSN = '...'`).

A tunnel drop does not lose work: `ingest_video.py` only opens a Postgres connection **after** each step has finished, writes + commits and closes right away, and retries 3 times if the connection is lost. Just reopen the tunnel while it waits. For a tunnel that reconnects by itself, use `autossh -M 0 -N -o ServerAliveInterval=30 -L 5433:127.0.0.1:5432 user@VPS_IP`.

### c) Alternatives to the SSH tunnel

| Option | When | Notes |
|---|---|---|
| **SSH tunnel** (default) | You can already SSH into the VPS | Nothing to install; open the tunnel each time you ingest. |
| **Tailscale / WireGuard** | Your home IP changes, or you want an always-on link | Install on both the VPS and the personal machine. Bind Postgres to the VPS's Tailscale IP, e.g. `"100.x.y.z:5432:5432"` (never `0.0.0.0`), then `PG_DSN=...@100.x.y.z:5432/...`. Tailscale must be up before Docker, otherwise Postgres cannot bind that IP. |
| **Expose port 5432 to the Internet** | Not recommended | Mandatory: firewall allowlist with only your IP, TLS on Postgres and `sslmode=verify-full` in `PG_DSN`, strong passwords. Bots scan port 5432 constantly. |

## 2. API keys

| Service | Where to get it | Notes |
|---|---|---|
| Supadata | supadata.ai → Dashboard → API key | The free plan has 100 credits/month. Videos without captions are transcribed by AI (costs more credits). |
| YouTube Data API v3 | Google Cloud Console → enable *YouTube Data API v3* → Credentials → API key | Free; used for the title and the **actual stream start time**. Optional if you always enter the date in the form. |
| OpenAI | platform.openai.com | Can be swapped for Gemini/Claude (see section 6). |
| Gemini | aistudio.google.com → Get API key | Used by workflow 3, the `rewatch_segment` tool and the extractor. With YouTube links it can only see **public** videos. |

## 3. Create credentials in n8n (Overview → Credentials → Create)

| Suggested name | Credential type | Settings |
|---|---|---|
| YouTube API key | **Query Auth** | Name: `key` · Value: YouTube API key |
| Supadata | **Header Auth** | Name: `x-api-key` · Value: Supadata API key |
| Gemini | **Header Auth** | Name: `x-goog-api-key` · Value: Gemini API key |
| OpenAI | **OpenAI** | API key |
| Postgres livestream (write) | **Postgres** | Host `postgres` · Database `livestream` · User `livestream_app` · Password `LIVESTREAM_APP_PASSWORD` · Port 5432 · SSL disable |
| Postgres livestream (read) | **Postgres** | Same, but User `livestream_reader` · Password `LIVESTREAM_READER_PASSWORD` |

The host is `postgres` (the service name in docker-compose), **not** `localhost`.

## 4. Import the workflows

Workflows → **Import from File** → pick each file in `workflows/`, then assign credentials to the nodes:

**Workflow 1 – Ingest and summarize**

- `YouTube: video details` → YouTube API key
- `Supadata: get transcript` and `Supadata: check job` → Supadata
- `Section summary model`, `Overall summary model` → OpenAI
- `Save to Postgres` → Postgres livestream (**write**)

**Workflow 2 – Q&A chat**

- `Assistant model` → OpenAI
- 8 Postgres tools → Postgres livestream (**read**): `find_by_date`, `find_by_title`, `read_transcript`, `list_recent`, `search_scenes`, `read_scene_log`, `search_screen_text`, `list_links`
- `rewatch_segment` → Gemini (public YouTube videos only)

**Workflow 3 – Visual analysis**

- `Look up livestream`, `Save scenes & links` → Postgres livestream (**write**)
- `Gemini: analyze chunk` → Gemini

Set all three workflows to **Active**.

## 5. Usage

**Ingest a livestream:** open the *Livestream form* node → copy the form's **Production URL** → paste a YouTube link.

- Leave *Stream date* empty → it is taken from the actual start time on YouTube, converted to Vietnam time.
- Enter a date → overrides the automatic date (useful when a stream runs past midnight).
- Processing takes a few minutes. Follow progress in the **Executions** tab.
- Ingesting the same video again **updates** the existing row instead of creating a duplicate.

**Visual analysis** (after ingesting with workflow 1): open the workflow 3 form → paste the same link.

- Gemini watches the video in 10-minute chunks, 1 frame per second, with audio.
- It writes a **scene log** (each time the picture changes: slide change, a web page opened, a change of clothes, a product held up…) with colours and on-screen text, plus a **list of links** seen on screen or read out loud.
- A 2-hour stream takes about 10–30 minutes. Re-running replaces the previous Gemini data; links found by OCR are kept.

**Ingest a local video file** (PC with an NVIDIA GPU or Apple Silicon Mac): see `extractor/README.md`. One command does all 4 steps:

```bash
cd extractor
python ingest_video.py ~/videos/live-10-11.mp4 --date 2026-11-10 --title "Live 10/11"
```

**Q&A:** open workflow 2 → **Open chat**. Examples (ask in Vietnamese or English):

- "What was yesterday's video about?" → lookup by date
- "Did the lipstick stream mention the price?" → lookup by **title**, then read the transcript
- "When does the red shirt appear?" / "What product is she holding?" / "How many people are there?" → search the scene log
- "What price is on the slide?" / "What is the discount code?" → search the OCR text (character-exact)
- "Which links were opened in this video?" → list links, times and source (screen or speech)
- "Which videos are there recently?"

Answers always include timestamps. YouTube videos also get a link that jumps to the exact second; local files only get the timestamp and file name.

To embed the chat elsewhere: enable *Make Chat Publicly Available* on the chat node. For Telegram: replace the chat node with a **Telegram Trigger**, add a **Telegram → Send Message** node that sends `{{ $json.output }}`, and set the `sessionKey` of *Conversation memory* to the chat id.

## 6. Customization

- **Change model:** delete a *… model* node (OpenAI) and connect a Google Gemini / Anthropic Chat Model node in its place. A small model (gpt-5-mini, Gemini Flash…) is enough for section summaries; the overall summary and the assistant may use a stronger one.
- **Section length:** change `CHUNK_MS` in the *Split into 20-minute sections* node (default 20 minutes).
- **Prompts:** edit them directly in *Summarize each section*, *Overall summary*, and the System Message of *Livestream assistant*.
- **Auto-ingest new livestreams:** add a workflow with a *Schedule Trigger* + YouTube Data API (`search?channelId=…&eventType=completed&type=video`) to find new streams and call workflow 1 (change its form trigger to an *Execute Workflow Trigger*, or call it through a Webhook).

## 7. Cost & accuracy of the visual analysis

- Per the Gemini docs, video costs about **300 tokens/second at high resolution** and about **100 tokens/second at low resolution**. A 2-hour stream ≈ 2.2 million input tokens (high) or ≈ 0.7 million (low), **paid once at analysis time**; Q&A afterwards only reads Postgres, so it is very cheap. Check the current price of the model on the Gemini API page.
- To read **links in the address bar** or small text on slides, choose **High** resolution. Low resolution is enough for questions about clothing, colours and objects.
- The Gemini free tier is limited to 8 hours of YouTube video per day.
- At 1 frame per second, very quick actions between two seconds can be missed. AI-written timestamps may be a few seconds off; the YouTube link in the answer lets you check quickly.
- The agent has a `rewatch_segment` tool that lets Gemini re-watch a segment of ≤ 3 minutes when the log lacks detail, costing only that segment.
- The default model is `gemini-3.8-flash` (change it in the URL of the `Gemini: analyze chunk` and `rewatch_segment` nodes). Change the chunk length with `CHUNK_SECONDS` in the `Split into 10-minute chunks` node.

**When do you need a vector database (pgvector)?** Only when the question **does not say which video** — e.g. "which video ever talked about product X?" — or to search by an example image. Asking by date or by title is faster and more accurate with SQL as it is now, because the agent reads all the data of that video rather than a few retrieved fragments.

If you need it later, switch the Docker image to `pgvector/pgvector`, add a vector column to the existing tables and add one search tool — nothing has to be rebuilt.

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Could not determine the stream date` | YouTube API key not assigned → enter the date in the form, or check the credential. |
| `Could not get a transcript` | The video is private / still live, or Supadata credits ran out. |
| `Supadata was still processing after 15 minutes` | The AI transcription job took too long → try again later. |
| Postgres says `password authentication failed` | Changing passwords in .env **after** the first start has no effect — change them with `ALTER USER … PASSWORD …` or delete the volume (loses data). |
| `Video ... is not in the livestreams table yet` (workflow 3) | Ingest the video with workflow 1 first. |
| Gemini error / `returned nothing` | The video is private or unlisted, wrong API key, or the free tier's 8 hours/day limit was reached. |
| `invalid JSON (finishReason=MAX_TOKENS)` | Too many scenes in a chunk → lower `CHUNK_SECONDS` to 300. |
| The agent says the stream has no visual analysis | Run workflow 3 (YouTube video) or `ingest_video.py` (file) for that video. |
| `extractor` cannot connect to Postgres | Is the SSH tunnel still running? Does `PG_DSN` use the tunnel's local port (`5433`)? Try `psql "$PG_DSN" -c 'select 1'`. |
| Workflow 3 says `column "produced_by" does not exist` | The database is missing migration 03 — run the commands in section 1. |
| n8n is not reachable on the domain | Does DNS point at the VPS, are ports 80/443 open, did you start with `--profile https`? See `docker compose logs caddy`. |
| The agent does not use `search_screen_text` | That video has no OCR data (`ocr_at` is empty) — run `ingest_video.py`. |
| A tool returns nothing although data exists | Check the `stream_date` column (Vietnam time) — a 6 a.m. stream in Vietnam is the previous day in UTC; the workflow converts this automatically. |

**Back up the livestream data:**

```bash
docker compose exec postgres pg_dump -U livestream_app livestream > livestream_backup.sql
```

Keep `N8N_ENCRYPTION_KEY`: without it n8n cannot decrypt the stored credentials.
