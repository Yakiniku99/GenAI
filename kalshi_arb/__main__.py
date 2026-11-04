"""Command line entry point.

    python -m kalshi_arb serve            # live dashboard on http://localhost:8000
    python -m kalshi_arb scan -o snap.json  # one-off scan to a JSON file
    python -m kalshi_arb serve --snapshot snap.json   # replay a saved scan
"""

from __future__ import annotations

import argparse
import json
import sys

from .client import KalshiClient
from .scanner import scan


def _print_progress(done: int, total: int) -> None:
    if done == total or done % 25 == 0:
        print(f"\r  scanned {done}/{total} series", end="" if done < total else "\n", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kalshi_arb", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--category", default="Economics", help="Kalshi series category (default: Economics)")
    common.add_argument("--series", nargs="*", help="Only scan these series tickers, e.g. KXFED KXCPI")
    common.add_argument("--rate", type=float, default=12.0, help="Max API requests per second (default: 12)")
    common.add_argument("--workers", type=int, default=10)

    s = sub.add_parser("scan", parents=[common], help="Scan once and print or save the results")
    s.add_argument("-o", "--out", help="Write the snapshot JSON here instead of printing a summary")

    v = sub.add_parser("serve", parents=[common], help="Run the dashboard web server")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8000)
    v.add_argument("--interval", type=int, default=180, help="Seconds between scans (default: 180)")
    v.add_argument("--snapshot", help="Serve a saved snapshot instead of scanning live")

    a = p.parse_args(argv)

    if a.cmd == "scan":
        snap = scan(KalshiClient(rate_per_sec=a.rate), a.category, a.series, a.workers, _print_progress)
        if a.out:
            with open(a.out, "w") as f:
                json.dump(snap, f, indent=1)
            print(f"wrote {a.out}", file=sys.stderr)
        arbs = [o for o in snap["opportunities"] if o["is_arb"]]
        print(f"{snap['markets_scanned']} markets in {snap['events_scanned']} events "
              f"({snap['scan_seconds']}s); {len(arbs)} arbitrage opportunities")
        for o in arbs[:20]:
            legs = " + ".join(f"{l['side'].upper()} {l['ticker']} @{l['price']:.2f}" for l in o["legs"])
            print(f"  ${o['net_edge']:.3f}/basket x{o['max_baskets']} = ${o['net_profit']:.2f}  "
                  f"[{o['strategy']}] {legs} {' '.join(o['flags'])}")
        return 0

    from .server import serve
    serve(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
