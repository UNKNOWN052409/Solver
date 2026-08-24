"""Pool clearance scorer.

Reads a proxy list (one per line: host:port:user:pass etc.), runs the
Cloudflare probe through every exit concurrently, writes a ranked CSV.

    python3 pool_score.py targets.txt proxies.txt -o scoreboard.csv
"""

import argparse
import csv
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from cf_probe import probe_browser, probe_requests


def score_one(proxy: str, url: str, window: int):
    row = {"proxy": proxy, "requests_outcome": "", "browser_outcome": "",
           "clearance_cookie": False, "elapsed_s": "", "error": ""}
    try:
        r = probe_requests(url, proxy=proxy)
        row["requests_outcome"] = r["outcome"]
    except Exception as ex:
        row["requests_outcome"] = f"error:{str(ex)[:60]}"

    # Only bother with the browser tier if raw HTTP wasn't flat-out blocked.
    if row["requests_outcome"] == "blocked":
        return row
    try:
        b = probe_browser(url, proxy=proxy, window=window)
        row["browser_outcome"] = b["outcome"]
        row["clearance_cookie"] = b["cf_clearance"]
        row["elapsed_s"] = b["elapsed_s"]
    except Exception as ex:
        row["browser_outcome"] = f"error:{str(ex)[:60]}"
        row["error"] = str(ex)[:120]
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("targets", help="file with target URLs (one per line)")
    ap.add_argument("proxies", help="file with proxy list, one per line")
    ap.add_argument("-o", "--out", default="scoreboard.csv")
    ap.add_argument("-w", "--workers", type=int, default=5)
    ap.add_argument("--window", type=int, default=45)
    args = ap.parse_args()

    urls = [u.strip() for u in Path(args.targets).read_text().splitlines() if u.strip()]
    proxies = [p.strip() for p in Path(args.proxies).read_text().splitlines() if p.strip()]
    if len(urls) != 1:
        print("[!] exactly one target URL per run for now", file=sys.stderr)
        sys.exit(2)

    rows, done = [], 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(score_one, px, urls[0], args.window): px for px in proxies}
        for fut in as_completed(futures):
            row = fut.result()
            rows.append(row)
            done += 1
            print(f"[{done}/{len(proxies)}] {row['proxy'][:34]:34s} "
                  f"http={row['requests_outcome'] or '-':10s} "
                  f"browser={row['browser_outcome'] or '-':10s} "
                  f"clearance={row['clearance_cookie']}")

    rows.sort(key=lambda r: (
        0 if r["browser_outcome"] == "cleared" else 1,
        0 if r["clearance_cookie"] else 1,
        float(r["elapsed_s"]) if r["elapsed_s"] else 999,
    ))
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    cleared = sum(1 for r in rows if r["browser_outcome"] == "cleared")
    print(f"\n[*] {cleared}/{len(rows)} exits cleared the challenge | scoreboard -> {args.out}")


if __name__ == "__main__":
    main()
