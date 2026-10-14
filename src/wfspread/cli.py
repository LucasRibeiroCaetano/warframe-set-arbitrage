from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn
from rich.table import Table

from .catalog import RESURGENCE, UNVAULTED, build_catalog
from .http import Http
from .pricing import Quote, SetSpread, fetch_quote

SORTS = {
    "delta-wts": lambda s: s.delta_wts,
    "delta-wtb": lambda s: s.delta_wtb,
    "flip": lambda s: s.flip,
    "set-wts": lambda s: s.set_quote.wts,
    "set-spread": lambda s: s.set_quote.spread,
}


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
    p.add_argument("--no-partial", action="store_true", help="skip unvaulted sets missing a dropping part")
    p.add_argument("-d", "--detail", action="store_true", help="show a per-part breakdown for each set")
    p.add_argument("--csv", metavar="PATH", help="also write the per-set table to a CSV file")
    p.add_argument("--rps", type=float, default=3.0, help="warframe.market requests per second (default 3)")
    p.add_argument("--platform", default="pc", choices=["pc", "ps4", "xbox", "switch", "mobile"])
    p.add_argument("--refresh", action="store_true", help="ignore cached drop tables / item lists")
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
            catalog = build_catalog(http)

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
        quotes: dict[str, Quote] = {}
        with Progress(
            TextColumn("Fetching orders"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("orders", total=len(slugs))
            for slug in slugs:
                quotes[slug] = fetch_quote(http, slug, depth=args.depth)
                progress.advance(task)
    finally:
        http.close()

    spreads = [SetSpread(t, quotes[t.slug], {p.slug: quotes[p.slug] for p in t.parts}) for t in targets]
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


def render(console: Console, spreads: list[SetSpread], catalog, args: argparse.Namespace) -> None:
    now = datetime.now().astimezone()
    title = f"Prime set spreads · {now:%Y-%m-%d %H:%M} · {args.platform}"
    if args.depth > 1:
        title += f" · avg of top {args.depth}"
    table = Table(title=title, header_style="bold", title_style="bold")
    table.add_column("Set")
    table.add_column("Src")
    table.add_column("Set WTS", justify="right")
    table.add_column("Set WTB", justify="right")
    table.add_column("Spread", justify="right", style="dim")
    table.add_column("Parts WTS Σ", justify="right")
    table.add_column("Parts WTB Σ", justify="right")
    table.add_column("Δ WTS", justify="right")
    table.add_column("Δ WTB", justify="right")
    table.add_column("Flip", justify="right")

    last_kind = None
    for s in spreads:
        if args.sort == "name" and last_kind and s.target.kind != last_kind:
            table.add_section()
        last_kind = s.target.kind
        table.add_row(
            s.target.name.removesuffix(" Set"),
            source_label(s),
            fmt(s.set_quote.wts),
            fmt(s.set_quote.wtb),
            fmt(s.set_quote.spread),
            fmt_parts(s, "wts"),
            fmt_parts(s, "wtb"),
            fmt_delta(s.delta_wts),
            fmt_delta(s.delta_wtb),
            fmt_delta(s.flip),
        )
    console.print(table)

    notes = [
        "Δ WTS = set WTS − Σ part WTS  (+ → parts are cheaper to buy than the set; build and sell the set)",
        "Δ WTB = set WTB − Σ part WTB  (− → buyers pay more for loose parts; sell parts, not the set)",
        "Flip  = set WTB − Σ part WTS  (+ → instant profit buying parts and selling the set to a buyer)",
        "Prices are the best orders from players currently in game. ≥N = some part has no in-game order on that side.",
        "Unv = parts drop from current relics · Unv* = some part isn't in a dropping relic · PR = Varzia's current stock.",
    ]
    if catalog.pr_expiry:
        notes.append(f"Prime Resurgence rotation ends {catalog.pr_expiry.astimezone():%Y-%m-%d %H:%M}.")
    if catalog.drops_updated:
        notes.append(f"Drop tables last updated {catalog.drops_updated:%Y-%m-%d}.")
    console.print("\n".join(f"[dim]{n}[/]" for n in notes))

    if args.detail:
        for s in spreads:
            detail = Table(title=s.target.name, title_justify="left", show_edge=False, header_style="bold")
            detail.add_column("Part")
            detail.add_column("Qty", justify="right")
            detail.add_column("WTS", justify="right")
            detail.add_column("WTB", justify="right")
            detail.add_column("Spread", justify="right")
            detail.add_column("Sellers/Buyers", justify="right", style="dim")
            for p in s.target.parts:
                q = s.part_quotes[p.slug]
                detail.add_row(p.name, str(p.qty), fmt(q.wts), fmt(q.wtb), fmt(q.spread), f"{q.sellers}/{q.buyers}")
            q = s.set_quote
            detail.add_section()
            detail.add_row("[bold]Set[/]", "", fmt(q.wts), fmt(q.wtb), fmt(q.spread), f"{q.sellers}/{q.buyers}")
            console.print()
            console.print(detail)


def write_csv(path: str, spreads: list[SetSpread]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["set", "type", "sources", "partial", "set_wts", "set_wtb", "parts_wts", "parts_wtb",
                    "delta_wts", "delta_wtb", "flip"])
        for s in spreads:
            t = s.target
            w.writerow([t.name, t.kind, "+".join(sorted(t.sources)), t.partial, s.set_quote.wts,
                        s.set_quote.wtb, s.parts_wts, s.parts_wtb, s.delta_wts, s.delta_wtb, s.flip])


if __name__ == "__main__":
    sys.exit(main())
