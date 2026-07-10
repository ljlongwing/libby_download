#!/usr/bin/env python3
"""
chirp_dl.py – Automated Chirp Books audiobook downloader.

EDUCATIONAL PURPOSES ONLY:
This tool is intended for personal, educational use. 
It requires the user to have purchased any accessed material.
Redistribution of copyrighted content is strictly prohibited.

Workflow
--------
1. Open a browser via Playwright.
2. Authenticate with Chirp (manual first run; session is saved afterwards).
3. List your library and let you pick a book.
4. Open the player and read the chapter list directly from the page's DOM
   (each chapter is a separate audio file on Chirp's CDN).
5. For each chapter: select it in the player, start playback, and intercept
   the resulting audio request via Playwright's route.fetch() (this goes
   through the real browser network stack so it isn't blocked by the CDN's
   bot protection), overriding the Range header to pull the whole file in
   one shot instead of the small buffered chunk the player itself would read.
6. Tag each downloaded M4A with metadata.

Usage
-----
    python chirp_dl.py [--out DIR] [--ffmpeg PATH] [--headless]
"""

import argparse
import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from mutagen.id3 import APIC, COMM, TALB, TIT2, TIT3, TPE1, TPE2, TYER, TRCK, ID3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.mp3 import MP3
from playwright.async_api import BrowserContext, Page, Request, async_playwright

