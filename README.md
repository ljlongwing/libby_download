# Audiobook Downloaders (Libby + Chirp)

This repo contains two related, standalone tools that are provided for **educational purposes only**. They are designed to demonstrate how web automation and network traffic analysis can be used to interact with web applications.

*   **`libby_dl.py`** — downloads your borrowed audiobooks from **Libby** (libbyapp.com).
*   **`chirp_dl.py`** — downloads your purchased audiobooks from **Chirp Books** (chirpbooks.com).
*   **`service/`** — an optional self-hosted web service that runs both tools automatically on a schedule (see below), so borrowing a book in Libby or owning one on Chirp gets it downloaded without you running anything by hand.

### 🐳 Automated download service (Docker)

`service/` packages a small always-on service around `libby_dl.py` **and** `chirp_dl.py` — one container, one dashboard, two independent scan loops (each on its own configurable interval):

- **Background scanning** — checks your Libby shelf and your Chirp library on their own schedules, downloading anything new and skipping what it's already grabbed.
- **Web dashboard** — one status/shelf/log panel per source, a manual "Scan Now" button each, and a combined download **history** (with a Source column).
- **In-browser (re-)authentication** — login has to happen on the source's own page (library card + PIN for Libby, account login for Chirp), so the container runs a real browser on a virtual display and streams it into the web UI via noVNC — you log in right there in your browser tab, no separate script or window needed, including whenever a session eventually expires. Only one source can be logging in at a time (they share the same display); scans aren't affected by this.
- **Config page** — change the shared output directory or either source's scan interval without editing files.

Run it with Docker Compose:

```bash
docker compose up --build -d
```

Then visit `http://<host>:8000`, go to **Authentication**, and log in.

