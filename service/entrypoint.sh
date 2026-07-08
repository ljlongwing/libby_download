#!/bin/bash
set -e

export DISPLAY=:99

# Virtual X display for the (non-headless) auth-flow browser to render into.
Xvfb :99 -screen 0 1280x800x24 &
sleep 1

# VNC server attached to that display. No password: this container is meant
# to sit behind your own network/reverse-proxy access control, not to be
# exposed directly to the internet.
x11vnc -display :99 -forever -shared -nopw -quiet &

# Bridges VNC to a websocket and serves noVNC's web client on the same port,
# so the web UI can embed it as a plain iframe.
websockify --web=/usr/share/novnc/ 6080 localhost:5900 &

# Foreground process: the web app, which also owns the background scan loop.
cd /app/service
exec uvicorn web:app --host 0.0.0.0 --port 8000
