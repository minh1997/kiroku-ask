#!/bin/bash
# Create the "livestream" database, its two users and the livestreams table.
# Runs only once, when the Postgres volume is still empty.
set -e

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_pw="$LIVESTREAM_APP_PASSWORD" \
  -v reader_pw="$LIVESTREAM_READER_PASSWORD" <<-'EOSQL'
	CREATE USER livestream_app WITH PASSWORD :'app_pw';
	CREATE USER livestream_reader WITH PASSWORD :'reader_pw';
	CREATE DATABASE livestream OWNER livestream_app;
	REVOKE ALL ON DATABASE livestream FROM PUBLIC;
	GRANT CONNECT ON DATABASE livestream TO livestream_app, livestream_reader;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname livestream <<-'EOSQL'
	SET ROLE livestream_app;

	CREATE TABLE livestreams (
	  id                SERIAL PRIMARY KEY,
	  video_id          TEXT        NOT NULL UNIQUE,
	  stream_date       DATE        NOT NULL,          -- date of the stream, Vietnam time
	  started_at        TIMESTAMPTZ,                   -- actual start time
	  title             TEXT,
	  url               TEXT,
	  duration_seconds  INTEGER,
	  language          TEXT,
	  summary           TEXT,                          -- overall summary
	  section_summaries JSONB,                         -- summary of each ~20-minute section
	  transcript        TEXT,                          -- full transcript, "[hh:mm:ss] text" per ~30 s
	  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
	  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
	);

	CREATE INDEX livestreams_stream_date_idx ON livestreams (stream_date);

	RESET ROLE;

	GRANT USAGE ON SCHEMA public TO livestream_reader;
	GRANT SELECT ON livestreams TO livestream_reader;
EOSQL

echo "Created database livestream."
