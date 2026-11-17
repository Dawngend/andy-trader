# lpgb profile check, 2026-11-17

Paper only. Wallet `lpgb`, address `0x1f5f9fa318fbda8350ac143b4df657287df850fa`.

## Verdict: UNCLEAR

The two numbers of the rule fixed in advance:

- 50.0% of the recent buys are priced 0.80 or higher (1 of 2). The limit is 40%.
- The scout's `longshot_share` was 0.797. The limit is 0.40.

Read to the letter, both limits are met. The rule is not applied, and this needs a decision from the owner: the first
number is one buy out of two. With two buys the share can only be 0%, 50% or 100%, so it says nothing about what the
wallet buys. The last check had 162 buys. The same count over the latest 5000 rows (811 buys, see "Trend") gives 39.2%,
just under the limit.

No code was changed. `lpgb` is not in `PAUSED_WALLETS`. To apply the rule to the letter anyway, add `"lpgb"` to that set.

What the data does show is a different problem: `lpgb` stopped buying on 2026-11-08 17:25 UTC. It made 809 buys from
2026-11-06 18:57 to then, and 2 buys in the 8.4 days since. It was added to the copy book on 2026-11-15, a week after it
stopped. The copy book copies buys only, so paused or not there is nothing to copy.

## Data

- Scout history: newest `wallet_profiles` row for the address, run `8568eb30624b4fe69ea05a29adfd015a`,
  2026-11-16 15:13 UTC. 2000 closed positions closed from 2026-11-06 to 2026-11-16, 24.75% wins, return on cost 26.2%,
  label `longshot`, no flags.
- Recent buys: `PublicReader().get("v2/activity", user=<address>, limit=500)` fetched 2026-11-17 04:07 UTC.
  500 rows covering 84.8 hours (2026-11-13 14:46 to 2026-11-17 03:37 UTC): 2 buys, 296 sells, 201 redeems,
  1 conversion.
- Wider window, outside the rule: the same call with `max_pages=10`, fetched 2026-11-17 04:09 UTC. 5000 rows covering
  10.4 days (2026-11-06 18:52 to 2026-11-17 03:37 UTC): 811 buys, 3001 sells, 878 redeems, 232 conversions, 60 merges,
  18 other rows. 7 sell rows appear twice.
- Database opened read only.

## Share of entries by price

| Price | Scout history, 2000 closed positions | Recent buys, 2 |
|---|---|---|
| Under 0.30 | 79.7% (1594) | 50.0% (1) |
| 0.30 to 0.80 | 9.05% (181) | 0.0% (0) |
| 0.80 or higher | 11.25% (225) | 50.0% (1) |

Share of dollars spent at 0.80 or higher: scout history 39.7% (`high_entry_cost_share`), recent buys 82.5%
($20.69 of $25.07).

The two buys:

- 2026-11-14 00:04 UTC, No at 0.985, 21 shares, $20.69, "Will the Democratic Party candidate win the 2026 MO-01 House
  election by 70%-75%?". The 1 conversion in the window is on the same event 26 seconds later, also 21 shares.
- 2026-11-14 20:28 UTC, Yes at 0.086, 51 shares, $4.39, "Will Elon Musk post 300-319 tweets from November 10 to
  November 17, 2026?".

Two buys, two tokens, so grouping by token gives the same table.

## Trend

Neither recent buy is in the last 24 hours, and both are before the scout run (2026-11-16 15:13 UTC). The 46 rows
after the scout run are 30 sells and 16 redeems. The splits used in the last check have nothing to split.

Buys by day in the wider 5000-row window:

| Day (UTC) | Rows | Buys | Under 0.30 | 0.30 to 0.80 | 0.80 or higher |
|---|---|---|---|---|---|
| 2026-11-06, from 18:52 | 332 | 97 | 16 | 51 | 30 |
| 2026-11-07 | 1622 | 457 | 94 | 176 | 187 |
| 2026-11-08 | 1283 | 255 | 37 | 118 | 100 |
| 2026-11-09 to 2026-11-13 | 1360 | 0 | 0 | 0 | 0 |
| 2026-11-14 | 183 | 2 | 1 | 0 | 1 |
| 2026-11-15 to 2026-11-17 | 220 | 0 | 0 | 0 | 0 |
| All | 5000 | 811 | 18.2% (148) | 42.5% (345) | 39.2% (318) |

