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


def fetch_quote(http: Http, slug: str, depth: int = 1) -> Quote:
    data = http.get_json(f"{MARKET}/orders/item/{slug}/top")["data"]
    sells = sorted(o["platinum"] for o in data.get("sell", []))
    buys = sorted((o["platinum"] for o in data.get("buy", [])), reverse=True)
    return Quote(
        wts=mean(sells[:depth]) if sells else None,
        wtb=mean(buys[:depth]) if buys else None,
        sellers=len(sells),
        buyers=len(buys),
    )


def _sum(values: list[float | None]) -> float | None:
    return None if any(v is None for v in values) else sum(values)  # type: ignore[arg-type]


def _diff(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a - b


@dataclass
class SetSpread:
    target: Target
    set_quote: Quote
    part_quotes: dict[str, Quote]

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

    @property
    def flip(self) -> float | None:
        """Instant arbitrage: buy all parts from sellers, sell the set to the best buyer."""
        return _diff(self.set_quote.wtb, self.parts_wts)


def _mul(v: float | None, qty: int) -> float | None:
    return None if v is None else v * qty
