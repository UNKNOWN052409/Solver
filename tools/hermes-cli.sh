#!/usr/bin/env bash
# hermes-cli — Hermes session survival + infra control (one-shot shortcut).
# Usage:
#   hermes-cli start      # healer cron + keepalive + xvfb + watchdog, sab up
#   hermes-cli status     # ek nazar me sab kya chal raha
#   hermes-cli heal       # ek ek karke healer logic chalao (no cron needed)
#   hermes-cli watch      # background watch loop (daemon) — auto-heal every 60s
#   hermes-cli stop       # cron healer hatao (manual mode)
#   hermes-cli fix        # deep: stale locks, xvfb, zombie chrome sweep
#
# Auto-start: `hermes-cli watch` as a daemon — cron @reboot + every 2-min
# healer already wired. Ye CLI sirf convenience/full-control.

set -u
DIR=/home/kali/NeoSolver/tools
H=$DIR/hermes-healer.sh
LOG=/tmp/opencode/hermes-cli.log
mkdir -p "$(dirname "$LOG")"
ts() { echo "[$(date '+%m-%d %H:%M:%S')] $*"; }

cmd="${1:-status}"

case "$cmd" in
  start)
    # healer cron ensure
    if ! crontab -l 2>/dev/null | grep -q hermes-healer; then
      (crontab -l 2>/dev/null; echo "*/2 * * * * bash $H >> /tmp/opencode/hermes-healer.log 2>&1") | crontab -
      ts "healer cron added (*/2)" >> "$LOG"
    fi
    bash "$H" >> "$LOG" 2>&1
    # keepalive + xvfb ensure
    bash "$DIR/keepalive.sh" start >> "$LOG" 2>&1
    bash "$DIR/xvfb-start.sh" start >> "$LOG" 2>&1
    ts "hermes-cli start done" >> "$LOG"
    echo "started — healer cron */2, keepalive, xvfb all ensured"
    ;;
  status)
    echo "hermes-procs : $(pgrep -f hermes | wc -l)"
    echo "healer-cron  : $(crontab -l 2>/dev/null | grep -c hermes-healer)"
    echo "keepalive    : $(bash "$DIR/keepalive.sh" status 2>&1 | head -1)"
    echo "xvfb         : $(bash "$DIR/xvfb-start.sh" status 2>&1 | head -1)"
    echo "watchdog-log : $(tail -1 /tmp/opencode/watchdog.log 2>/dev/null)"
    echo "ccapwatch    : $(/home/kali/Rev/venv/bin/python "$DIR/capwatch.py" 2>&1 | head -1)"
    ;;
  heal)
    bash "$H" 2>&1 | head -2
    ;;
  watch)
    ts "hermes-cli WATCH daemon start (auto-heal 60s)" >> "$LOG"
    nohup bash -c "while :; do bash \"$H\" >> \"$LOG\" 2>&1; sleep 60; done" >/dev/null 2>&1 &
    ts "  watch pid $! loop 60s" >> "$LOG"
    echo "watch daemon started (60s auto-heal loop)"
    ;;
  stop)
    crontab -l 2>/dev/null | grep -v hermes-healer | crontab -
    pkill -f "hermes-cli.*watch" 2>/dev/null
    ts "healer cron removed, watch stopped" >> "$LOG"
    echo "stopped — manual mode"
    ;;
  fix)
    # deep cleanup — stale locks, xvfb, zombie chrome
    rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null
    bash "$DIR/xvfb-start.sh" start >/dev/null 2>&1
    zb=$(pgrep -f "user-data-dir=/tmp/gw" | wc -l)
    if [ "$zb" -gt 12 ]; then
      for p in $(pgrep -f "user-data-dir=/tmp/gw"); do kill -9 "$p" 2>/dev/null; done
      echo "swept $zb zombie chrome"
    else
      echo "zombie chrome: $zb (ok)"
    fi
    bash "$H" >> "$LOG" 2>&1
    echo "fix done"
    ;;
  *)
    echo "Usage: hermes-cli {start|status|heal|watch|stop|fix}"
    ;;
esac
