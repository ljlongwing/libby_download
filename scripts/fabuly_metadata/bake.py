#!/usr/bin/env python3
"""Bake harvested metadata into what ships with the tool:

  librivox.db     -> add + populate `year`, `genre` columns (from LibriVox API)
  fabuly_meta.json -> {slug: {year}} for the ~435 Fabuly-hosted books, using
                      a LibriVox title/author match first (curated copyright_year)
                      and a sanity-capped Open Library year only as a fallback.
"""
import json, os, re, sqlite3

HERE = os.path.dirname(__file__)
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
LV_META = json.load(open(os.path.join(HERE, "librivox_meta.json")))
OL_META = json.load(open(os.path.join(HERE, "fabuly_meta.json")))
DB = os.path.join(REPO, "librivox.db")

lv_meta = {int(k): v for k, v in LV_META.items()}

# ---- 1. enrich librivox.db ------------------------------------------------
con = sqlite3.connect(DB)
have = {c[1] for c in con.execute("PRAGMA table_info(book)")}
if "year" not in have:
    con.execute("ALTER TABLE book ADD COLUMN year INTEGER")
if "genre" not in have:
    con.execute("ALTER TABLE book ADD COLUMN genre TEXT")

def to_year(v):
    m = re.search(r"-?\d{3,4}", str(v or ""))
    if not m:
        return None
    y = int(m.group())
    return y if 1450 <= y <= 2025 else None

db_rows = {r[0]: (r[1], r[2]) for r in con.execute("SELECT id,title,author FROM book")}
for bid, m in lv_meta.items():
    y = to_year(m.get("year"))
    g = "; ".join(dict.fromkeys(m.get("genres") or []))[:200] or None
    con.execute("UPDATE book SET year=?, genre=? WHERE id=?", (y, g, bid))
con.commit()
hy = con.execute("SELECT COUNT(*) FROM book WHERE year IS NOT NULL").fetchone()[0]
hg = con.execute("SELECT COUNT(*) FROM book WHERE genre IS NOT NULL").fetchone()[0]
tot = con.execute("SELECT COUNT(*) FROM book").fetchone()[0]
print(f"librivox.db: {tot} books -> {hy} year, {hg} genre")

# ---- 2. build a LibriVox title/author -> year index ---------------------
def norm_title(t):
    t = re.sub(r"\(.*?\)|\[.*?\]", "", t or "").lower()
    t = re.sub(r"^(the|a|an) ", "", t.strip())
    return re.sub(r"[^a-z0-9]+", " ", t).strip()

def surname(a):
    a = re.sub(r"\(.*?\)|\".*?\"|,.*$", "", a or "").strip()
    return a.split()[-1].lower() if a.split() else ""

lv_year_idx = {}
for bid, (t, a) in db_rows.items():
    y = to_year(lv_meta.get(bid, {}).get("year"))
    if y:
        lv_year_idx.setdefault((norm_title(t), surname(a)), y)
print(f"LibriVox year index: {len(lv_year_idx)} title/author keys")

# ---- 3. fabuly_meta.json  {slug: {year}} --------------------------------
books = json.load(open(os.path.join(HERE, "books_metadata_snapshot.json")))
out, via_lv, via_ol = {}, 0, 0
for b in books:
    slug, title, author = b["bookId"], b.get("title", ""), b.get("author", "")
    key = (norm_title(title), surname(author))
    y = lv_year_idx.get(key)
    if y and y > 1965:
        y = None
    if y:
        via_lv += 1
    else:
        oly = OL_META.get(slug, {}).get("year")
        # OL gives edition years; only trust it if plausibly public-domain-era
        if oly and 1400 <= int(oly) <= 1930:
            y = int(oly)
            via_ol += 1
    if y:
        out[slug] = {"year": y}
json.dump(out, open(os.path.join(REPO, "fabuly_meta.json"), "w"), indent=0)
print(f"fabuly_meta.json: {len(out)}/{len(books)} slugs with a year "
      f"({via_lv} from LibriVox, {via_ol} from Open Library)")

con.execute("VACUUM")
con.close()
print(f"librivox.db is now {os.path.getsize(DB)} bytes")
