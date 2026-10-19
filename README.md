# warframe-set-arbitrage

Quick check of **set vs. parts** spreads on [warframe.market](https://warframe.market) for the primes that are
actually farmable right now:

- **Unvaulted**: primes whose parts drop from relics in the current official drop tables.
- **Prime Resurgence (PR)**: primes in Varzia's current rotation.

Vaulted primes are skipped. Unvaulted primes are only shown during their first Prime Access cycle, which covers
warframes that have never been vaulted and the weapons released with them. That hides the permanent relic pool (Braton,
Lex, Paris Prime, ...) and the permanently unvaulted Valkyr and Nyx Prime with their weapons. Pass `--all-unvaulted` to
show everything that drops. Warframes and weapons are shown in separate tables.

## Dashboard

**Live: https://warframe-set-arbitrage.vercel.app**

The web dashboard has two tabs: prime sets and primed mods around Baro's visits. It has sortable tables, per-set part
breakdowns, and 90-day price charts for each mod. Run it locally with:

```bash
uv run wfspread web                    # http://127.0.0.1:8000, opens a browser tab
```

It's a static page ([static/index.html](src/warframe_set_arbitrage/static/index.html)) plus two JSON endpoints,
`/api/sets` and `/api/mods` ([web.py](src/warframe_set_arbitrage/web.py)). Both take `platform`, `depth` (1–5) and
`all=1`. On Vercel they run as Python functions ([api/](api/)), and the CDN caches each response for 10 minutes, so
warframe.market is hit at most once per window. A cold fetch takes about 20 s for mods and about 1 min for sets.
The Vercel project is linked to this GitHub repo, so pushing to `main` deploys to production.

## Run

```bash
uv run wfspread                        # short alias for: uv run warframe-set-arbitrage
```

warframe.market allows about 3 requests/second, and each set and each of its parts needs one request for orders and
one for volume. The first full run of the hour takes about 2 min. Volume is cached for an hour, so later runs take about
1 min. Narrow the run, or add `--no-volume`, to make it faster:

```bash
uv run wfspread --pr                   # only Prime Resurgence (same as --source pr)
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

## Primed mods around Baro's visits

When Baro Ki'Teer brings a primed mod, its price dips. If the mod has steady demand, buying it then and selling a
couple of months after he leaves usually pays. `wfspread mods` tracks a watchlist of those mods (Continuity, Flow,
Reach, Fever Strike, Target Cracker, Pressure Point, ...) plus any primed mod in Baro's current stock:

```bash
uv run wfspread mods                   # watchlist, deepest dip first
uv run wfspread mods --all             # every primed mod
uv run wfspread mods --last-baro       # only what Baro brought on his last visit
uv run wfspread mods --sort endo       # best max-rank premium per endo
uv run wfspread mods -f flow --csv mods.csv
```

| Column | Meaning |
| --- | --- |
| Baro | `here` = in Baro's current stock |
| Last Baro | When Baro last brought the mod, from the wiki's visit history |
| R0 WTS / WTB | Cheapest in-game seller / highest in-game buyer at rank 0 |
| R0 90d median | Median of the daily median rank 0 prices over 90 days: the usual price |
| Dip | R0 WTS vs the 90d median. **−** → cheaper than usual, which is the buy signal |
| % from 90d avg | R0 WTS vs the volume-weighted average sold price over 90 days. 90 days is the longest history warframe.market keeps; heavy trading in a Baro crash pulls the average down more than the median |
| Max WTS / WTB | Same for max rank |
| Max premium | Max WTS − R0 WTS: what maxing adds when you list it |
| p / 1k endo | Max premium per 1,000 endo. A primed mod takes 40,920 endo and 1,976,436 credits to max |

The heading line says when Baro arrives or leaves. A full run is 2 order requests per mod plus 1 stats request
(stats are cached for 1 h), so about 20 s for the watchlist.

## Data sources

- Orders: `api.warframe.market/v2/orders/item/{slug}/top`. These are the top 5 buy/sell orders from players who are in game.
- Volume: `api.warframe.market/v1/items/{slug}/statistics` (closed-trade stats), cached for 1 h.
- Set composition: `api.warframe.market/v2/item/{slug}/set`, cached for 7 days.
- Unvaulted relics: `drops.warframestat.us/data/all.json` (DE's official drop tables), cached for 12 h.
- Release/vault history: `api.warframestat.us/warframes` and `/weapons` (WFCD), cached for 7 days. Weapons are matched
  to frames by release date.
- Varzia stock: `api.warframestat.us/pc/vaultTrader`, cached for 1 h.
- Baro stock and schedule: `api.warframestat.us/pc/voidTrader`, cached for 1 h (refetched once he arrives).
- Baro's past visits: the wiki's [Module:Baro/data](https://wiki.warframe.com/w/Module:Baro/data) offering dates, cached for 6 h.
- Mod prices by rank: the same orders endpoint with `?rank=0` / `?rank={maxRank}`, and per-rank series from the stats endpoint.

Metadata is cached in `~/.cache/warframe-set-arbitrage`. You can override that path with `WFSPREAD_CACHE`. Pass `--refresh` to refetch it.
Order prices are never cached.
