"""Primed mods around Baro Ki'Teer's visits.

When Baro brings a primed mod its price dips, then recovers over the following months.
For each mod this compares live rank 0 prices with the 90-day median (the dip), and
rank 0 with max rank (whether maxing it is worth the endo and credits).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median

from .catalog import DAY, HOUR, MARKET, _name, _parse_ts
from .http import Http
from .pricing import STATS_URL, Quote

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


def baro_status(http: Http) -> Baro:
    baro = _baro(http.cached_json("baro", BARO, ttl=HOUR))
    if baro.active and not baro.items:
        # Cached from before he arrived: his stock is only listed once he's here.
        baro = _baro(http.cached_json("baro", BARO, ttl=0))
    return baro


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
    """The watchlist (or every primed mod), plus any primed mod Baro is selling right now."""
    items = http.cached_json("market_items", f"{MARKET}/items", ttl=DAY)["data"]
    mods = []
    for i in items:
        name = _name(i)
        if not name.startswith("Primed ") or "mod" not in i["tags"] or not i.get("maxRank"):
            continue
        in_baro = name in baro.items
        if all_mods or in_baro or i["slug"] in WATCHLIST:
            mods.append(PrimedMod(i["slug"], name, i["maxRank"], _rarity(i["tags"]), in_baro))
    return sorted(mods, key=lambda m: m.name)


@dataclass
class RankStats:
    """Closed trades on warframe.market for one rank (self-reported, so volume is a lower bound)."""

    vol_48h: int
    median_90d: float | None  # median of the daily medians: the "usual" price, robust to Baro dips
    low_90d: float | None
    high_90d: float | None


def _reduce_mod_stats(payload: dict) -> dict:
    closed = payload["payload"]["statistics_closed"]
    out = {}
    for rank in {b.get("mod_rank") for b in closed.get("90days", [])}:
        hourly = [b for b in closed.get("48hours", []) if b.get("mod_rank") == rank]
        daily = [b["median"] for b in closed.get("90days", []) if b.get("mod_rank") == rank]
        out[str(rank)] = {
            "vol_48h": sum(b["volume"] for b in hourly),
            "median_90d": median(daily) if daily else None,
            "low_90d": min(daily, default=None),
            "high_90d": max(daily, default=None),
        }
    return out


def mod_stats_cached(http: Http, slug: str) -> bool:
    return http.is_cached(f"modstats_{slug}", HOUR)


def fetch_mod_stats(http: Http, slug: str) -> dict[int, RankStats]:
    data = http.cached_json(f"modstats_{slug}", STATS_URL.format(slug=slug), ttl=HOUR, transform=_reduce_mod_stats)
    return {int(rank): RankStats(**s) for rank, s in data.items() if rank != "None"}


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
