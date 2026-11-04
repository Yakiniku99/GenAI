"""Tiny stdlib web server: serves the dashboard and a cached JSON snapshot.

A background thread rescans Kalshi on an interval. Every few cycles it does a
full discovery pass over all series in the category; in between it only
rescans series that had open events last time, which roughly halves the
request count.
"""

from __future__ import annotations

import json
import shutil
import threading
import webbrowser
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .client import KalshiClient
from .scanner import scan

STATIC = Path(__file__).parent / "static"
DEMO_SNAPSHOT = Path(__file__).parent / "demo_snapshot.json"
FULL_SCAN_EVERY = 10  # cycles


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.snapshot: dict | None = None
        self.scanning = False
        self.progress = (0, 0)
        self.last_error: str | None = None
        self.next_scan_at: float | None = None
        self.refresh_now = threading.Event()
        self.static = False  # serving a saved snapshot, no rescans

    def public(self) -> dict:
        with self.lock:
            return {
                "static": self.static,
                "scanning": self.scanning,
                "progress": {"done": self.progress[0], "total": self.progress[1]},
                "last_error": self.last_error,
                "next_scan_in": max(0, round(self.next_scan_at - time.time())) if self.next_scan_at else None,
                "snapshot": self.snapshot,
            }


def _scan_loop(state: State, args) -> None:
    client = KalshiClient(rate_per_sec=args.rate)
    active: list[str] | None = None
    cycle = 0
    while True:
        full = active is None or cycle % FULL_SCAN_EVERY == 0
        only = args.series if args.series else (None if full else active)
        with state.lock:
            state.scanning, state.progress = True, (0, 0)

        def progress(done, total):
            with state.lock:
                state.progress = (done, total)

        try:
            snap = scan(client, args.category, only, args.workers, progress, args.fees, args.depth)
            snap["full_discovery"] = full
            active = sorted({o["series_ticker"] for o in snap["opportunities"]}
                            | set(snap["series_titles"]))
            with state.lock:
                state.snapshot, state.last_error = snap, None
        except Exception as e:
            traceback.print_exc()
            with state.lock:
                state.last_error = f"{type(e).__name__}: {e}"
        cycle += 1
        with state.lock:
            state.scanning = False
            state.next_scan_at = time.time() + args.interval
        state.refresh_now.wait(args.interval)
        state.refresh_now.clear()


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):  # keep the console quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._send(200, json.dumps(state.public()).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path == "/api/refresh":
                state.refresh_now.set()
                self._send(202, b'{"ok":true}', "application/json")
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


def serve(args) -> None:
    state = State()
    if args.snapshot:
        with open(args.snapshot) as f:
            state.snapshot = json.load(f)
        state.static = True
    else:
        threading.Thread(target=_scan_loop, args=(state, args), daemon=True).start()
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    url = f"http://{args.host}:{args.port}/"
    mode = f"snapshot {args.snapshot}" if args.snapshot else "live scanning"
    print(f"Kalshi arbitrage dashboard ({mode}) on {url}  (Ctrl+C to stop)")
    if getattr(args, "open", False):
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def build_static_site(snapshot: dict, out_dir: str) -> None:
    """Write index.html + snapshot.json: a self-contained static dashboard."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(STATIC / "index.html", out / "index.html")
    (out / "snapshot.json").write_text(json.dumps(snapshot))
