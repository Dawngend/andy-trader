# Trend book concentration check, 2026-11-17

Paper only. Books `trend40` and `trend_vote`.

## Verdict

The `trend40` profit is one market-wide move, shorted across eight coins that move as one. The coins' daily returns
have an average pairwise correlation of 0.875, which is 1.12 independent bets and not 8; the short legs made
+$1,823.14 (113% of the +$1,608.03) and the long legs lost $215.10; 92% of the profit came after 2026-11-01, when all
eight coins were short while prices fell 20% on average; and holding all eight short from the first fill, with no rule
at all, would have made +$2,013.54. The 40-day rule did turn the book short by 2026-10-31, which is what a trend rule
is for, but that is one correct call on one move, so the result does not show a trend following edge yet.

## Data

- `paper_trades`, `paper_equity_curve` and `crypto_observations` in `D:\Andy Trader\crypto_observations.db`, opened
  read only. Last equity row used: 2026-11-17 06:05 UTC.
- At that row `trend40` is +$1,608.03 on $8,000 and `trend_vote` is -$310.18 on $8,000. Both have moved against the
  books since the +$1,657 and -$243 in the task. Seven `trend40` coins are up (+11.5% to +31.5%) and AVAX is down
  (-$14.47).
- Pnl is measured from the $1,000 starting cash of each coin, so it includes the fee and slippage of every fill
  ($49.72 in total for `trend40`, $17.99 for `trend_vote`).
- Daily closes are the Bybit `1d` candles, `degraded = 0`, latest seen value per candle, the same rule as
  `_completed_daily_closes` in `andy_trader/trend_book.py`. The unfinished 2026-11-17 candle is left out.
- The equity curve has one row per hour and has gaps. On 13 of the 26 days the last row is before 22:00 UTC, so a
  "day" in the equity tables is not always midnight to midnight. Two cases matter below: the last row before
  2026-11-01 is 2026-10-31 14:05, and the 2026-11-14 row covers 35 hours (the row before it is 2026-11-13 12:05).

## 1. Daily changes and correlation

### Correlation of daily price returns

Daily returns of the Bybit closes, 2026-10-23 to 2026-11-16, 25 days.

| | BTC | ETH | SOL | XRP | DOGE | ADA | AVAX | LINK | Average with the other seven |
|---|---|---|---|---|---|---|---|---|---|
| BTC | 1.00 | 0.94 | 0.91 | 0.82 | 0.88 | 0.85 | 0.78 | 0.75 | 0.85 |
| ETH | 0.94 | 1.00 | 0.93 | 0.88 | 0.91 | 0.89 | 0.77 | 0.79 | 0.87 |
| SOL | 0.91 | 0.93 | 1.00 | 0.92 | 0.97 | 0.96 | 0.87 | 0.89 | 0.92 |
| XRP | 0.82 | 0.88 | 0.92 | 1.00 | 0.92 | 0.90 | 0.76 | 0.82 | 0.86 |
| DOGE | 0.88 | 0.91 | 0.97 | 0.92 | 1.00 | 0.97 | 0.89 | 0.87 | 0.92 |
| ADA | 0.85 | 0.89 | 0.96 | 0.90 | 0.97 | 1.00 | 0.89 | 0.90 | 0.91 |
| AVAX | 0.78 | 0.77 | 0.87 | 0.76 | 0.89 | 0.89 | 1.00 | 0.84 | 0.83 |
| LINK | 0.75 | 0.79 | 0.89 | 0.82 | 0.87 | 0.90 | 0.84 | 1.00 | 0.84 |

`N_eff = N / (1 + (N - 1) * average_corr)` with `N = 8`:

| Window | Days | Average pairwise correlation | Lowest pair | Highest pair | N_eff |
|---|---|---|---|---|---|
| `trend40` life, 2026-10-23 to 2026-11-16 | 25 | 0.875 | 0.745 | 0.973 | 1.12 |
| Before 2026-11-01 | 9 | 0.886 | 0.705 | 0.974 | 1.11 |
| From 2026-11-01 | 16 | 0.894 | 0.731 | 0.988 | 1.10 |
| All Bybit daily candles, 2026-03-19 to 2026-11-16 | 243 | 0.770 | 0.633 | 0.902 | 1.25 |

