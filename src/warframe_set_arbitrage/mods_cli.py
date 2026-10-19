from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone

from rich.console import Console
from rich.rule import Rule
from rich.table import Table

from .cli import fetch_progress, fmt, fmt_delta, fmt_vol
from .http import Http
from .mods import Baro, ModSpread, baro_status, fetch_mod_spreads, primed_mods

SORTS = {
    "dip": lambda s: -s.dip if s.dip is not None else None,  # deepest dip first
    "premium": lambda s: s.premium,
    "endo": lambda s: s.plat_per_kendo,
    "r0-wts": lambda s: s.r0.wts,
    "volume": lambda s: s.rank_stats(0).vol_48h if s.rank_stats(0) else None,
}


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="wfspread mods",
        description="Primed mods around Baro's visits: rank 0 dip vs the 90-day median, and the max-rank premium.",
    )
    p.add_argument("--all", dest="all_mods", action="store_true",
                   help="every primed mod, not just the steady-demand watchlist")
    p.add_argument("-f", "--filter", help="only mods whose name contains this text")
    p.add_argument("--last-baro", action="store_true", help="only primed mods Baro brought on his last visit")
    p.add_argument("--sort", choices=["name", *SORTS], default="dip")
    p.add_argument("--depth", type=int, default=1, help="average the best N orders per side (default 1)")
    p.add_argument("--no-volume", action="store_true", help="skip trade stats (no dip or volume columns)")
    p.add_argument("--csv", metavar="PATH", help="also write the table to a CSV file")
    p.add_argument("--rps", type=float, default=3.0, help="warframe.market requests per second (default 3)")
    p.add_argument("--platform", default="pc", choices=["pc", "ps4", "xbox", "switch", "mobile"])
    p.add_argument("--refresh", action="store_true", help="ignore cached item lists / Baro / stats")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    console = Console()
    http = Http(rps=args.rps, refresh=args.refresh, platform=args.platform)
    try:
        with console.status("Loading Baro's stock and primed mods…"):
            baro = baro_status(http)
            mods = primed_mods(http, baro, all_mods=args.all_mods)
        if args.filter:
            mods = [m for m in mods if args.filter.lower() in m.name.lower()]
        if args.last_baro:
            mods = [m for m in mods if m.in_last_baro]
        if not mods:
            console.print("[yellow]No primed mods match those filters.[/]")
            return 1

        with fetch_progress(console) as on_progress:
            spreads = fetch_mod_spreads(http, mods, args.depth, not args.no_volume, on_progress)
    finally:
        http.close()

    if args.sort == "name":
        spreads.sort(key=lambda s: s.mod.name)
    else:
        key = SORTS[args.sort]
        spreads.sort(key=lambda s: (key(s) is None, -(key(s) or 0)))

    render(console, spreads, baro, args)
    if args.csv:
        write_csv(args.csv, spreads)
        console.print(f"Wrote {args.csv}")
    return 0


def fmt_dip(v: float | None) -> str:
    if v is None:
        return "[dim]—[/]"
    color = "green" if v <= -10 else "red" if v >= 10 else "dim"
    return f"[{color}]{v:+.0f}%[/]"


def mod_table(spreads: list[ModSpread], with_stats: bool) -> Table:
    table = Table(header_style="bold")
    table.add_column("Mod", min_width=18)
    table.add_column("Baro")
    table.add_column("Last\nBaro", style="dim")
    table.add_column("R0 WTS", justify="right")
    table.add_column("R0 WTB", justify="right")
    if with_stats:
        table.add_column("R0 90d\nmedian", justify="right", style="dim")
        table.add_column("Dip", justify="right")
        table.add_column("vs 90d\navg", justify="right")
        table.add_column("R0 vol\n48h", justify="right")
    table.add_column("Max WTS", justify="right")
    table.add_column("Max WTB", justify="right")
    if with_stats:
        table.add_column("Max vol\n48h", justify="right")
    table.add_column("Max\npremium", justify="right")
    table.add_column("p / 1k\nendo", justify="right")

    for s in spreads:
        r0, rmax = s.rank_stats(0), s.rank_stats(s.mod.max_rank)
        row = [
            s.mod.name.removeprefix("Primed "),
            "[yellow]here[/]" if s.mod.in_baro else "",
            s.mod.last_baro or "—",
            fmt(s.r0.wts),
            fmt(s.r0.wtb),
        ]
        if with_stats:
            row += [fmt(r0 and r0.median_90d), fmt_dip(s.dip), fmt_dip(s.vs_avg), fmt_vol(r0 and r0.vol_48h)]
        row += [fmt(s.rmax.wts), fmt(s.rmax.wtb)]
        if with_stats:
            row.append(fmt_vol(rmax and rmax.vol_48h))
        row += [fmt_delta(s.premium), fmt(s.plat_per_kendo)]
        table.add_row(*row)
    return table


