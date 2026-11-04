"""Command line entry point.

    python -m kalshi_arb demo             # instant demo from bundled sample data
    python -m kalshi_arb serve            # live dashboard on http://localhost:8000
    python -m kalshi_arb scan -o snap.json  # one-off scan to a JSON file
    python -m kalshi_arb serve --snapshot snap.json   # replay a saved scan
"""

from __future__ import annotations

import argparse
import json
import sys

from .arbitrage import DEFAULT_FEE_MODEL, FEE_MODELS
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
    common.add_argument("--fees", choices=sorted(FEE_MODELS), default=DEFAULT_FEE_MODEL,
                        help="Fee schedule to net out: robinhood (default, no Gold), robinhood-gold, or kalshi (direct)")
    common.add_argument("--depth", type=int, default=40, metavar="N",
                        help="Read full order books for the N best candidates (default: 40, 0 to skip)")

    s = sub.add_parser("scan", parents=[common], help="Scan once and print or save the results")
    s.add_argument("-o", "--out", help="Write the snapshot JSON here instead of printing a summary")

    v = sub.add_parser("serve", parents=[common], help="Run the dashboard web server")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8000)
    v.add_argument("--interval", type=int, default=180, help="Seconds between scans (default: 180)")
    v.add_argument("--snapshot", help="Serve a saved snapshot instead of scanning live")
    v.add_argument("--open", action="store_true", help="Open the dashboard in your browser")

    dm = sub.add_parser("demo", help="Open the dashboard on bundled sample data (no network, starts instantly)")
    dm.add_argument("--host", default="127.0.0.1")
    dm.add_argument("--port", type=int, default=8000)
    dm.add_argument("--no-open", dest="open", action="store_false", help="Don't open a browser")

    b = sub.add_parser("build", parents=[common], help="Scan once and write a static site (for GitHub Pages etc.)")
    b.add_argument("-o", "--out", default="_site", help="Output folder (default: _site)")

    a = p.parse_args(argv)

    if a.cmd == "scan":
        snap = scan(KalshiClient(rate_per_sec=a.rate), a.category, a.series, a.workers, _print_progress, a.fees, a.depth)
        if a.out:
            with open(a.out, "w") as f:
                json.dump(snap, f, indent=1)
            print(f"wrote {a.out}", file=sys.stderr)
        arbs = [o for o in snap["opportunities"] if o["is_arb"]]
        print(f"{snap['markets_scanned']} markets in {snap['events_scanned']} events "
              f"({snap['scan_seconds']}s); {len(arbs)} arbitrage opportunities "
              f"after {snap['fee_model']['label']} fees")
        for o in arbs[:20]:
            legs = " + ".join(f"{l['side'].upper()} {l['ticker']} @{l['price']:.2f}" for l in o["legs"])
            print(f"  ${o['net_edge']:.3f}/basket x{o['max_baskets']} = ${o['net_profit']:.2f}  "
                  f"[{o['strategy']}] {legs} {' '.join(o['flags'])}")
        return 0

    if a.cmd == "build":
        from .server import build_static_site
        snap = scan(KalshiClient(rate_per_sec=a.rate), a.category, a.series, a.workers, _print_progress, a.fees, a.depth)
        build_static_site(snap, a.out)
        print(f"wrote static dashboard to {a.out}/ ({snap['markets_scanned']} markets)", file=sys.stderr)
        return 0

    from .server import DEMO_SNAPSHOT, serve
    if a.cmd == "demo":
        a.snapshot = str(DEMO_SNAPSHOT)
    serve(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
