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

    python fabuly_dl.py                       # graphical catalogue browser
    python fabuly_dl.py --no-gui             # text browser: search -> pick a number
    python fabuly_dl.py --list --csv out.csv  # dump the whole ~19.5k catalogue
    python fabuly_dl.py --book "moby dick"                    # search both sources
    python fabuly_dl.py --book lv:54                          # LibriVox book id 54
    python fabuly_dl.py --book _the_viy_nicholas_gogol_en --enhanced
    python fabuly_dl.py --book "A Christmas Carol" --mp3 --ffmpeg C:\\ffmpeg\\bin\\ffmpeg.exe

With no --book you get the GUI (or, with --no-gui, a text browser that
searches title/author/genre/language and downloads by number).  The merged
catalogue is cached at ~/.fabuly_catalog.json for a week; --refresh rebuilds
it.  --template / the GUI "Folder" field controls the sub-folder layout,
e.g. --template "{author}/{title}"  (tokens: title author author_initial
genre language source narrator slug).

Third-party dependency: ``mutagen`` (``ffmpeg`` optional, only for --mp3;
``tkinter`` is stdlib and only needed for the GUI).  ``librivox.db`` must sit
next to this script for LibriVox titles.
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

# Merged catalogue is cached here so repeat runs / GUI launches skip the
# ~8 s rebuild.  `--refresh` ignores it; it self-expires after CATALOG_TTL.
CATALOG_CACHE = Path.home() / ".fabuly_catalog.json"
CATALOG_TTL = 7 * 24 * 3600  # seconds
_CACHE_SCHEMA = 3            # bump when the row shape changes


def _read_catalog_cache() -> "Optional[list[dict]]":
    try:
        if time.time() - CATALOG_CACHE.stat().st_mtime > CATALOG_TTL:
            return None
        blob = json.loads(CATALOG_CACHE.read_text(encoding="utf-8"))
        if blob.get("schema") == _CACHE_SCHEMA and blob.get("rows"):
            return blob["rows"]
    except (OSError, ValueError):
        pass
    return None


def _write_catalog_cache(rows: "list[dict]") -> None:
    try:
        CATALOG_CACHE.write_text(
            json.dumps({"schema": _CACHE_SCHEMA, "rows": rows}),
            encoding="utf-8")
    except OSError:
        pass


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


_GENRE_NAMES: Optional[dict[str, str]] = None


def load_genre_names() -> dict[str, str]:
    """Map Fabuly subCategoryId -> human display name."""
    global _GENRE_NAMES
    if _GENRE_NAMES is None:
        data = http_get_json(f"{BUCKET}/books_genres_hierarchy.json",
                             optional=True) or {}
        m: dict[str, str] = {}
        for cat in data.get("categories", []):
            for sub in cat.get("subCategories", []):
                if sub.get("subCategoryId"):
                    m[sub["subCategoryId"]] = sub.get("displayName") or sub["subCategoryId"]
        _GENRE_NAMES = m
    return _GENRE_NAMES


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


TEMPLATE_TOKENS = ("title", "author", "author_initial", "genre", "language",
                   "source", "narrator", "slug")
_TOKEN_RE = re.compile(r"\{(\w+)\}")


