#!/bin/bash
set -e

export DISPLAY=:99

# A container restart (rather than a fresh create) can leave a stale lock
# file behind from Xvfb's previous, uncleanly-terminated run, which would
# make it refuse to bind :99 again.
rm -f /tmp/.X99-lock

# Virtual X display for the (non-headless) auth-flow browser to render into.
Xvfb :99 -screen 0 1280x800x24 &

# A fixed sleep here isn't enough -- if the scan loop launches a browser
# before Xvfb has actually finished binding the display (more likely right
# after a host reboot under resource contention), Chromium fails outright
# with "Missing X server or $DISPLAY" instead of retrying. Wait for the X11
# socket to actually exist before starting the app that depends on it.
for i in $(seq 1 50); do
    [ -e /tmp/.X11-unix/X99 ] && break
    sleep 0.2
done
if [ ! -e /tmp/.X11-unix/X99 ]; then
    echo "Xvfb did not come up on :99 after 10s" >&2
    exit 1
fi

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
