# Fabuly / LibriVox metadata bake

One-off scripts that produce the year/genre data `fabuly_dl.py` ships with.
Re-run only if the catalogues have drifted enough to matter.

| file | what it does | output (in scratch dir) |
|---|---|---|
| `harvest_librivox.py` | walks the LibriVox API (`?format=json`), collects `copyright_year` + author birth/death for every book. Resumable, atomic writes, skips failing pages. The `extended` feed also has genres but throttles hard — run it separately if you want genres refreshed. | `librivox_meta.json` |
| `harvest_fabuly.py` | for each Fabuly storefront book (`books_metadata_snapshot.json`), asks Open Library for `first_publish_year` + subjects. | `fabuly_meta.json` (raw) |
| `harvest_lv_genres.py` | second pass over the `extended` feed to fill in `genres` (and any still-missing year). Slow (long sleeps to dodge the throttle), resumes from a `.genreprog` offset marker. | updates `librivox_meta.json` |
| `harvest_lv_year_ol.py` | for LibriVox books whose `copyright_year` is `0`/empty, tries Open Library `first_publish_year` (pre-1930). | `lv_year_ol.json` |
| `bake.py` | merges the harvests into what ships: populates `year` + `genre` columns on `../../librivox.db` (LibriVox `copyright_year`; `lv_year_ol.json` fallback; years clamped to 1000–2025), and writes `../../fabuly_meta.json` = `{slug: {year}}` using a LibriVox title/author match first (curated `copyright_year`, capped ≤1965) and an Open Library year only as a pre-1930 fallback. | `../../librivox.db`, `../../fabuly_meta.json` |

`books_metadata_snapshot.json` is a frozen copy of the storefront index
(`bookId/title/author`) so the bake is reproducible offline.

Coverage as shipped: **~88% of LibriVox books get a year, ~98% a genre**;
~63% of the ~400 storefront books get a year. LibriVox years/genres are the
catalogue's own (curated); Open-Library-sourced years are edition years and
only kept when plausibly public-domain-era. The ~12% of LibriVox books with
no year have `copyright_year = 0` upstream (anthologies, weekly-poetry
collections, non-book recordings) — genuinely no single publication year.