`N_eff` is from price returns. It describes the book when all eight positions point the same way, which is the case
from 2026-10-31 on (AVAX was flat for 10 hours on 2026-11-09). Before that the book was mixed: 5 long and 3 short at
the start.

### Daily equity change per coin, trend40, dollars

Last equity row of each UTC day minus the last row of the day before. The first day is measured from $1,000.

| Day | BTC | ETH | SOL | XRP | DOGE | ADA | AVAX | LINK | Book |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-23 | -2.67 | +2.74 | -2.17 | -1.35 | -6.28 | -2.43 | -9.60 | -11.51 | -33.27 |
| 2026-10-24 | -12.60 | +19.67 | -22.93 | +18.73 | +32.10 | -39.85 | -25.59 | -40.27 | -70.74 |
| 2026-10-25 | -2.85 | +4.27 | +12.68 | -2.81 | +1.64 | +18.86 | +0.00 | -7.46 | +24.34 |
| 2026-10-26 | -7.47 | +13.50 | -15.10 | -9.97 | +1.38 | +2.72 | -7.02 | -16.08 | -38.03 |
| 2026-10-27 | +14.16 | -20.03 | +13.80 | +33.66 | -24.05 | +8.15 | +8.64 | +23.30 | +57.63 |
| 2026-10-28 | +4.12 | -2.20 | -16.54 | -3.20 | -10.45 | -0.48 | +4.21 | +20.67 | -3.87 |
| 2026-10-29 | -29.95 | +39.40 | +57.95 | +64.66 | +50.74 | +71.27 | -72.50 | +91.20 | +272.75 |
| 2026-10-30 | +3.22 | +10.39 | +4.56 | -11.85 | -0.25 | -21.55 | -23.97 | -52.50 | -91.94 |
| 2026-10-31 | -6.00 | -5.65 | +2.62 | +4.91 | +5.04 | +11.97 | +1.52 | -6.41 | +7.99 |
| 2026-11-01 | -4.68 | -5.66 | +7.28 | -3.43 | -4.66 | -6.23 | -29.44 | -0.88 | -47.69 |
| 2026-11-02 | +13.90 | +6.00 | +29.41 | +2.42 | +19.14 | +23.94 | +39.29 | +39.21 | +173.31 |
| 2026-11-03 | -6.48 | -12.24 | -9.80 | -23.86 | -11.20 | -8.62 | -22.95 | -5.60 | -100.76 |
| 2026-11-04 | -11.46 | -14.15 | +1.75 | -1.17 | +5.04 | +6.70 | -4.01 | +6.21 | -11.09 |
| 2026-11-05 | +27.75 | +28.25 | +32.81 | +38.83 | +36.38 | +31.60 | +39.84 | +36.14 | +271.61 |
| 2026-11-06 | +1.46 | +6.91 | -9.61 | +30.72 | -14.48 | -26.34 | -12.34 | -16.98 | -40.65 |
| 2026-11-07 | +1.58 | -1.41 | +9.90 | +24.72 | +19.39 | +32.56 | +20.78 | +7.96 | +115.49 |
| 2026-11-08 | -2.37 | -2.65 | +0.78 | -1.79 | +2.01 | +4.31 | +2.06 | +6.13 | +8.47 |
| 2026-11-09 | +9.99 | +45.11 | +25.53 | +47.72 | +16.49 | +12.45 | -67.24 | +7.35 | +97.39 |
| 2026-11-10 | +18.15 | +35.70 | +37.18 | +34.93 | +27.32 | +20.11 | +29.12 | +33.25 | +235.76 |
| 2026-11-11 | +0.24 | -3.77 | -4.56 | +2.34 | -7.18 | -8.62 | -9.71 | -6.56 | -37.82 |
| 2026-11-12 | +22.79 | +17.44 | +14.17 | +20.35 | +33.74 | +20.11 | +8.54 | +32.47 | +169.62 |
| 2026-11-13 | +28.13 | +20.63 | +9.90 | +1.33 | +2.39 | -5.27 | -4.37 | -2.71 | +50.03 |
| 2026-11-14 | +72.27 | +91.94 | +114.05 | +102.15 | +116.96 | +131.21 | +136.28 | +116.48 | +881.33 |
| 2026-11-15 | -13.40 | -25.24 | -20.77 | -46.01 | -27.19 | -38.79 | -13.49 | -42.36 | -227.25 |
| 2026-11-16 | -7.63 | -9.18 | -3.11 | -1.56 | -8.31 | -0.48 | +3.30 | +3.50 | -23.47 |
| 2026-11-17 | +5.21 | -4.02 | -10.97 | -5.22 | -0.13 | -5.27 | -5.82 | -4.90 | -31.12 |
| Total | +115.40 | +235.75 | +258.80 | +315.25 | +255.58 | +232.07 | -14.47 | +209.65 | +1608.03 |

