#!/usr/bin/env python3
"""
libby_dl.py – Automated Libby audiobook downloader.

Workflow
--------
1. Open a Chromium browser via Playwright.
2. Authenticate with Libby (manual first run; session is saved afterwards).
3. List borrowed audiobooks and let you pick one.
4. Open the player, extract BIFOCAL metadata/TOC, then seek through every
   part to trigger the audio requests.
5. Re-execute each captured request to download the MP3 files.
6. Tag the files with ID3 metadata and write a .cue file for chapter info.
7. Optionally split into individual chapter files via ffmpeg (--ffmpeg).

Usage
-----
    python libby_dl.py [--out DIR] [--ffmpeg PATH] [--headless]

Requirements
------------
    pip install -r requirements.txt
    playwright install chromium
"""

import argparse
import asyncio
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from mutagen.id3 import APIC, COMM, TALB, TIT2, TIT3, TPE1, TPE2, TYER, TRCK, ID3
from mutagen.mp3 import MP3
from playwright.async_api import BrowserContext, Page, Request, async_playwright

# Where the Playwright browser session (cookies + localStorage) is persisted.
SESSION_FILE = Path.home() / ".libby_session.json"
LIBBY_URL = "https://libbyapp.com"


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class LibbyDownloader:
    def __init__(
        self,
        output_dir: str,
        ffmpeg: Optional[str] = None,
        headless: bool = False,
        skip_minutes: float = 5.0,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.ffmpeg = ffmpeg
        self.headless = headless
        # Number of 15-second skip-forward clicks per batch when filling gaps.
        self.skip_clicks = max(1, round(skip_minutes * 60 / 15))

        # Populated as audio requests are intercepted during seeking.
        self.captured: list[dict] = []          # [{url, headers, filename}]
        self.captured_filenames: set[str] = set()

        # Populated from the BIFOCAL <script> embedded in the player page.
        self.metadata: dict = {}
        self.toc: dict = {}                     # file_key -> [{title, offset}]
        self.reading_order: list[dict] = []
        self.total_book_duration: float = 0.0   # sum of readingOrder durations

        # Saved during UI-scrape so _seek_by_toc can reopen the TOC panel.
        self._toc_btn = None
        self._toc_frame = None

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        async with async_playwright() as pw:
            browser_candidates = [
                # Brave — Windows
                Path(r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"),
                Path.home() / r"AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe",
                # Brave — Linux / macOS
                Path("/usr/bin/brave-browser"),
                Path("/usr/bin/brave"),
                Path("/opt/brave.com/brave/brave"),
                Path("/snap/bin/brave"),
                Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
                # Chrome — Windows
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
                Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
                # Chrome — Linux / macOS
                Path("/usr/bin/google-chrome"),
                Path("/usr/bin/google-chrome-stable"),
                Path("/usr/bin/chromium-browser"),
                Path("/usr/bin/chromium"),
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                # Edge — Windows
                Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
                Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
                # Edge — Linux / macOS
                Path("/usr/bin/microsoft-edge"),
                Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            ]
            browser_path = next((str(p) for p in browser_candidates if p.exists()), None)

            launch_kwargs: dict = {
                "headless": self.headless,
                # Suppress automation indicators so sites treat this like a
                # real browser session.
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--autoplay-policy=no-user-gesture-required",
                    "--no-sandbox",
                ],
            }
            if browser_path:
                print(f"Using browser: {browser_path}")
                launch_kwargs["executable_path"] = browser_path
            else:
                print("Warning: no system browser found — falling back to Playwright's bundled Chromium")
            browser = await pw.chromium.launch(**launch_kwargs)

            ctx_kwargs: dict = {}
            if SESSION_FILE.exists():
                print(f"Loading saved session from {SESSION_FILE}")
                ctx_kwargs["storage_state"] = str(SESSION_FILE)

            context = await browser.new_context(**ctx_kwargs)
            # Hide the navigator.webdriver flag that sites use to detect automation.
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = await context.new_page()

            # Track any new tab/window the player opens.
            player_page: list = [page]

            def _on_new_page(new_pg) -> None:
                player_page[0] = new_pg

            context.on("page", _on_new_page)

            try:
                await self._ensure_authenticated(page, context)

                books = await self._get_shelf(page)
                if not books:
                    print("No audiobooks found on your shelf.")
                    return

                book = self._prompt_selection(books)
                if not book:
                    return

                # Pre-populate metadata with shelf info as a baseline.
                # Use a specific key to distinguish shelf title from scraped title.
                self.shelf_title = book.get("title", "")
                self.metadata["title"] = self.shelf_title
                self.metadata["author"] = book.get("author", "")

                # Register request capture on the context (not just one page) so
                # it covers new tabs and fires from the very first request,
                # including Part 01.
                context.on("request", self._on_request)

                await self._open_player(page, book)

                # If the player opened in a new tab, use that page.
                active = player_page[0]
                if active is not page:
                    print("  Player opened in a new tab — switching to it.")
                    try:
                        await active.wait_for_load_state("networkidle", timeout=60_000)
                    except Exception:
                        pass
                    await active.wait_for_timeout(2_000)

                await self._extract_bifocal(active)
                await self._seek_through_book(active)

                if not self.captured:
                    print("\nNo audio parts were captured. Cannot continue.")
                    return

                # Sort by part number so files download in order.
                self.captured.sort(key=lambda x: _part_number(x["filename"]))
                print(f"\nCaptured {len(self.captured)} part(s).")

                # Prioritize a sane title.
                m_title = self.metadata.get("title", "")
                if not m_title or m_title.lower() in ("libby", "about this audiobook", "audiobook", "now reading", "open this title"):
                    book_name = _safe(self.shelf_title)
                else:
                    book_name = _safe(m_title)

                await self._download_all(book_name)
                await self._verify_duration_and_refetch(active, book_name)
                self._write_cue(book_name)
                self._split_chapters(book_name)

            finally:
                # Always persist the session (including IndexedDB where Libby
                # stores auth tokens) so the next run skips the login step.
                try:
                    await context.storage_state(path=str(SESSION_FILE), indexed_db=True)
                except Exception:
                    try:
                        await context.storage_state(path=str(SESSION_FILE))
                    except Exception:
                        pass
                await browser.close()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def _ensure_authenticated(
        self, page: Page, context: BrowserContext
    ) -> None:
        await page.goto(LIBBY_URL, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(2_000)

        if await self._is_logged_in(page):
            print("Authenticated.")
            return

        print(
            "\nNot logged in. The browser window is open.\n"
            "Add your library card inside Libby (card number/email + PIN).\n"
            "Once your shelf is visible, come back here and press Enter.\n"
        )
        try:
            input(">>> Press Enter when logged in: ")
        except EOFError:
            pass

        try:
            await context.storage_state(path=str(SESSION_FILE), indexed_db=True)
        except Exception:
            await context.storage_state(path=str(SESSION_FILE))
        print(f"Session saved to {SESSION_FILE}\n")

    async def _is_logged_in(self, page: Page) -> bool:
        try:
            url = page.url
            # If Libby redirected us to the shelf, we're logged in.
            if "/shelf" in url:
                return True
            content = await page.content()
            # Libby embeds the word "loans" or "borrowed" in its app state
            # when a library card is registered.
            return (
                '"loans"' in content
                or '"borrowed"' in content
                or "data-media-id" in content
                or '"mybooks"' in content
            )
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Shelf
    # ------------------------------------------------------------------

    async def _get_shelf(self, page: Page) -> list[dict]:
        print("Loading shelf...")

        # Primary strategy: intercept the JSON shelf/loans API response.
        # We set up the listener BEFORE navigating so we don't miss the call.
        api_loans: list[dict] = []
        api_done = asyncio.Event()

        async def _on_shelf_response(resp) -> None:
            try:
                if resp.status != 200:
                    return
                ct = resp.headers.get("content-type", "")
                if "json" not in ct:
                    return
                data = await resp.json()
                # Accept any JSON body that contains a 'loans' or 'items' list
                # with objects that have a 'title' field.
                raw = data.get("loans") or data.get("items") or []
                if not isinstance(raw, list) or not raw:
                    return
                added = 0
                for loan in raw:
                    if not isinstance(loan, dict) or not loan.get("title"):
                        continue
                    media_type = (loan.get("type") or {}).get("id", "")
                    if media_type and media_type != "audiobook":
                        continue  # skip non-audiobooks only when type is known
                    api_loans.append({
                        "id": str(loan.get("id") or loan.get("titleId") or ""),
                        "card_id": str(loan.get("cardId") or loan.get("websiteId") or ""),
                        "title": loan.get("title", ""),
                        "author": loan.get("firstCreatorName", ""),
                        "href": None,
                    })
                    added += 1
                if added:
                    api_done.set()
            except Exception:
                pass

        page.on("response", _on_shelf_response)
        try:
            # Always navigate fresh so the shelf API call fires after the
            # listener is registered (avoids missing an already-loaded page).
            await page.goto(LIBBY_URL + "/shelf", wait_until="networkidle", timeout=30_000)
            await page.wait_for_timeout(2_000)
            try:
                await asyncio.wait_for(api_done.wait(), timeout=8)
            except asyncio.TimeoutError:
                pass
        except Exception:
            pass
        finally:
            page.remove_listener("response", _on_shelf_response)

        if api_loans:
            return sorted(api_loans, key=lambda b: b.get("title", "").lower())

        # Second strategy: read from localStorage (Libby caches shelf data).
        print("API interception found nothing — trying localStorage...")
        try:
            ls_books: list[dict] = await page.evaluate("""
                () => {
                    const results = [];
                    const seen = new Set();
                    for (let i = 0; i < localStorage.length; i++) {
                        const key = localStorage.key(i);
                        try {
                            const val = JSON.parse(localStorage.getItem(key));
                            if (!val || typeof val !== 'object') continue;
                            const loans = val.loans || val.items || [];
                            if (!Array.isArray(loans) || loans.length === 0) continue;
                            for (const loan of loans) {
                                if (!loan || !loan.title) continue;
                                const id = String(loan.id || loan.titleId || '');
                                if (seen.has(id)) continue;
                                seen.add(id);
                                const mt = (loan.type || {}).id || '';
                                if (mt && mt !== 'audiobook') continue;
                                results.push({
                                    id,
                                    title: loan.title || '',
                                    author: loan.firstCreatorName || '',
                                    href: null,
                                });
                            }
                        } catch (_) {}
                    }
                    return results;
                }
            """)
            if ls_books:
                return sorted(ls_books, key=lambda b: b.get("title", "").lower())
        except Exception:
            pass

        # Last resort: DOM scrape.
        print("localStorage empty — falling back to DOM scrape.")
        try:
            await page.wait_for_selector("[data-media-id]", timeout=10_000)
        except Exception:
            print("Warning: shelf cards not found in DOM either.")

        try:
            books: list[dict] = await page.evaluate("""
                () => {
                    const seen = new Set();
                    const results = [];
                    document.querySelectorAll('[data-media-id]').forEach(card => {
                        const id = card.getAttribute('data-media-id');
                        if (!id || seen.has(id)) return;
                        seen.add(id);

                        const titleEl = card.querySelector(
                            'h2, h3, [class*="title"]:not([aria-label]), [class*="name"]'
                        );
                        const authorEl = card.querySelector(
                            '[class*="author"], [class*="creator"], [class*="narrator"]'
                        );
                        const linkEl = card.querySelector(
                            'a[href*="open"], a[href*="audiobook"]'
                        );
                        results.push({
                            id,
                            title: titleEl ? titleEl.textContent.trim() : id,
                            author: authorEl ? authorEl.textContent.trim() : '',
                            href: linkEl ? linkEl.getAttribute('href') : null,
                        });
                    });
                    return results;
                }
            """)
        except Exception as exc:
            print(f"DOM scrape failed: {exc}")
            books = []

        return sorted(books, key=lambda b: b.get("title", "").lower())

    def _prompt_selection(self, books: list[dict]) -> Optional[dict]:
        print(f"\nFound {len(books)} item(s) on your shelf:\n")
        for i, b in enumerate(books, 1):
            suffix = f"  –  {b['author']}" if b["author"] else ""
            print(f"  {i:3}. {b['title']}{suffix}")
        print()

        while True:
            raw = input(f"Select [1-{len(books)}] or q to quit: ").strip()
            if raw.lower() == "q":
                return None
            try:
                idx = int(raw) - 1
                if 0 <= idx < len(books):
                    return books[idx]
            except ValueError:
                pass
            print(f"  Enter a number between 1 and {len(books)}.")

    # ------------------------------------------------------------------
    # Player navigation
    # ------------------------------------------------------------------

    async def _open_player(self, page: Page, book: dict) -> None:
        print(f"\nOpening: {book['title']}")

        # If the shelf gave us a direct href, try it first.
        href = book.get("href")
        if href:
            url = href if href.startswith("http") else LIBBY_URL + href
            await page.goto(url, wait_until="networkidle", timeout=60_000)
            await page.wait_for_timeout(3_000)
            if "/open/" in page.url:
                print(f"  Player URL: {page.url}")
                return

        # Try building the direct player URL from card+loan IDs captured via API.
        card_id = book.get("card_id", "")
        loan_id = book.get("id", "")
        if card_id and loan_id:
            direct_url = f"{LIBBY_URL}/open/loan/{card_id}/{loan_id}"
            print(f"  Trying direct URL: {direct_url}")
            await page.goto(direct_url, wait_until="networkidle", timeout=60_000)
            await page.wait_for_timeout(3_000)
            if "/open/" in page.url:
                print(f"  Player URL: {page.url}")
                return

        # Navigate to the shelf and try to click the Open Audiobook button.
        if "/shelf" not in page.url:
            await page.goto(LIBBY_URL + "/shelf", wait_until="networkidle", timeout=30_000)
            await page.wait_for_timeout(2_000)

        # Try text-based button matching (handles React onClick buttons with no href).
        title_lower = book["title"].lower()
        opened = False
        for label in ("Open Audiobook", "Listen", "Open", "Play"):
            try:
                # Find all buttons/links with this label text.
                locator = page.get_by_role("button", name=label, exact=False)
                count = await locator.count()
                if count == 0:
                    locator = page.get_by_role("link", name=label, exact=False)
                    count = await locator.count()
                if count == 0:
                    continue

                # If multiple, try to pick the one near our title.
                chosen = None
                for i in range(count):
                    el = locator.nth(i)
                    # Walk up the DOM to find a container with the book title.
                    card_text = await el.evaluate("""el => {
                        let node = el;
                        for (let i = 0; i < 8; i++) {
                            node = node.parentElement;
                            if (!node) break;
                            if (node.textContent.length < 2000) return node.textContent;
                        }
                        return '';
                    }""")
                    if title_lower in card_text.lower():
                        chosen = el
                        break
                if chosen is None and count == 1:
                    chosen = locator.first

                if chosen:
                    print(f"  Clicking '{label}' button...")
                    await chosen.click(timeout=15_000)
                    await page.wait_for_load_state("networkidle", timeout=60_000)
                    await page.wait_for_timeout(3_000)
                    opened = True
                    break
            except Exception:
                continue

        if not opened:
            print(
                "\n  Could not open the player automatically.\n"
                f"  Please click 'Open Audiobook' for '{book['title']}' in the browser,\n"
                "  wait for the player to fully load, then press Enter here."
            )
            try:
                input("  >>> Press Enter when player is open: ")
            except EOFError:
                await page.wait_for_timeout(10_000)

        await page.wait_for_timeout(2_000)
        print(f"  Player URL: {page.url}")

    # ------------------------------------------------------------------
    # BIFOCAL metadata / TOC extraction
    # ------------------------------------------------------------------

    async def _extract_bifocal(self, page: Page) -> None:
        """Extract metadata and TOC from the player.

        Tries three strategies in order:
        1. window.__BIFOCAL_DATA__ (populated by the player JS)
        2. <script id="BIFOCAL-data"> tag (older Libby versions)
        3. The visible Table of Contents panel in the player UI
        """
        print("Extracting metadata...")

        bifocal_js = r"""
            () => {
                // Strategy 1: well-known global variables.
                for (const name of ['__BIFOCAL_DATA__', 'BIFOCAL_DATA', '__bifocal__',
                                    'bifocalData', '__OD_DATA__', '__READER_DATA__']) {
                    if (window[name] && typeof window[name] === 'object'
                            && (window[name].readingOrder || window[name].nav))
                        return window[name];
                }

                // Strategy 2: <script id="BIFOCAL-data"> tag (older Libby).
                const el = document.getElementById('BIFOCAL-data');
                if (el) {
                    for (const line of el.textContent.split('\n')) {
                        const eq = line.indexOf('=');
                        if (eq < 0) continue;
                        const val = line.slice(eq + 1).trim().replace(/;$/, '');
                        if (val.startsWith('{')) {
                            try { return JSON.parse(val); } catch (_) {}
                        }
                    }
                }

                // Strategy 3: scan all inline <script> tags for JSON containing
                // readingOrder (the key BIFOCAL field we rely on).
                for (const s of document.querySelectorAll('script:not([src])')) {
                    const txt = s.textContent;
                    if (!txt.includes('readingOrder')) continue;
                    // Find the outermost {...} that contains readingOrder.
                    let depth = 0, start = -1;
                    for (let i = 0; i < txt.length; i++) {
                        if (txt[i] === '{') { if (depth++ === 0) start = i; }
                        else if (txt[i] === '}') {
                            if (--depth === 0 && start >= 0) {
                                try {
                                    const obj = JSON.parse(txt.slice(start, i + 1));
                                    if (obj.readingOrder || obj.nav) return obj;
                                } catch (_) {}
                                start = -1;
                            }
                        }
                    }
                }
                return null;
            }
        """

        data: Optional[dict] = None
        frames_to_check = [page.main_frame] + [
            f for f in page.frames if f is not page.main_frame
        ]
        # Give the player a moment to populate its globals.
        await page.wait_for_timeout(6_000)
        for frame in frames_to_check:
            try:
                data = await frame.evaluate(bifocal_js)
                if data:
                    break
            except Exception:
                continue

        if not data:
            # Strategy 3: scrape the visible TOC panel in the player UI.
            data = await self._extract_toc_from_ui(page)

        if not data:
            print("  Warning: could not extract metadata; metadata will be empty.")
            return

        # Creators
        for creator in data.get("creator", []):
            name = creator.get("name", "")
            role = creator.get("role", "")
            if name and role == "author":
                self.metadata["author"] = name
            elif name and role == "narrator":
                self.metadata["narrator"] = name

        # Title
        t = data.get("title", {})
        if isinstance(t, dict):
            if t.get("main"):
                self.metadata["title"] = t["main"]
            if t.get("subtitle"):
                self.metadata["subtitle"] = t["subtitle"]
            if t.get("collection"):
                self.metadata["series"] = t["collection"]
        elif isinstance(t, str):
            self.metadata["title"] = t

        # Description
        desc = data.get("description", {})
        if isinstance(desc, dict):
            self.metadata["description"] = desc.get("full", "")
        elif isinstance(desc, str):
            self.metadata["description"] = desc

        # Cover art URL
        cover = data.get("cover150Wide", {})
        if isinstance(cover, dict):
            self.metadata["cover_url"] = cover.get("href", "")
        elif isinstance(cover, str):
            self.metadata["cover_url"] = cover

        # Reading order – list of {href, duration} for each audio part.
        self.reading_order = data.get("readingOrder", [])
        if self.reading_order:
            self.total_book_duration = sum(
                float(item.get("duration", 0)) for item in self.reading_order
            )
            print(f"  Total duration (reading order): {self.total_book_duration / 3600:.2f}h")

        # Table of contents
        raw_toc_rows = []
        for entry in data.get("nav", {}).get("toc", []):
            raw_full = entry.get("title", "")
            raw = raw_full
            # Strip trailing human-readable duration Libby embeds in the title
            # (e.g. "13 minutes 26 seconds", "1 hour 12 minutes 8 seconds").
            # The separator may be \n, a bullet character, or nothing at all.
            raw = re.sub(
                r'[\s\W]*\d+\s+hours?\s*(?:\d+\s+minutes?\s*)?(?:\d+\s+seconds?\s*)?$'
                r'|[\s\W]*\d+\s+minutes?\s*(?:\d+\s+seconds?\s*)?$'
                r'|[\s\W]*\d+\s+seconds?\s*$',
                '', raw, flags=re.IGNORECASE | re.MULTILINE,
            ).strip().split('\n')[0].strip()
            # More permissive title cleaning to avoid losing info
            title = re.sub(r"[^\w\s\-.,!?()'&]", "", raw).strip()
            # Drop empty/junk titles early.
            if not title:
                continue
            # If the "title" is actually just a duration string (digits or word-numbers),
            # drop it. This catches things like "1 hour 28 minutes one second".
            dur_only_pat = (
                r"(?i)^\s*"
                r"(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+hours?\s*)?"
                r"(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+minutes?\s*)?"
                r"(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+seconds?\s*)?"
                r"\s*$"
            )
            # Only consider it duration-only if it contains a time unit word;
            # otherwise plain numeric titles like "1984" are valid book content.
            if re.search(r"(?i)\b(hours?|minutes?|seconds?)\b", title) and re.fullmatch(dur_only_pat, title):
                continue
            path = entry.get("path", "")
            file_key, offset = _parse_toc_path(path)
            # Some Libby titles include a visible timestamp (e.g. "Chapter 1\n1:19:42")
            # but the path does not include a "#seconds" fragment. In that case,
            # recover the timestamp from the full raw title so we don't collapse
            # everything to offset 0.
            if (not offset) and raw_full:
                ts = _timestamp_to_seconds(raw_full)
                if ts > 0:
                    offset = ts
            if file_key:
                raw_toc_rows.append({"file_key": file_key, "title": title, "offset": offset, "raw_title": raw_full})
                # Deduplicate within the same file_key (Libby sometimes repeats items).
                bucket = self.toc.setdefault(file_key, [])
                if not any(e.get("title") == title and int(e.get("offset", -1)) == int(offset) for e in bucket):
                    bucket.append({"title": title, "offset": offset})

        dur_str = _fmt_hms(self.total_book_duration) if self.total_book_duration > 0 else "unknown"
        print(
            f"  Title:    {self.metadata.get('title', '(unknown)')}\n"
            f"  Author:   {self.metadata.get('author', '(unknown)')}\n"
            f"  Parts:    {len(self.reading_order)} (from readingOrder)\n"
            f"  TOC:      {sum(len(v) for v in self.toc.values())} chapters\n"
            f"  Duration: {dur_str}"
        )

    async def _extract_toc_from_ui(self, page: Page) -> Optional[dict]:
        """Click the TOC/Chapters button in the player UI and scrape chapter
        titles + timestamps from the resulting panel.  Returns a minimal
        BIFOCAL-compatible dict with just enough for self.toc to be populated,
        or None if nothing is found.
        """
        frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame]

        # Try to open the TOC panel by clicking a chapters/TOC button.
        toc_btn = None
        toc_frame = None
        toc_labels = ("Table of Contents", "Chapters", "Contents", "TOC")
        for frame in frames:
            for label in toc_labels:
                try:
                    btn = frame.get_by_role("button", name=label, exact=False)
                    if await btn.count() == 0:
                        btn = frame.get_by_role("link", name=label, exact=False)
                    if await btn.count() > 0:
                        await btn.first.click(timeout=5_000)
                        toc_btn = btn.first
                        toc_frame = frame
                        self._toc_btn = toc_btn
                        self._toc_frame = toc_frame
                        await page.wait_for_timeout(2_000)
                        break
                except Exception:
                    continue
            if toc_btn is not None:
                break

        # Scrape chapter rows FIRST while TOC is definitely open.
        toc_js = """
            () => {
                const rows = [];
                const seen = new Set();
                const blacklist = [
                    'close', 'back', 'playback speed', 'sleep timer', 'bookmarks', 
                    'place bookmark', 'about this audiobook', 'libby', 'now reading',
                    'search', 'library', 'menu', 'shelf', 'tags', 'open this title',
                    'audiobook', 'listening', 'player', 'loading'
                ];

                const candidates = [
                    ...document.querySelectorAll('li, [role="listitem"], [class*="item"], [class*="row"]')
                ];
                
                for (const el of candidates) {
                    const text = el.textContent.trim();
                    if (!text || text.length < 2) continue;
                    
                    const lowerText = text.toLowerCase();
                    if (blacklist.some(b => lowerText.includes(b))) continue;

                    let title = null, timestamp = null;

                    // Strategy A: Look for child elements with specific class names
                    const titleEl = el.querySelector('[class*="title"], [class*="name"], [class*="label"], [class*="heading"], [class*="text"]');
                    const timeEl = el.querySelector('[class*="time"], [class*="duration"], [class*="position"], [class*="timestamp"]');
                    
                    if (titleEl) {
                        const tText = titleEl.textContent.trim();
                        if (tText && tText.length > 0 && tText.length < 200 && !blacklist.some(b => tText.toLowerCase().includes(b))) {
                            title = tText;
                        }
                    }
                    if (timeEl) {
                        timestamp = timeEl.textContent.trim();
                    }

                    // Strategy B: Fallback to parsing textContent
                    if (!title || !timestamp) {
                        // Colon format: "1:23" or "1:23:45"
                        const tsColon = text.match(/(\\d+:\\d{2}(?::\\d{2})?)/);
                        if (tsColon) {
                            if (!timestamp) timestamp = tsColon[1];
                            if (!title) title = text.replace(tsColon[1], '').trim();
                        } else {
                            // Text-format duration anchored to end: "12 minutes 5 seconds"
                            const tsDur = text.match(/(.*?\\D)((?:\\d+\\s+hours?\\s*)?(?:\\d+\\s+minutes?\\s*)?\\d+\\s+seconds?\\s*$|(?:\\d+\\s+hours?\\s*)?\\d+\\s+minutes?\\s*$)/i);
                            if (tsDur && tsDur[2]) {
                                if (!timestamp) timestamp = tsDur[2].trim();
                                if (!title) title = tsDur[1].replace(/[\\s\\W]+$/, '').trim();
                            } else if (!title && text.length < 200) {
                                title = text;
                            }
                        }
                    }

                    if (title) {
                        title = title.replace(/\\s+/g, ' ').trim();
                        if (blacklist.some(b => title.toLowerCase() === b)) continue;
                        if (title.length < 2) continue;
                    }

                    if (title && !seen.has(title + timestamp)) {
                        seen.add(title + timestamp);
                        rows.push({ title, timestamp: timestamp || null });
                    }
                }
                return rows;
            }
        """

        chapters = []
        for frame in frames:
            try:
                rows = await frame.evaluate(toc_js)
                if rows and len(rows) > 0:
                    chapters = rows
                    break
            except Exception:
                continue

        # NOW extract metadata from "About" panel while TOC is open.
        await self._extract_about_panel(page, frames)

        # Deduplicate and filter out junk from Python side.
        # Ensure we have timestamps for the chapters.
        final_chapters = []
        seen_ch = set()
        for ch in chapters:
            t = ch['title'].strip()
            ts = (ch['timestamp'] or '').strip()
            if not t or t == ts: continue
            # Filter out UI junk that might have slipped through
            t_low = t.lower()
            if any(b in t_low for b in ('playback speed', 'sleep timer', 'place bookmark', 'bookmarks', 'libby', 'close audiobook')):
                continue
            
            if (t, ts) in seen_ch: continue
            seen_ch.add((t, ts))
            final_chapters.append(ch)

        if not final_chapters:
            print("  Warning: could not find chapters in TOC panel.")
            return {"nav": {"toc": []}}

        print(f"  Found {len(final_chapters)} chapters in player TOC panel.")

        # Scrape the total book duration from the player's timeline elements.
        # timeline-start-minutes = current position (positive, e.g. "1:23:45")
        # timeline-end-minutes   = time remaining  (negative, e.g. "-22:55:13")
        # total = abs(end) + start  (works regardless of current playback position)
        if self.total_book_duration <= 0:
            timeline_js = _TIMELINE_JS
            for frame in frames:
                try:
                    result = await frame.evaluate(timeline_js)
                    if result and isinstance(result, dict):
                        start_s = _parse_timeline_seconds(result.get("start"))
                        end_s = _parse_timeline_seconds(result.get("end"))
                        total_s = start_s + end_s
                        print(f"  Total book duration: {_fmt_hms(total_s)}")
                        if total_s > 0:
                            self.total_book_duration = total_s
                            break
                except Exception:
                    continue

        # Click the first chapter entry to reset the player
        print("  Navigating player to start of book...")
        for frame in frames:
            try:
                # Target clickable element within the first li that looks like a real chapter
                rows = frame.locator("li, [role='listitem'], [class*='item']").filter(has_text=re.compile(r"Chapter|Part|01|Introduction", re.I))
                count = await rows.count()
                for i in range(min(count, 5)):
                    row = rows.nth(i)
                    txt = (await row.text_content()).lower()
                    if "about this" in txt or "sleep" in txt or "speed" in txt: continue
                    clickable = row.locator("a, button, [role='button']").first
                    if await clickable.count() > 0:
                        await clickable.click(timeout=5_000)
                    else:
                        await row.click(timeout=5_000)
                    await page.wait_for_timeout(2_000)
                    break
                else: continue
                break
            except Exception:
                continue

        if toc_frame is not None:
            try:
                await toc_frame.locator("body").press("Escape")
                await page.wait_for_timeout(800)
            except Exception:
                pass

        def _ts_to_sec(ts: Optional[str]) -> int:
            if not ts: return 0
            ts = ts.strip().lower()
            if ":" in ts:
                parts = ts.split(":")
                try:
                    if len(parts) == 2: return int(parts[0]) * 60 + int(parts[1])
                    if len(parts) == 3: return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                except ValueError: pass
            
            total = 0
            # Handle "one", "two" etc. if they appear
            words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
            
            def get_val(text, unit_pattern):
                m = re.search(r'(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+' + unit_pattern, text)
                if m:
                    val = m.group(1)
                    return words.get(val, int(val) if val.isdigit() else 0)
                return 0

            total += get_val(ts, r'hours?') * 3600
            total += get_val(ts, r'minutes?') * 60
            total += get_val(ts, r'seconds?')
            return total

        nav_toc = [
            {"title": ch["title"], "path": f"#{ _ts_to_sec(ch['timestamp']) }"}
            for ch in final_chapters
        ]
        return {"nav": {"toc": nav_toc}}

    async def _extract_about_panel(self, page: Page, frames: list) -> None:
        """Click 'About This Audiobook' in the TOC panel to get title/author/description."""
        about_btn = None
        about_frame = None
        for frame in frames:
            try:
                btn = frame.get_by_text("About This Audiobook", exact=False)
                if await btn.count() > 0:
                    about_btn = btn.first
                    about_frame = frame
                    break
            except Exception:
                continue

        if about_btn is None:
            return

        try:
            print("  Opening 'About This Audiobook' for extra metadata...")
            await about_btn.click(timeout=5_000)
            await page.wait_for_timeout(2_000)
        except Exception:
            return

        # Scrape title, author, and description from the about panel.
        about_js = """
            () => {
                const get = (sel) => {
                    const els = Array.from(document.querySelectorAll(sel));
                    for (const el of els) {
                        const t = el.textContent.trim();
                        // Title/Author should be reasonably short and not have many newlines
                        if (t && t.length > 2 && t.length < 150 && t.split('\\n').length < 3) return t;
                    }
                    return null;
                };
                
                const getDesc = () => {
                    const containers = Array.from(document.querySelectorAll('div, section, p'));
                    const descContainer = containers.find(c => {
                        const t = c.textContent.toLowerCase();
                        return (t.includes('description') || t.includes('summary')) && t.length > 50 && t.length < 5000;
                    });
                    if (descContainer) {
                        const p = descContainer.querySelector('p') || descContainer;
                        return p.textContent.trim();
                    }
                    return null;
                };

                return {
                    title:  get('h1') || get('h2') || get('[class*="title"]'),
                    author: get('[class*="author"]') || get('[class*="creator"]'),
                    narrator: get('[class*="narrator"]') || get('[class*="reader"]'),
                    description: getDesc(),
                };
            }
        """
        for frame in frames:
            try:
                info = await frame.evaluate(about_js)
                if info:
                    # Update metadata only with sane values. 
                    # DO NOT overwrite if shelf title is already better.
                    t = info.get("title")
                    if t and 3 < len(t) < 100 and t.lower() not in ("libby", "about this audiobook", "audiobook"):
                        # If current title is junk or from shelf but this one is more detailed
                        if self.metadata["title"].lower() in ("libby", "about this audiobook") or \
                           (len(t) > len(self.metadata["title"]) and self.shelf_title.lower() in t.lower()):
                            self.metadata["title"] = t
                    
                    a = info.get("author")
                    if a and 3 < len(a) < 100 and not any(x in a.lower() for x in ("narrator", "read by")):
                        self.metadata["author"] = a
                    
                    n = info.get("narrator")
                    if n and 3 < len(n) < 100:
                        self.metadata["narrator"] = n
                    
                    if info.get("description"):
                        self.metadata["description"] = info["description"]
                    
                    if t and t.lower() not in ("libby", "about this audiobook"):
                        break
            except Exception:
                continue

        try:
            if about_frame is not None:
                await about_frame.locator("body").press("Escape")
                await page.wait_for_timeout(1_000)
        except Exception:
            pass


    def _on_request(self, request: Request) -> None:
        """Called synchronously for every browser request.

        We capture requests whose URL looks like an audio part file
        (e.g. BookTitle-Part003.mp3).  The URL plus its original headers
        are stored so we can re-execute the request ourselves later.
        """
        try:
            url = request.url
            resource_type = request.resource_type

            # Libby's part file requests show up as 'media' or 'xhr'.
            if resource_type not in ("media", "xhr", "fetch", "other"):
                return

            # Skip CDN cache responses – they tend to fail on replay.
            if "cachefly" in url:
                return

            # Match only filenames like BookTitle-Part001.mp3
            clean_url = url.split("?")[0]
            filename = clean_url.split("/")[-1]
            if not re.search(r"[Pp]art\d+\.mp3$", filename):
                return

            # One entry per unique filename (first URL wins so we keep the
            # original Libby URL rather than any CDN redirect).
            if filename in self.captured_filenames:
                return

            headers = {
                k: v
                for k, v in request.headers.items()
                if not k.startswith(":")  # drop HTTP/2 pseudo-headers
            }

            self.captured_filenames.add(filename)
            self.captured.append(
                {"url": url, "headers": headers, "filename": filename}
            )
            print(f"  Captured: {filename}  ({len(self.captured)} total)")

        except Exception:
            pass  # Never crash the event loop from inside a listener

    # ------------------------------------------------------------------
    # Seeking through the book
    # ------------------------------------------------------------------

    async def _seek_through_book(self, page: Page) -> None:
        """Drive the player to load every audio part."""
        print("\nSeeking through book to trigger all part requests...")

        await self._start_playback(page)
        await page.wait_for_timeout(2_000)

        # Reset to the very beginning regardless of any saved position.
        print("  Resetting to start of book...")
        await self._set_audio_time(page, 0)
        await page.wait_for_timeout(2_000)

        if self.reading_order:
            await self._seek_by_reading_order(page)
        elif self.toc:
            await self._seek_by_toc(page)
        else:
            await self._seek_by_duration(page)

        # Extra wait for any in-flight requests to resolve.
        await page.wait_for_timeout(3_000)

        # Validate that we captured the expected set of parts and retry if needed.
        await self._ensure_all_parts_captured(page)

        # If we know the total book duration, advance through any content that
        # falls after the last TOC chapter (e.g. a final part not in the TOC).
        # This must run BEFORE _stop_playback while the audio element is live.
        if self.total_book_duration > 0:
            await self._advance_to_book_end(page)

        # Stop playback so the book isn't left running in the background.
        await self._stop_playback(page)

    async def _button_sweep_for_gaps(self, page: Page) -> None:
        """Sweep the book from the start using chapter-bar UI buttons to fill gaps.

        Called after the TOC panel is closed (so chapter-bar buttons are visible).
        Navigates to the start of the book by reopening the TOC and clicking
        the first chapter, then advances chapter-by-chapter.  When the sequential
        frontier stalls (a part between two TOC chapters was not triggered),
        falls back to 15-second skip-forward clicks until the gap is filled.
        """
        frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame]

        NEXT_CH = ("button.chapter-bar-next-button", ".chapter-bar-next-button")
        PREV_CH = ("button.chapter-bar-prev-button", ".chapter-bar-prev-button")
        SKIP_FWD = (
            "button.playback-jump-ahead",
            "[aria-label='Advance 15 seconds']",
            ".playback-jump-ahead",
        )

        async def click_btn(selectors) -> bool:
            for frame in frames:
                for sel in selectors:
                    try:
                        loc = frame.locator(sel).first
                        if await loc.count() > 0 and await loc.is_visible(timeout=500):
                            await loc.click(timeout=3_000)
                            return True
                    except Exception:
                        continue
            return False

        # ── Navigate to the very start via the first TOC chapter ─────────────
        print("  Navigating to start of book...")
        navigated = False
        if self._toc_btn is not None:
            try:
                await self._toc_btn.click(timeout=5_000)
                await page.wait_for_timeout(1_500)
                for frame in frames:
                    for sel in (
                        "[class*='toc'] li", "[class*='chapter'] li",
                        "[class*='contents'] li", "[role='listitem']",
                    ):
                        try:
                            loc = frame.locator(sel)
                            cnt = await loc.count()
                            if cnt == 0:
                                continue
                            for idx in range(cnt):
                                text = (await loc.nth(idx).text_content() or "").lower()
                                if "about this audiobook" in text:
                                    continue
                                clickable = loc.nth(idx).locator(
                                    "a, button, [role='button']"
                                ).first
                                if await clickable.count() > 0:
                                    await clickable.click(timeout=3_000)
                                else:
                                    await loc.nth(idx).click(timeout=3_000)
                                await page.wait_for_timeout(1_500)
                                navigated = True
                                break
                            if navigated:
                                break
                        except Exception:
                            continue
                    if navigated:
                        break
                # Close TOC
                if self._toc_frame is not None:
                    try:
                        await self._toc_frame.locator("body").press("Escape")
                        await page.wait_for_timeout(800)
                    except Exception:
                        pass
            except Exception:
                pass

        if not navigated:
            # Fallback: click chapter-bar-prev-button until we're at/near the start.
            print("  Navigating to start via chapter-prev button...")
            for back_step in range(200):
                covered = 0
                for frame in frames:
                    try:
                        result = await frame.evaluate(_TIMELINE_JS)
                        if result and isinstance(result, dict):
                            covered = _parse_timeline_seconds(result.get("start"))
                            break
                    except Exception:
                        continue
                if covered <= 30:
                    navigated = True
                    break
                prev_clicked = False
                for frame in frames:
                    for sel in PREV_CH:
                        try:
                            loc = frame.locator(sel).first
                            if await loc.count() > 0 and await loc.is_visible(timeout=500):
                                await loc.click(timeout=2_000)
                                prev_clicked = True
                                break
                        except Exception:
                            continue
                    if prev_clicked:
                        break
                if not prev_clicked:
                    break
                await page.wait_for_timeout(500)

        # ── Sweep forward: chapter-next with 15s-skip gap filling ────────────
        frontier = _seq_frontier(self.captured)
        print(f"  Sweeping forward from Part {frontier}...")

        for step in range(200):
            # Stop when at end of book
            remaining = 0
            for frame in frames:
                try:
                    result = await frame.evaluate(_TIMELINE_JS)
                    if result and isinstance(result, dict):
                        remaining = _parse_timeline_seconds(result.get("end"))
                        break
                except Exception:
                    continue
            if remaining <= 30:
                break

            # Stop if all expected parts are captured
            nums = {_part_number(p["filename"]) for p in self.captured
                    if _part_number(p["filename"]) > 0}
            expected_from_ro = len(self.reading_order) if self.reading_order else 0
            expected_total = max(expected_from_ro, max(nums) if nums else 0)
            if expected_total > 0 and _seq_frontier(self.captured) >= expected_total:
                break

            prev_frontier = frontier

            # Click next-chapter
            if not await click_btn(NEXT_CH):
                break

            await page.wait_for_timeout(2_000)
            frontier = _seq_frontier(self.captured)

            if frontier <= prev_frontier:
                # The chapter jump skipped a part — skip forward to fill the gap.
                print(f"  Filling gap at Part {prev_frontier + 1}...")
                for batch in range(300):
                    frontier = _seq_frontier(self.captured)
                    if frontier > prev_frontier:
                        break
                    clicks_done = 0
                    for frame in frames:
                        for sel in SKIP_FWD:
                            try:
                                loc = frame.locator(sel).first
                                if await loc.count() > 0 and await loc.is_visible(timeout=1_000):
                                    for _ in range(self.skip_clicks):
                                        await loc.click(timeout=2_000)
                                        clicks_done += 1
                                    break
                            except Exception:
                                continue
                        if clicks_done > 0:
                            break
                    if clicks_done == 0:
                        break
                    await page.wait_for_timeout(1_000)

    async def _fill_part_gap(self, page: Page, missing_parts: list[int]) -> None:
        """Fill a gap in captured parts using player UI navigation buttons.

        When a TOC chapter click jumps from Part N to N+2 (skipping N+1), call
        this method with the list of missing part numbers.  It:
          1. Clicks chapter-bar-prev-button to step back before the gap.
          2. Clicks playback-jump-ahead (15 s) repeatedly until every missing
             part has been captured.
          3. Clicks chapter-bar-next-button once to return past the gap.

        All navigation uses the player UI buttons, which work even when Libby
        has unloaded the <audio> element from the DOM.
        """
        PREV_CH_SELECTORS = (
            "button.chapter-bar-prev-button",
            ".chapter-bar-prev-button",
            "[aria-label*='previous chapter' i]",
        )
        SKIP_FWD_SELECTORS = (
            "button.playback-jump-ahead",
            "[aria-label='Advance 15 seconds']",
            ".playback-jump-ahead",
        )
        NEXT_CH_SELECTORS = (
            "button.chapter-bar-next-button",
            ".chapter-bar-next-button",
            "[aria-label*='next chapter' i]",
        )

        frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame]
        target_set = set(missing_parts)
        print(f"    Filling missing parts: {sorted(missing_parts)}")

        # ── Step 1: go back one chapter to position before the gap ───────────
        clicked_prev = False
        for frame in frames:
            for sel in PREV_CH_SELECTORS:
                try:
                    loc = frame.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible(timeout=1_000):
                        await loc.click(timeout=3_000)
                        pass
                        clicked_prev = True
                        break
                except Exception:
                    continue
            if clicked_prev:
                break

        if not clicked_prev:
            print("    Warning: prev-chapter button not found — skipping gap fill")
            return

        await page.wait_for_timeout(2_000)

        # ── Step 2: 15-second skips until all missing parts are captured ──────
        # Click skip-forward self.skip_clicks times per batch then wait 1 s.
        MAX_BATCHES = 300
        for batch_num in range(MAX_BATCHES):
            captured_nums = {_part_number(p["filename"]) for p in self.captured}
            still_missing = target_set - captured_nums
            if not still_missing:
                pass
                break

            clicks_done = 0
            for frame in frames:
                for sel in SKIP_FWD_SELECTORS:
                    try:
                        loc = frame.locator(sel).first
                        if await loc.count() > 0 and await loc.is_visible(timeout=1_000):
                            for _ in range(self.skip_clicks):
                                await loc.click(timeout=2_000)
                                clicks_done += 1
                            break
                    except Exception:
                        continue
                if clicks_done > 0:
                    break

            if clicks_done == 0:
                pass
                break

            await page.wait_for_timeout(1_000)

            if batch_num % 5 == 4:
                captured_nums = {_part_number(p["filename"]) for p in self.captured}
                still_missing = target_set - captured_nums
                print(f"    Still missing: {sorted(still_missing)}")
        else:
            captured_nums = {_part_number(p["filename"]) for p in self.captured}
            still_missing = target_set - captured_nums
            if still_missing:
                print(f"    Warning: still missing after {MAX_BATCHES} batches: {sorted(still_missing)}")

        # ── Step 3: click next-chapter to return past the gap ────────────────
        await page.wait_for_timeout(1_000)
        for frame in frames:
            for sel in NEXT_CH_SELECTORS:
                try:
                    loc = frame.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible(timeout=1_000):
                        await loc.click(timeout=3_000)
                        pass
                        break
                except Exception:
                    continue
            else:
                continue
            break

        await page.wait_for_timeout(2_000)

    async def _advance_to_book_end(self, page: Page) -> None:
        """Advance through any audio parts that follow the last TOC chapter.

        After TOC-based seeking the player sits at the last chapter's position.
        Parts that begin after that chapter are never triggered.  We detect the
        gap by reading the timeline clocks, then use the player's own UI buttons
        to navigate forward — these work even when the audio element is absent
        from the regular DOM (Libby unloads it after pausing).

        Primary:  chapter-bar-next-button  — jumps to the next chapter boundary.
                  Clicking it from the last chapter advances into trailing content
                  and forces Libby to load the next audio part.
        Fallback: 15-second skip-forward button in playback-controls-right.
        """
        THRESHOLD = 30.0
        frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame]

        # Player navigation buttons (confirmed HTML class names):
        #   Next chapter:    <button class="chapter-bar-next-button chapter-bar-jump-button halo">
        #   Prev chapter:    <button class="chapter-bar-prev-button chapter-bar-jump-button halo">
        #   +15 s forward:   <button class="playback-jump playback-jump-ahead halo"
        #                           aria-label="Advance 15 seconds">
        #   -15 s rewind:    <button class="playback-jump playback-jump-behind halo"
        #                           aria-label="Rewind 15 seconds">
        NEXT_CH_SELECTORS = (
            "button.chapter-bar-next-button",
            ".chapter-bar-next-button",
            "[aria-label*='next chapter' i]",
        )
        SKIP_FWD_SELECTORS = (
            "button.playback-jump-ahead",
            "[aria-label='Advance 15 seconds']",
            ".playback-jump-ahead",
        )

        print("  Checking for content beyond last TOC chapter...")

        for attempt in range(40):  # up to 40 nav clicks max
            # ── Read current timeline position ─────────────────────────────
            remaining = 0
            for frame in frames:
                try:
                    result = await frame.evaluate(_TIMELINE_JS)
                    if result and isinstance(result, dict):
                        remaining = _parse_timeline_seconds(result.get("end"))
                        break
                except Exception:
                    continue

            if remaining <= THRESHOLD:
                break

            prev = len(self.captured)

            # ── Strategy 1: click the next-chapter button ───────────────────
            clicked = False
            for frame in frames:
                for sel in NEXT_CH_SELECTORS:
                    try:
                        loc = frame.locator(sel).first
                        if await loc.count() > 0 and await loc.is_visible(timeout=1_000):
                            await loc.click(timeout=3_000)
                            clicked = True
                            break
                    except Exception:
                        continue
                if clicked:
                    break

            # ── Strategy 2: 15-second skip-forward button ───────────────────
            if not clicked:
                for frame in frames:
                    for sel in SKIP_FWD_SELECTORS:
                        try:
                            loc = frame.locator(sel).first
                            if await loc.count() > 0 and await loc.is_visible(timeout=1_000):
                                n_clicks = max(1, min(int(remaining / 15) + 2, 50))
                                for _ in range(n_clicks):
                                    await loc.click(timeout=2_000)
                                    await page.wait_for_timeout(150)
                                clicked = True
                                break
                        except Exception:
                            continue
                    if clicked:
                        break

            if not clicked:
                break

            await page.wait_for_timeout(3_000)

            if len(self.captured) > prev:
                print(f"  Captured trailing part (total: {len(self.captured)})")

    async def _start_playback(self, page: Page) -> None:
        # Libby's player runs inside an iframe; search all frames.
        selectors = (
            '[aria-label="Play"]',
            '[aria-label*="play" i]',
            'button[class*="play"]',
            '[title="Play"]',
        )
        frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame]
        for frame in frames:
            for selector in selectors:
                try:
                    await frame.wait_for_selector(selector, timeout=3_000)
                    btn = await frame.query_selector(selector)
                    if btn:
                        await btn.click(timeout=10_000)
                        return
                except Exception:
                    continue
        print("  Warning: could not find play button — player may already be playing.")

    async def _seek_by_reading_order(self, page: Page) -> None:
        """Seek to the start of each part using durations from the reading order.

        The reading order gives us the duration of each part, so we can
        compute each part's global start time and seek there directly.
        """
        total = len(self.reading_order)
        cumulative = 0.0

        for i, item in enumerate(self.reading_order):
            duration = float(item.get("duration", 0))
            href = item.get("href") or item.get("url") or ""
            filename = href.split("?")[0].split("/")[-1]

            print(f"  [{i + 1}/{total}] {filename or '(unknown)'}")

            # Seek to 2 seconds into this part to ensure the request fires.
            await self._set_audio_time(page, cumulative + 2.0)
            await page.wait_for_timeout(1_000)

            # Wait up to 10 s for a NEW capture to arrive. Matching by filename
            # is unreliable because readingOrder hrefs can be URL-encoded or
            # differ from the actual request URL.
            prev = len(self.captured)
            for _ in range(20):
                if len(self.captured) > prev:
                    break
                await page.wait_for_timeout(500)

            cumulative += duration

    async def _seek_by_toc(self, page: Page) -> None:
        """Seek through the book by reopening the TOC and clicking every chapter.

        Clicking a chapter tells Libby's player to load that audio position,
        triggering an HTTP request for the corresponding part file.
        Falls back to asking the user to manually fast-forward.
        """
        frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame]

        # ------------------------------------------------------------------
        # Strategy 1: reopen the TOC panel and click every chapter entry.
        # ------------------------------------------------------------------
        if self._toc_btn is not None:
            try:
                print("  Reopening TOC to click through all chapters...")
                await self._toc_btn.click(timeout=5_000)
                await page.wait_for_timeout(2_000)

                # Find all clickable chapter entries.
                chapter_containers = None
                for frame in frames:
                    for sel in (
                        "[class*='toc'] li", "[class*='chapter'] li",
                        "[class*='contents'] li", "[role='listitem']",
                    ):
                        try:
                            loc = frame.locator(sel)
                            count = await loc.count()
                            if count > 0:
                                # Verify this isn't just one big container
                                first_text = await loc.first.text_content()
                                if "about this audiobook" in first_text.lower():
                                    if count == 1: continue
                                chapter_containers = loc
                                break
                        except Exception:
                            continue
                    if chapter_containers is not None:
                        break

                if chapter_containers is not None:
                    count = await chapter_containers.count()
                    print(f"  Clicking {count} chapter rows to load parts...")
                    prev_frontier = _seq_frontier(self.captured)
                    for i in range(count):
                        container = chapter_containers.nth(i)
                        text = await container.text_content()
                        if "about this audiobook" in text.lower():
                            continue

                        prev_cap = len(self.captured)
                        try:
                            # Try to find a specific clickable element inside, or click the row
                            clickable = container.locator("a, button, [role='button']").first
                            if await clickable.count() > 0:
                                await clickable.click(timeout=3_000)
                            else:
                                await container.click(timeout=3_000)
                            await page.wait_for_timeout(1_500)
                        except Exception:
                            continue

                        new_cap = len(self.captured)
                        if new_cap > prev_cap:
                            print(f"    [{i+1}/{count}] {new_cap} part(s) captured")

                        # If a new part appeared but the sequential frontier didn't
                        # advance, the chapter jump skipped over a part.  Fill the
                        # gap immediately: close TOC, go back one chapter, skip
                        # forward, then reopen TOC to continue the loop.
                        curr_frontier = _seq_frontier(self.captured)
                        if new_cap > prev_cap and curr_frontier <= prev_frontier:
                            print(f"    [{i+1}/{count}] Gap at Part {curr_frontier + 1} — filling...")
                            # Close TOC so player buttons become visible.
                            if self._toc_frame is not None:
                                try:
                                    await self._toc_frame.locator("body").press("Escape")
                                    await page.wait_for_timeout(800)
                                except Exception:
                                    pass
                            # Go back TWO chapters to land just before the gap.
                            # First click: goes to start of current chapter (N).
                            # Second click: goes to start of chapter N-1 (before the gap).
                            for _back in range(2):
                                for frame in frames:
                                    for sel in ("button.chapter-bar-prev-button",
                                                ".chapter-bar-prev-button"):
                                        try:
                                            loc = frame.locator(sel).first
                                            if (await loc.count() > 0
                                                    and await loc.is_visible(timeout=1_000)):
                                                await loc.click(timeout=3_000)
                                                break
                                        except Exception:
                                            continue
                                    else:
                                        continue
                                    break
                                await page.wait_for_timeout(1_000)
                            await page.wait_for_timeout(1_000)
                            # Skip forward in batches until the frontier advances.
                            for batch in range(300):
                                if _seq_frontier(self.captured) > prev_frontier:
                                    break
                                clicks_done = 0
                                for frame in frames:
                                    for sel in ("button.playback-jump-ahead",
                                                "[aria-label='Advance 15 seconds']",
                                                ".playback-jump-ahead"):
                                        try:
                                            loc = frame.locator(sel).first
                                            if (await loc.count() > 0
                                                    and await loc.is_visible(timeout=1_000)):
                                                for _ in range(self.skip_clicks):
                                                    await loc.click(timeout=2_000)
                                                    clicks_done += 1
                                                break
                                        except Exception:
                                            continue
                                    if clicks_done > 0:
                                        break
                                if clicks_done == 0:
                                    break
                                await page.wait_for_timeout(1_000)
                            # Reopen the TOC to continue the chapter loop.
                            try:
                                await self._toc_btn.click(timeout=5_000)
                                await page.wait_for_timeout(1_500)
                            except Exception:
                                pass

                        prev_frontier = _seq_frontier(self.captured)

                # Close the TOC again.
                if self._toc_frame is not None:
                    try:
                        await self._toc_frame.locator("body").press("Escape")
                        await page.wait_for_timeout(800)
                    except Exception:
                        pass
                return

            except Exception as e:
                print(f"  TOC chapter-click failed ({e}); falling back to manual.")

        # ------------------------------------------------------------------
        # Strategy 2: retry finding the TOC button with broader selectors.
        # ------------------------------------------------------------------
        print("  Retrying TOC button search with broader selectors...")
        toc_labels_extended = (
            "Table of Contents", "Chapters", "Contents", "TOC",
            "Chapter List", "Bookmarks", "Navigation",
        )
        for frame in frames:
            for label in toc_labels_extended:
                try:
                    for role in ("button", "link", "tab"):
                        btn = frame.get_by_role(role, name=label, exact=False)
                        if await btn.count() > 0:
                            await btn.first.click(timeout=5_000)
                            await page.wait_for_timeout(1_500)
                            self._toc_btn = btn.first
                            self._toc_frame = frame
                            # Recurse once now that _toc_btn is set.
                            await self._seek_by_toc(page)
                            return
                except Exception:
                    continue

        # ------------------------------------------------------------------
        # Strategy 3: chapter-bar button sweep (no TOC needed).
        # Navigates to the start using chapter-prev clicks, then sweeps
        # forward with chapter-next + 15 s skip gap filling.
        # ------------------------------------------------------------------
        print("  TOC button not found — using chapter-bar button sweep.")
        await self._button_sweep_for_gaps(page)

    async def _ensure_all_parts_captured(self, page: Page) -> None:
        """Check for missing part numbers and attempt to fill them.

        This is stronger than "no gaps up to max observed": when we know the
        expected number of parts (readingOrder or TOC), we require that full set.
        """
        if not self.captured:
            return

        def get_nums() -> list[int]:
            return sorted({_part_number(p["filename"]) for p in self.captured if _part_number(p["filename"]) > 0})

        nums = get_nums()
        if not nums:
            return

        max_num = max(nums)
        # Derive expected part count from multiple sources:
        # - readingOrder length (Libby's intended part count)
        # - TOC part keys (e.g. "-Part003")
        # - highest part number we've *actually* seen so far
        expected_from_ro = len(self.reading_order) if self.reading_order else 0
        toc_max = 0
        toc_keys = list(self.toc.keys())
        for k in toc_keys:
            toc_max = max(toc_max, _part_number(k or ""))
        expected_total = max(expected_from_ro, toc_max, max_num)
        expected = set(range(1, expected_total + 1))
        missing = expected - set(nums)

        if not missing:
            print(f"  All {expected_total} part(s) captured.")
            return

        print(f"  Detected missing parts: {sorted(missing)} (expected {expected_total})")

        # Retry strategy:
        # - If we have readingOrder durations, seek to the computed start of each missing part
        #   (plus a small offset) to force the request.
        # - Otherwise do a more granular duration scan.
        if self.reading_order and expected_total == len(self.reading_order):
            # Precompute global start time of each part index (1-based).
            starts: list[float] = []
            cumulative = 0.0
            for item in self.reading_order:
                starts.append(cumulative)
                try:
                    cumulative += float(item.get("duration", 0.0))
                except Exception:
                    pass

            for m in sorted(missing):
                if 1 <= m <= len(starts):
                    seek_targets = [starts[m - 1] + 1.0, starts[m - 1] + 4.0]
                else:
                    seek_targets = [max(0.0, cumulative - 10.0)]

                print(f"  Retrying Part {m:02d} by seeking…")
                for t in seek_targets:
                    prev = len(self.captured)
                    await self._set_audio_time(page, t)
                    await page.wait_for_timeout(1_500)
                    # Give it a bit to emit requests
                    for _ in range(10):
                        if len(self.captured) > prev:
                            break
                        await page.wait_for_timeout(300)

            # Final check; if still missing, do a granular scan as a last resort.
            final_nums = set(get_nums())
            missing2 = expected - final_nums
            if missing2:
                print(f"  Still missing after targeted retries: {sorted(missing2)}")
                print("  Running granular seek to catch missing parts…")
                await self._seek_by_duration(page, step_sec=900)  # 15-min steps
                final_nums = set(get_nums())
                missing2 = expected - final_nums
                if missing2:
                    print(f"  STILL MISSING parts: {sorted(missing2)}")
                else:
                    print(f"  Gap filling successful. Total parts: {expected_total}")
            else:
                print(f"  Gap filling successful. Total parts: {expected_total}")
            return

        print("  No readingOrder durations available; running granular seek to catch missing parts…")
        await self._seek_by_duration(page, step_sec=900)  # 15-min steps

        final_nums = set(get_nums())
        missing2 = expected - final_nums
        if missing2:
            print(f"  STILL MISSING parts: {sorted(missing2)}")
        else:
            print(f"  Gap filling successful. Total parts: {expected_total}")

    async def _seek_by_duration(self, page: Page, step_sec: float = 1500.0) -> None:
        """Fallback when there is no reading order: step across the full
        audio duration in chunks.
        """
        total: float = await self._eval_in_frames(
            page,
            "() => { const a = document.querySelector('audio'); "
            "return a ? a.duration : 0; }",
        ) or 0.0

        if total <= 0:
            print("  Could not determine duration automatically.")
            return

        print(f"  Total duration: {total / 60:.1f} min  (stepping in {step_sec/60:.1f}-min chunks)")
        pos = 0.0
        while pos < total:
            prev_captured = len(self.captured)
            await self._set_audio_time(page, pos)
            await page.wait_for_timeout(2_000)
            if len(self.captured) > prev_captured:
                print(f"    At {pos/60:.1f} min: {len(self.captured)} part(s) captured")
            pos += step_sec

        # Seek near the very end to catch the last part.
        await self._set_audio_time(page, max(0.0, total - 10))
        await page.wait_for_timeout(2_000)


    async def _set_audio_time(self, page: Page, seconds: float) -> None:
        await self._eval_in_frames(
            page,
            f"() => {{ const a = document.querySelector('audio'); "
            f"if (a) {{ a.currentTime = {seconds}; "
            f"a.play().catch(() => {{}}); }} }}",
        )

    async def _eval_in_frames(self, page: Page, js: str):
        """Evaluate JS in the main frame, then each child frame until a
        non-null/non-zero result is returned."""
        frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame]
        for frame in frames:
            try:
                result = await frame.evaluate(js)
                if result:
                    return result
            except Exception:
                continue
        return None

    async def _stop_playback(self, page: Page) -> None:
        """Pause the Libby player so audio doesn't continue in the background."""
        js = """
            () => {
                // Try media elements directly
                const media = document.querySelector('audio, video');
                if (media && !media.paused) {
                    media.pause();
                }
                // Also try clicking any visible 'Pause' control in the UI.
                const selectors = [
                    '[aria-label="Pause"]',
                    '[aria-label*="pause" i]',
                    'button[class*="pause"]',
                    '[title="Pause"]'
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        try { el.click(); } catch (_) {}
                    }
                }
            }
        """
        await self._eval_in_frames(page, js)

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    async def _verify_duration_and_refetch(self, page: Page, book_name: str) -> None:
        """Compare total downloaded duration with the expected book duration.

        If there is a significant shortfall (e.g. a part that follows the last
        TOC chapter and was never seeked to), seek the player beyond the last
        known position to trigger HTTP requests for any un-captured parts, then
        download them.
        """
        THRESHOLD = 30.0  # seconds — less than this is rounding noise

        # ── 1. Expected total book duration ──────────────────────────────────
        expected = self.total_book_duration

        if expected <= 0:
            await page.wait_for_timeout(1_000)
            frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame]
            for frame in frames:
                try:
                    result = await frame.evaluate(_TIMELINE_JS)
                    if result and isinstance(result, dict):
                        start_s = _parse_timeline_seconds(result.get("start"))
                        end_s = _parse_timeline_seconds(result.get("end"))
                        total_s = start_s + end_s
                        if total_s > 0:
                            expected = total_s
                            break
                except Exception:
                    continue

        if expected <= 0:
            dom_dur = await self._eval_in_frames(
                page,
                "() => { const a = document.querySelector('audio'); "
                "return (a && isFinite(a.duration)) ? a.duration : 0; }",
            ) or 0.0
            expected = dom_dur

        if expected <= THRESHOLD:
            return

        # ── 2. Measure total duration of already-downloaded files ─────────────
        part_files = sorted(
            self.output_dir.glob(f"{book_name}-Part*.mp3"),
            key=lambda p: _part_number(p.name),
        )
        actual = 0.0
        for pf in part_files:
            try:
                actual += float(MP3(pf).info.length)
            except Exception:
                pass

        diff = expected - actual
        print(
            f"\nDuration check — Expected: {_fmt_hms(expected)}  "
            f"Downloaded: {_fmt_hms(actual)}  "
            f"Diff: {diff:.0f}s"
        )

        if diff <= THRESHOLD:
            print("Duration check passed.")
            return

        print(f"Warning: {diff:.0f}s of audio unaccounted for — some parts may be missing.")

    async def _download_all(self, book_name: str) -> None:
        print(f"\nDownloading {len(self.captured)} file(s) → {self.output_dir.resolve()}/")

        cover_path = self.output_dir / "coverArt.jpg"
        cover_url = self.metadata.get("cover_url", "")
        if cover_url and not cover_path.exists():
            for attempt in range(1, 4):
                try:
                    resp = requests.get(cover_url, timeout=30)
                    if resp.status_code == 200:
                        cover_path.write_bytes(resp.content)
                        print("  Downloaded cover art.")
                        break
                    print(f"  Warning: cover art attempt {attempt} returned {resp.status_code}.")
                except Exception as e:
                    print(f"  Warning: cover art attempt {attempt} failed: {e}")
                if attempt < 3:
                    time.sleep(2 ** attempt)
            else:
                print("  Warning: cover art download failed after 3 attempts.")

        total = len(self.captured)
        for i, part in enumerate(self.captured, 1):
            part_label = _part_label(part["filename"], i)
            out_name = f"{book_name}-{part_label}.mp3"
            out_path = self.output_dir / out_name
            print(f"  [{i}/{total}] {out_name}")

            try:
                # Strip range/partial-content headers so the server sends
                # the full file rather than a byte-range slice.
                clean_headers = {
                    k: v for k, v in part["headers"].items()
                    if k.lower() not in ("range", "if-range")
                }
                resp = requests.get(
                    part["url"],
                    headers=clean_headers,
                    allow_redirects=True,
                    timeout=300,
                    stream=True,
                )
                ct = resp.headers.get("content-type", "")
                # Accept 200 (full) and 206 (partial — normal for audio streaming).
                if resp.status_code in (200, 206) and "audio" in ct:
                    with open(out_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=8_192):
                            f.write(chunk)
                    _apply_id3(
                        out_path,
                        track=i,
                        total=total,
                        part_label=part_label,
                        metadata=self.metadata,
                        cover=cover_path if cover_path.exists() else None,
                    )
                else:
                    print(f"    WARNING: {resp.status_code} / {ct}")
            except Exception as e:
                print(f"    ERROR: {e}")

    # ------------------------------------------------------------------
    # CUE file
    # ------------------------------------------------------------------

    def _build_chapter_list(self, book_name: str) -> list[dict]:
        """Return chapters as a flat list suitable for writing CUE / splitting.

        Output items: {title, offset, file_name}

        Libby provides TOC in two different shapes:
        - **BIFOCAL nav.toc path**: "...-Part003#120" where offset is *local to that part*.
          This matches the Java tool behavior and should be preserved.
        - **UI-scraped fallback**: "#120" (no part info) where offset is best treated as
          *global book seconds*; we map to part files by measuring MP3 durations.
        """
        # Gather downloaded part files and index by numeric part number.
        part_files = sorted(
            self.output_dir.glob(f"{book_name}-Part*.mp3"),
            key=lambda p: _part_number(p.name),
        )
        if not part_files:
            print("  Warning: No part files found; cannot build chapter list.")
            return []

        part_by_num: dict[int, Path] = {}
        for pf in part_files:
            pn = _part_number(pf.name)
            if pn:
                part_by_num[pn] = pf

        # Treat offsets as book-global seconds and map to files by MP3 duration.
        # Libby often gives us duplicate TOC rows for the same title, some with
        # offset=0 and some with a real timestamp. Prefer the *largest* non-zero
        # offset per title and drop the zero-only duplicates.
        best_by_title: dict[str, float] = {}
        for entries in self.toc.values():
            for e in entries:
                title = e.get("title", "").strip()
                try:
                    off = float(e.get("offset", 0) or 0)
                except Exception:
                    off = 0.0
                cur = best_by_title.get(title)
                # Always take a larger offset; any positive beats zero.
                if cur is None or off > cur:
                    best_by_title[title] = off

        if not best_by_title:
            return []

        raw_chapters = [
            {"title": t, "offset": off} for t, off in best_by_title.items()
        ]
        raw_chapters.sort(key=lambda c: c["offset"])

        boundaries = []
        cumulative = 0.0
        for pf in part_files:
            try:
                dur = float(MP3(pf).info.length)
            except Exception:
                dur = 0.0
            boundaries.append((pf, cumulative, cumulative + dur))
            cumulative += dur

        mapped = []
        for ch in raw_chapters:
            try:
                global_off = float(ch["offset"])
            except Exception:
                global_off = 0.0
            chosen_pf = boundaries[-1][0]
            local_off = 0
            for pf, start, end in boundaries:
                if start <= global_off < end:
                    chosen_pf = pf
                    local_off = int(round(max(0.0, global_off - start)))
                    break
                if pf == boundaries[-1][0] and global_off >= start:
                    chosen_pf = pf
                    local_off = int(round(max(0.0, global_off - start)))
            mapped.append({"title": ch["title"], "offset": local_off, "file_name": chosen_pf.name})

        return mapped

    def _write_cue(self, book_name: str) -> None:
        all_chapters = self._build_chapter_list(book_name)
        if not all_chapters:
            print("No TOC data or part files; skipping .cue file.")
            return

        # Group by file_name for CUE structure
        from collections import defaultdict
        by_file = defaultdict(list)
        for ch in all_chapters:
            title = (ch.get("title") or "").strip()
            if not title:
                continue
            by_file[ch["file_name"]].append({"title": title, "offset": int(ch.get("offset", 0))})

        # Get file list in order (Part01, Part02...)
        sorted_files = sorted(by_file.keys(), key=lambda n: _part_number(n))

        lines = [
            f'REM GENRE Audiobook',
            f'REM DATE {time.strftime("%Y")}',
            f'PERFORMER "{_safe(self.metadata.get("author", "Unknown"))}"',
            f'TITLE "{book_name}"'
        ]
        
        track_num = 1
        for fname in sorted_files:
            lines.append(f'FILE "{fname}" MP3')
            # Sort + dedupe within a file by offset/title.
            # Only sort by offset; sorting by title causes "Chapter 10" to come
            # before "Chapter 2" when offsets tie (e.g. missing offsets -> 0).
            entries = sorted(by_file[fname], key=lambda e: e["offset"])
            deduped = []
            seen = set()
            for e in entries:
                key = (e["offset"], e["title"])
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(e)

            last_offset = -1
            for entry in deduped:
                # Enforce strictly increasing offsets within each file; if two
                # chapters resolve to the same second, nudge later ones forward.
                off = int(entry["offset"])
                if off <= last_offset:
                    off = last_offset + 1
                last_offset = off

                # Convert seconds to M:S:F (Frames are 1/75th of a second, we'll use 00)
                m, s = divmod(off, 60)
                lines.append(f"  TRACK {track_num:02d} AUDIO")
                lines.append(f'    TITLE "{entry["title"]}"')
                lines.append(f"    INDEX 01 {m:02d}:{s:02d}:00")
                track_num += 1

        out = self.output_dir / f"{book_name}.cue"
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"CUE file written: {out}")

    def _split_chapters(self, book_name: str) -> None:
        """Split downloaded part files into individual chapter MP3s via ffmpeg."""
        if not self.ffmpeg:
            return
        if not self.toc:
            print("No TOC data; skipping chapter splitting.")
            return

        all_chapters = self._build_chapter_list(book_name)

        from collections import defaultdict
        by_file: dict[str, list] = defaultdict(list)
        for ch in all_chapters:
            by_file[ch["file_name"]].append(ch)

        chapters_dir = self.output_dir / "chapters"
        chapters_dir.mkdir(exist_ok=True)
        print(f"\nSplitting into chapters → {chapters_dir.resolve()}/")

        track = 1
        for file_name in sorted(by_file.keys(), key=_part_number):
            input_path = self.output_dir / file_name
            if not input_path.exists():
                print(f"  Warning: {input_path.name} not found, skipping.")
                track += len(by_file[file_name])
                continue

            chapters_in_file = sorted(by_file[file_name], key=lambda c: c["offset"])
            for i, ch in enumerate(chapters_in_file):
                start = ch["offset"]
                end = chapters_in_file[i + 1]["offset"] if i + 1 < len(chapters_in_file) else None

                safe_title = _safe(ch["title"])[:50].strip()
                out_path = chapters_dir / f"{track:02d}-{safe_title}.mp3"

                # Prefer seek before input for speed. Use -t duration (not -to)
                # to avoid ambiguity and make segmenting consistent.
                cmd = [self.ffmpeg, "-y", "-ss", str(start), "-i", str(input_path)]
                if end is not None and end > start:
                    cmd += ["-t", str(end - start)]
                cmd += ["-map", "0:a:0", "-vn", "-sn", "-dn", "-c:a", "copy", str(out_path)]

                print(f"  [{track:02d}] {out_path.name}")
                try:
                    subprocess.run(cmd, check=True, capture_output=True)
                except subprocess.CalledProcessError as e:
                    print(f"    ERROR: {e.stderr.decode(errors='replace')[:200]}")

                track += 1

        print(f"Done — {track - 1} chapter(s) written.")


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

