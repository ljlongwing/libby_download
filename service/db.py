"""SQLite persistence for the multi-source (Libby + Chirp) auto-download
service.

Tracks which loans/purchases have already been downloaded (so each
source's periodic scan doesn't re-download them) plus a small history log,
and holds the handful of user-editable settings (output directory, one
scan interval per source).
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(os.environ.get("LIBBY_SERVICE_DB", "/data/db/service.db"))

# loan_id is source-specific: Libby's stable numeric loan id, or Chirp's
# player href (e.g. "/player/34151152") -- Chirp has no equivalent numeric
# id in its shelf listing. Scoping the primary key by source means the two
# id schemes can never collide, even coincidentally.
SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    source TEXT NOT NULL,          -- 'libby' | 'chirp'
    loan_id TEXT NOT NULL,
    card_id TEXT,
    title TEXT,
    author TEXT,
    status TEXT NOT NULL,          -- pending | downloading | complete | failed
    error TEXT,
    output_path TEXT,
    first_seen_at TEXT NOT NULL,
    downloaded_at TEXT,
    on_shelf INTEGER NOT NULL DEFAULT 0,   -- 1 if seen on the shelf/library in the most recent scan
    series TEXT,            -- NULL = not yet looked up; '' = looked up, not part of a series
    series_index TEXT,
    duration TEXT,
    detail_url TEXT,        -- link to the book's page on the source's own site
    PRIMARY KEY (source, loan_id)
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

DEFAULT_CONFIG = {
    # A dedicated mount point (separate from /data, which holds the session
    # files and this database) so it can be bind-mounted straight at
    # wherever your audiobook library actually lives on the host -- see
    # BOOKS_DIR in docker-compose.yml. Changing this value on the Config
    # page only moves where *inside the container* downloads land; it does
    # not create a new host mount, so it must point at a path that's
    # actually mounted. Shared between sources since each book already
    # nests under its own <Source Book Name>/ subfolder.
    "output_dir": "/books",
    "libby_scan_interval_minutes": "15",
    "chirp_scan_interval_minutes": "15",
}


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        _migrate(conn)
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_CONFIG.items():
            conn.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (key, value)
            )
        # Additive migration for installs from before on_shelf existed.
        try:
            conn.execute("ALTER TABLE books ADD COLUMN on_shelf INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists
        # Additive migration for installs from before series/duration lookup
        # existed.
        for col in ("series TEXT", "series_index TEXT", "duration TEXT", "detail_url TEXT"):
            try:
                conn.execute(f"ALTER TABLE books ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # column already exists
        # Additive migration for installs from before scan_interval_minutes
        # was split per-source: seed both new keys from the old single one.
        old = conn.execute(
            "SELECT value FROM config WHERE key = 'scan_interval_minutes'"
        ).fetchone()
        if old is not None:
            conn.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES ('libby_scan_interval_minutes', ?)",
                (old["value"],),
            )
            conn.execute("DELETE FROM config WHERE key = 'scan_interval_minutes'")


def _migrate(conn: sqlite3.Connection) -> None:
    """One-time rebuild for installs from before the "source" column
    existed. SQLite can't change a primary key via ALTER TABLE, so this
    creates the new-shaped table, copies existing rows across (the only
    source that has ever existed is 'libby'), and swaps it in. No-ops if
    "books" doesn't exist yet (fresh install) or already has "source".
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(books)").fetchall()}
    if not cols or "source" in cols:
        return
    conn.executescript(
        SCHEMA.split("CREATE TABLE IF NOT EXISTS config")[0].replace(
            "CREATE TABLE IF NOT EXISTS books", "CREATE TABLE books_new"
        )
    )
    old_cols = cols - {"source"}
    col_list = ", ".join(sorted(old_cols))
    conn.execute(
        f"INSERT INTO books_new (source, {col_list}) "
        f"SELECT 'libby', {col_list} FROM books"
    )
    conn.execute("DROP TABLE books")
    conn.execute("ALTER TABLE books_new RENAME TO books")


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


def is_downloaded(source: str, loan_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT status FROM books WHERE source = ? AND loan_id = ?", (source, loan_id)
        ).fetchone()
        return bool(row) and row["status"] == "complete"


