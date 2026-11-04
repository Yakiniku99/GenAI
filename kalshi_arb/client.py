"""Minimal, dependency-free client for Kalshi's public market-data API.

Only unauthenticated read endpoints are used, so no API key is needed.
Requests go through a shared rate limiter and are retried on 429s, 5xx
responses and dropped connections.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
USER_AGENT = "kalshi-arb-dashboard/0.1"


class RateLimiter:
    """Simple thread-safe limiter: at most `rate` request starts per second."""

    def __init__(self, rate: float):
        self.interval = 1.0 / rate if rate > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        delay = start - now
        if delay > 0:
            time.sleep(delay)

    def penalize(self, seconds: float) -> None:
        """Push every caller back after the server says we are too fast."""
        with self._lock:
            self._next = max(self._next, time.monotonic() + seconds)


class KalshiClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        rate_per_sec: float = 8.0,
        max_retries: int = 7,
        timeout: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.limiter = RateLimiter(rate_per_sec)
        self.max_retries = max_retries
        self.timeout = timeout

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self.limiter.wait()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 429:
                    self.limiter.penalize(min(2 ** attempt, 20))
                elif e.code < 500:
                    raise
            except (urllib.error.URLError, ConnectionError, TimeoutError,
                    http.client.HTTPException, json.JSONDecodeError) as e:
                last_err = e
            time.sleep(min(0.5 * 2 ** attempt, 20))
        raise RuntimeError(f"GET {url} failed after {self.max_retries + 1} attempts: {last_err}")

    def paginate(self, path: str, key: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
        params = dict(params or {})
        cursor = None
        while True:
            page = self.get(path, {**params, "cursor": cursor})
            items = page.get(key) or []
            yield from items
            cursor = page.get("cursor")
            if not cursor or not items:
                return

    # --- typed helpers -------------------------------------------------
    def series(self, category: str) -> list[dict]:
        return self.get("/series", {"category": category}).get("series") or []

    def open_events_with_markets(self, series_ticker: str) -> list[dict]:
        return list(self.paginate(
            "/events", "events",
            {"series_ticker": series_ticker, "status": "open",
             "with_nested_markets": "true", "limit": 200},
        ))
