from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn
from rich.rule import Rule
from rich.table import Table

from .catalog import RESURGENCE, UNVAULTED, build_catalog
from .http import Http
from .pricing import Quote, SetSpread, Volume, fetch_quote, fetch_volume, stats_cached

SORTS = {
    "delta-wts": lambda s: s.delta_wts,
    "delta-wtb": lambda s: s.delta_wtb,
    "set-wts": lambda s: s.set_quote.wts,
    "set-spread": lambda s: s.set_quote.spread,
    "volume": lambda s: s.set_volume.vol_48h if s.set_volume else None,
}

KINDS = [("frame", "Warframes"), ("weapon", "Weapons")]


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="wfspread",
        description="Set vs. parts spreads on warframe.market for unvaulted and Prime Resurgence primes.",
    )
    p.add_argument("--source", choices=["all", "pr", "unvaulted"], default="all")
    p.add_argument("--type", dest="kind", choices=["all", "frames", "weapons"], default="all")
    p.add_argument("-f", "--filter", help="only sets whose name contains this text")
    p.add_argument("--sort", choices=["name", *SORTS], default="delta-wts")
    p.add_argument("--depth", type=int, default=1, help="average the best N orders per side (default 1)")
    p.add_argument("--all-unvaulted", action="store_true",
                   help="show every unvaulted prime, not just those in their first Prime Access cycle")
    p.add_argument("--no-partial", action="store_true", help="skip unvaulted sets missing a dropping part")
    p.add_argument("--no-volume", action="store_true", help="skip trade volume (halves requests when uncached)")
    p.add_argument("-d", "--detail", action="store_true", help="show a per-part breakdown for each set")
    p.add_argument("--csv", metavar="PATH", help="also write the per-set table to a CSV file")
    p.add_argument("--rps", type=float, default=3.0, help="warframe.market requests per second (default 3)")
    p.add_argument("--platform", default="pc", choices=["pc", "ps4", "xbox", "switch", "mobile"])
    p.add_argument("--refresh", action="store_true", help="ignore cached drop tables / item lists / volume")
    return p.parse_args(argv)


def fmt(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.0f}" if float(v).is_integer() else f"{v:.1f}"


def fmt_parts(spread: SetSpread, side: str) -> str:
    total, missing = spread.partial_sum(side)
    if missing == len(spread.target.parts):
        return "—"
    return f"[yellow]≥{fmt(total)}[/]" if missing else fmt(total)


def fmt_delta(v: float | None) -> str:
    if v is None:
        return "[dim]—[/]"
    color = "green" if v > 0 else "red" if v < 0 else "dim"
    return f"[{color}]{v:+.0f}[/]"


def fmt_vol(v: int | None) -> str:
    if v is None:
        return "—"
    if v == 0:
        return "[red]0[/]"
    return f"[yellow]{v}[/]" if v < 5 else str(v)


