# Copy candidates price mix check, 2026-11-17

Paper only. The three copy candidates from scout run `8568eb30624b4fe69ea05a29adfd015a` (2026-11-16 15:13 UTC):

- `0x4488`: no user name, address `0x448861155279dbf833d041b963e3ac854599e319`
- `paddaa`: address `0x60a92c8620846d81f5ea17b0564e0d4b7c545a71`
- `UnderperformingForALiving`: address `0xdbf16bcffa41bd95faec2640daa2c95ab2bd86b5`

## Verdicts

- `0x4488`: **ADD**. 430 recent buys, newest 4.3 hours old, 22.6% priced 0.80 or higher, scout return on cost 0.111.
- `paddaa`: **ADD**. 296 recent buys, newest 4 minutes old, 23.6% priced 0.80 or higher, scout return on cost 0.057.
- `UnderperformingForALiving`: **SKIP**. 70 recent buys, newest 8.2 hours old, 15.7% priced 0.80 or higher, but scout
  return on cost is 0.009. The minimum is 0.05.

## Rule

Fixed before looking at the data. A wallet is ADD only when all of these hold:

- at least 20 buys in the activity window;
- the newest buy is less than 3 days old;
- fewer than 40% of the recent buys are priced 0.80 or higher;
- scout `return_on_cost` is at least 0.05.

Fewer than 20 buys, or no buy in the last 3 days, is UNCLEAR. Anything else is SKIP.

## Data

- Scout history: the `wallet_profiles` rows of the run above, the newest run in the table. Database opened read only.
- Recent buys: `PublicReader().get("v2/activity", user=<address>, limit=500)`, fetched 2026-11-17 04:48 UTC, 500 rows
  per wallet.
  - `0x4488`: 78.6 hours (2026-11-13 18:08 to 2026-11-17 00:45 UTC). 430 buys, 7 sells, 51 redeems, 12 rebate and
    reward rows.
  - `paddaa`: 81.3 hours (2026-11-13 19:25 to 2026-11-17 04:46 UTC). 296 buys, 171 sells, 10 redeems, 6 merges,
    17 rebate, reward and yield rows.
  - `UnderperformingForALiving`: 329.1 hours (2026-11-03 11:08 to 2026-11-17 04:16 UTC). 70 buys, 363 sells,
    16 redeems, 6 merges, 3 splits, 1 conversion, 41 rebate, reward and yield rows.

## The three wallets side by side

| Item | `0x4488` | `paddaa` | `UnderperformingForALiving` |
|---|---|---|---|
| BUY trades in the window | 430 | 296 | 70 |
| Recent buys under 0.30 | 2.6% (11) | 35.5% (105) | 50.0% (35) |
| Recent buys 0.30 to 0.80 | 74.9% (322) | 40.9% (121) | 34.3% (24) |
| Recent buys 0.80 or higher | 22.6% (97) | 23.6% (70) | 15.7% (11) |
| Buy dollars at 0.80 or higher | 43.9% ($150,703 of $343,358) | 20.5% ($21,457 of $104,495) | 27.6% ($77,304 of $280,444) |
| Newest buy (UTC) | 2026-11-17 00:30 | 2026-11-17 04:44 | 2026-11-16 20:38 |
| Age of newest buy at fetch | 4.3 hours | 4 minutes | 8.2 hours |
| Buys per hour over the window | 5.47 | 3.64 | 0.21 |
| Scout closed positions | 2000 | 2000 | 373 |
| Scout positions under 0.30 | 15.7% (313) | 38.6% (771) | 21.2% (79) |
| Scout positions 0.30 to 0.80 | 72.9% (1458) | 37.0% (739) | 62.7% (234) |
| Scout positions 0.80 or higher | 11.5% (229) | 24.5% (490) | 16.1% (60) |
| Scout dollars at 0.80 or higher (`high_entry_cost_share`) | 39.3% | 40.0% | 19.4% |
| Scout win rate | 52.2% | 57.9% | 38.6% |
| Scout `return_on_cost` | 0.111 | 0.057 | 0.009 |
| Verdict | ADD | ADD | SKIP |

Buys per hour is buys divided by the hours between the oldest and newest of the 500 rows, as the scout computes it.
The scout's own sample on 2026-11-16 gave 4.31, 3.85 and 0.21.

The scout counts positions and the activity feed counts fills, so one position can be several buys. Grouped by token
at the average buy price:

| Tokens | `0x4488`, 62 | `paddaa`, 56 | `UnderperformingForALiving`, 27 |
|---|---|---|---|
| Under 0.30 | 9.7% (6) | 46.4% (26) | 29.6% (8) |
| 0.30 to 0.80 | 77.4% (48) | 35.7% (20) | 44.4% (12) |
| 0.80 or higher | 12.9% (8) | 17.9% (10) | 25.9% (7) |

Recent buys and scout history agree for `paddaa`. `0x4488` has twice the scout share at 0.80 or higher by buy count
(22.6% against 11.5%) but about the same by token (12.9%). `UnderperformingForALiving` has more cheap buys than its
history (50.0% against 21.2% under 0.30), on 70 buys.

