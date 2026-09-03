#!/usr/bin/env bash
# ============================================================
#  Solver — EK-SHOT SHORTCUT (adaptive: GPU ho to GPU, warna CPU)
# ============================================================
#  Usage:
#    ./run.sh                  # full pipeline: env-check → serve (:8030)
#    ./run.sh harvest 100      # tiles collect karo (100 grids)
#    ./run.sh train 20         # harvested data se train (GPU-adaptive)
#    ./run.sh all              # harvest 30 → train → serve
#    ./run.sh device           # sirf device/backend report
#    ./run.sh test             # serve live classify sanity
#
#  GPU base: CUDA/MPS dikhe to model wahan train/serve hota hai
#  (bada batch + AMP). CPU pe chota batch. No config chahiye.
# ============================================================
set -u
cd "$(dirname "$0")"

PY="${SOLVER_PY:-}"
if [ -z "$PY" ]; then
    # adaptive: torch wala python dhoondo
    for c in ./venv/bin/python /home/kali/Rev/venv/bin/python python3; do
        if "$c" -c "import torch" 2>/dev/null; then PY="$c"; break; fi
    done
fi
PY="${PY:-python3}"

export MOZ_DISABLE_CONTENT_SANDBOX=1

cmd="${1:-serve}"

case "$cmd" in
  device)
    exec "$PY" -c "from solver.vision.device import device_report; print(device_report())"
    ;;
  harvest)
    MAX="${2:-30}"
    echo "== harvest: $MAX grids =="
    exec "$PY" -m solver.vision.harvest --demo recaptcha-v2 --out data/tiles --max "$MAX"
    ;;
  train)
    EP="${2:-20}"
    echo "== train: $EP epochs (GPU-adaptive) =="
    exec "$PY" -m solver.vision.train --data data/tiles --epochs "$EP"
    ;;
  serve)
    PORT="${2:-8030}"
    echo "== serve :$PORT =="
    exec "$PY" -m solver.vision.serve --port "$PORT"
    ;;
  test)
    echo "== live classify test =="
    "$PY" - << 'EOF'
import base64, glob, json, time, urllib.request
tiles = sorted(glob.glob("data/tiles/grid_*/tile_00.png"))[:3]
if not tiles:
    print("[!] pehle ./run.sh harvest chalao"); raise SystemExit(1)
payload = {"tiles": [base64.b64encode(open(t, "rb").read()).decode() for t in tiles]}
req = urllib.request.Request("http://127.0.0.1:8030/classify",
    data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
t0 = time.time()
r = json.load(urllib.request.urlopen(req, timeout=30))
print(f"OK {(time.time()-t0)*1000:.0f}ms | labels: {[l for l in r['labels']]}")
EOF
    ;;
  all)
    echo "== FULL PIPELINE =="
    "$PY" -c "from solver.vision.device import device_report; print('[device]', device_report())"
    "$PY" -m solver.vision.harvest --demo recaptcha-v2 --out data/tiles --max "${2:-30}" || true
    "$PY" -m solver.vision.train --data data/tiles --epochs "${3:-15}" || true
    exec "$PY" -m solver.vision.serve --port 8030
    ;;
  *)
    echo "Solver shortcut — usage:"
    echo "  ./run.sh device        # device/backend report"
    echo "  ./run.sh harvest [N]   # tiles collect (default 30 grids)"
    echo "  ./run.sh train [E]     # train (default 20 epochs)"
    echo "  ./run.sh serve [PORT]  # API server (default 8030)"
    echo "  ./run.sh test          # live classify sanity"
    echo "  ./run.sh all [N] [E]   # harvest → train → serve"
    ;;
esac
