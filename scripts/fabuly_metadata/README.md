# Fabuly / LibriVox metadata bake

One-off scripts that produce the year/genre data `fabuly_dl.py` ships with.
Re-run only if the catalogues have drifted enough to matter.

| file | what it does | output (in scratch dir) |
|---|---|---|
| `harvest_librivox.py` | walks the LibriVox API (`?format=json`), collects `copyright_year` + author birth/death for every book. Resumable, atomic writes, skips failing pages. The `extended` feed also has genres but throttles hard — run it separately if you want genres refreshed. | `librivox_meta.json` |
| `harvest_fabuly.py` | for each Fabuly storefront book (`books_metadata_snapshot.json`), asks Open Library for `first_publish_year` + subjects. | `fabuly_meta.json` (raw) |
| `bake.py` | merges the two harvests into what ships: adds/populates `year` + `genre` columns on `../../librivox.db`, and writes `../../fabuly_meta.json` = `{slug: {year}}` using a LibriVox title/author match first (curated `copyright_year`, capped ≤1965) and an Open Library year only as a pre-1930 fallback. | `../../librivox.db`, `../../fabuly_meta.json` |

`books_metadata_snapshot.json` is a frozen copy of the storefront index
(`bookId/title/author`) so the bake is reproducible offline.

Coverage as shipped: ~86% of LibriVox books and ~63% of the ~400 storefront
books get a year. LibriVox years are the original text's (curated); the
Open-Library-sourced ones are edition years and only kept when plausibly
public-domain-era.
