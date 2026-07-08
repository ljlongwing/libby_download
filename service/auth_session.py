"""Drives the (re-)authentication flow for the web UI, for either source.

Login has to happen on the source's own page (library card + PIN for
Libby, account login for Chirp), so this launches a real, visible
(non-headless) browser rendered into the container's virtual display
(Xvfb, $DISPLAY set by the entrypoint script), which the web UI streams
live via noVNC. We just poll the downloader's existing login-detection
logic in the background and save the session once it succeeds — same
mechanism the CLI's _ensure_authenticated() uses, just without blocking on
a terminal input().

Libby and Chirp logins are mutually exclusive with each other (not with
scans): both stream into the same Xvfb display for a human to interact
with, and two simultaneous interactive flows sharing one visual space
would be genuinely confusing. Scans don't need a human watching, so they
aren't part of this mutex.
"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources import SOURCES  # noqa: E402

logger = logging.getLogger("libby_service.auth")

_state: dict[str, dict] = {
    s: {"in_progress": False, "success": None, "message": "Not started."} for s in SOURCES
}
_auth_in_progress_source: Optional[str] = None


def get_status(source: str) -> dict:
    return {**_state[source], "authenticated": SOURCES[source]["session_file"].exists()}


async def start_login(source: str) -> dict:
    global _auth_in_progress_source

    if _state[source]["in_progress"]:
        return {"started": False, "reason": "A login attempt is already in progress."}

    if _auth_in_progress_source is not None and _auth_in_progress_source != source:
        other_label = SOURCES[_auth_in_progress_source]["label"]
        return {
            "started": False,
            "reason": f"A {other_label} login is already in progress — finish or wait for it first.",
        }

    _auth_in_progress_source = source
    _state[source].update(in_progress=True, success=None, message=f"Opening {SOURCES[source]['label']}...")
    asyncio.create_task(_run_login_flow(source))
    return {"started": True}


async def _run_login_flow(source: str) -> None:
    global _auth_in_progress_source
    cfg = SOURCES[source]
    # No downloads happen here; output_dir is just a required constructor
    # arg for the downloader classes, so a scratch directory is fine.
    downloader = cfg["downloader_cls"](output_dir=f"/tmp/{source}_auth_scratch", headless=False)
    try:
        async with async_playwright() as pw:
            browser, context, page, _player_page = await downloader._launch_browser_context(pw)
            try:
                await page.goto(cfg["login_url"], wait_until="load", timeout=60_000)
                await page.wait_for_timeout(2_000)

                if await downloader._is_logged_in(page):
                    try:
                        await context.storage_state(path=str(cfg["session_file"]), indexed_db=True)
                    except Exception:
                        await context.storage_state(path=str(cfg["session_file"]))
                    _state[source].update(success=True, message="Already logged in — session saved.")
                    return

                _state[source]["message"] = "Waiting for you to log in via the embedded browser..."
                ok = await downloader._wait_for_login(page, context, timeout_s=600)
                _state[source].update(
                    success=ok,
                    message="Logged in — session saved." if ok else "Timed out waiting for login.",
                )
            finally:
                await browser.close()
    except Exception as e:
        logger.exception("[%s] Login flow failed", source)
        _state[source].update(success=False, message=f"Error: {e}")
    finally:
        _state[source]["in_progress"] = False
        if _auth_in_progress_source == source:
            _auth_in_progress_source = None