# Where the Playwright browser session (cookies + localStorage) is persisted.
SESSION_FILE = Path.home() / ".chirp_session.json"
CHIRP_URL = "https://www.chirpbooks.com"


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class ChirpDownloader:
    def __init__(
        self,
        output_dir: str,
        ffmpeg: Optional[str] = None,
        headless: bool = False,
    ) -> None:
        # self.output_dir starts as the base directory and is narrowed to
        # base/<Book Name>/ once the book's title is known (see
        # _download_selected_book()). self._base_output_dir is kept so a
        # single instance can safely download more than one book in a row
        # (see _reset_for_next_book()).
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._base_output_dir = self.output_dir
        self.ffmpeg = ffmpeg
        self.headless = headless

        # Metadata
        self.metadata: dict = {}
        # [{number, name, duration_ms, offset_ms, part_number}], one per
        # chapter, scraped directly from the player's chapter-list DOM.
        self.toc: list[dict] = []

        # Filled in by _route_handler as each chapter's audio_proxy request
        # is intercepted; consumed by _download_chapters.
        self._chapter_bytes: dict[int, bytes] = {}
        self._chapter_events: dict[int, asyncio.Event] = {}

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        print("\n" + "=" * 60)
        print("  Chirp Books Audiobook Downloader (Educational Use Only)")
        print("=" * 60 + "\n")

        async with async_playwright() as pw:
            browser, context, page, player_page = await self._launch_browser_context(pw)

            try:
                await self._ensure_authenticated(page, context)

                books = await self._get_shelf(page)
                if not books:
                    print("No audiobooks found in your library.")
                    return

                book = self._prompt_selection(books)
                if not book:
                    return

                try:
                    await self._download_selected_book(page, context, player_page, book)
                except RuntimeError as e:
                    print(f"\n{e}")
                    return

            finally:
                try:
                    await context.storage_state(path=str(SESSION_FILE), indexed_db=True)
                except Exception:
                    try:
                        await context.storage_state(path=str(SESSION_FILE))
                    except Exception:
                        pass
                await browser.close()

    async def _launch_browser_context(self, pw):
        """Launch a browser + context, loading a saved session if present.

        Returns (browser, context, page, player_page_holder). Chirp's player
        never opens a new tab (unlike Libby's), so player_page_holder is
        just a 1-item list always pointing at the same page -- kept for
        signature parity with LibbyDownloader so the service's worker can
        drive either downloader through the same generic code path.

        Shared by the interactive run() and the batch/service worker, which
        want the exact same browser-discovery/session/anti-bot setup without
        duplicating it.
        """
        # Find a system browser (prefer Brave or Chrome)
        browser_path = self._find_browser()

        # Auto-fallback to headless if on Linux with no DISPLAY
        if not self.headless and sys.platform == "linux" and not os.environ.get("DISPLAY"):
            print("Warning: No X server detected ($DISPLAY is not set).")
            print("Falling back to headless mode. (Note: Login may be impossible if required.)")
            self.headless = True

        launch_kwargs: dict = {
            "headless": self.headless,
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
            print("No system browser found — using Playwright's bundled Chromium.")

        browser = await pw.chromium.launch(**launch_kwargs)

        ctx_kwargs: dict = {}
        if SESSION_FILE.exists():
            print(f"Loading saved session from {SESSION_FILE}")
            ctx_kwargs["storage_state"] = str(SESSION_FILE)

        context = await browser.new_context(**ctx_kwargs)
        # Hide the navigator.webdriver flag
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        # Intercept each chapter's audio request as it fires. Registered
        # once here (not per-book) so a batch caller downloading several
        # books doesn't stack duplicate route handlers.
        await context.route("**/audio_proxy/web_player/**", self._route_handler)

        return browser, context, page, [page]

    def _reset_for_next_book(self) -> None:
        """Clear per-book instance state so one ChirpDownloader instance can
        safely download multiple books in a row (used by the batch/service
        worker; the interactive CLI only ever downloads one book per run, so
        this is a no-op difference for it beyond state already being fresh).
        """
        self.output_dir = self._base_output_dir
        self.metadata = {}
        self.toc = []
        self._chapter_bytes = {}
        self._chapter_events = {}

    async def _download_selected_book(self, page, context, player_page, book: dict) -> None:
        """Download one book (a shelf entry dict from _get_shelf) using an
        already-open page/context. player_page is accepted but unused
        (Chirp never switches tabs) -- kept for signature parity with
        LibbyDownloader. Raises RuntimeError if no chapters could be found
        or downloaded, so batch callers can log the failure and move on to
        the next book instead of aborting the whole scan.
        """
        self._reset_for_next_book()

        # Baseline from the shelf card; _extract_metadata may overwrite
        # title/author/cover with better player-page data.
        self.metadata["title"] = book.get("title", "")
        self.metadata["author"] = book.get("author", "")
        self.metadata["cover_url"] = book.get("cover_url", "")

        await self._open_player(page, book)
        await self._extract_metadata(page)

        if not self.toc:
            raise RuntimeError("No chapters found in the player. Cannot continue.")

        book_name = _safe(self.metadata.get("title") or book["title"])

        # Nest this book's files under their own subfolder so multiple
        # downloads into the same --out don't pile up flat.
        self.output_dir = self._base_output_dir / book_name
        self.output_dir.mkdir(parents=True, exist_ok=True)

        downloaded = await self._download_chapters(page, book_name)

        if downloaded == 0:
            raise RuntimeError("No audio files were downloaded.")

        print(f"\nDownloaded {downloaded}/{len(self.toc)} chapter(s).")
        self._write_cue(book_name)
        self._write_chapter_info(book_name)

    def _find_browser(self) -> Optional[str]:
        candidates = [
            # Linux
            Path("/usr/bin/brave-browser"),
            Path("/usr/bin/brave"),
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/google-chrome-stable"),
            Path("/usr/bin/chromium-browser"),
            Path("/usr/bin/chromium"),
            # Windows
            Path(r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"),
            Path.home() / r"AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe",
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            # macOS
            Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
        return next((str(p) for p in candidates if p.exists()), None)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def _ensure_authenticated(self, page: Page, context: BrowserContext) -> None:
        # Prioritize /library as requested by user
        await page.goto(CHIRP_URL + "/library", wait_until="load", timeout=60_000)
        
        if await self._is_logged_in(page):
            print(f"Authenticated at {page.url}")
            return

        # Fallback to /my-library
        if "/library" not in page.url:
            await page.goto(CHIRP_URL + "/my-library", wait_until="load", timeout=60_000)

        if await self._is_logged_in(page):
            print(f"Authenticated at {page.url}")
            return

        if not self.headless:
            print(
                "\nNot logged in. The browser window is open.\n"
                "Please log in to your Chirp account.\n"
                "Once your library is visible, come back here and press Enter.\n"
            )
        else:
            print(
                "\nNot logged in and running in headless mode.\n"
                "Login is required, but the browser window cannot be displayed.\n"
                "Please run on a machine with a GUI to log in and create a session,\n"
                f"or copy a valid session file to {SESSION_FILE}\n"
            )

        try:
            input(">>> Press Enter when logged in and on the My Library page: ")
        except EOFError:
            pass

        try:
            await context.storage_state(path=str(SESSION_FILE), indexed_db=True)
        except Exception:
            await context.storage_state(path=str(SESSION_FILE))
        print(f"Session saved to {SESSION_FILE}\n")

    async def _is_logged_in(self, page: Page) -> bool:
        try:
            url = page.url.lower()
            if "library" in url:
                # Same selector _get_shelf() uses (verified against the real
                # site's DOM) -- this used to check unverified guessed
                # selectors (.book-card, .library-item, etc.) that never
                # actually matched, so this always reported "not logged in"
                # even with a valid session.
                count = await page.locator('[data-testid="user-audiobook-card"]').count()
                return count > 0
            return False
        except Exception:
            return False

    async def _wait_for_login(self, page: Page, context: BrowserContext, timeout_s: float = 600) -> bool:
        """Non-blocking variant of _ensure_authenticated's manual-login wait,
        for callers (the web service's auth flow) that can't use input() —
        there's no terminal to type into. Polls _is_logged_in() and requires
        it to hold for a few consecutive checks before accepting, so a
        mid-redirect false positive during the login flow doesn't save a
        bogus session.

        Saves the session and returns True once confirmed; returns False if
        timeout_s elapses first (session is not saved in that case).
        """
        consecutive_ok = 0
        elapsed = 0.0
        interval = 3.0
        while elapsed < timeout_s:
            await page.wait_for_timeout(int(interval * 1000))
            elapsed += interval
            if await self._is_logged_in(page):
                consecutive_ok += 1
            else:
                consecutive_ok = 0
            if consecutive_ok >= 3:
                try:
                    await context.storage_state(path=str(SESSION_FILE), indexed_db=True)
                except Exception:
                    await context.storage_state(path=str(SESSION_FILE))
                print(f"Session saved to {SESSION_FILE}\n")
                return True
        return False

    # ------------------------------------------------------------------
    # Library
    # ------------------------------------------------------------------

    async def _get_shelf(self, page: Page) -> list[dict]:
        print(f"Loading library from {page.url}...")

        # Ensure we are on a library page
        if "library" not in page.url.lower():
            await page.goto(CHIRP_URL + "/library", wait_until="load")

        # Chirp lazy-loads library cards via infinite scroll rather than
        # numbered pages (~20 cards per batch) -- keep scrolling to the
        # bottom until the card count stops growing, so libraries bigger
        # than one batch are fully captured instead of only the most
        # recent ~20. Cheap/fast for smaller libraries too: the loop exits
        # on the first iteration once the count stabilizes, so this is a
        # no-op in practice for anyone with under ~20 books.
        prev_count = -1
        for _ in range(30):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)
            count = await page.locator('[data-testid="user-audiobook-card"]').count()
            if count == prev_count:
                break
            prev_count = count
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(1000)

        try:
            await page.wait_for_selector('[data-testid="user-audiobook-card"]', timeout=10_000)
        except Exception:
            print("  Warning: no library cards found (site layout may have changed).")

        # Each owned book is a [data-testid="user-audiobook-card"]. The cover
        # link (data-testid="cover-image-cover") points at the /player/<id>
        # URL we need; the title/byline live in sibling divs within the card.
        books = await page.evaluate(
            """
            () => {
                const results = [];
                document.querySelectorAll('[data-testid="user-audiobook-card"]').forEach(card => {
                    const playerLink = card.querySelector('a[data-testid="cover-image-cover"]');
                    const titleLink = card.querySelector('[class*="user_audiobook_card-module__title"] a');
                    const bylineEl = card.querySelector('[class*="user_audiobook_card-module__byline"]');
                    const img = card.querySelector('img[data-testid="cover-image-image"]');

                    const href = playerLink ? playerLink.getAttribute('href') : null;
                    const title = titleLink ? titleLink.textContent.trim() : null;
                    let author = bylineEl ? bylineEl.textContent.trim() : '';
                    author = author.replace(/^by\\s+/i, '');

                    if (href && title) {
                        results.push({
                            title,
                            author,
                            href,
                            cover_url: img ? img.src : null,
                            // The book's own /audiobooks/<slug> page, not
                            // the /player/<id> URL above -- used to look up
                            // series/run time, which the player never shows.
                            detail_url: titleLink ? titleLink.getAttribute('href') : null,
                        });
                    }
                });
                return results;
            }
            """
        )

        if not books:
            link_count = await page.locator("a").count()
            print(f"  Debug: Found {link_count} links on the page but no library cards matched.")

        return sorted(books, key=lambda b: b["title"].lower())

    async def _get_series_metadata(self, page: Page, book: dict) -> dict:
        """Best-effort lookup of series name/position and total run time.

        Chirp's shelf/library GraphQL queries don't request either field,
        and there's no public catalog API like Overdrive's -- but the
        book's own /audiobooks/<slug> detail page (confirmed live) shows
        both as plain visible text ("Book #4 from the series: <name>",
        "Run Time" / "33h 5min"), so this navigates there and scrapes it.
        Failure just means these fields stay blank -- supplementary
        metadata, not essential to the download. Reuses the shared `page`
        since this always runs before any download navigation begins.
        """
        detail_url = book.get("detail_url") or ""
        if not detail_url:
            return {}
        url = detail_url if detail_url.startswith("http") else CHIRP_URL + detail_url
        try:
            await page.goto(url, wait_until="load", timeout=30_000)
            await page.wait_for_timeout(2_000)
            text = await page.locator("body").inner_text()
        except Exception:
            return {}

        lines = [line.strip() for line in text.split("\n") if line.strip()]
        result = {"series": "", "series_index": "", "duration": ""}
        for i, line in enumerate(lines):
            m = re.match(r"Book #([\d.]+) from the series:", line, re.IGNORECASE)
            if m and i + 1 < len(lines):
                result["series_index"] = m.group(1)
                result["series"] = lines[i + 1]
            elif line.lower() == "run time" and i + 1 < len(lines):
                result["duration"] = lines[i + 1]
        return result

    def _prompt_selection(self, books: list[dict]) -> Optional[dict]:
        print(f"\nFound {len(books)} item(s) in your library:\n")
        for i, b in enumerate(books, 1):
            suffix = f"  –  {b['author']}" if b.get("author") else ""
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
    # Player & Metadata
    # ------------------------------------------------------------------

    async def _open_player(self, page: Page, book: dict) -> None:
        url = book["href"]
        if not url.startswith("http"):
            url = CHIRP_URL + url
        print(f"\nOpening player: {url}")
        # Wait for networkidle to ensure player scripts are loaded
        await page.goto(url, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(3_000)

    async def _extract_metadata(self, page: Page) -> None:
        print("Extracting metadata...")

        meta = await page.evaluate(
            """
            () => {
                const title = document.querySelector('h1.book-title')?.textContent.trim() || null;

                const credits = Array.from(document.querySelectorAll('.credits .credit'))
                                      .map(el => el.textContent.trim());
                let author = null, narrator = null;
                for (const c of credits) {
                    if (/^written by/i.test(c)) author = c.replace(/^written by\\s*/i, '');
                    else if (/^narrated by/i.test(c)) narrator = c.replace(/^narrated by\\s*/i, '');
                }

                const cover = document.querySelector('img.cover-image')?.src || null;

                // Each chapter is its own audio file on Chirp's CDN; these
                // data attributes give us exact metadata with no clicking.
                const chapters = Array.from(
                    document.querySelectorAll('.chapter-data .chapter-row.chapter-select')
                ).map(el => ({
                    number: parseInt(el.getAttribute('data-chapter-number'), 10),
                    name: el.getAttribute('data-name')
                        || el.querySelector('.chapter-name')?.textContent.trim()
                        || null,
                    duration_ms: parseInt(el.getAttribute('data-duration'), 10) || 0,
                    offset_ms: parseInt(el.getAttribute('data-offset-from-book-start-ms'), 10) || 0,
                    part_number: parseInt(el.getAttribute('data-part-number'), 10) || 0,
                })).filter(ch => !Number.isNaN(ch.number));

                return { title, author, narrator, cover, chapters };
            }
            """
        )

        # Only overwrite the shelf-sourced baseline if the player page
        # actually yielded something better; don't stomp good data with
        # "Unknown"/None when this scrape comes up empty.
        if meta.get("title"):
            self.metadata["title"] = meta["title"]
        elif not self.metadata.get("title"):
            self.metadata["title"] = "Unknown Title"

        if meta.get("author"):
            self.metadata["author"] = meta["author"]
        elif not self.metadata.get("author"):
            self.metadata["author"] = "Unknown Author"

        if meta.get("narrator"):
            self.metadata["narrator"] = meta["narrator"]

        if meta.get("cover"):
            self.metadata["cover_url"] = meta["cover"]

        self.toc = sorted(meta.get("chapters") or [], key=lambda c: c["number"])

        print(f"  Title:  {self.metadata['title']}")
        print(f"  Author: {self.metadata['author']}")
        print(f"  Chapters: {len(self.toc)}")

    # ------------------------------------------------------------------
    # Audio capture
    # ------------------------------------------------------------------

    async def _route_handler(self, route) -> None:
        """Intercept each chapter's audio_proxy request.

        Chirp's CDN sits behind bot-detection that rejects requests replayed
        through a plain HTTP client (even with the exact same cookies/token),
        so we can't just capture-and-replay like the Libby downloader does.
        route.fetch() performs the request through the real browser network
        stack instead, which passes that check. We override the Range header
        to pull the whole file in one shot rather than the small chunk the
        player itself buffers before pausing.
        """
        req = route.request
        m = re.search(r"[?&]c=(\d+)", req.url)
        chapter_num = int(m.group(1)) if m else None
        try:
            headers = await req.all_headers()
            headers["range"] = "bytes=0-"
            resp = await route.fetch(headers=headers)
            await route.fulfill(response=resp)

            if resp.status not in (200, 206):
                # The player sometimes fires two requests per chapter; the
                # second one's token can come back invalidated (401). Don't
                # let a failed response overwrite a good one, and don't
                # treat it as the answer for this chapter.
                return
            if chapter_num is not None and chapter_num not in self._chapter_bytes:
                body = await resp.body()
                self._chapter_bytes[chapter_num] = body
                self._chapter_events.setdefault(chapter_num, asyncio.Event()).set()
        except Exception as e:
            print(f"    Warning: audio fetch failed for chapter {chapter_num}: {e}")
            try:
                await route.continue_()
            except Exception:
                pass

    async def _download_chapters(self, page: Page, book_name: str) -> int:
        total = len(self.toc)
        print(f"\nDownloading {total} chapter(s) -> {self.output_dir.resolve()}/")

        cover_path = self._download_cover()

        downloaded = 0
        for ch in self.toc:
            num = ch["number"]
            label = ch.get("name") or f"Chapter {num}"
            print(f"  [{num}/{total}] {label} ({_fmt_ms(ch.get('duration_ms', 0))})")

            body = None
            for attempt in range(2):
                event = asyncio.Event()
                self._chapter_events[num] = event
                self._chapter_bytes.pop(num, None)

                if not await self._select_chapter(page, num):
                    print(f"    Warning: could not select chapter {num}, skipping.")
                    break

                # Let the player's internal state (audio.src) settle before
                # triggering playback, to reduce duplicate/stale requests.
                await page.wait_for_timeout(800)
                await self._click_if_exists(page, "button.play-pause")

                try:
                    await asyncio.wait_for(event.wait(), timeout=20)
                except asyncio.TimeoutError:
                    print(f"    Warning: timed out waiting for audio (chapter {num}), "
                          f"{'retrying' if attempt == 0 else 'giving up'}.")
                    continue

                body = self._chapter_bytes.pop(num, None)
                if body:
                    break
                print(f"    Warning: no audio data captured for chapter {num}, "
                      f"{'retrying' if attempt == 0 else 'giving up'}.")

            if not body:
                continue

            out_name = f"{book_name}-Part{num:03d}.m4a"
            out_path = self.output_dir / out_name
            out_path.write_bytes(body)
            self._apply_tags(out_path, num, total, label, cover_path)
            downloaded += 1

        return downloaded

    async def _select_chapter(self, page: Page, num: int) -> bool:
        selector = f'[data-chapter-number="{num}"]'
        if await self._click_if_exists(page, selector):
            return True
        # Chapter panel may be closed; open it and retry.
        await self._click_if_exists(page, "button.chapters.chapter-list")
        await page.wait_for_timeout(500)
        return await self._click_if_exists(page, selector)

    def _download_cover(self) -> Optional[Path]:
        cover_path = self.output_dir / "coverArt.jpg"
        if self.metadata.get("cover_url") and not cover_path.exists():
            try:
                resp = requests.get(self.metadata["cover_url"], timeout=30)
                if resp.status_code == 200:
                    cover_path.write_bytes(resp.content)
                    print("  Downloaded cover.")
            except Exception as e:
                print(f"  Warning: cover download failed: {e}")
        return cover_path if cover_path.exists() else None

    async def _click_if_exists(self, page: Page, selector: str) -> bool:
        try:
            # Check main page
            loc = page.locator(selector).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=5_000)
                return True
            
            # Check frames
            for frame in page.frames:
                if frame == page.main_frame: continue
                try:
                    loc = frame.locator(selector).first
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click(timeout=5_000)
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # Tagging
    # ------------------------------------------------------------------

    def _apply_tags(
        self, path: Path, track: int, total: int, chapter_title: str, cover: Optional[Path]
    ) -> None:
        try:
            title = self.metadata.get("title", "Unknown Title")
            author = self.metadata.get("author", "Unknown Author")
            narrator = self.metadata.get("narrator", "")

            if path.suffix.lower() == ".m4a":
                audio = MP4(str(path))
                audio["\xa9nam"] = [chapter_title or f"{title} - Part {track}"]
                audio["\xa9alb"] = [title]
                audio["\xa9ART"] = [author]
                audio["trkn"] = [(track, total)]
                if narrator:
                    audio["\xa9wrt"] = [narrator]
                if cover:
                    audio["covr"] = [MP4Cover(cover.read_bytes(), imageformat=MP4Cover.FORMAT_JPEG)]
                audio.save()
            else:
                audio = MP3(str(path), ID3=ID3)
                if audio.tags is None: audio.add_tags()
                tags = audio.tags
                tags.add(TIT2(encoding=3, text=chapter_title or f"{title} - Part {track}"))
                tags.add(TALB(encoding=3, text=title))
                tags.add(TPE1(encoding=3, text=author))
                tags.add(TRCK(encoding=3, text=f"{track}/{total}"))
                if narrator:
                    tags.add(TPE2(encoding=3, text=narrator))
                if cover:
                    tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover.read_bytes()))
                audio.save()
        except Exception as e:
            print(f"    Warning: Tagging failed: {e}")

    def _write_cue(self, book_name: str) -> None:
        # Chirp already delivers one file per chapter (unlike Libby, which
        # groups several chapters into one part file), so each FILE entry
        # here just has a single TRACK starting at its own beginning.
        if not self.toc:
            return
        lines = [
            "REM GENRE Audiobook",
            f'REM DATE {time.strftime("%Y")}',
            f'PERFORMER "{self.metadata.get("author", "Unknown")}"',
            f'TITLE "{book_name}"',
        ]
        track_num = 1
        for ch in self.toc:
            num = ch["number"]
            fname = f"{book_name}-Part{num:03d}.m4a"
            if not (self.output_dir / fname).exists():
                continue
            title = ch.get("name") or f"Chapter {num}"
            lines.append(f'FILE "{fname}" MP4')
            lines.append(f"  TRACK {track_num:02d} AUDIO")
            lines.append(f'    TITLE "{title}"')
            lines.append("    INDEX 01 00:00:00")
            track_num += 1

        if track_num == 1:
            print("No downloaded files found; skipping .cue file.")
            return

        out = self.output_dir / f"{book_name}.cue"
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"CUE file written: {out}")

    def _write_chapter_info(self, book_name: str) -> None:
        if not self.toc:
            return
        out = self.output_dir / f"{book_name}_chapters.txt"
        lines = [f"Title: {self.metadata.get('title')}", f"Author: {self.metadata.get('author')}", ""]
        for ch in self.toc:
            lines.append(f"Chapter {ch['number']}: {ch.get('name')}  ({_fmt_ms(ch.get('duration_ms', 0))})")
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"Chapter info written: {out}")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe(s: str) -> str:
    if not s: return "Unknown"
    return re.sub(r'[\\/*?:"<>|]', "", s).strip()[:80]

def _fmt_ms(ms: int) -> str:
    total_seconds = int(ms / 1000)
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="./chirp_downloads")
    parser.add_argument("--ffmpeg", help="Path to ffmpeg binary")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    downloader = ChirpDownloader(args.out, ffmpeg=args.ffmpeg, headless=args.headless)
    try:
        asyncio.run(downloader.run())
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        # Keep the window open (e.g. when double-clicking the standalone
        # .exe) so warnings/errors in the output above stay visible.
        try:
            input("\nPress Enter to exit...")
        except EOFError:
            pass