def sync_shelf(source: str, books: list[dict]) -> None:
    """Record the current shelf/library snapshot for this source: on_shelf=1
    for everything just seen (creating a 'pending' row for titles never
    encountered before), on_shelf=0 for anything previously tracked (for
    this source) that's no longer present (returned/expired). Called at the
    start of every scan so the dashboard can show "what's available right
    now" independent of download history.

    series/series_index/duration are only written when the caller actually
    looked them up this cycle (worker.py skips re-fetching for books that
    already have them) -- COALESCE keeps whatever was already stored
    instead of blanking it out on every ordinary sync. detail_url is cheap
    to recompute every time (no extra lookup, same as title/author) so it's
    always overwritten fresh.
    """
    now = datetime.now(timezone.utc).isoformat()
    seen_ids = [b["loan_id"] for b in books if b.get("loan_id")]
    with _connect() as conn:
        for b in books:
            loan_id = b.get("loan_id")
            if not loan_id:
                continue
            existing = conn.execute(
                "SELECT loan_id FROM books WHERE source = ? AND loan_id = ?", (source, loan_id)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE books SET on_shelf = 1, title = ?, author = ?, card_id = ?, detail_url = ?, "
                    "series = COALESCE(?, series), series_index = COALESCE(?, series_index), "
                    "duration = COALESCE(?, duration) "
                    "WHERE source = ? AND loan_id = ?",
                    (
                        b.get("title", ""), b.get("author", ""), b.get("card_id", ""), b.get("detail_url", ""),
                        b.get("series"), b.get("series_index"), b.get("duration"),
                        source, loan_id,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO books "
                    "(source, loan_id, card_id, title, author, status, first_seen_at, on_shelf, "
                    " series, series_index, duration, detail_url) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', ?, 1, ?, ?, ?, ?)",
                    (
                        source, loan_id, b.get("card_id", ""), b.get("title", ""), b.get("author", ""), now,
                        b.get("series"), b.get("series_index"), b.get("duration"), b.get("detail_url", ""),
                    ),
                )
        if seen_ids:
            placeholders = ",".join("?" * len(seen_ids))
            conn.execute(
                f"UPDATE books SET on_shelf = 0 WHERE source = ? AND loan_id NOT IN ({placeholders})",
                [source, *seen_ids],
            )
        else:
            conn.execute("UPDATE books SET on_shelf = 0 WHERE source = ?", (source,))


def get_series_lookup_status(source: str) -> set[str]:
    """loan_ids (for this source, regardless of current on_shelf status)
    that already have a series lookup on record -- series is NULL only
    when it's never been looked up; '' means it was checked and the book
    just isn't part of one. Lets worker.py skip re-fetching for books it
    already has an answer for, on-shelf or not.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT loan_id FROM books WHERE source = ? AND series IS NOT NULL", (source,)
        ).fetchall()
        return {r["loan_id"] for r in rows}


def list_shelf(source: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM books WHERE source = ? AND on_shelf = 1 ORDER BY title COLLATE NOCASE",
            (source,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_for_redownload(source: str, loan_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE books SET status = 'pending', error = NULL WHERE source = ? AND loan_id = ?",
            (source, loan_id),
        )


def upsert_book(
    source: str,
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
            "SELECT first_seen_at, downloaded_at FROM books WHERE source = ? AND loan_id = ?",
            (source, loan_id),
        ).fetchone()
        first_seen_at = existing["first_seen_at"] if existing else now
        downloaded_at = now if mark_downloaded else (existing["downloaded_at"] if existing else None)
        conn.execute(
            """
            INSERT INTO books
                (source, loan_id, card_id, title, author, status, error, output_path, first_seen_at, downloaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, loan_id) DO UPDATE SET
                card_id=excluded.card_id, title=excluded.title, author=excluded.author,
                status=excluded.status, error=excluded.error, output_path=excluded.output_path,
                downloaded_at=excluded.downloaded_at
            """,
            (source, loan_id, card_id, title, author, status, error, output_path, first_seen_at, downloaded_at),
        )


def list_books() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM books ORDER BY first_seen_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
