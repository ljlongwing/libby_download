"""Shared per-source registry used by worker.py and auth_session.py, so
adding/adjusting a source means editing one dict here rather than
duplicating worker/auth logic per source.

Both libby_dl.LibbyDownloader and chirp_dl.ChirpDownloader now share the
same shape (_launch_browser_context / _download_selected_book /
_wait_for_login / _get_shelf / _is_logged_in), which is what makes a
single generic scan/auth implementation possible instead of one copy per
source.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import chirp_dl  # noqa: E402
import libby_dl  # noqa: E402

SOURCES = {
    "libby": {
        "label": "Libby",
        "downloader_cls": libby_dl.LibbyDownloader,
        "session_file": libby_dl.SESSION_FILE,
        "login_url": libby_dl.LIBBY_URL,
        # Libby's shelf API gives a stable numeric loan id.
        "get_loan_id": lambda book: book.get("id", ""),
        "get_card_id": lambda book: book.get("card_id", ""),
    },
    "chirp": {
        "label": "Chirp",
        "downloader_cls": chirp_dl.ChirpDownloader,
        "session_file": chirp_dl.SESSION_FILE,
        # _is_logged_in() only recognizes a URL containing "library", and
        # (unlike Libby's root, which auto-redirects to /shelf) Chirp's
        # bare root doesn't reliably land there on its own -- the CLI's
        # own _ensure_authenticated() explicitly appends /library for the
        # same reason.
        "login_url": chirp_dl.CHIRP_URL + "/library",
        # Chirp's shelf has no numeric id; the player href (e.g.
        # "/player/34151152") is the closest stable, unique identifier.
        "get_loan_id": lambda book: book.get("href", ""),
        "get_card_id": lambda book: "",
    },
}