def baro_line(baro: Baro) -> str:
    now = datetime.now(timezone.utc)
    if baro.active and baro.expiry:
        days = (baro.expiry - now).total_seconds() / 86400
        primed = sorted(i for i in baro.items if i.startswith("Primed "))
        stock = f" Primed mods in stock: {', '.join(primed)}." if primed else " No primed mods in stock."
        return (f"[yellow]Baro is at {baro.location} until {baro.expiry.astimezone():%Y-%m-%d %H:%M} "
                f"({days:.1f}d left).[/]{stock}")
    if baro.activation and baro.activation > now:
        days = (baro.activation - now).total_seconds() / 86400
        return (f"Baro arrives at {baro.location} on {baro.activation.astimezone():%Y-%m-%d %H:%M} "
                f"(in {days:.1f}d).")
    return "Baro's next visit is unknown."


def last_visit_line(baro: Baro) -> str | None:
    if not baro.last_visit:
        return None
    primed = sorted(baro.last_visit_items)
    return f"Baro's last visit ({baro.last_visit}) brought: {', '.join(primed) if primed else 'no primed mods'}."


def render(console: Console, spreads: list[ModSpread], baro: Baro, args: argparse.Namespace) -> None:
    now = datetime.now().astimezone()
    heading = f"Primed mods · {now:%Y-%m-%d %H:%M} · {args.platform}"
    if args.depth > 1:
        heading += f" · avg of top {args.depth}"
    console.print(Rule(heading, style="bold"))
    console.print(baro_line(baro))
    if line := last_visit_line(baro):
        console.print(f"[dim]{line}[/]")
    console.print()
    with_stats = not args.no_volume
    console.print(mod_table(spreads, with_stats))

    sample = spreads[0].mod
    notes = []
    if with_stats:
        notes.append("Dip = live R0 WTS vs the 90-day median of daily medians (− → cheaper than usual; "
                     "buy during Baro's visit, sell once it recovers).")
    notes += [
        "Max premium = max rank WTS − R0 WTS: what maxing adds when you list it.",
        f"Maxing a {sample.rarity} mod to rank {sample.max_rank} costs {sample.endo_to_max:,} endo and "
        f"{sample.credits_to_max:,} credits. p / 1k endo = premium per 1,000 endo (credits not counted).",
        "Prices are the best orders from players currently in game. Baro = in his current stock.",
    ]
    if with_stats:
        notes.append("Vol = trades closed on warframe.market in 48h (self-reported, so real volume is higher).")
    console.print()
    console.print("\n".join(f"[dim]{n}[/]" for n in notes))


def write_csv(path: str, spreads: list[ModSpread]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["mod", "max_rank", "in_baro", "last_baro", "baro_visits", "r0_wts", "r0_wtb", "r0_median_90d", "r0_low_90d", "r0_high_90d",
                    "dip_pct", "r0_avg_90d", "vs_avg_pct", "r0_vol_48h", "max_wts", "max_wtb", "max_vol_48h", "premium", "endo_to_max",
                    "credits_to_max", "plat_per_1k_endo"])
        for s in spreads:
            r0, rmax = s.rank_stats(0), s.rank_stats(s.mod.max_rank)
            w.writerow([s.mod.name, s.mod.max_rank, s.mod.in_baro, s.mod.last_baro, s.mod.baro_visits, s.r0.wts, s.r0.wtb, r0 and r0.median_90d,
                        r0 and r0.low_90d, r0 and r0.high_90d, s.dip, r0 and r0.avg_90d, s.vs_avg, r0 and r0.vol_48h, s.rmax.wts, s.rmax.wtb,
                        rmax and rmax.vol_48h, s.premium, s.mod.endo_to_max, s.mod.credits_to_max,
                        s.plat_per_kendo])