# JavaScript that reads both Libby timeline clock elements and returns their
# raw text strings so Python can compute the total book duration regardless of
# the current playback position:
#   timeline-start-minutes  → current position, e.g.  "1:23:45"  (positive)
#   timeline-end-minutes    → time remaining,   e.g. "-22:55:13"  (negative)
#   total = abs(end) + start
_TIMELINE_JS = """
    () => {
        function getText(selectors) {
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el) {
                    const t = el.textContent.trim();
                    if (t && /\\d+:\\d{2}/.test(t)) return t;
                }
            }
            return null;
        }
        const start = getText([
            'place-phrase.timeline-start-minutes place-phrase-visual',
            'place-phrase.timeline-start-minutes',
            '.timeline-start-minutes .place-phrase-visual',
            '.timeline-start-minutes',
            '[class*="timeline-start"]',
        ]);
        const end = getText([
            'place-phrase.timeline-end-minutes place-phrase-visual',
            'place-phrase.timeline-end-minutes',
            '.timeline-end-minutes .place-phrase-visual',
            '.timeline-end-minutes',
            '[class*="timeline-end"]',
        ]);
        if (!start && !end) return null;
        return { start: start, end: end };
    }
"""


def _parse_timeline_seconds(text: Optional[str]) -> int:
    """Parse a Libby timeline clock string to seconds.

    Strips a leading minus (end-time is negative), then handles H:M:S or M:S.
    Returns 0 on any parse failure.
    """
    if not text:
        return 0
    t = text.lstrip("-").strip()
    parts = t.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        pass
    return 0