## Profit by entry price, from the scout `price_bands` column

Each cell is profit, then positions, then return on cost.

| Entry price | `0x4488` | `paddaa` | `UnderperformingForALiving` |
|---|---|---|---|
| Under 0.50 | $286,137, 1020, 19.1% | $280,245, 1064, 22.9% | $130,844, 169, 3.4% |
| 0.50 to 0.80 | $387,212, 751, 12.8% | $123,448, 446, 5.5% | $638,998, 144, 1.8% |
| 0.80 to 0.95 | $152,884, 50, 10.9% | -$33,855, 357, -1.9% | -$292,692, 37, -5.8% |
| 0.95 to 0.99 | -$4,552, 16, -2.7% | -$14,989, 100, -5.4% | $632, 13, 0.2% |
| 0.99 or higher | $3,225, 163, 0.2% | -$22,122, 33, -9.7% | -$18,472, 10, -0.4% |
| All under 0.80 | $673,350, 1771, 14.9% | $403,693, 1510, 11.6% | $769,842, 313, 1.9% |
| All 0.80 or higher | $151,557, 229, 5.2% | -$70,966, 490, -3.1% | -$310,531, 60, -3.2% |
| All | $824,907, 2000, 11.1% | $332,727, 2000, 5.7% | $459,310, 373, 0.9% |

As with `bigggggggg` and `lpgb`, the profit of all three comes from entries under 0.80.

## Cautions on the two ADD wallets

The rule is applied as written. These numbers are outside it.

- `0x4488`: 43.9% of its recent buy dollars are at 0.80 or higher. The rule counts buys, and the copy book buys 100
  shares of every buy, so the count is what it copies. 33 of the 97 high-price buys are at 0.98 or higher, where the
  copy book skips (`fill_too_high`). In the scout history the 163 positions at 0.99 or higher made $3,225 on
  $1,355,547.
- `0x4488`: 430 buys cover 62 tokens, about 7 buys per token. The copy book opens 100 shares for each buy, so it will
  hold several copies of the same token.
- `0x4488`: $787,853 of the $824,907 scout profit is in the second half of the sample period (`second_half_pnl`). Its
  842 sports positions lost $64,229 (`market_types`).
- `paddaa`: its scout `return_on_cost` is 0.0575, close to the 0.05 minimum. Its 490 scout positions at 0.80 or higher
  lost $70,966, and 23.6% of its recent buys are in that range.
- `paddaa`: 171 of its 500 rows are sells. 12 of its buys are under 0.05, where the copy book skips (`fill_too_low`).

## What the data cannot show

- No copy has been opened for any of the three, so there is no live result.
- The activity window is the latest 500 rows: 3.3 days for `0x4488`, 3.4 days for `paddaa`, 13.7 days for
  `UnderperformingForALiving`. It says nothing about earlier days or whether these days are typical.
  `UnderperformingForALiving` has 9 buys in the last 3 days.
- The scout history is the latest closed positions the scout read, not the full history: closed from 2026-08-15
  (`0x4488`), 2026-01-17 (`paddaa`) and 2026-02-18 (`UnderperformingForALiving`) to 2026-11-16.
- Scout history counts closed positions at their average price; recent buys are single fills. The comparison is close
  but not exact.
- Scout profit is in the wallet's own dollars, so large positions decide `return_on_cost`. The copy book buys 100
  shares of every buy, so its result weights every buy the same. The scout return does not say what a copy would earn.
- The price the copy book would get. It reads the order book up to 35 minutes after the wallet's buy.
- The sells (7, 171 and 363 rows). The copy book holds to settlement and does not copy exits. Whether the profit of
  `paddaa` and `UnderperformingForALiving` depends on selling early was not checked.
- Whether the wallets are market makers. The windows have 4, 4 and 11 `MAKER_REBATE` rows, and the stored scout rows
  carry the label `market_maker` from the older rule. The current `strategy_label` ignores rebate rows and gives
  `mixed`; `copy_candidates` on the stored run returns exactly these three wallets. A buy filled as a resting order
  cannot be copied at the same price by a taker. Not checked here.
- Which market types the recent buys are in. Not checked here.
- How fast the open positions of `0x4488` settle. At 5.47 buys per hour the copy book's 60 open positions per wallet
  could fill within a day.

## Code change

Two entries added to `WALLETS` in `andy_trader/copy_book.py`, with a dated comment:

- `"0x448861155279dbf833d041b963e3ac854599e319"`, same address. The wallet has no user name, so the name is the
  address, as in the scout row.
- `"paddaa": "0x60a92c8620846d81f5ea17b0564e0d4b7c545a71"`

`UnderperformingForALiving` is not added. Sizing, fill limits, `PAUSED_WALLETS` and the other wallets are untouched.
`tests/test_copy_book.py` pins the wallet list in two tests; both now include the two new wallets. Drop either wallet
if it shows no edge after `VERDICT_THRESHOLD` settled positions.
