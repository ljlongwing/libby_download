#!/usr/bin/env python3
"""Fill in LibriVox `genres` (and any still-missing year) using the slow
`extended=1` feed.  Gentle + resumable + atomic writes.  Seeds from and
updates librivox_meta.json.  Safe to re-run to close gaps."""
import json, os, time, urllib.request, urllib.error

OUT = os.path.join(os.path.dirname(__file__), "librivox_meta.json")
UA = "audiobook-tagger/1.0 (personal metadata bake; github ljlongwing/libby_download)"
LIMIT = 100
MAX_OFFSET = 40000

data = {int(k): v for k, v in json.load(open(OUT)).items()} if os.path.exists(OUT) else {}
PROG = OUT + ".genreprog"
start_off = int(open(PROG).read()) if os.path.exists(PROG) else 0
print(f"start: {len(data)} books, "
      f"{sum(1 for v in data.values() if v.get('genres'))} have genres", flush=True)

def save():
    json.dump({str(k): v for k, v in data.items()}, open(OUT + ".tmp", "w"))
    os.replace(OUT + ".tmp", OUT)

def fetch(offset):
    url = (f"https://librivox.org/api/feed/audiobooks/"
           f"?format=json&extended=1&limit={LIMIT}&offset={offset}")
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            return json.loads(urllib.request.urlopen(req, timeout=90).read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {"books": []}
            time.sleep(10 + attempt * 15)
        except Exception:
            time.sleep(10 + attempt * 15)
    return None

t0 = time.time()
misses, empty = [], 0
for offset in range(start_off, MAX_OFFSET, LIMIT):
    # skip a page only if every id we'd expect there already has genres -- we
    # don't know the ids up front, so just walk everything but go gently.
    d = fetch(offset)
    if d is None:
        misses.append(offset)
        time.sleep(20)
        continue
    books = d.get("books", []) or []
    if not books:
        empty += 1
        if empty >= 3 and offset > 22000:
            break
        continue
    empty = 0
    changed = 0
    for b in books:
        try:
            bid = int(b["id"])
        except (KeyError, ValueError, TypeError):
            continue
        genres = [g.get("name", "") for g in (b.get("genres") or [])
                  if g.get("name") and not g["name"].lower().startswith("published ")]
        cur = data.get(bid, {})
        y = str(b.get("copyright_year") or "").strip() or cur.get("year") or None
        auth = (b.get("authors") or [{}])[0]
        newrow = {"year": y,
                  "genres": genres or cur.get("genres") or [],
                  "dob": (auth.get("dob") or "").strip() or cur.get("dob") or None,
                  "dod": (auth.get("dod") or "").strip() or cur.get("dod") or None}
        if newrow != cur:
            data[bid] = newrow
            changed += 1
    if offset % 1000 == 0:
        save()
        open(PROG, "w").write(str(offset))
        print(f"  off {offset}: {len(data)} books, "
              f"{sum(1 for v in data.values() if v.get('genres'))} genre "
              f"(+{changed} this page, {int(time.time()-t0)}s)", flush=True)
    time.sleep(3.5)

save()
print(f"DONE {len(data)} books; "
      f"{sum(1 for v in data.values() if v.get('genres'))} genre, "
      f"{sum(1 for v in data.values() if v.get('year'))} year. misses={misses}",
      flush=True)
