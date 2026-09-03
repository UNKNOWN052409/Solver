#!/usr/bin/env bash
# xvfb-start — rootless Xvfb virtual display :99 (proot, no root needed)
# Managed by hermes-watchdog? Nahi — manual start/stop. Browser-headed tests
# ke liye: DISPLAY=:99 use karo.
#
# Usage:
#   bash ~/Solver/tools/xvfb-start.sh start   # launch :99 (idempotent)
#   bash ~/Solver/tools/xvfb-start.sh status
#   bash ~/Solver/tools/xvfb-start.sh stop
set -u
RX=/home/kali/Tools/rootless/rootx
PIDF=/tmp/opencode/xvfb99.pid
LOG=/tmp/opencode/xvfb99.log
mkdir -p /tmp/opencode

case "${1:-status}" in
  start)
    if DISPLAY=:99 xdpyinfo >/dev/null 2>&1; then
        echo "[xvfb] :99 already up"
        exit 0
    fi
    if [ ! -x "$RX/usr/bin/Xvfb" ]; then
        echo "[xvfb] missing — install: apt-get download xvfb xserver-common; dpkg -x ... rootx/ (README)"
        exit 1
    fi
    LD_LIBRARY_PATH="$RX/usr/lib/aarch64-linux-gnu:$RX/usr/lib:$LD_LIBRARY_PATH" \
        "$RX/usr/bin/Xvfb" :99 -screen 0 1280x800x24 -nolisten tcp >> "$LOG" 2>&1 &
    echo $! > "$PIDF"
    sleep 1.5
    if DISPLAY=:99 xdpyinfo >/dev/null 2>&1; then
        echo "[xvfb] :99 UP (pid $(cat "$PIDF"))"
    else
        echo "[xvfb] start FAIL — check $LOG"
    fi
    ;;
  stop)
    [ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2>/dev/null
    rm -f "$PIDF" /tmp/.X11-unix/X99
    echo "[xvfb] stopped"
    ;;
  status)
    if DISPLAY=:99 xdpyinfo >/dev/null 2>&1; then
        echo "[xvfb] :99 UP"
    else
        echo "[xvfb] :99 down"
    fi
    ;;
esac
