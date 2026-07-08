"""SQLite persistence for the Libby auto-download service.

Tracks which loans have already been downloaded (so the worker's periodic
shelf scan doesn't re-download them) plus a small history log, and holds
the handful of user-editable settings (output directory, scan interval).
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(os.environ.get("LIBBY_SERVICE_DB", "/data/db/service.db"))

# loan_id (book["id"] from LibbyDownloader._get_shelf) is the one field
# reliably present across all three shelf-scraping strategies; card_id is
# only populated by the primary JSON-API strategy, so it's kept as
# informational metadata rather than part of the primary key.
SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    loan_id TEXT PRIMARY KEY,
    card_id TEXT,
    title TEXT,
    author TEXT,
    status TEXT NOT NULL,          -- pending | downloading | complete | failed
    error TEXT,
    output_path TEXT,
    first_seen_at TEXT NOT NULL,
    downloaded_at TEXT
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

DEFAULT_CONFIG = {
    # A dedicated mount point (separate from /data, which holds the session
    # file and this database) so it can be bind-mounted straight at wherever
    # your audiobook library actually lives on the host -- see BOOKS_DIR in
    # docker-compose.yml. Changing this value on the Config page only moves
    # where *inside the container* downloads land; it does not create a new
    # host mount, so it must point at a path that's actually mounted.
    "output_dir": "/books",
    "scan_interval_minutes": "15",
}


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_CONFIG.items():
            conn.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (key, value)
            )


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_config(key: str) -> str:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else DEFAULT_CONFIG.get(key, "")


def set_config(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_all_config() -> dict:
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM config").fetchall()
        return {r["key"]: r["value"] for r in rows}


def is_downloaded(loan_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT status FROM books WHERE loan_id = ?", (loan_id,)
        ).fetchone()
        return bool(row) and row["status"] == "complete"


def upsert_book(
    loan_id: str,
    title: str,
    author: str,
    status: str,
    card_id: str = "",
    error: Optional[str] = None,
    output_path: Optional[str] = None,
    mark_downloaded: bool = False,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT first_seen_at, downloaded_at FROM books WHERE loan_id = ?", (loan_id,)
        ).fetchone()
        first_seen_at = existing["first_seen_at"] if existing else now
        downloaded_at = now if mark_downloaded else (existing["downloaded_at"] if existing else None)
        conn.execute(
            """
            INSERT INTO books
                (loan_id, card_id, title, author, status, error, output_path, first_seen_at, downloaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(loan_id) DO UPDATE SET
                card_id=excluded.card_id, title=excluded.title, author=excluded.author,
                status=excluded.status, error=excluded.error, output_path=excluded.output_path,
                downloaded_at=excluded.downloaded_at
            """,
            (loan_id, card_id, title, author, status, error, output_path, first_seen_at, downloaded_at),
        )


def list_books() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM books ORDER BY first_seen_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
