# Libby Audiobook Downloader

This tool helps you download your borrowed audiobooks from Libby (libbyapp.com) and save them to your computer as MP3 files.

---

## 🚀 Quick Start (No Python Required)

If you don't want to install Python, you can download the "standalone" version for your computer:

1.  Go to the **[Releases](https://github.com/ljlongwing/libby_download/releases)** page.
2.  Download the file for your system:
    *   **Windows**: Download `LibbyDownloader.exe`
    *   **Linux**: Download `LibbyDownloader`
3.  Double-click the file to start!

*Note: You may still need to install **FFmpeg** if you want the tool to split the book into chapters (see below).*

---

## 🛠️ Option 2: Run with Python (Advanced)

Before you start, you need to install a few free pieces of software on your computer:

### Python (Required)
This is the "engine" that runs the downloader script.
- **Windows**: Download the latest version from [python.org](https://www.python.org/downloads/windows/). During installation, **make sure to check the box that says "Add Python to PATH"**.
- **Linux**: Most Linux systems already have this. If not, run `sudo apt install python3 python3-pip` (on Ubuntu/Debian).

### Browser (Required)
The tool uses a web browser in the background to talk to Libby.
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

# 3. Install the "helper browser" that the script uses to visit Libby
python -m playwright install chromium
```

Using `python -m` is the safest way to run these commands on both Windows and Linux, as it ensures the computer uses the exact version of Python you just installed.

---

## 3. How to Use the Downloader

### The Easy Way (Shortcuts)
I have included "Start" scripts to make it easier to run the tool:

- **Windows**: Double-click the file named `start_windows.bat`.
- **Linux**: Run the file named `start_linux.sh`.

These scripts will automatically start the downloader and save your books into a folder named `MyBooks`. They will also keep the window open at the end so you can read any messages.

### The Manual Way (Terminal)
If you prefer to run it yourself or want to use special options:

```bash
python libby_dl.py --out "./MyBooks"
```
*(Remember to use `python3` instead of `python` if you are on Linux!)*

- A browser window will pop up and take you to **libbyapp.com**.
- Log in just like you usually do: add your library card and your PIN.
- Once you see your "Shelf" with your borrowed books, go back to your terminal window and **press the Enter key**.
- **Good news!** The tool will now remember your login. You won't have to do this again.

### Step 2: Picking a book
Every time you run the script after that, it will:
1. Show you a list of the audiobooks you currently have borrowed.
2. Ask you to type a number (like `1` or `2`) to pick the book you want.
3. Start the download process!

### Step 3: Let it work
The script will "scan" through the book in the background. You might see messages about "Capturing parts." This is normal! It is simulating a listener to make sure every piece of the audio is found.

---

## 4. Troubleshooting & Tips

- **Headless Mode**: If you don't want to see the browser window pop up every time, you can add `--headless` to the command:
  `python libby_dl.py --out "./MyBooks" --headless`
- **Missing Chapters**: If you find that the chapters aren't being split, make sure you have **FFmpeg** installed (see Step 1).
- **Session File**: If you ever want the tool to "forget" your login, delete the file named `.libby_session.json` in your user folder.

---

## Simplified "How It Works"

1. **Browsing**: The tool looks at your Libby shelf and shows you what you've borrowed.
2. **Scanning**: It opens the book player and "fast-forwards" through it very quickly. This tricks the library system into sending the audio files to the browser.
3. **Saving**: As those audio files arrive, the tool grabs them and saves them to your computer.
4. **Organizing**: It looks at the book's data to find the narrator, the author, and the cover art, then it attaches all that info to your new MP3 files so they look great in your music player.
