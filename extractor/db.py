"""Write results to Postgres (the "livestream" database).

Postgres usually runs on a VPS and is reached through an SSH tunnel. OCR and uploads can
take tens of minutes, so we never hold one connection for the whole run: each step finishes
its work first, then opens a connection, writes, commits and closes (see write_step()).
"""

from __future__ import annotations

import json
import os
import time
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg

# Port 5433 = local end of the SSH tunnel (see README), so it does not clash with a
# Postgres that may already be running on the personal machine
DEFAULT_DSN = "postgresql://livestream_app@127.0.0.1:5433/livestream"

# Detect a dead connection (tunnel dropped, Wi-Fi changed) quickly instead of hanging forever
KEEPALIVE = {
    "keepalives": "1",
    "keepalives_idle": "30",
    "keepalives_interval": "10",
    "keepalives_count": "3",
    "connect_timeout": "15",
}


def add_keepalive(dsn: str) -> str:
    """Add keepalive parameters to a DSN WITHOUT overriding ones the user already set.

    Accepts both URI form (postgresql://...?sslmode=require) and key=value form
    (host=... dbname=...).
    """
    dsn = dsn.strip()
    if dsn.startswith(("postgresql://", "postgres://")):
        parts = urlsplit(dsn)
        existing = parse_qsl(parts.query, keep_blank_values=True)
        already_set = {k for k, _ in existing}
        extra = [(k, v) for k, v in KEEPALIVE.items() if k not in already_set]
        return urlunsplit(parts._replace(query=urlencode(existing + extra)))

    # key=value form: let libpq parse it, then add only the missing keys
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    already_set = conninfo_to_dict(dsn)
    extra = {k: v for k, v in KEEPALIVE.items() if k not in already_set}
    return make_conninfo(dsn, **extra)


def get_dsn() -> str:
    return add_keepalive(os.environ.get("PG_DSN") or DEFAULT_DSN)


def connect():
    return psycopg.connect(get_dsn())


def write_step(label: str, fn: Callable[[psycopg.Connection], None], attempts: int = 3) -> None:
    """Open a fresh connection, run fn(conn) in ONE transaction, commit, close.

    On a connection error (tunnel dropped) it retries: the computed data is still in
    memory, so the heavy step does not have to run again. Every save_* function deletes
    then inserts, so retrying is safe.
    """
    for attempt in range(1, attempts + 1):
        try:
            # psycopg 3: leaving the with block commits (if no error) and CLOSES the connection
            with connect() as conn:
                fn(conn)
                conn.commit()
            return
        except psycopg.OperationalError as e:
            if attempt == attempts:
                raise RuntimeError(
                    f"Could not write '{label}' to Postgres after {attempts} attempts: {e}\n"
                    "Check that the SSH tunnel is still running and PG_DSN uses the right port."
                ) from e
            wait = 10 * attempt
            print(f"  ! Lost the Postgres connection while writing '{label}' ({e}); retrying in {wait}s")
            time.sleep(wait)


def upsert_video(conn, meta: dict) -> None:
    """Create or update the row in the livestreams table."""
    conn.execute(
        """
        INSERT INTO livestreams
          (video_id, stream_date, title, url, file_path, duration_seconds, language, source)
        VALUES (%(video_id)s, %(stream_date)s::date, %(title)s, %(url)s, %(file_path)s,
                %(duration_seconds)s, %(language)s, 'file')
        ON CONFLICT (video_id) DO UPDATE SET
          stream_date = EXCLUDED.stream_date,
          title = EXCLUDED.title,
          file_path = EXCLUDED.file_path,
          duration_seconds = EXCLUDED.duration_seconds,
          language = EXCLUDED.language,
          source = EXCLUDED.source,
          updated_at = now()
        """,
        meta,
    )


def save_transcript(conn, video_id: str, transcript: str, summary: str, section_summaries: list[dict]) -> None:
    conn.execute(
        """
        UPDATE livestreams
           SET transcript = %s, summary = %s, section_summaries = %s::jsonb, updated_at = now()
         WHERE video_id = %s
        """,
        (transcript, summary, json.dumps(section_summaries, ensure_ascii=False), video_id),
    )


def save_scenes(conn, video_id: str, scenes: list[dict], links: list[dict]) -> None:
    """Replace this video's scene log and the links produced by Gemini."""
    conn.execute("DELETE FROM video_scenes WHERE video_id = %s", (video_id,))
    conn.execute("DELETE FROM video_links WHERE video_id = %s AND produced_by = 'gemini'", (video_id,))
    if scenes:
        conn.cursor().executemany(
            """INSERT INTO video_scenes (video_id, start_sec, end_sec, description, on_screen_text, keywords)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            [(video_id, s["start_sec"], s["end_sec"], s["description"],
              s["on_screen_text"], s["keywords"]) for s in scenes],
        )
    _insert_links(conn, video_id, links, "gemini")
    conn.execute(
        "UPDATE livestreams SET visual_analyzed_at = now(), updated_at = now() WHERE video_id = %s",
        (video_id,),
    )


def save_screen_text(conn, video_id: str, rows: list[dict], links: list[dict]) -> None:
    """Replace this video's on-screen text (OCR) and the links found by OCR."""
    conn.execute("DELETE FROM screen_text WHERE video_id = %s", (video_id,))
    conn.execute("DELETE FROM video_links WHERE video_id = %s AND produced_by = 'ocr'", (video_id,))
    if rows:
        conn.cursor().executemany(
            "INSERT INTO screen_text (video_id, start_sec, end_sec, text) VALUES (%s, %s, %s, %s)",
            [(video_id, r["start_sec"], r["end_sec"], r["text"]) for r in rows],
        )
    _insert_links(conn, video_id, links, "ocr")
    conn.execute(
        "UPDATE livestreams SET ocr_at = now(), updated_at = now() WHERE video_id = %s",
        (video_id,),
    )


def _insert_links(conn, video_id: str, links: list[dict], produced_by: str) -> None:
    if not links:
        return
    conn.cursor().executemany(
        """INSERT INTO video_links (video_id, at_sec, url, source, context, produced_by)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        [(video_id, l["at_sec"], l["url"], l["source"], l["context"], produced_by) for l in links],
    )
