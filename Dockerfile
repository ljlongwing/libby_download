# Libby auto-download service: FastAPI web UI + background scan loop,
# reusing libby_dl.py directly. Includes Xvfb + x11vnc + noVNC/websockify so
# the (re-)authentication flow can stream a real, interactive browser into
# the web UI -- see service/entrypoint.sh and service/auth_session.py.

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Fixed, HOME-independent browser install location: HOME gets overridden to
# /data below (so libby_dl.py's Path.home()-based session file lands on the
# persisted volume), but Playwright's browser lookup also depends on $HOME
# by default -- pinning this explicitly means the browser installed at build
# time is still found at runtime once HOME points somewhere else.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

COPY requirements.txt ./
COPY service/requirements.txt ./service/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r service/requirements.txt \
    && playwright install --with-deps chromium

COPY libby_dl.py ./
COPY service/ ./service/
RUN chmod +x ./service/entrypoint.sh

ENV DISPLAY=:99 \
    HOME=/data \
    LIBBY_SERVICE_DB=/data/db/service.db \
    PYTHONUNBUFFERED=1

EXPOSE 8000 6080

ENTRYPOINT ["./service/entrypoint.sh"]
