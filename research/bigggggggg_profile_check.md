# bigggggggg profile check, 2026-11-17

Paper only. Wallet `bigggggggg`, address `0x511f9c771449162349795d17becf8b37031acbe1`.

## Verdict: CHANGED

The rule fixed in advance fires:

- 69.1% of the recent buys are priced 0.80 or higher (112 of 162). The limit is 40%.
- The scout's `longshot_share` was 0.504. The limit is 0.40.

`bigggggggg` is added to `PAUSED_WALLETS` in `andy_trader/copy_book.py`. Its 34 open copies still settle and report.

One caution on the word CHANGED: the recent buys do not match the `longshot` label, but the scout data already
showed the high-price side (see "What the scout row already showed"). The label hid it; the wallet may not have moved.

## Data

- Scout history: newest `wallet_profiles` row for the address, run `8568eb30624b4fe69ea05a29adfd015a`,
  2026-11-16 15:13 UTC. 2000 closed positions closed from 2026-10-29 to 2026-11-16, 49.5% wins, return on cost 22.4%,
  label `longshot`, no flags.
- Recent buys: `PublicReader().get("v2/activity", user=<address>, limit=500)` fetched 2026-11-17 03:45 UTC.
  500 rows covering 17.4 hours (2026-11-16 10:09 to 2026-11-17 03:33 UTC): 162 buys, 277 sells, 44 conversions,
  12 redeems, 5 other rows.
- Database opened read only.

## Share of entries by price

| Price | Scout history, 2000 closed positions | Recent buys, 162 |
|---|---|---|
| Under 0.30 | 50.4% (1008) | 7.4% (12) |
| 0.30 to 0.80 | 12.0% (240) | 23.5% (38) |
| 0.80 or higher | 37.6% (752) | 69.1% (112) |

Share of dollars spent at 0.80 or higher: scout history 79.9% (`high_entry_cost_share`), recent buys 71.6%
($19,579 of $27,357).

Average recent buy price is 0.79. 65 of the 162 buys are at 0.98 or higher.

The scout counts positions and the activity feed counts fills, so one position can be several buys. Grouped by token,
the recent buys cover 67 tokens: 7 (10.4%) with an average price under 0.30, 13 (19.4%) from 0.30 to 0.80, and
47 (70.1%) at 0.80 or higher. Same picture.

## Trend

All 162 recent buys are in the last 24 hours (100%); none are older (0%). The 500 rows only reach back 17.4 hours, so
this split cannot show a trend.

Splitting the same buys at the scout run (2026-11-16 15:13 UTC) instead:

| Buys | Count | Under 0.30 | 0.80 or higher |
|---|---|---|---|
| Before the scout run | 95 | 8.4% (8) | 72.6% (69) |
| After the scout run | 67 | 6.0% (4) | 64.2% (43) |

No shift inside the window. The wallet was already buying mostly at high prices in the five hours before it was picked.

## What the scout row already showed

- 37.6% of the 2000 closed positions were entered at 0.80 or higher, and they took 79.9% of the dollars.
- Those high-price positions made $15,296 in total. The positions under 0.80 made $733,051. Positions at 0.99 or
  higher lost $93,352 with 9.9% wins.
- The scout's own activity sample on 2026-11-16 had 157 buys, 51.0% of them at 0.95 or higher
  (`high_price_buy_share`).

So the wallet's profit came from cheap entries, while most of its buying by count this past day, and most of its
dollars in the scout history, are high-price entries. The copy book copies every buy with the same 100 shares, so it
mostly copies the side that earned almost nothing.

## Live copy results

From `copy_book_positions` and `copy_book_skips`, read 2026-11-17 03:45 UTC.

| Item | Value |
|---|---|
| Copies opened | 34, from 2026-11-16 17:20 to 2026-11-17 02:51 UTC |
| Settled | 0 |
| Open | 34 |
| Average fill | 0.72 (their average price 0.69) |
| Fills under 0.30 | 3 |
| Fills 0.30 to 0.80 | 12 |
| Fills 0.80 or higher | 19 |
| Skipped `fill_too_high` (0.98 or higher) | 23 |
| Skipped `fill_too_low` | 1 |
| Cost of open copies | $2,447.88, fees $31.32 |

Of the 58 buys the copy book handled, 42 (72%) filled or would have filled at 0.80 or higher.

## What the data cannot show

- No copy has settled, so there is no live profit or loss for this wallet yet.
- The activity window is only the latest 500 rows, 17.4 hours. It says nothing about earlier days or about whether
  this day is typical.
- The scout history is the latest 2000 closed positions (18 days), not the wallet's full history.
- Scout history counts closed positions at their average price; recent buys are single fills. The comparison is close
  but not exact.
- 277 of the 500 rows are sells. The copy book holds to settlement, so it does not copy the wallet's exits. Not checked
  here.
- The window has 1 `MAKER_REBATE` row. The scout labels a wallet `market_maker` when it sees any. Not checked here.

## Code change

`PAUSED_WALLETS = {"sharky6999", "bigggggggg"}` in `andy_trader/copy_book.py`, with a dated comment. No other wallet,
sizing, or fill limit was touched. To resume, remove the name from the set.