def render_path_template(template: str, meta: dict) -> Path:
    """Turn a folder template like ``{author}/{title}`` into a relative Path.

    Tokens come from ``meta`` (each value is sanitised).  ``/`` and ``\\``
    split into sub-folders.  A path segment that renders empty (e.g.
    ``{genre}`` for a LibriVox book) is dropped rather than left blank.
    """
    author = str(meta.get("author") or "")
    tokens = {
        "title": str(meta.get("title") or ""),
        "author": author,
        "author_initial": next((c.upper() for c in author if c.isalpha()), "#"),
        "genre": str(meta.get("genre") or "").split(";")[0].strip(),
        "language": str(meta.get("language") or ""),
        "source": str(meta.get("source") or ""),
        "narrator": str(meta.get("narrators") or meta.get("narrator") or "")
                    .split(";")[0].strip(),
        "slug": str(meta.get("slug") or meta.get("bookId")
                    or (f"lv{meta['librivox_id']}" if "librivox_id" in meta else "")),
    }
    def _san(s: str) -> str:  # like safe_name but no "book" fallback
        s = _UNSAFE.sub("", s or "").strip().strip(".")
        return re.sub(r"\s+", " ", s)[:150]

    segments = []
    for seg in re.split(r"[\\/]+", template.strip()):
        rendered = _TOKEN_RE.sub(lambda m: tokens.get(m.group(1), m.group(0)), seg)
        clean = _san(rendered)
        if clean:
            segments.append(clean)
    if not segments:
        segments = [_san(tokens["title"]) or "book"]
    return Path(*segments)


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
                 want_cover: bool = True, debug: bool = False,
                 refresh: bool = False, path_template: str = "{title}") -> None:
        self.out_dir = Path(out_dir)
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg")
        self.enhanced = enhanced
        self.to_mp3 = to_mp3
        self.want_cover = want_cover
        self.debug = debug
        self.refresh = refresh
        self.path_template = path_template or "{title}"
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

    _LANG_CODES = {"en": "English", "de": "German", "es": "Spanish",
                   "fr": "French", "it": "Italian", "pt": "Portuguese",
                   "ru": "Russian", "nl": "Dutch"}

    def catalogue_rows(self) -> list[dict]:
        """Every downloadable book.  Row keys: title, author, narrators,
        duration, language, genre, enhanced, slug, source."""
        authors = {b.get("author", "") for b in self.featured if b.get("author")}
        authors |= {c.get("name", "") for c in
                    (load_creators_full() or []) if c.get("type") == "AUTHOR"}
        authors = {a for a in authors if a}
        gnames = load_genre_names()

        def _hms(d: int) -> str:
            return f"{d // 3600}:{d % 3600 // 60:02d}" if d else ""

        def _slug_lang(slug: str) -> str:
            m = re.search(r"_([a-z]{2})(?:_original)?$", slug)
            return self._LANG_CODES.get(m.group(1), "") if m else ""

        rows: dict[str, dict] = {}
        for b in self.featured:
            sid = b["bookId"]
            rows[sid] = {
                "title": b.get("title", ""),
                "author": b.get("author", ""),
                "narrators": "; ".join(self.creators.get(n, n.replace("_", " ").title())
                                       for n in b.get("narratorsIds", []) or []),
                "duration": _hms(b.get("durationInSeconds") or 0),
                "language": _slug_lang(sid) or "English",
                "genre": "; ".join(dict.fromkeys(
                    gnames.get(g, g.replace("_", " ").title())
                    for g in b.get("subCategoryIds", []) or []
                    if g not in ("new", "recommendations"))),
                "enhanced": "yes" if b.get("isEnhancedAudioAvailable") else "",
                "slug": sid,
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
                          "duration": "", "language": _slug_lang(slug) or "English",
                          "genre": "", "enhanced": "", "slug": slug,
                          "source": "bucket-only"}
        for b in load_librivox():
            rows[f"lv:{b['librivox_id']}"] = {
                "title": b["title"], "author": b["author"], "narrators": "",
                "duration": _hms(b["duration"]), "language": b["language"],
                "genre": "", "enhanced": "", "slug": f"lv:{b['librivox_id']}",
                "source": "librivox",
            }
        return sorted(rows.values(),
                      key=lambda r: (r["author"].lower(), r["title"].lower()))

    @property
    def catalog(self) -> list[dict]:
        """The full merged catalogue.  Held for the process, and cached on
        disk (see CATALOG_CACHE) so repeat runs / GUI launches are instant.
        """
        if self._catalog is not None:
            return self._catalog
        if not self.refresh:
            cached = _read_catalog_cache()
            if cached is not None:
                age_h = (time.time() - CATALOG_CACHE.stat().st_mtime) / 3600
                print(f"Catalogue: {len(cached):,} books from cache "
                      f"({age_h:.0f}h old; --refresh to rebuild)")
                self._catalog = cached
                return self._catalog
        self._catalog = self.catalogue_rows()
        _write_catalog_cache(self._catalog)
        return self._catalog

    @staticmethod
    def _row_text(r: dict) -> str:
        """All searchable text for a row, lower-cased."""
        return " ".join((r["title"], r["author"], r["genre"], r["language"],
                         r["slug"], _deslug(r["slug"]))).lower()

    def filter_catalog(self, query: str = "", *, source: str = "",
                       language: str = "", genre: str = "") -> list[dict]:
        """Filter the full catalogue.  ``query`` is AND-ed word-by-word over
        title/author/genre/language/slug; the others are exact-ish facets."""
        rows = self.catalog
        words = [w for w in re.split(r"\s+", query.strip().lower()) if w]
        src = source.strip().lower()
        lang = language.strip().lower()
        gen = genre.strip().lower()
        out = []
        for r in rows:
            if src and r["source"] != src:
                continue
            if lang and r["language"].lower() != lang:
                continue
            if gen and gen not in r["genre"].lower():
                continue
            if words:
                hay = self._row_text(r)
                if not all(w in hay for w in words):
                    continue
            out.append(r)
        return out

    @staticmethod
    def _print_row(r: dict, n: Optional[int] = None) -> None:
        num = f"{n:4d}. " if n is not None else "  "
        bits = [b for b in (r["duration"], r["language"] if r["source"] == "librivox"
                            else "", "enhanced" if r["enhanced"] else "",
                            r["source"]) if b]
        print(f"{num}{r['title']}  -- {r['author'] or '?'}  ({', '.join(bits)})")
        print(f"      {r['slug']}")

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

    @staticmethod
    def _book_ref(b: dict) -> str:
        if "librivox_id" in b:
            return f"lv:{b['librivox_id']}"
        return b.get("bookId") or b.get("slug") or "?"

    @staticmethod
    def row_to_book(row: dict) -> dict:
        """Catalogue row -> a book dict process() understands.  Carries the
        extra metadata so folder templates can use {genre} etc."""
        meta = {k: row.get(k, "") for k in
                ("title", "author", "genre", "language", "narrators", "source")}
        if row["slug"].startswith("lv:"):
            meta["librivox_id"] = int(row["slug"][3:])
        else:
            meta["bookId"] = row["slug"]
        return meta

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
        raw = input("\nSelect (number, 1-3, 'all', or blank to cancel): ")
        idx = parse_selection(raw, len(candidates))
        return [candidates[i] for i in idx] if idx else []

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

        book_dir = self.out_dir / render_path_template(
            self.path_template,
            {**book, "title": title, "author": author, "slug": f"lv:{lid}",
             "source": "librivox"})
        book_dir.mkdir(parents=True, exist_ok=True)
        print(f"    -> {book_dir}")
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
        ) or (book.get("narrators") or "")
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

        book_dir = self.out_dir / render_path_template(
            self.path_template,
            {**book, "title": title, "author": author, "slug": slug,
             "narrators": narrator, "source": book.get("source", "storefront")})
        book_dir.mkdir(parents=True, exist_ok=True)
        print(f"    -> {book_dir}")
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

    def _download_books(self, books: list[dict]) -> None:
        for b in books:
            try:
                self.process(b)
            except Exception as e:  # noqa: BLE001
                print(f"  ERROR on {self._book_ref(b)}: {e}")
                if self.debug:
                    raise

    def run(self, query: Optional[str]) -> None:
        """--book path: resolve, then download (or fall into the browser)."""
        if not query:
            return self.browse_repl()
        candidates = self._resolve(query)
        if not candidates:
            print(f"Nothing matched {query!r}. Opening the browser instead.\n")
            return self.browse_repl()
        chosen = candidates if len(candidates) == 1 else self._prompt(candidates)
        self._download_books(chosen)

    def browse_repl(self) -> None:
        """Forgiving text browser: search -> numbered list -> pick a number."""
        print("\nType words to search the catalogue "
              f"({len(self.catalog):,} books, Fabuly + LibriVox).")
        print("Search matches title, author, genre, language.  Examples:")
        print("    sherlock holmes        dickens        twain ghost")
        print("Then type a number to download -- or several: 1,4,7  or  2-6  or  all.")
        print("'q' to quit.\n")
        results: list[dict] = []
        while True:
            try:
                raw = input("search (or number)> ").strip()
            except EOFError:
                return
            if raw.lower() in ("q", "quit", "exit"):
                return
            if not raw:
                continue
            if results and re.fullmatch(r"(all|\*|[\d,\s-]+)", raw.lower()):
                idx = parse_selection(raw, len(results))
                if idx:
                    self._download_books(
                        [self.row_to_book(results[i]) for i in idx])
                else:
                    print(f"  nothing valid in {raw!r} (list is 1-{len(results)})")
                continue
            # explicit slug / lv:id still works
            if re.fullmatch(r"(?:lv[:#]?|#)\d{1,7}|[a-z0-9_]{6,}", raw):
                cand = self._resolve(raw)
                if cand:
                    self._download_books(cand[:1])
                    continue
            results = self.filter_catalog(raw)
            if not results:
                print(f"  no matches for {raw!r} -- try fewer or different words")
                continue
            shown = results[:60]
            for n, r in enumerate(shown, 1):
                self._print_row(r, n)
            tail = f"  (showing 60 of {len(results)}; add words to narrow)\n" \
                if len(results) > 60 else ""
            print(f"\n{tail}  {len(shown)} shown. Type a number to download, "
                  f"or search again.")


