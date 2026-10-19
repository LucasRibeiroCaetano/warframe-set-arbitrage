"""JSON API and local server for the dashboard.

The same handlers run as Vercel functions (api/sets.py, api/mods.py) and behind
`wfspread web`. On Vercel the CDN caches each response for CACHE_SECONDS, so
warframe.market is hit at most once per window per query; locally an in-memory
cache does the same job.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import time
import traceback
import webbrowser
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .catalog import RESURGENCE, UNVAULTED, build_catalog
from .http import Http
from .mods import ModSpread, baro_status, fetch_mod_spreads, primed_mods
from .pricing import Quote, SetSpread, fetch_set_spreads

STATIC = Path(__file__).parent / "static"
CACHE_SECONDS = 600
PLATFORMS = {"pc", "ps4", "xbox", "switch", "mobile"}


def _iso(d: datetime | None) -> str | None:
    return d.astimezone(timezone.utc).isoformat() if d else None


def _quote(q: Quote) -> dict:
    return {**asdict(q), "spread": q.spread}


def _options(query: dict[str, list[str]]) -> dict:
    def flag(name: str) -> bool:
        return query.get(name, ["0"])[0] in ("1", "true")

    try:
        depth = min(max(int(query.get("depth", ["1"])[0]), 1), 5)
    except ValueError:
        depth = 1
    platform = query.get("platform", ["pc"])[0]
    return {
        "depth": depth,
        "platform": platform if platform in PLATFORMS else "pc",
        "all": flag("all"),
    }


def sets_payload(query: dict[str, list[str]]) -> dict:
    opts = _options(query)
    http = Http(platform=opts["platform"])
    try:
        catalog = build_catalog(http, all_unvaulted=opts["all"])
        spreads = fetch_set_spreads(http, catalog.targets, depth=opts["depth"])
    finally:
        http.close()
    return {
        "generated_at": _iso(datetime.now(timezone.utc)),
        **opts,
        "pr_expiry": _iso(catalog.pr_expiry),
        "drops_updated": _iso(catalog.drops_updated),
        "hidden": catalog.hidden,
        "sets": [_set(s) for s in spreads],
    }


def _set(s: SetSpread) -> dict:
    t = s.target
    vols = s.volumes or {}

    def vol(slug: str) -> dict:
        v = vols.get(slug)
        return asdict(v) if v else {}

    wts_sum, wts_missing = s.partial_sum("wts")
    wtb_sum, wtb_missing = s.partial_sum("wtb")
    return {
        "slug": t.slug,
        "name": t.name.removesuffix(" Set"),
        "kind": t.kind,
        "pr": RESURGENCE in t.sources,
        "unvaulted": UNVAULTED in t.sources,
        "partial": t.partial,
        "set": {**_quote(s.set_quote), **vol(t.slug)},
        "parts_wts": {"total": wts_sum, "missing": wts_missing},
        "parts_wtb": {"total": wtb_sum, "missing": wtb_missing},
        "min_part_vol_48h": s.min_part_vol_48h,
        "delta_wts": s.delta_wts,
        "delta_wtb": s.delta_wtb,
        "parts": [
            {"slug": p.slug, "name": p.name, "qty": p.qty, **_quote(s.part_quotes[p.slug]), **vol(p.slug)}
            for p in t.parts
        ],
    }


def mods_payload(query: dict[str, list[str]]) -> dict:
    opts = _options(query)
    http = Http(platform=opts["platform"])
    try:
        baro = baro_status(http)
        mods = primed_mods(http, baro, all_mods=opts["all"])
        spreads = fetch_mod_spreads(http, mods, depth=opts["depth"])
    finally:
        http.close()
    return {
        "generated_at": _iso(datetime.now(timezone.utc)),
        **opts,
        "baro": {
            "active": baro.active,
            "location": baro.location,
            "activation": _iso(baro.activation),
            "expiry": _iso(baro.expiry),
            "primed_in_stock": sorted(i for i in baro.items if i.startswith("Primed ")),
            "last_visit": baro.last_visit,
            "last_visit_primed": sorted(baro.last_visit_items),
        },
        "mods": [_mod(s) for s in spreads],
    }


def _mod(s: ModSpread) -> dict:
    m = s.mod

    def stats(rank: int) -> dict | None:
        st = s.rank_stats(rank)
        return asdict(st) if st else None

    return {
        "slug": m.slug,
        "name": m.name.removeprefix("Primed "),
        "max_rank": m.max_rank,
        "rarity": m.rarity,
        "in_baro": m.in_baro,
        "in_last_baro": m.in_last_baro,
        "last_baro": m.last_baro,
        "baro_visits": m.baro_visits,
        "endo_to_max": m.endo_to_max,
        "credits_to_max": m.credits_to_max,
        "r0": _quote(s.r0),
        "rmax": _quote(s.rmax),
        "r0_stats": stats(0),
        "rmax_stats": stats(m.max_rank),
        "dip": s.dip,
        "vs_avg": s.vs_avg,
        "premium": s.premium,
        "plat_per_kendo": s.plat_per_kendo,
    }


ROUTES = {"/api/sets": sets_payload, "/api/mods": mods_payload}


class ApiHandler(BaseHTTPRequestHandler):
    """Serves /api/sets and /api/mods. Vercel imports this as each function's `handler`."""

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        route = ROUTES.get(url.path.rstrip("/"))
        if route is None:
            self._send(404, {"error": "not found"}, cache=False)
            return
        try:
            body = self.payload(route, url.path.rstrip("/"), parse_qs(url.query))
        except Exception as exc:
            traceback.print_exc()
            self._send(502, {"error": f"{type(exc).__name__}: {exc}"}, cache=False)
            return
        self._send(200, body, cache=True)

    def payload(self, route, path: str, query: dict[str, list[str]]) -> dict:
        return route(query)

    def _send(self, status: int, body: dict, cache: bool) -> None:
        data = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if cache:
            # Only Vercel's CDN caches (serving a stale copy while it refetches in the background);
            # browsers always ask it, so they never show a copy older than the CDN's.
            self.send_header("Vercel-CDN-Cache-Control", f"max-age={CACHE_SECONDS}, stale-while-revalidate=3600")
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


class LocalHandler(ApiHandler):
    """Adds the static page and an in-memory response cache for `wfspread web`."""

    # One fetch at a time keeps all requests under warframe.market's rate limit.
    lock = threading.Lock()
    memo: dict[str, tuple[float, dict]] = {}

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            super().do_GET()
            return
        file = STATIC / ("index.html" if path == "/" else path.lstrip("/"))
        if not file.resolve().is_relative_to(STATIC.resolve()) or not file.is_file():
            self.send_error(404)
            return
        data = file.read_bytes()
        kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{kind}; charset=utf-8" if kind.startswith("text/") else kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def payload(self, route, path: str, query: dict[str, list[str]]) -> dict:
        key = f"{path}?{sorted(_options(query).items())}"
        with self.lock:
            hit = self.memo.get(key)
            if hit and time.time() - hit[0] < CACHE_SECONDS:
                return hit[1]
            body = route(query)
            self.memo[key] = (time.time(), body)
            return body

    def log_message(self, format: str, *args) -> None:
        if "/api/" in self.path:
            super().log_message(format, *args)


def serve(port: int = 8000, open_browser: bool = True) -> int:
    server = ThreadingHTTPServer(("127.0.0.1", port), LocalHandler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Dashboard at {url} (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
