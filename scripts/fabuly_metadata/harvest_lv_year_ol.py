#!/usr/bin/env python3
"""For LibriVox books that have NO copyright_year, ask Open Library for a
first_publish_year as a fallback.  Keyed by title/author from librivox.db.
Writes lv_year_ol.json {id: year}.  Resumable + atomic."""
import json, os, re, sqlite3, time, urllib.parse, urllib.request, urllib.error

HERE = os.path.dirname(__file__)
DB = "C:/Users/ljlon/Nextcloud/MyStuph/workspace/libby-download/librivox.db"
OUT = os.path.join(HERE, "lv_year_ol.json")
UA = "audiobook-tagger/1.0 (personal metadata bake; github ljlongwing/libby_download)"

done = json.load(open(OUT)) if os.path.exists(OUT) else {}
con = sqlite3.connect(DB)
todo = [(r[0], r[1], r[2]) for r in
        con.execute("SELECT id,title,author FROM book WHERE year IS NULL")]
print(f"{len(todo)} year-less LibriVox books; {len(done)} already tried", flush=True)

def get(url):
    for a in range(4):
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": UA}),
                timeout=45).read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return b"{}"
            time.sleep(2 + a * 2)
        except Exception:
            time.sleep(2 + a * 2)
    return b"{}"

def surname(a):
    a = re.sub(r"\(.*?\)|\".*?\"|,.*$", "", a or "").strip()
    return a.split()[-1].lower() if a.split() else ""

def save():
    json.dump(done, open(OUT + ".tmp", "w"))
    os.replace(OUT + ".tmp", OUT)

for i, (bid, title, author) in enumerate(todo, 1):
    k = str(bid)
    if k in done:
        continue
    q = urllib.parse.urlencode({"title": title or "", "author": author or "",
                                "fields": "author_name,first_publish_year",
                                "limit": "5"})
    docs = json.loads(get(f"https://openlibrary.org/search.json?{q}")).get("docs", [])
    sn = surname(author)
    year = None
    for d in docs:
        names = " ".join(d.get("author_name", [])).lower()
        if sn and sn not in names:
            continue
        y = d.get("first_publish_year")
        if y and 1400 <= int(y) <= 1930 and (year is None or y < year):
            year = int(y)
    done[k] = year
    if i % 50 == 0:
        save()
        print(f"  {i}/{len(todo)}  ({sum(1 for v in done.values() if v)} found)",
              flush=True)
    time.sleep(0.3)

save()
print(f"DONE {len(done)} tried, {sum(1 for v in done.values() if v)} got a year",
      flush=True)
