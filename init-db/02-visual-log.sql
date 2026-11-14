-- Tables for the visual analysis (workflow 3 and extractor/ingest_video.py).
-- Runs automatically the first time Postgres starts. For an EXISTING database, run it by hand:
--   docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d livestream -f /docker-entrypoint-initdb.d/02-visual-log.sql'
-- Safe to run any number of times (IF NOT EXISTS).

\connect livestream

SET ROLE livestream_app;

-- When the visual analysis last ran (NULL = never)
ALTER TABLE livestreams ADD COLUMN IF NOT EXISTS visual_analyzed_at TIMESTAMPTZ;

-- Scene log: one row per span of time in which the picture stays roughly the same
CREATE TABLE IF NOT EXISTS video_scenes (
  id              BIGSERIAL PRIMARY KEY,
  video_id        TEXT    NOT NULL REFERENCES livestreams (video_id) ON DELETE CASCADE,
  start_sec       INTEGER NOT NULL,
  end_sec         INTEGER NOT NULL,
  description     TEXT    NOT NULL,   -- people, clothing, colours, objects, actions
  on_screen_text  TEXT,               -- legible text: slides, prices, product names, address bar...
  keywords        TEXT                -- comma-separated keywords
);
CREATE INDEX IF NOT EXISTS video_scenes_video_idx ON video_scenes (video_id, start_sec);

-- Links shown on screen or read out loud
CREATE TABLE IF NOT EXISTS video_links (
  id        BIGSERIAL PRIMARY KEY,
  video_id  TEXT    NOT NULL REFERENCES livestreams (video_id) ON DELETE CASCADE,
  at_sec    INTEGER NOT NULL,
  url       TEXT    NOT NULL,
  source    TEXT    NOT NULL,         -- screen | speech
  context   TEXT
);
CREATE INDEX IF NOT EXISTS video_links_video_idx ON video_links (video_id, at_sec);

RESET ROLE;

GRANT SELECT ON video_scenes, video_links TO livestream_reader;
