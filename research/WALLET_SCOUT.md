# Wallet Scout

Run `python run_wallet_scout.py --top 50 --period MONTH --max-pages 40`.
Optional `--database PATH` uses a separate SQLite file; otherwise the existing
store default applies. `--report PATH` defaults to `research/wallet_scout_report.md`.
The runner only reads public APIs and writes local profiles and the report.
It does not trade, change books or schedules, delete files, lock, or commit files.

Live checks on 2026-10-27 confirmed all three data sources. The preferred
`https://data-api.polymarket.com/v2/leaderboard` returns a `data` list with
`rank`, `user_id`, `user_name`, `volume`, and `pnl`. V1 also works but uses
`proxyWallet`, `userName`, and `vol`. Closed positions return `avgPrice`,
`totalBought`, `realizedPnl`, `title`, `outcome`, and Unix-second `timestamp`.
Activity returns `type`, `side` on trades, `price`, `usdcSize`, and `timestamp`.
The runner uses the project's HTTP helper and waits 0.3 seconds before every
request, including retries. Three failures across an account's requests stop
that account; its failure is stored and the next account proceeds. Recovered
failures are also stored. Exhausted accounts do not get partial profiles.
A leaderboard failure stops the run because no account list is available.

## Report Columns

| Column | Meaning |
|---|---|
| User Name | Public name and shortened wallet address; ordered by leaderboard rank. |
| Label | First matching strategy rule below. |
| Flags | Sample warnings and observed trading patterns below. |
| Sample Size | Number of sampled closed positions. |
| Win Rate | Positions with positive realized PnL divided by all positions; zero PnL is not a win. |
| Return On Cost | Sample realized PnL divided by total position cost (`totalBought * avgPrice`). |
| Loss To Win Ratio | Absolute average loss divided by average win. |
| Worst Loss | Most negative position PnL, or zero if there are no losses. |
| First Half PnL | PnL before the midpoint between the earliest and latest closed timestamps. |
| Second Half PnL | PnL at or after that midpoint. Equal timestamps all enter this half. |

Each account's closed-position and activity windows are printed in UTC below
the table. Missing windows and undefined ratios are `N/A` (SQL NULL), including
buy rates when the activity timestamps have no elapsed span.

## Stored Profile Numbers

`wallet_profiles` stores one row per run ID and address, with run time, leaderboard
period, rank, PnL and volume, and all report numbers. Average wins are positive;
average losses are negative. Median and 90th percentile dollar sizes use position
cost and linear interpolation between sorted values. `price_bands` and
`market_types` are JSON objects containing count, PnL, cost and win rate per group.
Bands are lower-inclusive and upper-exclusive at 0.50, 0.80, 0.95 and 0.99.
Title rules are case-insensitive, first match wins: up/down or up or down;
dip/reach/hit; above/below; versus/vs.; other.

Activity fields include count, first/last timestamps, buy count, share of TRADE
BUY rows priced at least 0.95 (by count), and buys divided by elapsed hours across
all activity rows. Additional shares store the exact-0.5 position count, SPLIT
and MERGE activity count, cost at entries at least 0.80, positions below 0.30,
and price-target positions at entries at least 0.80. Maker rebate count is stored.
Flags are a JSON list. `wallet_scout_failures` stores run ID, address and reason.
New runs append rows without updating old ones. Overlapping runs have separate
UUIDs; the shared report path shows the most recent write. Use distinct report
paths to retain separate reports.

## Rules And Limits

Flags: `inventory_heavy` means at least 20% of positions have avgPrice exactly
0.5, or at least 10% of activity is SPLIT/MERGE; `bot_like` means more than 20
buys/hour; `thin_sample` means fewer than 200 closed positions;
`one_sided_period` means PnL is positive in exactly one time half.

Labels, in order: `market_maker` for inventory-heavy or any MAKER_REBATE;
`bond` for at least 60% of cost at entries >=0.80 and win rate >=90%; `longshot`
for at least 40% of positions below 0.30; `price_target_no` for at least 40%
of positions in price-target titles at entries >=0.80; otherwise `mixed`.
The price_target_no rule does not check the outcome name, as specified.

Closed positions are only the latest rows, so lifetime PnL can be much larger
than the sample's. Paging stops at an empty page, max-pages (50 rows per page),
or 10,000 rows. The leaderboard period does not filter closed positions or
activity. Activity is only the latest 500 rows. No open-position risk, fees,
deposits, or unsampled history is reconstructed. API pagination can shift while
markets close. Title keywords can misclassify markets, and inventory bookkeeping
can look like directional wins. Labels and flags are heuristics, not evidence of
automation or a recommendation to copy an account. Choosing strategies for paper
books remains a later manual decision. Generated reports are local artifacts;
the tool never stages or commits them.
