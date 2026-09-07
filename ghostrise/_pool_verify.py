"""Real end-to-end verification of the same-IP pool + Tor-grade rotation.

Launches N REAL GhostWire chromium contexts via PoolManager, then for each:
  * confirms egress == system IP (real HTTP fetch to api.ipify.org from
    inside the page, compared to host system IP)
  * reads the js fingerprint (UA + canvas hash + WebGL vendor) BEFORE rotation
  * rotates identity (new UA + canvas/WebGL noise + cookie clear)
  * re-reads the fingerprint AFTER rotation
  * proves UA changed AND canvas/WebGL noise was injected
  * re-checks egress AFTER rotation (must still == system IP)
No mocks — every number is a real browser run against api.ipify.org.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ghostrise.pool import PoolManager  # noqa: E402
from ghostrise.identity import (  # noqa: E402
    inspect_fingerprint, rotate_identity, verify_rotation,
)

N = int(os.environ.get("POOL_N", "2"))
SYSTEM_IP = os.environ.get("EXPECT_SYS_IP")  # optional pin; else read host


def main():
    sys_ip = None
    host_start = time.time()
    print(f"== launching same-IP pool of {N} real contexts ==", flush=True)
    with PoolManager(size=N, headless=True, verify_egress=True,
                     rotate_on_acquire=False) as pool:
        sys_ip = pool.sys_ip
        report = {
            "system_ip": sys_ip,
            "pool_size": N,
            "contexts": [],
        }
        n_egress = 0
        n_rotated = 0
        for idx in range(N):
            ctx = pool.acquire(timeout=60)
            # warm page -> fingerprint/egress need a live document
            ctx.open("about:blank", timeout=25000)
            time.sleep(0.5)
            before = inspect_fingerprint(ctx.wire)
            e1 = ctx.egress()          # (browser_ip, system_ip, ok)
            before["egress"] = e1[0]
            before["egress_ok"] = e1[2]

            # rotate
            rot = rotate_identity(ctx.wire)
            # navigate so the new-document noise script actually runs
            try:
                ctx.wire.goto("about:blank", timeout=25000)
            except Exception as e:  # noqa: BLE001
                print(f"[verify] ctx {ctx.id} post-rotate nav: {e}")
            time.sleep(0.5)
            after = inspect_fingerprint(ctx.wire)
            e2 = ctx.egress()
            after["egress"] = e2[0]
            after["egress_ok"] = e2[2]

            v = verify_rotation(before, after)
            matched = bool(e1[2]) and bool(e2[2])
            if matched:
                n_egress += 1
            if v["ua_changed"] and v["noise_injected"]:
                n_rotated += 1

            report["contexts"].append({
                "id": ctx.id,
                "egress_before": e1[0], "egress_after": e2[0],
                "egress_match": matched,
                "before": before,
                "after": after,
                "rotation": v,
                "ua": rot["ua"],
                "cookies_cleared": rot["cookies_cleared"],
            })
            emoji = "ok" if matched else "MISMATCH"
            print(f"  [ctx {ctx.id}] egress={emoji} "
                  f"({e1[0]}) ua_changed={v['ua_changed']} "
                  f"noise={v['noise_injected']}", flush=True)
            pool.release(ctx)

        report["contexts_egress_match"] = n_egress
        report["contexts_rotated_ua"] = n_rotated
        report["real_launches"] = pool.launched
        report["duration_s"] = round(time.time() - host_start, 2)
        report["pool_stats"] = pool.stats()

        out = "/home/kali/NeoSolver/ghostrise/_pool_verify_report.json"
        with open(out, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print("\n== REPORT ==", flush=True)
        print(json.dumps({
            "system_ip": sys_ip,
            "contexts_egress_match": n_egress,
            "contexts_rotated_ua": n_rotated,
            "real_launches": pool.launched,
            "pool_stats": pool.stats(),
        }, indent=2, default=str), flush=True)


if __name__ == "__main__":
    main()
