"""Background scan/download loop for the multi-source auto-download
service.

Drives whichever downloader sources.SOURCES[source] points at generically
-- both LibbyDownloader and ChirpDownloader share the same
_launch_browser_context/_get_shelf/_download_selected_book shape, so there
is no per-source branching in the scan logic itself.
"""

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from sources import SOURCES  # noqa: E402

logger = logging.getLogger("libby_service.worker")

_scan_running: dict[str, bool] = {s: False for s in SOURCES}
last_scan_result: dict[str, dict] = {s: {} for s in SOURCES}
last_scan_at: dict[str, str] = {s: "" for s in SOURCES}

# Each downloader already prints detailed, line-by-line progress (each part
# captured, download progress, duration checks, etc.) -- rather than
# re-instrumenting it, tee stdout into this buffer during a scan so the web
# UI can show the same detail live instead of just "a scan is running".
# Cleared at the start of each scan; holds the last scan's full output
# (capped) in between. One buffer per source.
scan_log: dict[str, list[str]] = {s: [] for s in SOURCES}
_SCAN_LOG_MAXLEN = 1000


class _TeeWriter:
    """Writes through to the real stream (container logs stay intact) while
    also appending completed lines to a shared buffer."""

    def __init__(self, real_stream, buffer: list, maxlen: int):
        self._real = real_stream
        self._buffer = buffer
        self._maxlen = maxlen
        self._partial = ""

    def write(self, s: str) -> int:
        self._real.write(s)
        self._partial += s
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            if line.strip():
                self._buffer.append(line)
        if len(self._buffer) > self._maxlen:
            del self._buffer[: len(self._buffer) - self._maxlen]
        return len(s)

    def flush(self) -> None:
        self._real.flush()


async def scan_once(source: str) -> dict:
    """Run one shelf/library scan for the given source: download any loan
    not already marked complete.

    Safe to call while a scan for this source is already running (e.g.
    "Scan Now" clicked during a scheduled scan) — it just reports back
    without starting a second, overlapping scan for that source. A scan for
    the *other* source may run concurrently; each source has independent
    state. Single-threaded asyncio means the check-then-set below is
    race-free without a lock.
    """
    cfg = SOURCES[source]

    if _scan_running[source]:
        return {"skipped": True, "reason": "scan already in progress"}

    if not cfg["session_file"].exists():
        result = {"skipped": True, "reason": "not authenticated"}
        last_scan_result[source] = result
        return result

    _scan_running[source] = True
    log = scan_log[source]
    log.clear()
    old_stdout = sys.stdout
    sys.stdout = _TeeWriter(old_stdout, log, _SCAN_LOG_MAXLEN)
    downloaded = failed = skipped = 0
    try:
        output_dir = db.get_config("output_dir")
        # headless=True was tried first for Libby but its player doesn't
        # fully initialize under real headless Chromium (found ~1 chapter
        # instead of the full TOC, no play/TOC buttons, zero parts
        # captured). Confirmed via local repro that the exact same bundled
        # Chromium works correctly non-headless. Chirp hasn't been proven
        # safe headless either, so both sources use the same non-headless
        # approach, rendering into the container's existing Xvfb display
        # (already needed for the auth flow) without needing anyone to
        # actually be watching.
        downloader = cfg["downloader_cls"](output_dir=output_dir, headless=False)

        async with async_playwright() as pw:
            browser, context, page, player_page = await downloader._launch_browser_context(pw)
            try:
                books = await downloader._get_shelf(page)
                db.sync_shelf(source, [
                    {
                        "loan_id": cfg["get_loan_id"](b),
                        "title": b.get("title", ""),
                        "author": b.get("author", ""),
                        "card_id": cfg["get_card_id"](b),
                    }
                    for b in books
                ])
                for book in books:
                    loan_id = cfg["get_loan_id"](book)
                    if not loan_id:
                        continue
                    if db.is_downloaded(source, loan_id):
                        skipped += 1
                        continue

                    title = book.get("title", "")
                    author = book.get("author", "")
                    card_id = cfg["get_card_id"](book)
                    db.upsert_book(source, loan_id, title, author, status="downloading", card_id=card_id)

                    try:
                        await downloader._download_selected_book(page, context, player_page, book)
                        db.upsert_book(
                            source, loan_id, title, author, status="complete", card_id=card_id,
                            output_path=str(downloader.output_dir), mark_downloaded=True,
                        )
                        downloaded += 1
                    except Exception as e:
                        logger.exception("[%s] Download failed for %r", source, title)
                        db.upsert_book(
                            source, loan_id, title, author, status="failed", card_id=card_id, error=str(e),
                        )
                        failed += 1
            finally:
                try:
                    await context.storage_state(path=str(cfg["session_file"]), indexed_db=True)
                except Exception:
                    try:
                        await context.storage_state(path=str(cfg["session_file"]))
                    except Exception:
                        pass
                await browser.close()

        result = {"downloaded": downloaded, "failed": failed, "skipped": skipped}
    except Exception as e:
        logger.exception("[%s] Scan failed", source)
        result = {"error": str(e)}
    finally:
        sys.stdout = old_stdout
        _scan_running[source] = False

    last_scan_result[source] = result
    last_scan_at[source] = datetime.now(timezone.utc).isoformat()
    return result


async def loop_forever(source: str) -> None:
    """Scan, sleep for the configured interval, repeat forever, for one
    source. Re-reads the interval from the DB each cycle so a config change
    takes effect on the next cycle without restarting the service."""
    while True:
        try:
            result = await scan_once(source)
            logger.info("[%s] Scan result: %s", source, result)
        except Exception:
            logger.exception("[%s] Unexpected error in scan loop", source)

        try:
            interval_minutes = float(db.get_config(f"{source}_scan_interval_minutes") or 15)
        except ValueError:
            interval_minutes = 15
        await asyncio.sleep(max(60.0, interval_minutes * 60))