The book was up on 13 days and down on 13. The 2026-11-14 row is +$881.33, 55% of the profit; the three best rows
(2026-11-14, 2026-10-29, 2026-11-05) add up to 89% of it. Without the 2026-11-14 row the book is +$726.70.

### Daily equity change per coin, trend_vote, dollars

The book was created flat on 2026-11-14 17:05 and opened all eight coins short on 2026-11-15 00:05.

| Day | BTC | ETH | SOL | XRP | DOGE | ADA | AVAX | LINK | Book |
|---|---|---|---|---|---|---|---|---|---|
| 2026-11-14 | +0.00 | +0.00 | +0.00 | +0.00 | +0.00 | +0.00 | +0.00 | +0.00 | +0.00 |
| 2026-11-15 | -9.71 | -20.54 | -15.03 | -61.47 | -20.01 | -58.60 | -9.96 | -32.89 | -228.21 |
| 2026-11-16 | -9.76 | -12.49 | -4.56 | -3.97 | -11.52 | -5.74 | +4.80 | +4.50 | -38.74 |
| 2026-11-17 | +6.66 | -5.47 | -16.09 | -6.60 | -0.17 | -6.80 | -8.46 | -6.30 | -43.23 |
| Total | -12.81 | -38.49 | -35.67 | -72.03 | -31.70 | -71.15 | -13.63 | -34.69 | -310.18 |

All eight coins are short and all eight are down. Prices are 1.1% to 5.1% above the entry fills.

## 2. Where the trend40 profit came from

A leg is one entry in `paper_trades` (`long` or `short`) until its `flat` row, or until the last equity row if it is
still open. Leg pnl is the equity at the end of the leg minus the cash just before the entry. The before and after
split uses the last equity row before 2026-11-01 (2026-10-31 14:05); no trade happened between that row and the next
one. All eight legs open at that row are short.

| Side | Legs | Winning legs | Pnl before 2026-11-01 | Pnl from 2026-11-01 | Pnl | Share of profit |
|---|---|---|---|---|---|---|
| Long | 8 | 2 | -$215.10 | $0.00 | -$215.10 | -13.4% |
| Short | 13 | 12 | +$339.97 | +$1,483.17 | +$1,823.14 | 113.4% |
| All | 21 | 14 | +$124.86 | +$1,483.17 | +$1,608.03 | 100.0% |
| Share of profit | | | 7.8% | 92.2% | 100.0% | |

Marking the same positions at the Bybit 2026-10-31 close instead of the 14:05 row gives +$103.42 before and
+$1,504.61 after (6.4% and 93.6%).

| Coin | Long legs | Short legs | Before 2026-11-01 | From 2026-11-01 | Total |
|---|---|---|---|---|---|
| BTC | -46.38 | +161.78 | -40.04 | +155.44 | +115.40 |
| ETH | none | +235.75 | +62.09 | +173.66 | +235.75 |
| SOL | -23.70 | +282.49 | +34.87 | +223.92 | +258.80 |
| XRP | +19.81 | +295.44 | +92.78 | +222.47 | +315.25 |
| DOGE | none | +255.58 | +49.87 | +205.72 | +255.58 |
| ADA | -31.07 | +263.15 | +48.67 | +183.40 | +232.07 |
| AVAX | -101.86 | +87.39 | -124.31 | +109.84 | -14.47 |
| LINK | -31.91 | +241.56 | +0.94 | +208.72 | +209.65 |

The split is not an up market against a down market. Prices fell in both parts: the eight closes fell 5.6% on average
from 2026-10-22 to 2026-10-31 and 20.7% from 2026-10-31 to 2026-11-16. The first part was sideways except for
2026-10-29, when the coins fell 4.1% to 10.2% in one day. There is no rising period in the data.

