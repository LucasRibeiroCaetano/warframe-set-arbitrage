# wfspread

Quick check of **set vs. parts** spreads on [warframe.market](https://warframe.market) for the primes that are
actually farmable right now:

- **Unvaulted**: primes whose parts drop from relics in the current official drop tables.
- **Prime Resurgence (PR)**: primes in Varzia's current rotation.

Vaulted primes are skipped. Unvaulted weapons are only shown when they came out in the same Prime Access as an
unvaulted warframe that hasn't been vaulted yet. That hides permanent-pool weapons like Braton, Lex and Paris Prime. It
also hides old weapons that came back with a re-released frame, such as Cernos and Venka with Valkyr Prime. Pass
`--all-weapons` to show every unvaulted weapon. Warframes and weapons are shown in separate tables.

## Run

```bash
uv run wfspread
```

warframe.market allows about 3 requests/second, and each set and each of its parts needs one request for orders and
one for volume. The first full run of the hour takes about 2 min. Volume is cached for an hour, so later runs take about
1 min. Narrow the run, or add `--no-volume`, to make it faster:

```bash
uv run wfspread --source pr            # only Prime Resurgence
uv run wfspread --type frames          # only warframes
uv run wfspread -f ivara -d            # one set, with per-part breakdown
uv run wfspread --depth 3              # average top 3 orders (ignores bait listings)
uv run wfspread --sort delta-wtb --csv spreads.csv
uv run wfspread --sort volume          # most-traded sets first
```

## Columns

| Column | Meaning |
| --- | --- |
| Set WTS / WTB | Cheapest in-game seller / highest in-game buyer for the full set |
| Spread | Set WTS − Set WTB |
| Parts WTS Σ / WTB Σ | Sum over parts (× quantity in set) of cheapest seller / highest buyer. `≥N` = some part has no in-game order on that side |
| Set vol 48h | Set trades closed on warframe.market in the last 48h |
| Min part vol 48h | 48h volume of the least-traded part, which is the bottleneck when you build or split a set |
| Δ WTS | Set WTS − Parts WTS Σ. **+** → buying parts is cheaper than buying the set (build and sell the set) |
| Δ WTB | Set WTB − Parts WTB Σ. **−** → buyers pay more for loose parts than for the set |

Src: `PR` is Varzia's current stock, `Unv` is fully dropping, and `Unv*` means a part isn't in any currently dropping relic.

Volume shown in yellow is under 5 trades in 48h; red means 0. Volume counts trades that players reported as closed on
the site, so real volume is higher. `-d` also shows per-part 48h and 7d volume and the average sold price over the last 48h.

## Data sources

- Orders: `api.warframe.market/v2/orders/item/{slug}/top`. These are the top 5 buy/sell orders from players who are in game.
- Volume: `api.warframe.market/v1/items/{slug}/statistics` (closed-trade stats), cached for 1 h.
- Set composition: `api.warframe.market/v2/item/{slug}/set`, cached for 7 days.
- Unvaulted relics: `drops.warframestat.us/data/all.json` (DE's official drop tables), cached for 12 h.
- Release/vault history: `api.warframestat.us/warframes` and `/weapons` (WFCD), cached for 7 days. Weapons are matched
  to frames by release date.
- Varzia stock: `api.warframestat.us/pc/vaultTrader`, cached for 1 h.

Metadata is cached in `~/.cache/wfspread`. You can override that path with `WFSPREAD_CACHE`. Pass `--refresh` to refetch it.
Order prices are never cached.
