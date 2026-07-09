"""FastAPI app for the multi-source (Libby + Chirp) auto-download service.

Serves the dashboard/auth/history/config pages and starts one background
scan loop per source on startup.
"""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth_session  # noqa: E402
import db  # noqa: E402
import worker  # noqa: E402
from sources import SOURCES  # noqa: E402

logging.basicConfig(level=logging.INFO)

# websockify's port: it serves both the noVNC static web client and the
# VNC-over-websocket proxy on the same port (see entrypoint.sh). Shared by
# both sources' auth pages since only one login flow can be active at a
# time (see auth_session.py's mutex).
VNC_PORT = int(os.environ.get("VNC_PORT", "6080"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    tasks = [asyncio.create_task(worker.loop_forever(s)) for s in SOURCES]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Audiobook Auto-Download Service", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _source_or_404(source: str) -> str:
    if source not in SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source!r}")
    return source


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    panels = []
    for key, cfg in SOURCES.items():
        panels.append({
            "key": key,
            "label": cfg["label"],
            "authenticated": cfg["session_file"].exists(),
            "last_scan": worker.last_scan_result[key],
            "last_scan_at": worker.last_scan_at[key],
            "next_scan_at": worker.next_scan_at[key],
            "scan_running": worker._scan_running[key],
            "scan_log": "\n".join(worker.scan_log[key]),
            "shelf": db.list_shelf(key),
        })
    return templates.TemplateResponse(request, "dashboard.html", {"sources": panels})


@app.post("/scan/{source}")
async def scan_now(source: str):
    _source_or_404(source)
    asyncio.create_task(worker.scan_once(source))
    return RedirectResponse("/", status_code=303)


@app.get("/scan/{source}/log")
async def scan_log(source: str):
    _source_or_404(source)
    return {
        "running": worker._scan_running[source],
        "last_result": worker.last_scan_result[source],
        "log": "\n".join(worker.scan_log[source]),
        "next_scan_at": worker.next_scan_at[source],
    }


@app.post("/rebook/{source}/{loan_id}")
async def rebook(source: str, loan_id: str):
    _source_or_404(source)
    db.mark_for_redownload(source, loan_id)
    asyncio.create_task(worker.scan_once(source))
    return RedirectResponse("/", status_code=303)


@app.get("/auth/{source}", response_class=HTMLResponse)
async def auth_page(request: Request, source: str):
    _source_or_404(source)
    return templates.TemplateResponse(
        request,
        "auth.html",
        {
            "source": source,
            "label": SOURCES[source]["label"],
            "vnc_port": VNC_PORT,
            "status": auth_session.get_status(source),
        },
    )


@app.post("/auth/{source}/start")
async def auth_start(source: str):
    _source_or_404(source)
    return await auth_session.start_login(source)


@app.get("/auth/{source}/status")
async def auth_status(source: str):
    _source_or_404(source)
    return auth_session.get_status(source)


@app.get("/history", response_class=HTMLResponse)
async def history(request: Request):
    books = db.list_books()
    for b in books:
        b["source_label"] = SOURCES.get(b["source"], {}).get("label", b["source"])
    return templates.TemplateResponse(request, "history.html", {"books": books})


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    config = db.get_all_config()
    return templates.TemplateResponse(
        request, "config.html", {"config": config, "sources": SOURCES}
    )


@app.post("/config")
async def config_save(
    output_dir: str = Form(...),
    libby_scan_interval_minutes: str = Form(...),
    chirp_scan_interval_minutes: str = Form(...),
):
    db.set_config("output_dir", output_dir)
    db.set_config("libby_scan_interval_minutes", libby_scan_interval_minutes)
    db.set_config("chirp_scan_interval_minutes", chirp_scan_interval_minutes)
    return RedirectResponse("/config", status_code=303)
