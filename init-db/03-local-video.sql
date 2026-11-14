-- On-screen text (OCR) table + columns for videos ingested from local files.
-- Runs automatically the first time Postgres starts. For an EXISTING database, run it by hand:
--   docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d livestream -f /docker-entrypoint-initdb.d/03-local-video.sql'
-- Safe to run any number of times.

\connect livestream

SET ROLE livestream_app;

-- Path of the original file (videos ingested from a file have no YouTube URL)
ALTER TABLE livestreams ADD COLUMN IF NOT EXISTS file_path TEXT;
-- When OCR last ran (NULL = never)
ALTER TABLE livestreams ADD COLUMN IF NOT EXISTS ocr_at TIMESTAMPTZ;
-- Where the data came from: 'youtube' (workflows 1/3) or 'file' (extractor tool)
ALTER TABLE livestreams ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'youtube';

-- Text read from the screen, grouped into spans in which the text does not change
CREATE TABLE IF NOT EXISTS screen_text (
  id         BIGSERIAL PRIMARY KEY,
  video_id   TEXT    NOT NULL REFERENCES livestreams (video_id) ON DELETE CASCADE,
  start_sec  INTEGER NOT NULL,
  end_sec    INTEGER NOT NULL,
  text       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS screen_text_video_idx ON screen_text (video_id, start_sec);

-- Which step produced a link ('gemini' or 'ocr'), so each step only replaces its own links
ALTER TABLE video_links ADD COLUMN IF NOT EXISTS produced_by TEXT NOT NULL DEFAULT 'gemini';

RESET ROLE;

GRANT SELECT ON screen_text TO livestream_reader;
