"""Drives the (re-)authentication flow for the web UI.

Login has to happen on Libby's own page (library card + PIN), so this
launches a real, visible (non-headless) browser rendered into the
container's virtual display (Xvfb, $DISPLAY set by the entrypoint script),
which the web UI streams live via noVNC. We just poll LibbyDownloader's
existing login-detection logic in the background and save the session once
it succeeds — same mechanism the CLI's _ensure_authenticated() uses, just
without blocking on a terminal input().
"""

import asyncio
import logging
import sys
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import libby_dl  # noqa: E402

logger = logging.getLogger("libby_service.auth")

_state = {
    "in_progress": False,
    "success": None,       # None = no attempt resolved yet; True/False once one has
    "message": "Not started.",
}


def get_status() -> dict:
    return {**_state, "authenticated": libby_dl.SESSION_FILE.exists()}


async def start_login() -> dict:
    if _state["in_progress"]:
        return {"started": False, "reason": "A login attempt is already in progress."}

    _state.update(in_progress=True, success=None, message="Opening Libby...")
    asyncio.create_task(_run_login_flow())
    return {"started": True}


async def _run_login_flow() -> None:
    # No downloads happen here; output_dir is just a required constructor
    # arg for LibbyDownloader, so a scratch directory is fine.
    downloader = libby_dl.LibbyDownloader(output_dir="/tmp/libby_auth_scratch", headless=False)
    try:
        async with async_playwright() as pw:
            browser, context, page, _player_page = await downloader._launch_browser_context(pw)
            try:
                await page.goto(libby_dl.LIBBY_URL, wait_until="load", timeout=60_000)
                await page.wait_for_timeout(2_000)

                if await downloader._is_logged_in(page):
                    try:
                        await context.storage_state(path=str(libby_dl.SESSION_FILE), indexed_db=True)
                    except Exception:
                        await context.storage_state(path=str(libby_dl.SESSION_FILE))
                    _state.update(success=True, message="Already logged in — session saved.")
                    return

                _state["message"] = "Waiting for you to log in via the embedded browser..."
                ok = await downloader._wait_for_login(page, context, timeout_s=600)
                _state.update(
                    success=ok,
                    message="Logged in — session saved." if ok else "Timed out waiting for login.",
                )
            finally:
                await browser.close()
    except Exception as e:
        logger.exception("Login flow failed")
        _state.update(success=False, message=f"Error: {e}")
    finally:
        _state["in_progress"] = False
