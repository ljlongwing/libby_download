#!/usr/bin/env python3
"""For each Fabuly storefront book, ask Open Library for first_publish_year
and subjects.  Writes fabuly_meta.json  {slug: {year, subjects}}.  Resumable."""
import json, os, re, time, urllib.parse, urllib.request, urllib.error

HERE = os.path.dirname(__file__)
OUT = os.path.join(HERE, "fabuly_meta.json")
BUCKET = "https://storage.googleapis.com/dopex_public_us"
UA = "audiobook-tagger/1.0 (personal metadata bake; github ljlongwing/libby_download)"

def get(url):
    for attempt in range(4):
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": UA}),
                timeout=45).read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return b"{}"
            time.sleep(2 * (attempt + 1))
        except Exception:
            time.sleep(2 * (attempt + 1))
    return b"{}"

books = json.loads(get(f"{BUCKET}/books_metadata.json"))["booksMetadata"]
out = json.load(open(OUT)) if os.path.exists(OUT) else {}
print(f"{len(books)} storefront books; {len(out)} already done")

STOP = {"the", "a", "an", "of", "and", "or"}

def surname(author):
    a = re.sub(r"\(.*?\)|\".*?\"|,.*$", "", author).strip()
    parts = a.split()
    return parts[-1].lower() if parts else ""

for i, b in enumerate(books, 1):
    slug = b["bookId"]
    if slug in out:
        continue
    title, author = b.get("title", ""), b.get("author", "")
    q = urllib.parse.urlencode({
        "title": title, "author": author,
        "fields": "title,author_name,first_publish_year,subject",
        "limit": "5"})
    docs = json.loads(get(f"https://openlibrary.org/search.json?{q}")).get("docs", [])
    sn = surname(author)
    year, subjects = None, []
    for d in docs:
        names = " ".join(d.get("author_name", [])).lower()
        if sn and sn not in names:
            continue
        y = d.get("first_publish_year")
        if y and (year is None or y < year):
            year = y
        if not subjects:
            subjects = [s for s in (d.get("subject") or [])[:8]]
    out[slug] = {"year": year, "subjects": subjects}
    if i % 25 == 0:
        json.dump(out, open(OUT, "w"))
        got = sum(1 for v in out.values() if v.get("year"))
        print(f"  {i}/{len(books)}  ({got} with a year)", flush=True)
    time.sleep(0.3)

json.dump(out, open(OUT, "w"))
got = sum(1 for v in out.values() if v.get("year"))
print(f"DONE: {len(out)} slugs, {got} with a year -> {OUT}")
