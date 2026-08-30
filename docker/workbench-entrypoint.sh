#!/usr/bin/env bash
# Bring up the desktop the agent operates and the window a human can take over.
#
# Order matters: the display must exist before the window manager, the window
# manager before the browser (otherwise windows never map and every click lands
# on nothing), and the proxy before the browser so no request can escape the
# allowlist even during startup.
set -euo pipefail

DISPLAY_NUM="${DISPLAY:-:99}"
GEOMETRY="${CUA_GEOMETRY:-1280x800x24}"
TARGET="${CUA_TARGET:-http://target:8800}"
PROXY_PORT="${CUA_PROXY_PORT:-8888}"

log() { echo "[workbench] $*" >&2; }

wait_for_x() {
  for _ in $(seq 1 100); do
    if xdotool getdisplaygeometry >/dev/null 2>&1; then return 0; fi
    sleep 0.1
  done
  log "X server never came up on $DISPLAY_NUM"; return 1
}

log "starting Xvfb on $DISPLAY_NUM ($GEOMETRY)"
Xvfb "$DISPLAY_NUM" -screen 0 "$GEOMETRY" -nolisten tcp &
wait_for_x

# A window manager is not optional. Without one, Chromium's window is never
# mapped or focused, screenshots show an empty root, and every click misses.
log "starting openbox"
openbox --sm-disable &
sleep 0.5

# Publish the SAME display for human takeover. -shared so the operator can
# attach while the agent is working; -forever so the session survives them
# disconnecting and reconnecting mid-run.
log "starting x11vnc + noVNC"
x11vnc -display "$DISPLAY_NUM" -nopw -shared -forever -quiet -rfbport 5900 &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

# The allowlist, enforced where the agent cannot argue with it. Started before
# the browser so there is no window in which an unfiltered request could go out.
log "starting policy proxy on :$PROXY_PORT"
python -m cua.safety.proxy --port "$PROXY_PORT" --audit /app/evidence/proxy.jsonl &
for _ in $(seq 1 50); do
  curl -s -o /dev/null "http://127.0.0.1:${PROXY_PORT}" && break
  sleep 0.1
done

log "starting chromium against $TARGET"
chromium \
  --no-sandbox \
  --disable-gpu \
  --disable-dev-shm-usage \
  --no-first-run \
  --no-default-browser-check \
  --disable-features=TranslateUI \
  --disable-infobars \
  --window-position=0,0 \
  --window-size=1280,800 \
  --user-data-dir=/tmp/chrome-profile \
  --proxy-server="http://127.0.0.1:${PROXY_PORT}" \
  "${TARGET}/login" &

sleep 3
log "ready -- noVNC on :6080, display $DISPLAY_NUM"

exec "$@"
