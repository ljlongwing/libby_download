# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

LibbyDownload is a Java CLI tool that processes HAR (HTTP Archive) files to download audiobooks and other media from Libby (digital library) and Hoopla services, applying ID3 metadata tags and optionally splitting into chapters via ffmpeg.

## Build & Run

```bash
# Build
mvn clean package

# Run (audiobooks from Libby)
java -jar LibbyDownload.jar -book "Book Name" -har "file.har" -out "output_folder" [options]

# Run (video from Hoopla)
java -jar VideoDownload.jar -book "Book Name" -har "file.har" -out "output_folder" [options]
```

**CLI flags:**
- `-book` — name used for output files
- `-har` — HAR file to process
- `-out` — output directory
- `-ffmpeg` — path to ffmpeg binary (required for chapter processing)
- `-cue` — CUE file to process for chapters (alternative to HAR)
- `-d` — debug output
- `-bc` — bypass cache (skip cached requests that may fail to download)
- `-c` — process chapters
- `-df` — delete original part files after chapter creation

No test suite is present in this codebase.

## Python Automation Tool (`libby_dl.py`)

A Playwright-based replacement for the manual HAR workflow.

```bash
# One-time setup
pip install -r requirements.txt
playwright install chromium

# Run (first run opens a real browser window for login)
python libby_dl.py --out /path/to/output

# Subsequent runs reuse the saved session in ~/.libby_session.json
python libby_dl.py --out /path/to/output --headless
```

**First-run auth flow:** the browser window opens at libbyapp.com. Add your library card (card number/email + PIN) manually in the UI, wait for your shelf to load, then press Enter in the terminal. The session is saved to `~/.libby_session.json` so subsequent runs are fully automated.

**What the script does:**
1. Lists borrowed audiobooks from the shelf; you pick one in the terminal.
2. Opens the book player and extracts BIFOCAL metadata (title, author, narrator, TOC).
3. Seeks through the book part by part (using the reading order durations from BIFOCAL to calculate seek positions) to trigger every audio-part HTTP request.
4. Re-executes each captured request (URL + original auth headers) to download the MP3 files.
5. Applies ID3 tags and writes a `.cue` file.

The `.cue` file output is compatible with the Java tool's `-cue` mode for chapter splitting via ffmpeg.

## Architecture

The application is a sequential processing pipeline:

1. **HAR parsing** — reads JSON, extracts HTTP entries with 302 redirect status codes
2. **Metadata extraction** — recursively traverses nested JSON/HTML to find book title, author, narrator, etc.; extracts BIFOCAL player data from `<script>` tags in HTML responses
3. **Table of contents extraction** — parses BIFOCAL JSON to build chapter structure
4. **Download** — makes HTTP requests using headers from the HAR, downloads only `audio/mpeg` responses, writes MP3 files
5. **ID3 tagging** — applies metadata and cover art using jaudiotagger
6. **Chapter processing (optional)** — builds ffmpeg commands to concatenate/trim audio files per chapter, generates CUE files

**Entry points:**
- `LibbyDownload.java` (1033 lines) — full pipeline for Libby audiobooks
- `VideoDownload.java` (478 lines) — simplified variant for Hoopla video, no chapter handling

**Data models:** `Request`, `Chapter`, `Cue`, `FileLocation` — simple POJOs used to pass state through the pipeline.

**State management:** uses static fields throughout; this is an intentional design for a single-run CLI tool.

## Key Dependencies

- **jsoup 1.18.3** — parses HTML to extract BIFOCAL JSON from `<script>` tags
- **jaudiotagger 3.0.1** — reads/writes ID3 tags on MP3 files
- **org.json 20240303** — parses HAR JSON structure
- **Java 17**, Maven build
