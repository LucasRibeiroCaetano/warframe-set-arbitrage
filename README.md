# wfspread

Quick check of **set vs. parts** spreads on [warframe.market](https://warframe.market) for the primes that are
actually farmable right now:

- **Unvaulted**: primes whose parts drop from relics in the current official drop tables.
- **Prime Resurgence (PR)**: primes in Varzia's current rotation.

Vaulted primes are skipped. Covers warframes and weapons.

## Run

```bash
uv run wfspread
```

A full run takes about 1–1.5 min, because warframe.market allows about 3 requests/second and each set plus each
of its parts needs one request. Narrow the run to make it faster:

```bash
uv run wfspread --source pr            # only Prime Resurgence
uv run wfspread --type frames          # only warframes
uv run wfspread -f ivara -d            # one set, with per-part breakdown
uv run wfspread --depth 3              # average top 3 orders (ignores bait listings)
uv run wfspread --sort flip --csv spreads.csv
```

## Columns

| Column | Meaning |
| --- | --- |
| Set WTS / WTB | Cheapest in-game seller / highest in-game buyer for the full set |
| Spread | Set WTS − Set WTB |
| Parts WTS Σ / WTB Σ | Sum over parts (× quantity in set) of cheapest seller / highest buyer. `≥N` = some part has no in-game order on that side |
| Δ WTS | Set WTS − Parts WTS Σ. **+** → buying parts is cheaper than buying the set (build and sell the set) |
| Δ WTB | Set WTB − Parts WTB Σ. **−** → buyers pay more for loose parts than for the set |
| Flip | Set WTB − Parts WTS Σ. **+** → instant profit: buy every part from sellers, sell the set to the top buyer |

Src: `PR` is Varzia's current stock, `Unv` is fully dropping, and `Unv*` means a part isn't in any currently dropping relic.

## Data sources

- Orders: `api.warframe.market/v2/orders/item/{slug}/top`. These are the top 5 buy/sell orders from players who are in game.
- Set composition: `api.warframe.market/v2/item/{slug}/set`, cached for 7 days.
- Unvaulted relics: `drops.warframestat.us/data/all.json` (DE's official drop tables), cached for 12 h.
- Varzia stock: `api.warframestat.us/pc/vaultTrader`, cached for 1 h.

Metadata is cached in `~/.cache/wfspread`. You can override that path with `WFSPREAD_CACHE`. Pass `--refresh` to refetch it.
Order prices are never cached.
