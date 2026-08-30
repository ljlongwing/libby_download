#!/usr/bin/env python3
"""
fabuly_dl.py -- download classic audiobooks from Fabuly.

Unlike the Libby and Chirp tools in this repo, Fabuly needs no browser
automation, no login, and no HAR capture.  There is no DRM anywhere.

The app draws on two content sources, both fully public:

* **Fabuly-hosted (~435 books)** -- curated classics, "Fabuly Originals",
  and AI-"enhanced" narrations.  Audio + metadata are plain files in the
  world-readable Google Cloud Storage bucket ``dopex_public_us``.  Audio
  is AAC/M4A.
* **LibriVox (~19,000 books -- the app's "20,000" figure)** -- the book
  list ships inside the APK as ``librivox.db`` (bundled here next to this
  script); per-book section lists live at
  ``dopex_public_us/librivox_metadata/<id>.json`` and the audio is plain
  64kbps MP3 served straight from archive.org.

What this script does:

1. Reads the public catalogue and resolves the book you asked for
   (exact slug, title substring, or an interactive picker).
2. Downloads every audio part for that book (the original narration, or
   the "enhanced" premium narration with ``--enhanced``).
3. Pulls the per-book ``.bin`` blob to recover real chapter titles
   (Fabuly ships exactly one audio part per chapter/section).
4. Tags each part (MP4 tags for .m4a, ID3 for .mp3) with title / author /
   narrator / track / cover art.
5. Writes a ``<Book>.cue`` chapter index (same shape chirp_dl.py produces).
   The parts are already one-per-chapter, so nothing needs splitting -- the
   .cue is just for players that show a combined chapter list.

Usage:

    python fabuly_dl.py                       # then: list <term> / a title / lv:<id>
    python fabuly_dl.py --list --csv out.csv  # dump the whole ~19.5k catalogue
    python fabuly_dl.py --book "Captains Courageous"          # Fabuly-hosted
    python fabuly_dl.py --book "moby dick"                    # picks across both sources
    python fabuly_dl.py --book lv:54                          # LibriVox book id 54
    python fabuly_dl.py --book _the_viy_nicholas_gogol_en --enhanced
    python fabuly_dl.py --book "A Christmas Carol" --mp3 --ffmpeg C:\\ffmpeg\\bin\\ffmpeg.exe

Run with no --book for an interactive prompt: ``list twain`` to filter the
catalogue, then a title / Fabuly slug / ``lv:<id>`` to download.

Third-party dependency: ``mutagen`` (``ffmpeg`` optional, only for --mp3).
``librivox.db`` must sit next to this script for LibriVox titles.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, Optional

from mutagen.id3 import APIC, COMM, ID3, TALB, TCON, TIT2, TPE1, TPE2, TRCK
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover

BUCKET = "https://storage.googleapis.com/dopex_public_us"
UA = "fabuly_dl/1.0 (+https://fabuly.io)"
HTTP_RETRIES = 4


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _request(url: str, *, method: str = "GET", headers: Optional[dict] = None):
    hdrs = {"User-Agent": UA}
    if headers:
        hdrs.update(headers)
    return urllib.request.Request(url, method=method, headers=hdrs)


def http_get(url: str, *, optional: bool = False) -> Optional[bytes]:
    """GET a URL, returning the body.  Returns None on 404 when optional."""
    last: Optional[Exception] = None
    for attempt in range(HTTP_RETRIES):
        try:
            with urllib.request.urlopen(_request(url), timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                if optional:
                    return None
                raise
            last = e
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {HTTP_RETRIES} tries: {url} ({last})")


def http_get_json(url: str, *, optional: bool = False) -> Optional[dict]:
    raw = http_get(url, optional=optional)
    if raw is None:
        return None
    return json.loads(raw.decode("utf-8"))


def object_size(url: str) -> Optional[int]:
    """Return the byte size of a bucket object, or None if it doesn't exist.

    Uses a 1-byte range GET because GCS HEAD on public objects is flaky.
    """
    for attempt in range(HTTP_RETRIES):
        try:
            req = _request(url, headers={"Range": "bytes=0-0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                cr = resp.headers.get("Content-Range", "")
                m = re.search(r"/(\d+)\s*$", cr)
                if m:
                    return int(m.group(1))
                cl = resp.headers.get("Content-Length")
                return int(cl) if cl is not None else 0
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 416:  # range not satisfiable -> object exists, empty
                return 0
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(1.0 * (attempt + 1))
    return None


def download_file(url: str, dest: Path, *, expected: Optional[int] = None,
                  label: str = "") -> None:
    """Stream ``url`` to ``dest``.  Skips the download if ``dest`` already
    matches ``expected`` size.  Writes to a .part file then renames."""
    if expected is None:
        expected = object_size(url)
    # A finished part is >= the raw object size (tagging/cover art grows it a
    # little); anything smaller is a partial download and gets refetched.
    if dest.exists() and expected and dest.stat().st_size >= expected:
        print(f"    skip (already have {label or dest.name})")
        return

    tmp = dest.with_suffix(dest.suffix + ".part")
    got = 0
    for attempt in range(HTTP_RETRIES):
        try:
            with urllib.request.urlopen(_request(url), timeout=120) as resp, \
                    open(tmp, "wb") as fh:
                total = expected or int(resp.headers.get("Content-Length") or 0)
                last_print = 0.0
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
                    now = time.time()
                    if total and now - last_print > 0.25:
                        pct = got * 100 // total
                        sys.stdout.write(
                            f"\r    {label or dest.name}: {pct:3d}%  "
                            f"({got // 1024:,} / {total // 1024:,} KiB)")
                        sys.stdout.flush()
                        last_print = now
            if total:
                sys.stdout.write("\r" + " " * 100 + "\r")
                sys.stdout.flush()
            if expected and tmp.stat().st_size != expected:
                raise IOError(
                    f"size mismatch: got {tmp.stat().st_size}, want {expected}")
            tmp.replace(dest)
            print(f"    saved {label or dest.name}  ({dest.stat().st_size // 1024:,} KiB)")
            return
        except (urllib.error.URLError, TimeoutError, IOError) as e:
            print(f"\r    retry {attempt + 1}: {e}")
            time.sleep(2.0 * (attempt + 1))
            got = 0
    raise RuntimeError(f"download failed: {url}")


# ---------------------------------------------------------------------------
# Minimal protobuf reader for the per-book ".bin" (BookFullText)
# ---------------------------------------------------------------------------
#
# Observed wire layout:
#   1: string  bookId
#   2: string  title
#   3: string  author
#   4: repeated Section {
#          1: string title            ("CHAPTER I", ...)
#          2: bytes  fullText
#          3: repeated WordTiming { 1: double seconds; 2: varint charIndex }
#      }

def _pb_read_varint(buf: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _pb_fields(buf: bytes) -> Iterable[tuple[int, int, object]]:
    """Yield (field_number, wire_type, value) for a protobuf message."""
    i = 0
    n = len(buf)
    while i < n:
        key, i = _pb_read_varint(buf, i)
        field, wt = key >> 3, key & 7
        if wt == 0:
            val, i = _pb_read_varint(buf, i)
            yield field, wt, val
        elif wt == 2:
            ln, i = _pb_read_varint(buf, i)
            yield field, wt, buf[i:i + ln]
            i += ln
        elif wt == 5:
            yield field, wt, buf[i:i + 4]
            i += 4
        elif wt == 1:
            yield field, wt, buf[i:i + 8]
            i += 8
        else:
            raise ValueError(f"bad wire type {wt} at offset {i}")


def parse_book_bin(raw: Optional[bytes]) -> dict:
    """Return {'title', 'author', 'sections': [{'title', 'seconds'}]}."""
    out: dict = {"title": None, "author": None, "sections": []}
    if not raw:
        return out
    try:
        for field, wt, val in _pb_fields(raw):
            if wt == 2 and field == 2:
                out["title"] = val.decode("utf-8", "replace").strip() or None
            elif wt == 2 and field == 3:
                out["author"] = val.decode("utf-8", "replace").strip() or None
            elif wt == 2 and field == 4:
                sec = {"title": None, "seconds": 0.0}
                for sf, swt, sv in _pb_fields(val):
                    if swt == 2 and sf == 1:
                        sec["title"] = sv.decode("utf-8", "replace").strip() or None
                    elif swt == 2 and sf == 3:
                        for tf, twt, tv in _pb_fields(sv):
                            if twt == 1 and tf == 1 and len(tv) == 8:
                                t = struct.unpack("<d", tv)[0]
                                if t > sec["seconds"]:
                                    sec["seconds"] = t
                out["sections"].append(sec)
    except Exception as e:  # noqa: BLE001 - best effort, never fatal
        print(f"  (could not fully parse .bin: {e})")
    return out


# ---------------------------------------------------------------------------
# Bucket catalogue
# ---------------------------------------------------------------------------

def load_featured() -> list[dict]:
    """The curated catalogue: a few hundred books with rich metadata."""
    data = http_get_json(f"{BUCKET}/books_metadata.json", optional=True) or {}
    return data.get("booksMetadata", [])


_CREATORS_RAW: Optional[list[dict]] = None


def load_creators_full() -> list[dict]:
    """Raw creator records: {id, type: AUTHOR|NARRATOR|DESIGNER, name, ...}."""
    global _CREATORS_RAW
    if _CREATORS_RAW is None:
        data = http_get_json(f"{BUCKET}/creators_metadata.json", optional=True) or {}
        _CREATORS_RAW = data.get("creatorsMetadata", [])
    return _CREATORS_RAW


def load_creators() -> dict[str, str]:
    """Map creator id -> display name (authors, narrators, designers)."""
    return {c["id"]: c.get("name") or c["id"]
            for c in load_creators_full() if c.get("id")}


# Top-level bucket prefixes that are asset/config folders, not books.
NON_BOOK_PREFIXES = {
    "creators", "dev", "dictionary", "featured_today", "librivox_metadata",
    "website",
}


def iter_all_slugs() -> Iterable[str]:
    """Every Fabuly-hosted book slug in the bucket, paginated.

    This is only the ~435 Fabuly-hosted titles; the ~19k LibriVox titles
    are handled separately via ``librivox.db`` -- see ``load_librivox``.
    """
    token = ""
    while True:
        params = {"list-type": "2", "delimiter": "/", "max-keys": "1000"}
        if token:
            params["continuation-token"] = token
        xml = http_get(f"{BUCKET}/?{urllib.parse.urlencode(params)}").decode("utf-8")
        for pref in re.findall(r"<Prefix>([^<]+)</Prefix>", xml):
            slug = html.unescape(pref).rstrip("/")
            if slug and slug not in NON_BOOK_PREFIXES:
                yield slug
        m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", xml)
        if not m or "<IsTruncated>true</IsTruncated>" not in xml:
            break
        token = html.unescape(m.group(1))


# ---------------------------------------------------------------------------
# LibriVox catalogue  (~19k books; list ships in librivox.db, audio on archive.org)
# ---------------------------------------------------------------------------

_LV_DB: Optional[list[dict]] = None
_ARCHIVE_ID_RE = re.compile(r"archive\.org/download/([^/]+)/")


def librivox_db_path() -> Optional[Path]:
    """Locate the bundled ``librivox.db`` (next to the script, in a
    PyInstaller bundle, or in the current directory)."""
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(Path(getattr(sys, "_MEIPASS", ".")))
    roots += [Path(__file__).resolve().parent, Path.cwd()]
    for r in roots:
        p = r / "librivox.db"
        if p.is_file():
            return p
    return None


def load_librivox() -> list[dict]:
    """Rows from librivox.db: {librivox_id, title, author, duration, language}."""
    global _LV_DB
    if _LV_DB is None:
        p = librivox_db_path()
        if not p:
            print("  (librivox.db not found -- LibriVox titles unavailable; "
                  "put librivox.db next to the program)")
            _LV_DB = []
        else:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            _LV_DB = [
                {"librivox_id": r[0], "title": r[1] or "",
                 "author": (r[2] or "").strip(), "duration": r[3] or 0,
                 "language": r[4] or ""}
                for r in con.execute("SELECT id, title, author, "
                                     "total_duration_seconds, language FROM book")
            ]
            con.close()
    return _LV_DB


def librivox_meta_url(lid: int) -> str:
    return f"{BUCKET}/librivox_metadata/{lid}.json"


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(text: str) -> str:
    text = _UNSAFE.sub("", text or "").strip().strip(".")
    text = re.sub(r"\s+", " ", text)
    return text[:150] or "book"


def _deslug(s: str) -> str:
    s = s.replace("___", ": ").replace("__", " - ").replace("_", " ")
    return re.sub(r"\s+", " ", s).strip()


def slug_to_title(slug: str) -> str:
    s = slug
    for suffix in ("_en_original", "_original", "_en"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return _deslug(s).title()


def split_slug(slug: str, known_authors: Iterable[str]) -> tuple[str, str]:
    """Best-effort (title, author) from a bucket slug.

    Slugs look like ``<title_words>_<author_words>_en[_original]``.  There's
    no delimiter between title and author, so we peel a known author name
    off the end when we can; otherwise author is left blank.
    """
    s = slug
    for suffix in ("_en_original", "_original", "_en"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    flat = _deslug(s).lower()
    best = ""
    for name in known_authors:
        n = name.lower()
        if flat.endswith(" " + n) and len(n) > len(best):
            best = name
    if best:
        title = _deslug(s)[: -(len(best) + 1)].rstrip(" -:")
        return title.title(), best
    return _deslug(s).title(), ""


def parse_selection(raw: str, count: int) -> Optional[list[int]]:
    """'all' or a list/range spec like '1,3,5-7' -> 0-based indices."""
    raw = raw.strip().lower()
    if not raw:
        return None
    if raw in ("all", "*", "a"):
        return list(range(count))
    picked: set[int] = set()
    for tok in re.split(r"[,\s]+", raw):
        if not tok:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", tok)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            for n in range(min(lo, hi), max(lo, hi) + 1):
                if 1 <= n <= count:
                    picked.add(n - 1)
        elif tok.isdigit():
            n = int(tok)
            if 1 <= n <= count:
                picked.add(n - 1)
        else:
            return None
    return sorted(picked) or None


# ---------------------------------------------------------------------------
# Downloader
# ---------------------------------------------------------------------------

class FabulyDownloader:
    def __init__(self, out_dir: str, *, ffmpeg: Optional[str] = None,
                 enhanced: bool = False, to_mp3: bool = False,
                 want_cover: bool = True, debug: bool = False) -> None:
        self.out_dir = Path(out_dir)
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg")
        self.enhanced = enhanced
        self.to_mp3 = to_mp3
        self.want_cover = want_cover
        self.debug = debug
        self._featured: Optional[list[dict]] = None
        self._creators: Optional[dict[str, str]] = None
        self._catalog: Optional[list[dict]] = None

        if self.to_mp3 and not self.ffmpeg:
            sys.exit("--mp3 needs ffmpeg; pass --ffmpeg PATH or put ffmpeg on PATH.")

    # -- catalogue ------------------------------------------------------

    @property
    def featured(self) -> list[dict]:
        if self._featured is None:
            print("Loading catalogue ...")
            self._featured = load_featured()
        return self._featured

    @property
    def creators(self) -> dict[str, str]:
        if self._creators is None:
            self._creators = load_creators()
        return self._creators

    def catalogue_rows(self) -> list[dict]:
        """Every downloadable book: the curated storefront plus the handful
        of bucket-only titles.  Rows: title, author, narrators, duration,
        enhanced, categories, slug, source."""
        authors = {b.get("author", "") for b in self.featured if b.get("author")}
        authors |= {c.get("name", "") for c in
                    (load_creators_full() or []) if c.get("type") == "AUTHOR"}
        authors = {a for a in authors if a}

        rows: dict[str, dict] = {}
        for b in self.featured:
            d = b.get("durationInSeconds") or 0
            rows[b["bookId"]] = {
                "title": b.get("title", ""),
                "author": b.get("author", ""),
                "narrators": "; ".join(self.creators.get(n, n.replace("_", " ").title())
                                       for n in b.get("narratorsIds", []) or []),
                "duration": f"{d // 3600}:{d % 3600 // 60:02d}",
                "enhanced": "yes" if b.get("isEnhancedAudioAvailable") else "",
                "categories": "; ".join(b.get("subCategoryIds", []) or []),
                "slug": b["bookId"],
                "source": "storefront",
            }
        print("Scanning bucket for any titles outside the storefront ...")
        extras = [s for s in iter_all_slugs() if s not in rows]
        if extras:
            print(f"  {len(extras)} bucket-only titles; reading their metadata ...")
        for slug in extras:
            title, author = split_slug(slug, authors)
            blob = http_get(f"{BUCKET}/{slug}/{slug}.bin", optional=True)
            if blob:
                bm = parse_book_bin(blob)
                title = bm["title"] or title
                author = bm["author"] or author
            rows[slug] = {"title": title, "author": author, "narrators": "",
                          "duration": "", "enhanced": "", "categories": "",
                          "slug": slug, "source": "bucket-only"}
        for b in load_librivox():
            d = b["duration"]
            rows[f"lv:{b['librivox_id']}"] = {
                "title": b["title"], "author": b["author"], "narrators": "",
                "duration": f"{d // 3600}:{d % 3600 // 60:02d}" if d else "",
                "enhanced": "", "categories": b["language"],
                "slug": f"lv:{b['librivox_id']}", "source": "librivox",
            }
        return sorted(rows.values(),
                      key=lambda r: (r["author"].lower(), r["title"].lower()))

    @property
    def catalog(self) -> list[dict]:
        """Cached full catalogue (built once per run)."""
        if self._catalog is None:
            self._catalog = self.catalogue_rows()
        return self._catalog

    @staticmethod
    def _print_row(r: dict) -> None:
        dur = f"  ({r['duration']})" if r["duration"] else ""
        enh = "  [enhanced]" if r["enhanced"] else ""
        extra = "  [bucket-only]" if r["source"] == "bucket-only" else ""
        print(f"  {r['title']}  -- {r['author'] or '?'}{dur}{enh}{extra}")
        print(f"      slug: {r['slug']}")

    def list_catalogue(self, csv_path: Optional[str] = None) -> None:
        rows = self.catalog
        if csv_path:
            import csv
            with open(csv_path, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            print(f"\nWrote {len(rows)} books to {csv_path}")
            return
        by_src: dict[str, int] = {}
        for r in rows:
            by_src[r["source"]] = by_src.get(r["source"], 0) + 1
        brk = ", ".join(f"{v} {k}" for k, v in by_src.items())
        print(f"\n{len(rows)} downloadable books ({brk}):\n")
        for r in rows:
            self._print_row(r)

    def _browse(self, term: str = "") -> None:
        """Interactive catalogue view, optionally filtered by ``term``."""
        rows = self.catalog
        t = term.strip().lower()
        if not t:
            print(f"  {len(rows)} titles total (Fabuly + LibriVox) -- too many to "
                  f"scroll.\n  Narrow it:  list twain   list \"sherlock holmes\"   "
                  f"list dickens\n  Or export everything:  --list --csv books.csv")
            return
        tn = re.sub(r"[^a-z0-9]+", "_", t).strip("_")
        rows = [r for r in rows
                if t in r["title"].lower() or t in r["author"].lower()
                or t in r["slug"].lower() or tn in r["slug"].lower()
                or t in _deslug(r["slug"]).lower()]
        if not rows:
            print(f"  nothing matches {term!r}")
            return
        for r in rows[:400]:
            self._print_row(r)
        more = f"  (showing first 400 of {len(rows)})\n" if len(rows) > 400 else ""
        print(f"\n{more}  {min(len(rows), 400)} of {len(rows)} title(s) matching "
              f"{term!r}. Type a title or slug to download.")

    @staticmethod
    def _book_ref(b: dict) -> str:
        if "librivox_id" in b:
            return f"lv:{b['librivox_id']}"
        return b.get("bookId") or b.get("slug") or "?"

    def _resolve(self, query: str) -> list[dict]:
        """Return candidate book dicts.  A Fabuly book carries ``bookId``;
        a LibriVox book carries ``librivox_id``."""
        q = query.strip().lower()
        qn = re.sub(r"[^a-z0-9]+", "_", q).strip("_")  # "the bet, poe" -> "the_bet_poe"

        # 0. explicit LibriVox id: "lv:1234" / "lv1234" / "#1234"
        m = re.fullmatch(r"(?:lv[:#]?|#)(\d{1,7})", q)
        if m:
            lid = int(m.group(1))
            for b in load_librivox():
                if b["librivox_id"] == lid:
                    return [{"librivox_id": lid, "title": b["title"],
                             "author": b["author"]}]
            return [{"librivox_id": lid, "title": f"LibriVox #{lid}", "author": ""}]

        # 1. exact Fabuly slug in the bucket
        if re.fullmatch(r"[a-z0-9_]+", q):
            if object_size(f"{BUCKET}/{q}/{q}_0.m4a") is not None:
                for b in self.featured:
                    if b.get("bookId") == q:
                        return [b]
                return [{"bookId": q}]

        # 2. substring over Fabuly storefront + LibriVox (title / author)
        hits = [b for b in self.featured
                if q in b.get("title", "").lower()
                or qn in b.get("bookId", "").lower()]
        lv = [b for b in load_librivox()
              if q in b["title"].lower() or q in b["author"].lower()]
        lv_cands = [{"librivox_id": b["librivox_id"], "title": b["title"],
                     "author": b["author"]} for b in lv[:120]]
        if hits or lv_cands:
            if len(lv) > 120:
                print(f"  ({len(lv)} LibriVox matches -- showing 120; "
                      f"add words to narrow)")
            return hits + lv_cands

        # 3. bucket-only Fabuly scan (de-slugged title match too)
        print("Not in the storefront; scanning the Fabuly bucket ...")
        wide = [slug for slug in iter_all_slugs()
                if qn in slug.lower() or q in _deslug(slug).lower()]
        return [{"bookId": s} for s in wide]

    def _prompt(self, candidates: list[dict]) -> list[dict]:
        print(f"\n{len(candidates)} matches:\n")
        for i, b in enumerate(candidates, 1):
            ref = self._book_ref(b)
            title = b.get("title") or slug_to_title(ref)
            author = b.get("author") or ""
            print(f"  {i:3d}. {title}  {('-- ' + author) if author else ''}")
            print(f"       {ref}")
        raw = input("\nSelect (number, list, 1-3, or 'all'): ")
        idx = parse_selection(raw, len(candidates))
        if not idx:
            sys.exit("Nothing selected.")
        return [candidates[i] for i in idx]

    # -- per book -----------------------------------------------------

    def _discover_parts(self, slug: str) -> list[tuple[str, int]]:
        suffix = "_enhanced" if self.enhanced else ""
        parts: list[tuple[str, int]] = []
        i = 0
        while i < 400:
            url = f"{BUCKET}/{slug}/{slug}_{i}{suffix}.m4a"
            size = object_size(url)
            if size is None:
                break
            parts.append((url, size))
            i += 1
        if not parts and self.enhanced:
            print("  No enhanced audio for this title; falling back to original.")
            self.enhanced = False
            return self._discover_parts(slug)
        return parts

    def _fetch_cover(self, slug: str, dest_dir: Path) -> Optional[Path]:
        # Fabuly's cover objects are misnamed: "<slug>.jpg" is often really a
        # PNG.  Fetch the bytes, then name/type the file by its magic number.
        if not self.want_cover:
            return None
        for name in (f"{slug}.jpg", f"{slug}.png", f"{slug}_high_res.jpg"):
            url = f"{BUCKET}/{slug}/{name}"
            if object_size(url) is None:
                continue
            try:
                raw = http_get(url, optional=True)
            except Exception as e:  # noqa: BLE001
                print(f"    (cover download failed: {e})")
                return None
            if not raw:
                continue
            ext = "png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"
            dest = dest_dir / f"cover.{ext}"
            dest.write_bytes(raw)
            print(f"    saved {dest.name}  ({len(raw) // 1024:,} KiB)")
            return dest
        return None

    def _transcode(self, src: Path) -> Path:
        dest = src.with_suffix(".mp3")
        if dest.exists() and dest.stat().st_size > 0:
            print(f"    skip transcode (already have {dest.name})")
            src.unlink(missing_ok=True)
            return dest
        cmd = [self.ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
               "-vn", "-map_metadata", "-1", "-c:a", "libmp3lame", "-q:a", "4",
               str(dest)]
        if self.debug:
            print("    " + " ".join(cmd))
        subprocess.run(cmd, check=True)
        src.unlink(missing_ok=True)
        print(f"    transcoded -> {dest.name}")
        return dest

    def _tag(self, path: Path, *, track: int, total: int, chapter: str,
             book: str, author: str, narrator: str, summary: str,
             cover: Optional[Path]) -> None:
        cover_bytes = cover.read_bytes() if cover else None
        is_png = bool(cover_bytes) and cover_bytes[:8] == b"\x89PNG\r\n\x1a\n"
        try:
            if path.suffix.lower() == ".m4a":
                audio = MP4(str(path))
                audio["\xa9nam"] = [chapter or f"{book} - Part {track}"]
                audio["\xa9alb"] = [book]
                audio["\xa9ART"] = [author]
                audio["aART"] = [author]
                audio["trkn"] = [(track, total)]
                if narrator:
                    audio["\xa9wrt"] = [narrator]
                if summary:
                    audio["\xa9cmt"] = [summary]
                audio["\xa9gen"] = ["Audiobook"]
                if cover_bytes:
                    fmt = MP4Cover.FORMAT_PNG if is_png else MP4Cover.FORMAT_JPEG
                    audio["covr"] = [MP4Cover(cover_bytes, imageformat=fmt)]
                audio.save()
            else:
                audio = MP3(str(path), ID3=ID3)
                if audio.tags is None:
                    audio.add_tags()
                tags = audio.tags
                tags.clear()  # LibriVox source MP3s carry stale LINK/TLEN/etc.
                tags.add(TIT2(encoding=3, text=chapter or f"{book} - Part {track}"))
                tags.add(TALB(encoding=3, text=book))
                tags.add(TPE1(encoding=3, text=author))
                tags.add(TRCK(encoding=3, text=f"{track}/{total}"))
                tags.add(TCON(encoding=3, text="Audiobook"))
                if narrator:
                    tags.add(TPE2(encoding=3, text=narrator))
                if summary:
                    tags.add(COMM(encoding=3, lang="eng", desc="", text=summary))
                if cover_bytes:
                    tags.add(APIC(encoding=3,
                                  mime="image/png" if is_png else "image/jpeg",
                                  type=3, desc="Cover", data=cover_bytes))
                audio.save()
        except Exception as e:  # noqa: BLE001
            print(f"    Warning: tagging failed for {path.name}: {e}")

    def _write_cue(self, book: str, author: str, out_dir: Path,
                   entries: list[tuple[str, str]]) -> None:
        """entries: [(filename, chapter_title)] in play order."""
        if not entries:
            return
        lines = [
            "REM GENRE Audiobook",
            f'REM DATE {time.strftime("%Y")}',
            f'PERFORMER "{author}"',
            f'TITLE "{book}"',
        ]
        for n, (fname, title) in enumerate(entries, 1):
            fmt = "MP3" if fname.lower().endswith(".mp3") else "MP4"
            lines.append(f'FILE "{fname}" {fmt}')
            lines.append(f"  TRACK {n:02d} AUDIO")
            lines.append(f'    TITLE "{title}"')
            lines.append("    INDEX 01 00:00:00")
        out = out_dir / f"{safe_name(book)}.cue"
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"  CUE written: {out}")

    def process(self, book: dict) -> None:
        if "librivox_id" in book:
            self._process_librivox(book)
        else:
            self._process_fabuly(book)

    def _process_librivox(self, book: dict) -> None:
        lid = book["librivox_id"]
        meta = http_get_json(librivox_meta_url(lid), optional=True)
        secs = (meta or {}).get("sections") or []
        if not secs:
            print(f"  No LibriVox metadata for id {lid}; skipping.")
            return
        title = book.get("title") or f"LibriVox #{lid}"
        author = book.get("author") or "Unknown"
        summary = ((meta or {}).get("description") or "").strip()
        print(f"\n=== {title} ===")
        print(f"    LibriVox id {lid} -- {len(secs)} section(s), audio from archive.org")

        book_dir = self.out_dir / safe_name(title)
        book_dir.mkdir(parents=True, exist_ok=True)
        cover = self._fetch_librivox_cover(secs, book_dir)

        cue_entries: list[tuple[str, str]] = []
        for i, sec in enumerate(secs, 1):
            url = sec.get("url")
            if not url:
                continue
            chapter = sec.get("title") or (title if len(secs) == 1 else f"Part {i}")
            dest = book_dir / f"{safe_name(title)}-Part{i:03d}.mp3"
            print(f"  [{i}/{len(secs)}] {chapter}")
            try:
                download_file(url, dest, label=dest.name)
            except Exception as e:  # noqa: BLE001
                print(f"    skipped ({e})")
                continue
            self._tag(dest, track=i, total=len(secs), chapter=chapter,
                      book=title, author=author, narrator="", summary=summary,
                      cover=cover)
            cue_entries.append((dest.name, chapter))

        self._write_cue(title, author, book_dir, cue_entries)
        print(f"\n  Done -> {book_dir}")
        print(f"  {len(cue_entries)} file(s), one per LibriVox section. "
              f"The .cue is a combined chapter index.")

    def _fetch_librivox_cover(self, sections: list[dict],
                              dest_dir: Path) -> Optional[Path]:
        if not self.want_cover:
            return None
        ident = ""
        for s in sections:
            m = _ARCHIVE_ID_RE.search(s.get("url", ""))
            if m:
                ident = m.group(1)
                break
        if not ident:
            return None
        for url in (f"https://archive.org/services/img/{ident}",
                    f"https://archive.org/download/{ident}/__ia_thumb.jpg"):
            try:
                raw = http_get(url, optional=True)
            except Exception:  # noqa: BLE001
                raw = None
            if not raw:
                continue
            is_png = raw[:8] == b"\x89PNG\r\n\x1a\n"
            if not (is_png or raw[:3] == b"\xff\xd8\xff"):
                continue
            dest = dest_dir / ("cover.png" if is_png else "cover.jpg")
            dest.write_bytes(raw)
            print(f"    saved {dest.name}  ({len(raw) // 1024:,} KiB)")
            return dest
        return None

    def _process_fabuly(self, book: dict) -> None:
        slug = book["bookId"]
        binblob = http_get(f"{BUCKET}/{slug}/{slug}.bin", optional=True)
        binmeta = parse_book_bin(binblob)
        reviews = http_get_json(f"{BUCKET}/{slug}/metadata.json", optional=True) or {}

        title = book.get("title") or binmeta["title"] or slug_to_title(slug)
        author = book.get("author") or binmeta["author"] or "Unknown"
        print(f"\n=== {title} ===")
        print(f"    slug: {slug}")
        narrator = ", ".join(
            self.creators.get(nid, nid.replace("_", " ").title())
            for nid in book.get("narratorsIds", []) or []
        )
        summary = (reviews.get("summary") or "").strip()

        parts = self._discover_parts(slug)
        if not parts:
            print("  No audio parts found; skipping.")
            return
        sections = binmeta["sections"]
        kind = "enhanced narration" if self.enhanced else "original narration"
        print(f"  {len(parts)} audio part(s), {kind}"
              + (f"; {len(sections)} chapter title(s) from .bin" if sections else ""))
        if sections and len(sections) != len(parts):
            print(f"  Note: {len(sections)} chapters but {len(parts)} parts -- "
                  f"titles past the overlap fall back to 'Part N'.")

        book_dir = self.out_dir / safe_name(title)
        book_dir.mkdir(parents=True, exist_ok=True)
        cover = self._fetch_cover(slug, book_dir)

        cue_entries: list[tuple[str, str]] = []
        for i, (url, size) in enumerate(parts):
            n = i + 1
            chapter = ""
            if i < len(sections) and sections[i]["title"]:
                chapter = sections[i]["title"]
            if not chapter:
                chapter = title if len(parts) == 1 else f"Part {n}"
            dest = book_dir / f"{safe_name(title)}-Part{n:03d}.m4a"
            print(f"  [{n}/{len(parts)}] {chapter}")
            download_file(url, dest, expected=size, label=dest.name)
            if self.to_mp3:
                dest = self._transcode(dest)
            self._tag(dest, track=n, total=len(parts), chapter=chapter,
                      book=title, author=author, narrator=narrator,
                      summary=summary, cover=cover)
            cue_entries.append((dest.name, chapter))

        self._write_cue(title, author, book_dir, cue_entries)
        n = len(cue_entries)
        print(f"\n  Done -> {book_dir}")
        print(f"  {n} file{'s' if n != 1 else ''}, already one per chapter. "
              f"The .cue is just a combined chapter index for players that use it.")

    # -- entry --------------------------------------------------------

    def run(self, query: Optional[str]) -> None:
        if not query:
            print("Enter a book title, Fabuly slug, or 'lv:<id>' to download.")
            print("Browse first:")
            print("    list <term>     titles matching <term>  (Fabuly + ~19k LibriVox)")
            print("    q               quit")
        while not query:
            raw = input("\nfabuly> ").strip()
            head = raw.split(None, 1)[0].lower() if raw else ""
            if head in ("q", "quit", "exit"):
                return
            if head in ("list", "l", "ls", "?"):
                term = raw.split(None, 1)[1] if len(raw.split(None, 1)) > 1 else ""
                self._browse(term)
                continue
            query = raw
        candidates = self._resolve(query)
        if not candidates:
            sys.exit(f"No book matched {query!r}.")
        chosen = candidates if len(candidates) == 1 else self._prompt(candidates)
        for b in chosen:
            try:
                self.process(b)
            except Exception as e:  # noqa: BLE001
                print(f"  ERROR on {self._book_ref(b)}: {e}")
                if self.debug:
                    raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    # Book titles carry curly quotes / accents; a cp1252 console would crash
    # on print().  Fall back to replacement chars instead.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    parser = argparse.ArgumentParser(
        description="Download classic audiobooks from Fabuly (no login needed).")
    parser.add_argument("--book", metavar="NAME",
                        help="Book title substring, exact Fabuly slug, or "
                             "'lv:<id>' for a LibriVox book. Omit to be prompted.")
    parser.add_argument("--out", default="./fabuly_downloads", metavar="DIR",
                        help="Output directory (default: ./fabuly_downloads)")
    parser.add_argument("--enhanced", action="store_true",
                        help="Fabuly books: grab the premium 'enhanced' narration "
                             "when available (no effect on LibriVox books)")
    parser.add_argument("--mp3", action="store_true",
                        help="Fabuly books: transcode the .m4a parts to MP3 "
                             "(requires ffmpeg; LibriVox parts are already MP3)")
    parser.add_argument("--ffmpeg", metavar="PATH",
                        help="Path to ffmpeg (defaults to one found on PATH)")
    parser.add_argument("--no-cover", action="store_true", help="Skip cover art")
    parser.add_argument("--list", action="store_true",
                        help="Print the full catalogue (every downloadable book) and exit")
    parser.add_argument("--csv", metavar="FILE",
                        help="With --list: write the catalogue to a CSV file instead of printing")
    parser.add_argument("--debug", action="store_true", help="Verbose errors")
    args = parser.parse_args()

    dl = FabulyDownloader(
        out_dir=args.out,
        ffmpeg=args.ffmpeg,
        enhanced=args.enhanced,
        to_mp3=args.mp3,
        want_cover=not args.no_cover,
        debug=args.debug,
    )
    try:
        if args.list or args.csv:
            dl.list_catalogue(csv_path=args.csv)
        else:
            dl.run(args.book)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
    finally:
        try:
            input("\nPress Enter to exit...")
        except EOFError:
            pass


if __name__ == "__main__":
    main()
