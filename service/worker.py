"""Background scan/download loop for the Libby auto-download service.

Reuses LibbyDownloader from the repo-root libby_dl.py rather than
reimplementing shelf/player/download logic.
"""

import asyncio
import logging
import sys
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import libby_dl  # noqa: E402

import db  # noqa: E402

logger = logging.getLogger("libby_service.worker")

_scan_running = False
last_scan_result: dict = {}


async def scan_once() -> dict:
    """Run one shelf scan: download any loan not already marked complete.

    Safe to call while a scan is already running (e.g. "Scan Now" clicked
    during a scheduled scan) — it just reports back without starting a
    second, overlapping scan. Single-threaded asyncio means the
    check-then-set below is race-free without a lock.
    """
    global _scan_running, last_scan_result

    if _scan_running:
        return {"skipped": True, "reason": "scan already in progress"}

    if not libby_dl.SESSION_FILE.exists():
        result = {"skipped": True, "reason": "not authenticated"}
        last_scan_result = result
        return result

    _scan_running = True
    downloaded = failed = skipped = 0
    try:
        output_dir = db.get_config("output_dir")
        downloader = libby_dl.LibbyDownloader(output_dir=output_dir, headless=True)

        async with async_playwright() as pw:
            browser, context, page, player_page = await downloader._launch_browser_context(pw)
            try:
                books = await downloader._get_shelf(page)
                for book in books:
                    loan_id = book.get("id", "")
                    if not loan_id:
                        continue
                    if db.is_downloaded(loan_id):
                        skipped += 1
                        continue

                    title = book.get("title", "")
                    author = book.get("author", "")
                    card_id = book.get("card_id", "")
                    db.upsert_book(loan_id, title, author, status="downloading", card_id=card_id)

                    try:
                        await downloader._download_selected_book(page, context, player_page, book)
                        db.upsert_book(
                            loan_id, title, author, status="complete", card_id=card_id,
                            output_path=str(downloader.output_dir), mark_downloaded=True,
                        )
                        downloaded += 1
                    except Exception as e:
                        logger.exception("Download failed for %r", title)
                        db.upsert_book(
                            loan_id, title, author, status="failed", card_id=card_id, error=str(e),
                        )
                        failed += 1
            finally:
                try:
                    await context.storage_state(path=str(libby_dl.SESSION_FILE), indexed_db=True)
                except Exception:
                    try:
                        await context.storage_state(path=str(libby_dl.SESSION_FILE))
                    except Exception:
                        pass
                await browser.close()

        result = {"downloaded": downloaded, "failed": failed, "skipped": skipped}
    except Exception as e:
        logger.exception("Scan failed")
        result = {"error": str(e)}
    finally:
        _scan_running = False

    last_scan_result = result
    return result


async def loop_forever() -> None:
    """Scan, sleep for the configured interval, repeat forever. Re-reads the
    interval from the DB each cycle so a config change takes effect on the
    next cycle without restarting the service."""
    while True:
        try:
            result = await scan_once()
            logger.info("Scan result: %s", result)
        except Exception:
            logger.exception("Unexpected error in scan loop")

        try:
            interval_minutes = float(db.get_config("scan_interval_minutes") or 15)
        except ValueError:
            interval_minutes = 15
        await asyncio.sleep(max(60.0, interval_minutes * 60))
