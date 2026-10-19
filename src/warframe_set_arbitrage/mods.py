"""Primed mods around Baro Ki'Teer's visits.

When Baro brings a primed mod its price dips, then recovers over the following months.
For each mod this compares live rank 0 prices with the 90-day median (the dip), and
rank 0 with max rank (whether maxing it is worth the endo and credits).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import median

from .catalog import DAY, HOUR, MARKET, _name, _parse_ts
from .http import Http
from .pricing import STATS_URL, Progress, Quote, fetch_quote

BARO = "https://api.warframestat.us/pc/voidTrader?language=en"

# Primed mods with steady demand: the ones most builds want, so they recover after Baro leaves.
WATCHLIST = [
    "primed_continuity",
    "primed_flow",
    "primed_reach",
    "primed_fever_strike",
    "primed_target_cracker",
    "primed_pressure_point",
    "primed_point_blank",
    "primed_pistol_gambit",
    "primed_heavy_trauma",
    "primed_morphic_transformer",
    "primed_ravage",
    "primed_fast_hands",
    "primed_cryo_rounds",
    "primed_firestorm",
    "primed_magazine_warp",
    "primed_tactical_pump",
    "primed_animal_instinct",
    "primed_pack_leader",
]

# Endo and credits for each fusion step scale as base × 2^rank (wiki: Fusion).
FUSION_BASE = {"common": (10, 483), "uncommon": (20, 966), "rare": (30, 1449), "legendary": (40, 1932)}


@dataclass
class PrimedMod:
    slug: str
    name: str
    max_rank: int
    rarity: str
    in_baro: bool = False
    in_last_baro: bool = False  # brought on his most recent finished visit
    last_baro: str | None = None  # date Baro last brought it
    baro_visits: int = 0  # times Baro has brought it

    @property
    def endo_to_max(self) -> int:
        return FUSION_BASE[self.rarity][0] * (2**self.max_rank - 1)

    @property
    def credits_to_max(self) -> int:
        return FUSION_BASE[self.rarity][1] * (2**self.max_rank - 1)


@dataclass
class Baro:
    active: bool
    location: str | None
    activation: datetime | None
    expiry: datetime | None
    items: set[str]  # names in his current stock
    # From the wiki's offering history: primed mod name -> dates Baro brought it.
    offerings: dict[str, list[str]] = field(default_factory=dict)
    last_visit: str | None = None  # start date of his most recent finished visit
    last_visit_items: set[str] = field(default_factory=set)  # primed mods he brought then


def baro_status(http: Http) -> Baro:
    baro = _baro(http.cached_json("baro", BARO, ttl=HOUR))
    if baro.active and not baro.items:
        # Cached from before he arrived: his stock is only listed once he's here.
        baro = _baro(http.cached_json("baro", BARO, ttl=0))
    history = baro_history(http)
    baro.offerings = history["primed"]
    # While he's here, "last visit" means the one before this one.
    if baro.active and baro.activation:
        cutoff = baro.activation.date().isoformat()
        past = [d for d in history["visits"] if d < cutoff]
    else:
        past = [d for d in history["visits"] if d <= _today()]
    if past:
        baro.last_visit = past[-1]
        baro.last_visit_items = {name for name, dates in baro.offerings.items() if baro.last_visit in dates}
    return baro


# The wiki keeps every item's Baro offering dates in a Lua data module.
BARO_HISTORY = "https://wiki.warframe.com/w/Module:Baro/data?action=raw"
_WIKI_ITEM = re.compile(r'^\t\t\["(?P<name>[^"]+)"\] = \{\n(?P<body>.*?)^\t\t\},?$', re.M | re.S)
# OfferingDates (cross-platform, 2023+) and PcOfferingDates (older); console dates lagged PC.
_WIKI_DATES = re.compile(r"^\t\t\t(?:Pc)?OfferingDates = \{(.*?)\}", re.M | re.S)
_DATE = re.compile(r'"(\d{4}-\d{2}-\d{2})"')


def _reduce_baro_history(lua: str) -> dict:
    primed: dict[str, list[str]] = {}
    visits: set[str] = set()
    for m in _WIKI_ITEM.finditer(lua):
        dates = sorted({d for block in _WIKI_DATES.findall(m["body"]) for d in _DATE.findall(block)})
        visits.update(dates)
        if m["name"].startswith("Primed ") and dates:
            primed[m["name"]] = dates
    return {"primed": primed, "visits": sorted(visits)}


def baro_history(http: Http) -> dict:
    """Past Baro visits from the wiki. Empty if the wiki can't be reached and nothing is cached."""
    try:
        return http.cached_json("baro_history", BARO_HISTORY, ttl=6 * HOUR, transform=_reduce_baro_history, text=True)
    except Exception:
        return {"primed": {}, "visits": []}


def _baro(trader: dict) -> Baro:
    activation, expiry = _parse_ts(trader.get("activation")), _parse_ts(trader.get("expiry"))
    now = datetime.now(timezone.utc)
    return Baro(
        active=bool(activation and expiry and activation <= now < expiry),
        location=trader.get("location"),
        activation=activation,
        expiry=expiry,
        items={entry["item"] for entry in trader.get("inventory", []) if entry.get("item")},
    )


def _rarity(tags: list[str]) -> str:
    return next((r for r in FUSION_BASE if r in tags), "legendary")


