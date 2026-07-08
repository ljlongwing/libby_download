"""FastAPI app for the Libby auto-download service.

Serves the dashboard/auth/history/config pages and starts the background
scan loop on startup.
"""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import libby_dl  # noqa: E402

import auth_session  # noqa: E402
import db  # noqa: E402
import worker  # noqa: E402

logging.basicConfig(level=logging.INFO)

# websockify's port: it serves both the noVNC static web client and the
# VNC-over-websocket proxy on the same port (see entrypoint.sh).
VNC_PORT = int(os.environ.get("VNC_PORT", "6080"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    task = asyncio.create_task(worker.loop_forever())
    yield
    task.cancel()


app = FastAPI(title="Libby Auto-Download Service", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "authenticated": libby_dl.SESSION_FILE.exists(),
            "last_scan": worker.last_scan_result,
            "last_scan_at": worker.last_scan_at,
            "scan_running": worker._scan_running,
            "scan_log": "\n".join(worker.scan_log),
            "shelf": db.list_shelf(),
        },
    )


@app.post("/scan")
async def scan_now():
    asyncio.create_task(worker.scan_once())
    return RedirectResponse("/", status_code=303)


@app.post("/rebook/{loan_id}")
async def rebook(loan_id: str):
    db.mark_for_redownload(loan_id)
    asyncio.create_task(worker.scan_once())
    return RedirectResponse("/", status_code=303)


@app.get("/scan/log")
async def scan_log():
    return {
        "running": worker._scan_running,
        "last_result": worker.last_scan_result,
        "log": "\n".join(worker.scan_log),
    }


@app.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request):
    return templates.TemplateResponse(
        request, "auth.html", {"vnc_port": VNC_PORT, "status": auth_session.get_status()}
    )


@app.post("/auth/start")
async def auth_start():
    return await auth_session.start_login()


@app.get("/auth/status")
async def auth_status():
    return auth_session.get_status()


@app.get("/history", response_class=HTMLResponse)
async def history(request: Request):
    return templates.TemplateResponse(request, "history.html", {"books": db.list_books()})


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    return templates.TemplateResponse(request, "config.html", {"config": db.get_all_config()})


@app.post("/config")
async def config_save(
    output_dir: str = Form(...),
    scan_interval_minutes: str = Form(...),
):
    db.set_config("output_dir", output_dir)
    db.set_config("scan_interval_minutes", scan_interval_minutes)
    return RedirectResponse("/config", status_code=303)