def _fmt_hms(seconds: float) -> str:
    """Format a duration in seconds as h:mm:ss."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _part_number(filename: str) -> int:
    m = re.search(r"[Pp]art0*(\d+)", filename)
    return int(m.group(1)) if m else 0


def _seq_frontier(captured: list) -> int:
    """Return the highest N such that parts 1..N are ALL present in captured.

    Unlike max(), this is immune to early-captured trailing parts (e.g. Part27
    captured from the initial page load before the TOC loop runs).
    """
    nums = {_part_number(p["filename"]) for p in captured if _part_number(p["filename"]) > 0}
    n = 0
    while (n + 1) in nums:
        n += 1
    return n


def _safe(s: str) -> str:
    """Strip characters that are invalid in most file systems and limit length."""
    if not s:
        return "Unknown"
    # Replace newlines/tabs with space
    s = re.sub(r"[\r\n\t]+", " ", s)
    # Keep only safe chars
    s = re.sub(r"[^ a-zA-Z0-9\-.]", "", s)
    # Collapse multiple spaces
    s = " ".join(s.split())
    # Limit to 80 chars to be safe on all OSs
    return s[:80].strip()


def _part_label(filename: str, fallback_index: int) -> str:
    """Return a clean 'PartXX' label from a raw URL filename.

    '%7B...%7DFmt425-Part02.mp3'  ->  'Part02'
    Falls back to 'PartNN' using the capture order index.
    """
    m = re.search(r"[Pp]art(\d+)", filename)
    if m:
        return f"Part{m.group(1).zfill(2)}"
    return f"Part{fallback_index:02d}"


def _parse_toc_path(path: str) -> tuple[Optional[str], int]:
    """Extract the file-key suffix and second-offset from a TOC path.

    BIFOCAL paths:  BookTitle-Part003#120  ->  ('-Part003', 120)
    UI-scraped:     #120                   ->  ('-Part01',   120)  [best-effort]
    """
    file_key: Optional[str] = None
    offset = 0

    # Pure timestamp path from UI scrape: '#seconds'
    if path.startswith("#") and "-" not in path:
        try:
            offset = int(path[1:])
        except ValueError:
            pass
        return "-Part01", offset  # assume single-file book; caller can adjust

    if "-" in path:
        file_key = path[path.rfind("-"):]
        if "#" in file_key:
            try:
                offset = int(file_key[file_key.index("#") + 1:])
            except ValueError:
                pass
            file_key = file_key[: file_key.index("#")]
    return file_key, offset


def _timestamp_to_seconds(text: str) -> int:
    """Extract a timestamp/duration from arbitrary text and return seconds.

    Handles:
    - "1:23" / "1:23:45"
    - "1 hour 28 minutes 1 second"
    - also tolerates basic word-numbers for 1-10 (as seen in some UI strings)
    """
    if not text:
        return 0
    t = str(text).strip().lower()

    m = re.search(r"(\d+:\d{2}(?::\d{2})?)", t)
    if m:
        parts = m.group(1).split(":")
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except ValueError:
            return 0

    words = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
    }

    def _val(unit_pat: str) -> int:
        mm = re.search(rf"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+{unit_pat}", t)
        if not mm:
            return 0
        v = mm.group(1)
        if v.isdigit():
            return int(v)
        return words.get(v, 0)

    hours = _val(r"hours?")
    minutes = _val(r"minutes?")
    seconds = _val(r"seconds?")
    total = hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else 0


def _apply_id3(
    path: Path,
    track: int,
    total: int,
    part_label: str,
    metadata: dict,
    cover: Optional[Path],
) -> None:
    try:
        audio = MP3(str(path), ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags

        book = metadata.get("title", path.stem)
        # VLC commonly overwrites CUE track titles with the underlying file's TIT2
        # once played. Make TIT2 part-specific so the UI stays meaningful.
        file_title = f"{book} \u2013 {part_label}"

        # Replace (not accumulate) common frames on re-runs.
        for key in ("TALB", "TIT2", "TIT3", "TPE1", "TPE2", "TYER", "TRCK", "COMM", "APIC"):
            try:
                tags.delall(key)
            except Exception:
                pass

        tags.add(TALB(encoding=3, text=book))
        tags.add(TIT2(encoding=3, text=file_title))
        tags.add(TRCK(encoding=3, text=f"{track}/{total}"))

        if metadata.get("author"):
            tags.add(TPE1(encoding=3, text=metadata["author"]))
            tags.add(TPE2(encoding=3, text=metadata["author"]))
        if metadata.get("subtitle"):
            tags.add(TIT3(encoding=3, text=metadata["subtitle"]))
        if metadata.get("description"):
            tags.add(COMM(encoding=3, lang="eng", desc="", text=metadata["description"]))
        if metadata.get("year"):
            tags.add(TYER(encoding=3, text=str(metadata["year"])))
        if cover:
            tags.add(
                APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,
                    desc="Cover",
                    data=cover.read_bytes(),
                )
            )

        audio.save()
    except Exception as e:
        print(f"    Warning: ID3 tagging failed for {path.name}: {e}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download Libby audiobooks automatically."
    )
    parser.add_argument(
        "--out",
        default="libby_downloads",
        metavar="DIR",
        help="Output directory  (default: ./libby_downloads)",
    )
    parser.add_argument(
        "--ffmpeg",
        metavar="PATH",
        help="Path to ffmpeg binary; when provided, splits output into individual chapter files",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser headlessly – NOT recommended for the first run",
    )
    parser.add_argument(
        "--debug-toc",
        action="store_true",
        help="Print detailed TOC extraction and mapping debug output",
    )
    parser.add_argument(
        "--skip-minutes",
        type=float,
        default=5.0,
        metavar="MINS",
        help="Minutes of audio to skip per batch when filling part gaps (default: 5.0)",
    )
    args = parser.parse_args()

    dl = LibbyDownloader(
        output_dir=args.out,
        ffmpeg=args.ffmpeg,
        headless=args.headless,
        skip_minutes=args.skip_minutes,
    )
    asyncio.run(dl.run())


if __name__ == "__main__":
    main()
