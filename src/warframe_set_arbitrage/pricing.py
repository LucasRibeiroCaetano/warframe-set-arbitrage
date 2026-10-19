"""Live order book quotes and set-vs-parts spreads."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean

from .catalog import MARKET, Target
from .http import Http


@dataclass
class Quote:
    """Best prices from players currently in game.

    `wts` is the cheapest sell order, `wtb` the highest buy order. With depth > 1
    each is the average of the best `depth` orders, which smooths out bait listings.
    """

    wts: float | None
    wtb: float | None
    sellers: int
    buyers: int

    @property
    def spread(self) -> float | None:
        if self.wts is None or self.wtb is None:
            return None
        return self.wts - self.wtb


def fetch_quote(http: Http, slug: str, depth: int = 1, rank: int | None = None) -> Quote:
    query = "" if rank is None else f"?rank={rank}"
    data = http.get_json(f"{MARKET}/orders/item/{slug}/top{query}")["data"]
    sells = sorted(o["platinum"] for o in data.get("sell", []))
    buys = sorted((o["platinum"] for o in data.get("buy", [])), reverse=True)
    return Quote(
        wts=mean(sells[:depth]) if sells else None,
        wtb=mean(buys[:depth]) if buys else None,
        sellers=len(sells),
        buyers=len(buys),
    )


STATS_URL = "https://api.warframe.market/v1/items/{slug}/statistics"
STATS_TTL = 3600  # warframe.market aggregates these hourly


@dataclass
class Volume:
    """Trades players closed on warframe.market (self-reported, so a lower bound)."""

    vol_48h: int
    vol_7d: int
    avg_48h: float | None  # volume-weighted average sold price over 48h


def _reduce_stats(payload: dict) -> dict:
    closed = payload["payload"]["statistics_closed"]
    # Mods carry one series per rank; prime parts/sets have no rank.
    hourly = [b for b in closed.get("48hours", []) if not b.get("mod_rank")]
    daily = [b for b in closed.get("90days", []) if not b.get("mod_rank")]
    vol_48h = sum(b["volume"] for b in hourly)
    return {
        "vol_48h": vol_48h,
        "vol_7d": sum(b["volume"] for b in daily[-7:]),
        "avg_48h": sum(b["avg_price"] * b["volume"] for b in hourly) / vol_48h if vol_48h else None,
    }


def stats_cached(http: Http, slug: str) -> bool:
    return http.is_cached(f"stats_{slug}", STATS_TTL)


def fetch_volume(http: Http, slug: str) -> Volume:
    data = http.cached_json(f"stats_{slug}", STATS_URL.format(slug=slug), ttl=STATS_TTL, transform=_reduce_stats)
    return Volume(**data)


def _sum(values: list[float | None]) -> float | None:
    return None if any(v is None for v in values) else sum(values)  # type: ignore[arg-type]


def _diff(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a - b


@dataclass
class SetSpread:
    target: Target
    set_quote: Quote
    part_quotes: dict[str, Quote]
    volumes: dict[str, Volume] | None = None  # by slug, for the set and its parts

    @property
    def set_volume(self) -> Volume | None:
        return self.volumes.get(self.target.slug) if self.volumes else None

    @property
    def min_part_vol_48h(self) -> int | None:
        """48h volume of the least-traded part: the bottleneck when building or splitting a set."""
        if not self.volumes:
            return None
        return min(self.volumes[p.slug].vol_48h for p in self.target.parts)

    @property
    def parts_wts(self) -> float | None:
        """Cost to buy every part from the cheapest sellers."""
        return _sum([_mul(self.part_quotes[p.slug].wts, p.qty) for p in self.target.parts])

    @property
    def parts_wtb(self) -> float | None:
        """What you'd get selling every part to the highest buyers."""
        return _sum([_mul(self.part_quotes[p.slug].wtb, p.qty) for p in self.target.parts])

    def partial_sum(self, side: str) -> tuple[float, int]:
        """Sum over parts that have an order on `side` ("wts"/"wtb"), and how many lack one."""
        total, missing = 0.0, 0
        for p in self.target.parts:
            price = getattr(self.part_quotes[p.slug], side)
            if price is None:
                missing += 1
            else:
                total += price * p.qty
        return total, missing

    @property
    def delta_wts(self) -> float | None:
        """Set WTS minus parts WTS. Positive: parts are cheaper than the set."""
        return _diff(self.set_quote.wts, self.parts_wts)

    @property
    def delta_wtb(self) -> float | None:
        """Set WTB minus parts WTB. Positive: buyers pay more for the set."""
        return _diff(self.set_quote.wtb, self.parts_wtb)


def _mul(v: float | None, qty: int) -> float | None:
    return None if v is None else v * qty