The one losing short leg is AVAX from 2026-10-30 to the interlock exit on 2026-11-09 (-$56.47). The largest loss is
the AVAX long from the start (-$101.86): its 40-day return was +23.4% on 2026-10-23 and it fell with the others.

For comparison, $1,000 per coin sold short once at the 2026-10-23 04:14 fill prices, with the same entry cost and no
later trades, would be +$2,013.54 at the last row (+$526.49 before 2026-11-01 and +$1,487.05 after). `trend40` has 80%
of that. Prices are 20.7% to 30.2% below those fills, 25.4% on average.

## 3. Risk interlock forced exits

All five have the reason `daily loss >= limit 5.00%`. Later prices are the Binance `1m` closes the books fill on, the
latest candle at or before the stated time.

| Book | Exit, UTC | Coin | Side held | Interlock daily loss | Leg pnl | Exit price | Price 7 days later | Price change | Re-entry |
|---|---|---|---|---|---|---|---|---|---|
| trend40 | 2026-10-29 21:05 | AVAX | long | 7.40% | -$101.86 (-10.2%) | 8.331 | 8.332 | +0.0% | short, 2026-10-30 00:05 at 8.297 |
| trend40 | 2026-10-30 22:05 | LINK | short | 5.14% | +$39.26 (+4.1%) | 11.306 | 10.944 | -3.2% | short, 2026-10-31 00:05 at 11.511 |
| trend40 | 2026-11-09 14:05 | AVAX | short | 8.98% | -$56.47 (-6.3%) | 8.793 | 7.104 | -19.2% | short, 2026-11-10 00:05 at 8.671 |
| trend_vote | 2026-11-15 13:05 | XRP | short | 5.85% | -$61.47 (-6.1%) | 0.9699 | no data yet | -0.8% after 41 hours (0.9624) | short, 2026-11-16 07:23 at 0.9534 |
| trend_vote | 2026-11-15 13:05 | ADA | short | 5.56% | -$58.60 (-5.9%) | 0.1559 | no data yet | -1.2% after 41 hours (0.1540) | short, 2026-11-16 07:23 at 0.1522 |

- AVAX long, 2026-10-29: the price was unchanged a week later. The 40-day rule turned AVAX short three hours after
  the exit, so the long would have been closed anyway. The 7-day price is the 2026-11-05 19:22 candle because the
  AVAX `1m` data has a gap until 21:21 (8.281, -0.6%).
- The four short exits were all on a bounce, and the price was lower afterwards in all four, so each short would have
  gained if held: 3.2% and 19.2% over 7 days, 0.8% and 1.2% over the 41 hours that exist.
- Every exit was followed by a re-entry on the side the rule wanted at the next rebalance, so little of the later move
  was missed. The cost of each exit is the gap between exit and re-entry price plus one extra round trip of fees:
  the AVAX short re-entered 1.4% lower, XRP 1.7% lower and ADA 2.4% lower (worse for a short), LINK 1.8% higher
  (better).
- The LINK leg was closed at a profit. The interlock measures the loss within the day, not the loss of the position.

## What the data cannot show

- Whether the rule has an edge. `trend40` has 3.5 weeks of history and `trend_vote` has 2.5 days. With 1.12
  independent bets and one direction for the whole market, this is close to one observation.
- How the rule does in a rising or sideways market. There is no rising period in the data. The only sideways stretch
  is the first six days, and there the book lost $64 (to the 2026-10-28 row) while changing sides 6 times on SOL, XRP
  and ADA.
- Whether the correlation stays this high. 0.875 is from 25 daily returns in a falling market; the 243-day figure is
  0.77, which still gives only 1.25 independent bets.
- Anything inside a day. The rule and the correlation use daily closes. The equity curve is hourly with gaps, so the
  daily equity rows are uneven and the 2026-11-01 split is really at 2026-10-31 14:05.
- Real fills. Fills are paper, at the last Binance `1m` close with 10 bps fee and 5 bps slippage per leg. Short
  funding, borrow limits and the size available at that price are not modelled.
- Whether the interlock helps. Five exits, three of them with a full 7 days after, and all re-entered within a day.
- How much of the +$1,608.03 is kept. The eight short legs that are still open hold +$1,803.47; the 13 closed legs
  sum to -$195.44. The book gave back $281.84 in the three rows after 2026-11-14.