def source_label(spread: SetSpread) -> str:
    t = spread.target
    labels = []
    if RESURGENCE in t.sources:
        labels.append("[magenta]PR[/]")
    if UNVAULTED in t.sources:
        labels.append("[cyan]Unv*[/]" if t.partial else "[cyan]Unv[/]")
    return " ".join(labels)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    console = Console()
    http = Http(rps=args.rps, refresh=args.refresh, platform=args.platform)
    try:
        with console.status("Loading drop tables, Varzia stock and set lists…"):
            catalog = build_catalog(http, all_unvaulted=args.all_unvaulted)

        targets = catalog.targets
        if args.source == "pr":
            targets = [t for t in targets if RESURGENCE in t.sources]
        elif args.source == "unvaulted":
            targets = [t for t in targets if UNVAULTED in t.sources]
        if args.kind != "all":
            targets = [t for t in targets if t.kind == args.kind.rstrip("s")]
        if args.filter:
            targets = [t for t in targets if args.filter.lower() in t.name.lower()]
        if args.no_partial:
            targets = [t for t in targets if not t.partial]
        if not targets:
            console.print("[yellow]No sets match those filters.[/]")
            return 1

        slugs = list(dict.fromkeys([s for t in targets for s in [t.slug, *(p.slug for p in t.parts)]]))
        jobs = [("orders", s) for s in slugs]
        if not args.no_volume:
            # Cached volume is free; only uncached items cost a request.
            jobs += [("volume", s) for s in slugs]
        quotes: dict[str, Quote] = {}
        volumes: dict[str, Volume] = {}
        with Progress(
            TextColumn("Fetching {task.fields[what]}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        ) as progress:
            uncached = sum(1 for kind, s in jobs if kind == "orders" or not stats_cached(http, s))
            task = progress.add_task("fetch", total=uncached, what="orders")
            for kind, slug in jobs:
                if kind == "orders":
                    quotes[slug] = fetch_quote(http, slug, depth=args.depth)
                    progress.advance(task)
                else:
                    progress.update(task, what="volume")
                    was_cached = stats_cached(http, slug)
                    volumes[slug] = fetch_volume(http, slug)
                    if not was_cached:
                        progress.advance(task)
    finally:
        http.close()

    spreads = [
        SetSpread(t, quotes[t.slug], {p.slug: quotes[p.slug] for p in t.parts}, volumes or None)
        for t in targets
    ]
    if args.sort == "name":
        spreads.sort(key=lambda s: s.target.name)
    else:
        key = SORTS[args.sort]
        spreads.sort(key=lambda s: (key(s) is None, -(key(s) or 0)))

    render(console, spreads, catalog, args)
    if args.csv:
        write_csv(args.csv, spreads)
        console.print(f"Wrote {args.csv}")
    return 0


def spread_table(title: str, spreads: list[SetSpread], with_volume: bool) -> Table:
    table = Table(title=title, header_style="bold", title_style="bold", title_justify="left")
    table.add_column("Set", min_width=18)
    table.add_column("Src")
    table.add_column("Set WTS", justify="right")
    table.add_column("Set WTB", justify="right")
    table.add_column("Spread", justify="right", style="dim")
    if with_volume:
        table.add_column("Set vol\n48h", justify="right")
    table.add_column("Parts\nWTS Σ", justify="right")
    table.add_column("Parts\nWTB Σ", justify="right")
    if with_volume:
        table.add_column("Min part\nvol 48h", justify="right")
    table.add_column("Δ WTS", justify="right")
    table.add_column("Δ WTB", justify="right")

    for s in spreads:
        row = [
            s.target.name.removesuffix(" Set"),
            source_label(s),
            fmt(s.set_quote.wts),
            fmt(s.set_quote.wtb),
            fmt(s.set_quote.spread),
        ]
        if with_volume:
            row.append(fmt_vol(s.set_volume.vol_48h if s.set_volume else None))
        row += [fmt_parts(s, "wts"), fmt_parts(s, "wtb")]
        if with_volume:
            row.append(fmt_vol(s.min_part_vol_48h))
        row += [fmt_delta(s.delta_wts), fmt_delta(s.delta_wtb)]
        table.add_row(*row)
    return table


def detail_table(s: SetSpread) -> Table:
    with_volume = s.volumes is not None
    detail = Table(title=s.target.name, title_justify="left", show_edge=False, header_style="bold")
    detail.add_column("Part")
    detail.add_column("Qty", justify="right")
    detail.add_column("WTS", justify="right")
    detail.add_column("WTB", justify="right")
    detail.add_column("Spread", justify="right")
    detail.add_column("Sellers/Buyers", justify="right", style="dim")
    if with_volume:
        detail.add_column("Vol 48h", justify="right")
        detail.add_column("Vol 7d", justify="right")
        detail.add_column("Avg sold 48h", justify="right", style="dim")

    def row(name: str, qty: str, slug: str, q: Quote) -> list[str]:
        cells = [name, qty, fmt(q.wts), fmt(q.wtb), fmt(q.spread), f"{q.sellers}/{q.buyers}"]
        if with_volume:
            v = s.volumes[slug]
            cells += [fmt_vol(v.vol_48h), str(v.vol_7d), fmt(v.avg_48h)]
        return cells

    for p in s.target.parts:
        detail.add_row(*row(p.name, str(p.qty), p.slug, s.part_quotes[p.slug]))
    detail.add_section()
    detail.add_row(*row("[bold]Set[/]", "", s.target.slug, s.set_quote))
    return detail


def render(console: Console, spreads: list[SetSpread], catalog, args: argparse.Namespace) -> None:
    now = datetime.now().astimezone()
    heading = f"Prime set spreads · {now:%Y-%m-%d %H:%M} · {args.platform}"
    if args.depth > 1:
        heading += f" · avg of top {args.depth}"
    console.print(Rule(heading, style="bold"))
    with_volume = not args.no_volume

    for kind, label in KINDS:
        group = [s for s in spreads if s.target.kind == kind]
        if not group:
            continue
        console.print()
        console.print(spread_table(f"{label} ({len(group)})", group, with_volume))
        if args.detail:
            for s in group:
                console.print()
                console.print(detail_table(s))

    notes = [
        "Δ WTS = set WTS − Σ part WTS  (+ → parts are cheaper to buy than the set; build and sell the set)",
        "Δ WTB = set WTB − Σ part WTB  (− → buyers pay more for loose parts; sell parts, not the set)",
        "Prices are the best orders from players currently in game. ≥N = some part has no in-game order on that side.",
        "Unv = parts drop from current relics · Unv* = some part isn't in a dropping relic · PR = Varzia's current stock.",
    ]
    if with_volume:
        notes.append(
            "Vol = trades closed on warframe.market (self-reported, so real volume is higher). "
            "Min part vol = least-traded part, the bottleneck when building or splitting a set."
        )
    if catalog.hidden:
        notes.append(
            "Hidden: permanently unvaulted / old primes, not in their first Prime Access cycle "
            f"(--all-unvaulted to show): {', '.join(catalog.hidden)}."
        )
    if catalog.pr_expiry:
        notes.append(f"Prime Resurgence rotation ends {catalog.pr_expiry.astimezone():%Y-%m-%d %H:%M}.")
    if catalog.drops_updated:
        notes.append(f"Drop tables last updated {catalog.drops_updated:%Y-%m-%d}.")
    console.print()
    console.print("\n".join(f"[dim]{n}[/]" for n in notes))


def write_csv(path: str, spreads: list[SetSpread]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["set", "type", "sources", "partial", "set_wts", "set_wtb", "set_vol_48h", "set_vol_7d",
                    "parts_wts", "parts_wtb", "min_part_vol_48h", "delta_wts", "delta_wtb"])
        for s in spreads:
            t = s.target
            v = s.set_volume
            w.writerow([t.name, t.kind, "+".join(sorted(t.sources)), t.partial, s.set_quote.wts,
                        s.set_quote.wtb, v and v.vol_48h, v and v.vol_7d, s.parts_wts, s.parts_wtb,
                        s.min_part_vol_48h, s.delta_wts, s.delta_wtb])


if __name__ == "__main__":
    sys.exit(main())