def primed_mods(http: Http, baro: Baro, all_mods: bool = False) -> list[PrimedMod]:
    """The watchlist (or every primed mod), plus any primed mod Baro is selling now or brought last visit."""
    items = http.cached_json("market_items", f"{MARKET}/items", ttl=DAY)["data"]
    # A visit that's on now isn't "last" yet, though the wiki may already list it.
    current = baro.activation.date().isoformat() if baro.active and baro.activation else None
    today = _today()
    # Names are matched case-insensitively: the market writes "Bane Of", the game and wiki "Bane of".
    stock = {n.lower() for n in baro.items}
    last_visit = {n.lower() for n in baro.last_visit_items}
    offerings = {n.lower(): dates for n, dates in baro.offerings.items()}
    mods = []
    for i in items:
        name = _name(i)
        if not name.startswith("Primed ") or "mod" not in i["tags"] or not i.get("maxRank"):
            continue
        key = name.lower()
        in_baro, in_last = key in stock, key in last_visit
        if all_mods or in_baro or in_last or i["slug"] in WATCHLIST:
            dates = [d for d in offerings.get(key, []) if d != current and d <= today]
            mods.append(PrimedMod(
                i["slug"], name, i["maxRank"], _rarity(i["tags"]), in_baro, in_last,
                last_baro=dates[-1] if dates else None, baro_visits=len(dates),
            ))
    return sorted(mods, key=lambda m: m.name)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


@dataclass
class RankStats:
    """Closed trades on warframe.market for one rank (self-reported, so volume is a lower bound)."""

    vol_48h: int
    median_90d: float | None  # median of the daily medians: the "usual" price, robust to Baro dips
    low_90d: float | None
    high_90d: float | None
    history: list[tuple[str, float]]  # (date, median) per day over 90 days


def _reduce_mod_stats(payload: dict) -> dict:
    closed = payload["payload"]["statistics_closed"]
    out = {}
    for rank in {b.get("mod_rank") for b in closed.get("90days", [])}:
        hourly = [b for b in closed.get("48hours", []) if b.get("mod_rank") == rank]
        history = [(b["datetime"][:10], b["median"]) for b in closed.get("90days", []) if b.get("mod_rank") == rank]
        daily = [m for _, m in history]
        out[str(rank)] = {
            "vol_48h": sum(b["volume"] for b in hourly),
            "median_90d": median(daily) if daily else None,
            "low_90d": min(daily, default=None),
            "high_90d": max(daily, default=None),
            "history": history,
        }
    return out


def mod_stats_cached(http: Http, slug: str) -> bool:
    return http.is_cached(f"mod_stats_{slug}", HOUR)


def fetch_mod_stats(http: Http, slug: str) -> dict[int, RankStats]:
    data = http.cached_json(f"mod_stats_{slug}", STATS_URL.format(slug=slug), ttl=HOUR, transform=_reduce_mod_stats)
    return {int(rank): RankStats(**s) for rank, s in data.items() if rank != "None"}


def fetch_mod_spreads(
    http: Http, mods: list[PrimedMod], depth: int = 1, with_stats: bool = True, on_progress: Progress | None = None
) -> list[ModSpread]:
    """Rank 0 and max rank quotes for every mod, plus per-rank trade stats."""
    to_fetch = [m.slug for m in mods if not mod_stats_cached(http, m.slug)] if with_stats else []
    total, done = 2 * len(mods) + len(to_fetch), 0
    quotes: dict[tuple[str, int], Quote] = {}
    for m in mods:
        for rank in (0, m.max_rank):
            quotes[m.slug, rank] = fetch_quote(http, m.slug, depth=depth, rank=rank)
            done += 1
            if on_progress:
                on_progress(done, total, "orders")
    stats: dict[str, dict[int, RankStats]] = {}
    if with_stats:
        for m in mods:
            stats[m.slug] = fetch_mod_stats(http, m.slug)
            if m.slug in to_fetch:
                done += 1
                if on_progress:
                    on_progress(done, total, "stats")
    return [ModSpread(m, quotes[m.slug, 0], quotes[m.slug, m.max_rank], stats.get(m.slug)) for m in mods]


def _pct(now: float | None, usual: float | None) -> float | None:
    return None if now is None or not usual else (now - usual) / usual * 100


@dataclass
class ModSpread:
    mod: PrimedMod
    r0: Quote
    rmax: Quote
    stats: dict[int, RankStats] | None = None

    def rank_stats(self, rank: int) -> RankStats | None:
        return self.stats.get(rank) if self.stats else None

    @property
    def dip(self) -> float | None:
        """Live rank 0 WTS vs its 90-day median, in %. Negative: cheaper than usual (Baro dip)."""
        s = self.rank_stats(0)
        return _pct(self.r0.wts, s.median_90d if s else None)

    @property
    def premium(self) -> float | None:
        """Max rank WTS minus rank 0 WTS: what maxing adds when you list it."""
        return None if self.rmax.wts is None or self.r0.wts is None else self.rmax.wts - self.r0.wts

    @property
    def plat_per_kendo(self) -> float | None:
        """Premium per 1,000 endo spent maxing (credits not counted)."""
        return None if self.premium is None else self.premium / self.mod.endo_to_max * 1000