# ---------------------------------------------------------------------------
# GUI  (tkinter -- stdlib; optional)
# ---------------------------------------------------------------------------

class _QueueWriter:
    """A file-like that funnels writes to a queue, splitting on \\r and \\n
    so a Tk log can show progress lines that overwrite themselves."""

    def __init__(self, q):
        self.q = q
        self._buf = ""

    def write(self, s: str) -> int:
        for ch in s:
            if ch == "\n":
                self.q.put(("line", self._buf))
                self._buf = ""
            elif ch == "\r":
                self.q.put(("cr", self._buf))
                self._buf = ""
            else:
                self._buf += ch
        return len(s)

    def flush(self) -> None:
        if self._buf:
            self.q.put(("cr", self._buf))


def run_gui(dl: "FabulyDownloader") -> None:
    import queue
    import threading
    import tkinter as tk
    from tkinter import filedialog, ttk
    from tkinter.scrolledtext import ScrolledText

    root = tk.Tk()  # raises if there's no display -> caller falls back to text

    # Tk is up: on a double-clicked Windows .exe a console also opened; hide
    # it so the GUI stands alone.  (CLI invocations never reach here.)
    if sys.platform == "win32":
        try:
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)
        except Exception:  # noqa: BLE001
            pass

    root.title("Fabuly / LibriVox audiobook browser")
    root.geometry("1000x680")
    root.minsize(760, 480)

    state = {"rows": [], "shown": [], "busy": False}
    logq: "queue.Queue" = queue.Queue()

    # --- top: search + facets ------------------------------------------
    top = ttk.Frame(root, padding=8)
    top.pack(fill="x")
    ttk.Label(top, text="Search").grid(row=0, column=0, sticky="w")
    q_var = tk.StringVar()
    q_entry = ttk.Entry(top, textvariable=q_var)
    q_entry.grid(row=0, column=1, columnspan=5, sticky="ew", padx=(4, 12))
    q_entry.focus_set()

    src_var = tk.StringVar(value="all sources")
    lang_var = tk.StringVar(value="all languages")
    genre_var = tk.StringVar(value="all genres")
    src_cb = ttk.Combobox(top, textvariable=src_var, state="readonly", width=16,
                          values=["all sources", "storefront", "bucket-only",
                                  "librivox"])
    lang_cb = ttk.Combobox(top, textvariable=lang_var, state="readonly", width=16)
    genre_cb = ttk.Combobox(top, textvariable=genre_var, state="readonly", width=26)
    src_cb.grid(row=1, column=1, sticky="w", pady=(6, 0))
    lang_cb.grid(row=1, column=2, sticky="w", padx=6, pady=(6, 0))
    genre_cb.grid(row=1, column=3, sticky="w", pady=(6, 0))
    top.columnconfigure(1, weight=1)

    # --- middle: results table ---------------------------------------
    mid = ttk.Frame(root, padding=(8, 0))
    mid.pack(fill="both", expand=True)
    cols = ("title", "author", "duration", "language", "source")
    tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
    widths = {"title": 420, "author": 220, "duration": 70, "language": 90,
              "source": 90}
    for c in cols:
        tree.heading(c, text=c.title(),
                     command=lambda cc=c: _sort_by(cc))
        tree.column(c, width=widths[c], anchor="w")
    vs = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vs.set)
    tree.pack(side="left", fill="both", expand=True)
    vs.pack(side="right", fill="y")

    status = ttk.Label(root, text="Loading catalogue ...", padding=(10, 2))
    status.pack(fill="x")

    # --- bottom: output + download + log ---------------------------
    bot = ttk.Frame(root, padding=8)
    bot.pack(fill="x")
    ttk.Label(bot, text="Save to").grid(row=0, column=0, sticky="w")
    out_var = tk.StringVar(value=str(dl.out_dir.resolve()))
    ttk.Entry(bot, textvariable=out_var).grid(row=0, column=1, sticky="ew", padx=4)
    ttk.Button(bot, text="Browse...",
               command=lambda: out_var.set(filedialog.askdirectory(
                   initialdir=out_var.get()) or out_var.get())
               ).grid(row=0, column=2)
    enh_var = tk.BooleanVar(value=dl.enhanced)
    ttk.Checkbutton(bot, text="Enhanced narration (Fabuly only)",
                    variable=enh_var).grid(row=0, column=3, padx=10)
    dl_btn = ttk.Button(bot, text="Download selected")
    dl_btn.grid(row=0, column=4)
    bot.columnconfigure(1, weight=1)

    ttk.Label(bot, text="Folder").grid(row=1, column=0, sticky="w", pady=(6, 0))
    tmpl_var = tk.StringVar(value=dl.path_template)
    tmpl_cb = ttk.Combobox(bot, textvariable=tmpl_var, values=[
        "{title}",
        "{author}/{title}",
        "{author_initial}/{author}/{title}",
        "{source}/{author}/{title}",
        "{language}/{author}/{title}",
        "{genre}/{title}",
    ])
    tmpl_cb.grid(row=1, column=1, columnspan=2, sticky="ew", padx=4, pady=(6, 0))
    ttk.Label(bot, text="tokens: " + "  ".join("{%s}" % t for t in TEMPLATE_TOKENS),
              foreground="#888").grid(row=2, column=1, columnspan=4, sticky="w")

    log = ScrolledText(root, height=9, state="disabled", wrap="word")
    log.pack(fill="both", padx=8, pady=(0, 8))

    # --- behaviour -------------------------------------------------
    def _sort_by(col: str, _flip=[False]):  # noqa: B006
        _flip[0] = not _flip[0]
        state["shown"].sort(key=lambda r: str(r[col]).lower(), reverse=_flip[0])
        _fill(state["shown"])

    def _fill(rows: list[dict]) -> None:
        tree.delete(*tree.get_children())
        for i, r in enumerate(rows[:2000]):
            tree.insert("", "end", iid=str(i),
                        values=(r["title"], r["author"] or "?", r["duration"],
                                r["language"], r["source"]))
        extra = f"  (first 2000 shown)" if len(rows) > 2000 else ""
        status.config(text=f"{len(rows):,} of {len(state['rows']):,} books{extra}")

    def _apply(*_):
        if state["busy"] or not state["rows"]:
            return
        src = "" if src_var.get().startswith("all") else src_var.get()
        lang = "" if lang_var.get().startswith("all") else lang_var.get()
        gen = "" if genre_var.get().startswith("all") else genre_var.get()
        rows = dl.filter_catalog(q_var.get(), source=src, language=lang, genre=gen)
        state["shown"] = rows
        _fill(rows)

    _debounce = {"id": None}

    def _on_key(*_):
        if _debounce["id"]:
            root.after_cancel(_debounce["id"])
        _debounce["id"] = root.after(250, _apply)

    q_entry.bind("<KeyRelease>", _on_key)
    for cb in (src_cb, lang_cb, genre_cb):
        cb.bind("<<ComboboxSelected>>", _apply)

    def _append(kind: str, text: str) -> None:
        log.configure(state="normal")
        if kind == "cr":
            log.delete("end-1l linestart", "end-1c")
        log.insert("end", text + ("\n" if kind == "line" else ""))
        log.see("end")
        log.configure(state="disabled")

    def _catalog_ready(rows: list[dict]) -> None:
        state["rows"] = rows
        lang_cb.config(values=["all languages"] +
                       sorted({r["language"] for r in rows if r["language"]}))
        genre_cb.config(values=["all genres"] +
                        sorted({g.strip() for r in rows
                                for g in r["genre"].split(";") if g.strip()}))
        q_entry.config(state="normal")
        _apply()

    def _dl_done() -> None:
        state["busy"] = False
        dl_btn.config(state="normal")
        _selection_count()

    # Everything that touches Tk happens here, on the main thread, draining
    # a queue that the worker threads write to (tkinter is not thread-safe).
    def _pump():
        try:
            while True:
                msg = logq.get_nowait()
                if msg[0] == "catalog":
                    _catalog_ready(msg[1])
                elif msg[0] == "dl_done":
                    _dl_done()
                elif msg[0] == "btn":
                    dl_btn.config(text=msg[1])
                else:
                    _append(*msg)
        except queue.Empty:
            pass
        root.after(120, _pump)

    def _selection_count(*_):
        n = len(tree.selection())
        dl_btn.config(text=f"Download {n} selected" if n > 1 else "Download selected")

    tree.bind("<<TreeviewSelect>>", _selection_count)

    def _download(only_iid: Optional[str] = None):
        if state["busy"]:
            return
        iids = (only_iid,) if only_iid else tree.selection()
        if not iids:
            _append("line", "Select one or more books in the list first "
                            "(Ctrl-click / Shift-click for several).")
            return
        rows = [state["shown"][int(i)] for i in iids]
        state["busy"] = True
        dl_btn.config(state="disabled",
                      text=f"Downloading 1/{len(rows)} ...")
        dl.out_dir = Path(out_var.get())
        dl.enhanced = bool(enh_var.get())
        dl.path_template = tmpl_var.get().strip() or "{title}"

        def worker():
            import contextlib
            w = _QueueWriter(logq)
            for k, row in enumerate(rows, 1):
                logq.put(("btn", f"Downloading {k}/{len(rows)} ..."))
                if len(rows) > 1:
                    logq.put(("line", f"===== [{k}/{len(rows)}] {row['title']} ====="))
                try:
                    with contextlib.redirect_stdout(w):
                        dl.process(dl.row_to_book(row))
                except Exception as e:  # noqa: BLE001
                    logq.put(("line", f"ERROR ({row['title']}): {e}"))
            w.flush()
            logq.put(("line", f"--- finished {len(rows)} book(s) ---"))
            logq.put(("dl_done", None))

        threading.Thread(target=worker, daemon=True).start()

    dl_btn.config(command=_download)
    tree.bind("<Double-1>", lambda _e: _download(
        tree.identify_row(_e.y) or None))

    def _load():
        try:
            rows = dl.catalog  # network; this is a worker thread
            logq.put(("catalog", rows))
        except Exception as e:  # noqa: BLE001
            logq.put(("line", f"Could not load catalogue: {e}"))

    q_entry.config(state="disabled")
    threading.Thread(target=_load, daemon=True).start()
    _pump()
    root.mainloop()


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
        description="Download classic audiobooks from Fabuly (no login needed). "
                    "With no arguments, opens a graphical browser.")
    parser.add_argument("--book", metavar="NAME",
                        help="Book title words, exact Fabuly slug, or 'lv:<id>' "
                             "for a LibriVox book. Omit for the text browser.")
    parser.add_argument("--gui", action="store_true",
                        help="Open the graphical catalogue browser (the default "
                             "when no arguments are given)")
    parser.add_argument("--no-gui", action="store_true",
                        help="Force the text browser instead of the GUI")
    parser.add_argument("--out", default="./fabuly_downloads", metavar="DIR",
                        help="Output directory (default: ./fabuly_downloads)")
    parser.add_argument("--template", default="{title}", metavar="TMPL",
                        help="Sub-folder layout under --out. Tokens: "
                             + " ".join("{%s}" % t for t in TEMPLATE_TOKENS)
                             + "  (default: {title}; e.g. \"{author}/{title}\")")
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
    parser.add_argument("--refresh", action="store_true",
                        help="Rebuild the catalogue from the network, ignoring "
                             f"the local cache ({CATALOG_CACHE.name})")
    parser.add_argument("--debug", action="store_true", help="Verbose errors")
    args = parser.parse_args()

    dl = FabulyDownloader(
        out_dir=args.out,
        ffmpeg=args.ffmpeg,
        enhanced=args.enhanced,
        to_mp3=args.mp3,
        want_cover=not args.no_cover,
        debug=args.debug,
        refresh=args.refresh,
        path_template=args.template,
    )
    # No CLI intent given -> graphical browser (nice for a double-clicked exe).
    want_gui = args.gui or (not args.no_gui and not args.book
                            and not args.list and not args.csv)
    gui_ran = False
    try:
        if args.list or args.csv:
            dl.list_catalogue(csv_path=args.csv)
        elif want_gui:
            try:
                run_gui(dl)
                gui_ran = True
            except Exception as e:  # noqa: BLE001 - fall back to text
                print(f"(GUI unavailable: {e}) -- using the text browser.\n")
                dl.browse_repl()
        else:
            dl.run(args.book)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
    finally:
        if not gui_ran:
            try:
                input("\nPress Enter to exit...")
            except EOFError:
                pass


if __name__ == "__main__":
    main()
