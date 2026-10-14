"""Works out which prime sets to track and what parts each set is made of.

- Unvaulted: a prime whose parts appear in a relic that currently drops somewhere
  in the official drop tables (missions, bounties, etc.).
- Prime Resurgence: a prime sold by Varzia in the current rotation (worldstate).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from .http import Http

MARKET = "https://api.warframe.market/v2"
DROPS_ALL = "https://drops.warframestat.us/data/all.json"
VARZIA = "https://api.warframestat.us/pc/vaultTrader?language=en"

HOUR = 3600
DAY = 24 * HOUR

RELIC_NAME = re.compile(r"\b(?:Lith|Meso|Neo|Axi) [A-Z]+\d+ Relic\b")
PRIME_BASE = re.compile(r"^(.+? Prime)\b")

UNVAULTED = "Unvaulted"
RESURGENCE = "PR"


@dataclass
class Part:
    slug: str
    name: str
    qty: int


@dataclass
class Target:
    slug: str
    name: str
    kind: str  # "frame" | "weapon"
    sources: set[str] = field(default_factory=set)
    parts: list[Part] = field(default_factory=list)
    # Unvaulted set where some part does not drop from any active relic.
    partial: bool = False


@dataclass
class Catalog:
    targets: list[Target]
    pr_expiry: datetime | None
    drops_updated: datetime | None


def _name(item: dict) -> str:
    return item["i18n"]["en"]["name"]


def _kind(tags: list[str]) -> str | None:
    if "warframe" in tags:
        return "frame"
    if "weapon" in tags:
        return "weapon"
    return None


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def active_relic_parts(http: Http) -> tuple[set[str], datetime | None]:
    """Names of all prime parts contained in relics that currently drop."""
    drops = http.cached_json("drops_all", DROPS_ALL, ttl=12 * HOUR)
    sources = {k: v for k, v in drops.items() if k != "relics"}
    active = set(RELIC_NAME.findall(json.dumps(sources)))
    parts: set[str] = set()
    for relic in drops["relics"]:
        if "relicName" not in relic:
            continue
        if f"{relic['tier']} {relic['relicName']} Relic" in active:
            parts.update(r["itemName"] for r in relic["rewards"] if " Prime " in r["itemName"])

    updated = None
    try:
        info = http.cached_json("drops_info", "https://drops.warframestat.us/data/info.json", ttl=12 * HOUR)
        updated = datetime.fromtimestamp(info["modified"] / 1000).astimezone()
    except Exception:
        pass
    return parts, updated


def resurgence_game_refs(http: Http) -> tuple[set[str], datetime | None]:
    """gameRefs (warframe.market style) of primes Varzia currently sells."""
    trader = http.cached_json("varzia", VARZIA, ttl=HOUR)
    refs = {
        entry["uniqueName"].replace("/StoreItems/", "/", 1)
        for entry in trader.get("inventory", [])
        if entry.get("uniqueName", "").startswith("/Lotus/StoreItems/")
    }
    return refs, _parse_ts(trader.get("expiry"))


def set_parts(http: Http, set_slug: str) -> list[Part]:
    data = http.cached_json(f"set_{set_slug}", f"{MARKET}/item/{set_slug}/set", ttl=7 * DAY)
    return [
        Part(slug=i["slug"], name=_name(i), qty=i.get("quantityInSet") or 1)
        for i in data["data"]["items"]
        if not i.get("setRoot")
    ]


def build_catalog(http: Http) -> Catalog:
    items = http.cached_json("market_items", f"{MARKET}/items", ttl=DAY)["data"]
    sets_by_name = {_name(i): i for i in items if "set" in i["tags"] and "prime" in i["tags"]}
    sets_by_ref = {i["gameRef"]: i for i in sets_by_name.values()}

    targets: dict[str, Target] = {}

    def add(item: dict, source: str) -> None:
        kind = _kind(item["tags"])
        if kind is None:
            return
        t = targets.setdefault(item["slug"], Target(slug=item["slug"], name=_name(item), kind=kind))
        t.sources.add(source)

    dropping_parts, drops_updated = active_relic_parts(http)
    for part_name in dropping_parts:
        m = PRIME_BASE.match(part_name)
        if m and (item := sets_by_name.get(f"{m.group(1)} Set")):
            add(item, UNVAULTED)

    pr_refs, pr_expiry = resurgence_game_refs(http)
    for ref in pr_refs:
        if item := sets_by_ref.get(ref):
            add(item, RESURGENCE)

    for t in targets.values():
        t.parts = set_parts(http, t.slug)
        if t.sources == {UNVAULTED}:
            t.partial = any(p.name not in dropping_parts for p in t.parts)

    ordered = sorted(targets.values(), key=lambda t: (t.kind != "frame", t.name))
    return Catalog(targets=ordered, pr_expiry=pr_expiry, drops_updated=drops_updated)