**Where things persist:**
- App state (session file + history/config database) lives in a Docker-managed **named volume** (`libby-data`) by default — deliberately, not a folder next to the compose file, since a relative bind mount can silently reset to empty on redeploy under some deploy methods (notably Portainer's "Repository" stack build, which clones the repo into a stack-specific directory each time). To use a specific host folder instead (for easy backup/inspection), set `DATA_DIR` to an **absolute** path, e.g. `DATA_DIR=/srv/libby-data`.
- Downloaded books default to `./MyBooks`, which has that same relative-path fragility. **Set `BOOKS_DIR` to an absolute path** on your host (e.g. `BOOKS_DIR=/mnt/media/audiobooks`) rather than relying on the relative default.
- Both `DATA_DIR` and `BOOKS_DIR` are set as stack environment variables in Portainer (Stack → Environment variables), or in a `.env` file next to `docker-compose.yml` if running via the CLI.
- If your host already has something bound to ports 8000/6080, override them the same way with `WEB_PORT`/`VNC_PORT`.

This is newer and less battle-tested than the CLI tools above — if something's off, check `docker compose logs -f`.

### 🔒 Why isn't there a Hoopla downloader?
Hoopla's audiobooks *and* video both stream through **Widevine/PlayReady DRM** (via castLabs DRMtoday — confirmed by inspecting the DASH manifest: `ContentProtection` / `cenc:default_KID` elements and real Widevine/PlayReady license requests to `patron-api-gateway.hoopladigital.com`). That's genuine content encryption, not just an access-token quirk like the ones these tools work around for Libby/Chirp, so building a downloader for it would mean circumventing DRM — illegal under DMCA §1201 regardless of having a valid loan. This isn't going to change, so there's no need to re-investigate it.

### ⚖️ Disclaimer
*   **Valid Access Required:** These tools are intended only for users who have a **valid, active loan** (Libby) or have **purchased** the title (Chirp) they are accessing.
*   **Personal Use Only:** Any files downloaded using these tools should be for your own personal, private use. Redistribution of copyrighted material is a violation of copyright law and the Terms of Service of your library, Libby, and Chirp.
*   **No Affiliation:** This project is **not** affiliated with, endorsed by, or supported by Libby, OverDrive, Chirp Books, or any library system.
*   **User Responsibility:** By using these tools, you agree to comply with all applicable laws and the Terms of Service of the platforms you are accessing. The author assumes no liability for misuse of these tools.

---

## 🚀 Quick Start (No Python Required)

If you don't want to install Python, you can download the "standalone" version for your computer:

1.  Go to the **[Releases](https://github.com/ljlongwing/libby_download/releases)** page.
2.  Download the file(s) for your system:
    *   **Windows**: `LibbyDownloader.exe` and/or `ChirpDownloader.exe`
    *   **Linux**: `LibbyDownloader` and/or `ChirpDownloader`
3.  Double-click the file to start!

*Note: You may still need to install **FFmpeg** if you want the Libby tool to split the book into chapters (see below). The Chirp tool downloads each chapter as its own file already, so FFmpeg isn't needed for it.*

---

## 🛠️ Option 2: Run with Python (Advanced)

Before you start, you need to install a few free pieces of software on your computer:

### Python (Required)
This is the "engine" that runs the downloader script.
- **Windows**: Download the latest version from [python.org](https://www.python.org/downloads/windows/). During installation, **make sure to check the box that says "Add Python to PATH"**.
- **Linux**: Most Linux systems already have this. If not, run `sudo apt install python3 python3-pip` (on Ubuntu/Debian).

### Browser (Required)
Both tools use a web browser in the background to talk to Libby or Chirp.
- It will automatically look for **Google Chrome**, **Microsoft Edge**, or **Brave Browser** on your computer.
- If you don't have those, the tool will install its own "helper browser" during the setup step.

### FFmpeg (Optional but Recommended)
This is used if you want the tool to split the one big audiobook file into individual chapter files.
- **Windows**: Download the "essentials" version from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/).
- **Linux**: Run `sudo apt install ffmpeg`.

---

## 2. Setting Up the Downloader

Once Python is installed, you need to set up the "dependencies" (the parts that help the script talk to the internet).

1. Open your **Terminal** (on Windows, search for "PowerShell" or "Command Prompt" in the Start menu).
2. Copy and paste the following commands one at a time. 

> **Note for Linux users**: You may need to use `python3` instead of `python` in the commands below.

```bash
# 1. Update the setup tool
python -m pip install --upgrade pip

# 2. Install the tools the script needs
python -m pip install -r requirements.txt

# 3. Install the "helper browser" that the scripts use to visit Libby/Chirp
python -m playwright install chromium
```

Using `python -m` is the safest way to run these commands on both Windows and Linux, as it ensures the computer uses the exact version of Python you just installed.

---

## 3. How to Use the Downloaders

Both tools share the same setup and follow the same basic flow; the differences are called out below.

### The Easy Way (Shortcuts)
I have included "Start" scripts to make it easier to run each tool:

- **Libby — Windows**: Double-click `start_windows.bat`.
- **Libby — Linux**: Run `start_linux.sh`.
- **Chirp — Windows**: Double-click `start_chirp.bat`.
- **Chirp — Linux**: Run `start_chirp.sh`.

These scripts automatically start the downloader and save your books into a folder named `MyBooks`. Each book gets its own subfolder inside `MyBooks` (named after the book title), so it's safe to use the same `MyBooks` folder for both tools. They also keep the window open at the end so you can read any messages.

### The Manual Way (Terminal)
If you prefer to run it yourself or want to use special options:

```bash
python libby_dl.py --out "./MyBooks"
python chirp_dl.py --out "./MyBooks"
```
*(Remember to use `python3` instead of `python` if you are on Linux!)*

- A browser window will pop up and take you to **libbyapp.com** or **chirpbooks.com**.
- Log in just like you usually do (library card + PIN for Libby; your account for Chirp).
- Once you see your shelf/library, go back to your terminal window and **press the Enter key**.
- **Good news!** The tool will now remember your login. You won't have to do this again.

### Step 2: Picking a book
Every time you run a script after that, it will:
1. Show you a list of the audiobooks in your Libby shelf or Chirp library.
2. Ask you to type a number (like `1` or `2`) to pick the book you want.
3. Start the download process!

### Step 3: Let it work
- **Libby**: the script "scans" through the book in the background, "fast-forwarding" to trigger every audio request, then downloads and stitches the resulting files together with a `.cue` file for chapters.
- **Chirp**: the script reads the chapter list straight from the book's page, then downloads each chapter individually (Chirp already delivers audiobooks as one file per chapter, so there's no splitting step). You'll see a line printed for each chapter as it downloads.

---

## 4. Troubleshooting & Tips

- **Headless Mode**: If you don't want to see the browser window pop up every time, add `--headless` to the command:
  `python libby_dl.py --out "./MyBooks" --headless`
  `python chirp_dl.py --out "./MyBooks" --headless`
- **Missing Chapters (Libby)**: If you find that the chapters aren't being split, make sure you have **FFmpeg** installed (see Step 1). This doesn't apply to Chirp — its chapters are already separate files.
- **Session Files**: If you ever want a tool to "forget" your login, delete `.libby_session.json` or `.chirp_session.json` from your user folder.

---

## Simplified "How It Works"

### Libby
1. **Browsing**: The tool looks at your Libby shelf and shows you what you've borrowed.
2. **Scanning**: It opens the book player and "fast-forwards" through it very quickly. This tricks the library system into sending the audio files to the browser.
3. **Saving**: As those audio files arrive, the tool grabs them and saves them to your computer.
4. **Organizing**: It looks at the book's data to find the narrator, the author, and the cover art, then it attaches all that info to your new MP3 files so they look great in your music player, plus writes a `.cue` file marking each chapter.

### Chirp
1. **Browsing**: The tool looks at your Chirp library and shows you what you own.
2. **Reading the chapter list**: It opens the book's player page and reads the exact chapter list (names, lengths) directly from the page — no guessing or fast-forwarding needed.
3. **Downloading**: For each chapter, it selects it in the player and starts playback just long enough to capture that chapter's complete audio file.
4. **Organizing**: It tags each chapter file with the title, author, narrator, and cover art, and writes both a `.cue` file and a plain-text chapter list.
