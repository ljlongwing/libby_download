#!/usr/bin/env python3
"""Collect year (+ author birth/death) for every LibriVox book into
librivox_meta.json.  Uses the lightweight (non-extended) feed -- reliable
where the extended one throttles.  Existing `genres` values are kept.
Resumable; a failing page is skipped and retried next run."""
import json, os, time, urllib.request, urllib.error

OUT = os.path.join(os.path.dirname(__file__), "librivox_meta.json")
UA = "audiobook-tagger/1.0 (personal metadata bake; github ljlongwing/libby_download)"
LIMIT = 100
MAX_OFFSET = 40000

data = {}
if os.path.exists(OUT):
    data = {int(k): v for k, v in json.load(open(OUT)).items()}
print(f"resuming with {len(data)} books", flush=True)

def fetch(offset):
    url = (f"https://librivox.org/api/feed/audiobooks/"
           f"?format=json&limit={LIMIT}&offset={offset}")
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            return json.loads(urllib.request.urlopen(req, timeout=45).read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {"books": []}
            time.sleep(3 + attempt * 3)
        except Exception:
            time.sleep(3 + attempt * 3)
    return None

t0 = time.time()
misses, consec_empty = [], 0
for offset in range(0, MAX_OFFSET, LIMIT):
    d = fetch(offset)
    if d is None:
        misses.append(offset)
        continue
    books = d.get("books", []) or []
    if not books:
        consec_empty += 1
        if consec_empty >= 3 and offset > len(data):
            break
        continue
    consec_empty = 0
    for b in books:
        try:
            bid = int(b["id"])
        except (KeyError, ValueError, TypeError):
            continue
        auth = (b.get("authors") or [{}])[0]
        cur = data.get(bid, {})
        data[bid] = {
            "year": (str(b.get("copyright_year") or "").strip()
                     or cur.get("year")) or None,
            "genres": cur.get("genres") or [],   # only the extended feed has these
            "dob": (auth.get("dob") or "").strip() or cur.get("dob") or None,
            "dod": (auth.get("dod") or "").strip() or cur.get("dod") or None,
        }
    if (offset // LIMIT) % 10 == 0:
        json.dump(data, open(OUT+".tmp", "w")); __import__("os").replace(OUT+".tmp", OUT)
        print(f"  offset {offset}: {len(data)} books, "
              f"{sum(1 for v in data.values() if v['year'])} yr "
              f"({int(time.time()-t0)}s)", flush=True)
    time.sleep(0.6)

json.dump(data, open(OUT+".tmp", "w")); __import__("os").replace(OUT+".tmp", OUT)
print(f"DONE {len(data)} books; "
      f"{sum(1 for v in data.values() if v['year'])} year, "
      f"{sum(1 for v in data.values() if v['genres'])} genre. misses: {misses}",
      flush=True)