The last buy of the busy period is 2026-11-08 17:25 UTC. Since then the wallet only sells and redeems: 296 sells in the
500-row window, 290 of them priced under 0.05, $63 in total.

When it was buying, 46.9% of its buy dollars went in at 0.80 or higher ($24,314 of $51,814), average buy price 0.63,
65 buys at 0.95 or higher, 9 at 0.98 or higher.

## What the scout row already showed

- 79.7% of the 2000 closed positions were entered under 0.30. 11.25% were entered at 0.80 or higher, and they took
  39.7% of the dollars.
- Profit by price band, from the scout `price_bands` column:

| Entry price | Positions | Profit | Cost | Wins | Return on cost |
|---|---|---|---|---|---|
| Under 0.50 | 1652 | $45,832 | $88,283 | 17.3% | 51.9% |
| 0.50 to 0.80 | 123 | $23,633 | $87,699 | 80.5% | 26.9% |
| 0.80 to 0.95 | 166 | $7,743 | $86,121 | 56.6% | 9.0% |
| 0.95 to 0.99 | 53 | -$378 | $28,941 | 32.1% | -1.3% |
| 0.99 or higher | 6 | -$308 | $877 | 0.0% | -35.1% |
| All | 2000 | $76,522 | $291,921 | 24.75% | 26.2% |

- The positions at 0.80 or higher made $7,058 in total, 9.2% of the profit on 39.7% of the dollars. The positions
  under 0.80 made $69,465.
- The scout's own activity sample on 2026-11-16 had 2 buys, 1 of them at 0.95 or higher. That is the whole basis of
  `high_price_buy_share` 0.5. The two scout runs on 2026-11-14 saw 1 buy each (`high_price_buy_share` 1.0).
  `buys_per_hour` was 0.026.

So, as with `bigggggggg`, the profit came from the cheaper entries. Unlike `bigggggggg`, the scout history is mostly
cheap entries by count, and the dollars are spread evenly over the three bands under 0.95.

The scout history and the buys do not describe the same thing. The closed positions are 79.7% under 0.30; the 811 buys
are 18.2% under 0.30. 751 of the 811 buys are No, and 655 share an event and minute with another buy: the wallet buys No
on several outcomes of one event at once. Where the cheap closed positions come from was not checked.

## Live copy results

From `copy_book_positions` and `copy_book_skips`, read 2026-11-17 04:08 UTC.

| Item | Value |
|---|---|
| Copies opened | 0 |
| Settled | 0 |
| Open | 0 |
| Skipped `stale` | 1, on 2026-11-16: the Yes buy at 0.086 from 2026-11-14 20:28 UTC |

## What the data cannot show

- Two buys cannot give a share of buys by price. The 50.0% is one trade.
- No copy was opened, so there is no live result for this wallet at all.
- Whether `lpgb` will buy again. It bought 457 and 255 times on its two full days in the window, then stopped.
- The wider window has 1.9 days of buying (2026-11-06 18:57 to 2026-11-08 17:25 UTC). It says nothing about earlier
  days or about whether those days are typical. Its 39.2% is so close to the 40% limit that a few trades either way
  would move it across.
- The scout history is the latest 2000 closed positions (10 days), not the wallet's full history.
- Scout history counts closed positions at their average price; buys are single fills. Here the two differ a lot
  (79.7% against 18.2% under 0.30), so the `longshot` label does not describe what a copy of its buys would hold.
- The sells, the 232 conversions and the 60 merges in the wider window. The copy book holds to settlement and copies
  none of them. Not checked here.
- The wider window has 5 `MAKER_REBATE` rows, none in the latest 500. The scout labels a wallet `market_maker` when it
  sees any. Not checked here.

## Code change

None. `PAUSED_WALLETS` is still `{"sharky6999", "bigggggggg"}`. No wallet, sizing, or fill limit was touched.
