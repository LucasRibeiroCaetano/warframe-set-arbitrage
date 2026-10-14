"""HTTP client with per-host throttling, retries and a small on-disk JSON cache."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

USER_AGENT = "wfspread/0.1 (+https://github.com/; personal spread checker)"
CACHE_DIR = Path(os.environ.get("WFSPREAD_CACHE", Path.home() / ".cache" / "wfspread"))

# warframe.market asks clients to stay at or below ~3 requests/second.
THROTTLED_HOSTS = {"api.warframe.market"}


class Http:
    def __init__(self, rps: float = 3.0, refresh: bool = False, platform: str = "pc"):
        self.client = httpx.Client(
            timeout=30,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Language": "en",
                "Platform": platform,
                "Crossplay": "true",
            },
        )
        self.min_interval = 1.0 / rps
        self.refresh = refresh
        self._last_call: dict[str, float] = {}

    def close(self) -> None:
        self.client.close()

    def _wait_turn(self, host: str) -> None:
        if host not in THROTTLED_HOSTS:
            return
        elapsed = time.monotonic() - self._last_call.get(host, 0.0)
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call[host] = time.monotonic()

    def get_json(self, url: str):
        host = urlsplit(url).hostname or ""
        delay = 1.0
        for attempt in range(6):
            self._wait_turn(host)
            try:
                resp = self.client.get(url)
            except httpx.TransportError:
                if attempt == 5:
                    raise
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == 5:
                    resp.raise_for_status()
                time.sleep(delay)
                delay *= 2
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"unreachable: {url}")

    def is_cached(self, key: str, ttl: float) -> bool:
        path = CACHE_DIR / f"{key}.json"
        return not self.refresh and path.exists() and time.time() - path.stat().st_mtime < ttl

    def cached_json(self, key: str, url: str, ttl: float, transform=None):
        """Fetch JSON, reusing a cached copy younger than `ttl` seconds.

        `transform` reduces the response before it is cached and returned.
        If the fetch fails and a stale copy exists, the stale copy is returned.
        """
        path = CACHE_DIR / f"{key}.json"
        if self.is_cached(key, ttl):
            return json.loads(path.read_text(encoding="utf-8"))
        try:
            data = self.get_json(url)
            if transform:
                data = transform(data)
        except (httpx.HTTPError, ValueError):
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
            raise
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return data
