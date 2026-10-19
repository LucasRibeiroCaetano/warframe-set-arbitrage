"""Works out which prime sets to track and what parts each set is made of.

- Unvaulted: a prime whose parts appear in a relic that currently drops somewhere
  in the official drop tables (missions, bounties, etc.).
- Prime Resurgence: a prime sold by Varzia in the current rotation (worldstate).

Unvaulted primes are only kept while in their first Prime Access cycle: warframes that
have never been vaulted, and weapons released alongside one of those warframes. That
drops the permanent relic pool (Braton, Lex, Paris Prime, ...) and the permanently
unvaulted Valkyr/Nyx Prime along with their weapons.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .http import Http

MARKET = "https://api.warframe.market/v2"
DROPS_ALL = "https://drops.warframestat.us/data/all.json"
VARZIA = "https://api.warframestat.us/pc/vaultTrader?language=en"
WFCD_FIELDS = "language=en&only=name,vaulted,vaultDate,releaseDate"
WFCD_WEAPONS = f"https://api.warframestat.us/weapons/?{WFCD_FIELDS}"
WFCD_FRAMES = f"https://api.warframestat.us/warframes/?{WFCD_FIELDS}"

HOUR = 3600
DAY = 24 * HOUR

RELIC_NAME = re.compile(r"\b(?:Lith|Meso|Neo|Axi) [A-Z]+\d+ Relic\b")
PRIME_BASE = re.compile(r"^(.+? Prime)\b")

UNVAULTED = "Unvaulted"
RESURGENCE = "PR"
VAULTED = "Vaulted"


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
    # The prime's original release date (YYYY-MM-DD). Older primes in the unvaulted pool are vaulted first.
    released: str | None = None


@dataclass
class Catalog:
    targets: list[Target]
    pr_expiry: datetime | None
    drops_updated: datetime | None
    hidden: list[str] = field(default_factory=list)


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


def release_info(http: Http) -> dict[str, dict]:
    """WFCD release/vault data for every warframe and weapon, by name."""
    frames = http.cached_json("wfcd_frames", WFCD_FRAMES, ttl=7 * DAY)
    weapons = http.cached_json("wfcd_weapons", WFCD_WEAPONS, ttl=7 * DAY)
    return {x["name"]: x for x in [*frames, *weapons] if x.get("name")}


def _fresh(info: dict | None) -> bool:
    """In its first Prime Access cycle: never vaulted and released within the last 3 years.

    The age check guards against stale vault data for items that were vaulted and then
    permanently unvaulted (Valkyr/Nyx Prime).
    """
    if not info or info.get("vaulted") or info.get("vaultDate"):
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(days=3 * 365)).date().isoformat()
    return (info.get("releaseDate") or "") >= cutoff


def set_parts(http: Http, set_slug: str) -> list[Part]:
    data = http.cached_json(f"set_{set_slug}", f"{MARKET}/item/{set_slug}/set", ttl=7 * DAY)
    return [
        Part(slug=i["slug"], name=_name(i), qty=i.get("quantityInSet") or 1)
        for i in data["data"]["items"]
        if not i.get("setRoot")
    ]


def base(t: Target) -> str:
    """The prime's own name, as WFCD lists it: "Ivara Prime Set" -> "Ivara Prime"."""
    return t.name.removesuffix(" Set")


def build_catalog(http: Http, all_unvaulted: bool = False) -> Catalog:
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

    hidden: list[str] = []
    info = release_info(http)
    if not all_unvaulted:
        fresh_frame_dates = {
            info[base(t)]["releaseDate"]
            for t in targets.values()
            if t.kind == "frame" and UNVAULTED in t.sources and _fresh(info.get(base(t)))
        }
        for slug, t in list(targets.items()):
            if t.sources != {UNVAULTED}:
                continue  # Prime Resurgence items always stay
            i = info.get(base(t))
            keep = _fresh(i) and (t.kind == "frame" or i.get("releaseDate") in fresh_frame_dates)
            if not keep:
                hidden.append(base(t))
                del targets[slug]

    for t in targets.values():
        t.parts = set_parts(http, t.slug)
        t.released = (info.get(base(t)) or {}).get("releaseDate")
        if t.sources == {UNVAULTED}:
            t.partial = any(p.name not in dropping_parts for p in t.parts)

    ordered = sorted(targets.values(), key=lambda t: (t.kind != "frame", t.name))
    return Catalog(targets=ordered, pr_expiry=pr_expiry, drops_updated=drops_updated, hidden=sorted(hidden))


def vaulted_targets(http: Http) -> list[Target]:
    """Prime sets that neither drop from a current relic nor sit in Varzia's stock, frames first.

    Parts are not attached (see attach_parts) so callers can narrow the list first: each set and
    part costs market requests, and there are over a hundred vaulted sets.
    """
    items = http.cached_json("market_items", f"{MARKET}/items", ttl=DAY)["data"]
    dropping_parts, _ = active_relic_parts(http)
    dropping_sets = {f"{m.group(1)} Set" for p in dropping_parts if (m := PRIME_BASE.match(p))}
    pr_refs, _ = resurgence_game_refs(http)
    targets = [
        Target(slug=i["slug"], name=_name(i), kind=kind, sources={VAULTED})
        for i in items
        if "set" in i["tags"] and "prime" in i["tags"] and (kind := _kind(i["tags"]))
        and _name(i) not in dropping_sets and i["gameRef"] not in pr_refs
    ]
    info = release_info(http)
    for t in targets:
        t.released = (info.get(base(t)) or {}).get("releaseDate")
    return sorted(targets, key=lambda t: (t.kind != "frame", t.name))


def attach_parts(http: Http, targets: list[Target]) -> None:
    for t in targets:
        t.parts = set_parts(http, t.slug)
