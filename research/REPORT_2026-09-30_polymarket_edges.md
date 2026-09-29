# Polymarket Crypto Up/Down: Is There A Fee-Proof Edge?

Overnight research, 2026-09-29 to 2026-09-30. Paper only: no wallet, no keys, no orders. Every number
below is reproducible from the scripts in `research/polymarket_history/` and `research/pmxt_crosscheck/`.
Raw data lives in `research_data/` (gitignored, on D:, 1.8 GB of the agreed 20 GB).

## Bottom Line

1. **The CT-11 complete-set "edges" are not real arbitrage.** Polymarket's Up and Down books mirror
   each other, so in a consistent book Up best ask + Down best ask = $1 + spread. Andy's own live
   record shows the sub-$1 readings almost vanished the day the collector started reading both books
   together (2026-09-20, commit `d6942ed`). Historical order books show the remaining sub-$1 moments
   coincide with one side's recorded quotes freezing while the other side moves.
2. **Late-round prices are well calibrated, and no directional pattern survives the taker fee at
   executable prices.** Across 134,253 resolved rounds, the only bins that looked profitable were
   "buy the 95%+ favorite near the close", and at the prices buyers actually paid they turn negative.
3. **Recommendation:** stop treating complete sets as a strategy candidate. Keep the CT-11 collector
   only as a data-quality monitor. Spend research time on edges with an information source the market
   does not already price, such as the weather-repricing idea from the Phase 3 notes.

## Data

| Source | What | Coverage |
| --- | --- | --- |
| SII-WANGZJ/Polymarket_data (Hugging Face, MIT) | 381,587,085 fills, $3.22B, six columns only | 135,280 BTC/ETH/SOL/XRP 5m and 15m rounds, 2025-10-09 to 2026-06-18 |
| PMXT archive (r2v2.pmxt.dev) | Full L2 order books: snapshots, price changes, trades | About 2026-04-26 to 2026-07-20; about 80 of 88 Up/Down rounds per hour; gaps mid-June and around 07-12 |
| Andy Trader live record | 130,704 CT-11 observations | 2026-09-06 to 2026-09-29 |

The PMXT archive and Andy's live record do not overlap, so the cross-check compares periods, not the
same rounds.

## Finding 1: Complete Sets Below $1 Come From Reading Skew

### Andy's Live Record

Of 100,023 measurable live observations, 91 had Up best ask + Down best ask below $1, and 20 stayed
below $1 after fees. By day:

| Period | Observations | Best-ask sum below $1 | Net of fee below $1 |
| --- | --- | --- | --- |
| Sep 6 to 19 (books fetched separately) | 6,099 | 84 | 18 |
| Sep 20 to 29 (synchronized bursts, `d6942ed`) | 124,609 | 7 | 2 |

The rate fell from about 1.4% to about 0.006% of observations, a roughly 250x drop, on the day
the collector began sampling both books together, while daily observation volume rose about tenfold.
All 20 net readings were BTC; ETH, SOL and XRP produced essentially none.

### Historical Order Books (PMXT)

`replay_complete_set.py` rebuilds each BTC 5m round's ask books from PMXT and calls Andy's unchanged
`observe_complete_set` once per second at feeRate 0.07 and 0.10. Seconds where the rebuilt best ask
disagrees with the exchange's own recorded best ask are excluded as unverified.

| Measure | Value |
| --- | --- |
| Hours replayed | 64 |
| Rounds | 653 |
| Verified seconds | 153,828 (89.9% of sampled seconds) |
| Best-ask sum below $1 | 913 seconds (0.59%) |
| Net of 0.07 fee below $1 | 575 seconds (0.37%) |
| Net of 0.10 fee below $1 | 227 seconds (0.15%) |
| Mean best-ask sum | 1.0115 |

The 575 seconds form 64 episodes in 39 of 653 rounds (6%): 18 one-second, 22 of two to five
seconds, 24 longer, the longest 289 seconds.

The largest real-feed episode (`btc-updown-5m-1780711500`, seconds 35 to 48, net cost down to
$0.80) shows what happened: only Up traded, its price sliding from 0.62 to 0.37, while Down recorded
no trades at all until 48.3 s, when it jumped straight to 0.63 to 0.73 and the "gap" vanished within a
second. The sum paired a live Up ask with a frozen Down ask. A market this active would not leave a
$0.20 riskless profit on the table for 14 seconds.

`episode_one_sided.py` counts price changes per token inside every episode window:

- **29 of 64 episodes are strictly one-sided**: the quieter token recorded at most 2% of the busier
  token's price changes, often zero (for example 2,228 vs 0 over 289 s, 0 vs 5,709 over 12 s).
- That count is a lower bound. The 14-second episode above is classified two-sided (16,557 vs 1,483
  changes) even though its Down top of book was demonstrably frozen: the churn was deeper in the book.
- The 35 remaining episodes total 142 seconds, 0.09% of verified seconds, mostly one or two seconds
  each, and most are still heavily lopsided between the two tokens.

So the historical sub-$1 moments are dominated by one side's recorded quotes going stale, the archive
equivalent of the reading skew that the live collector fixed on Sep 20. What little is left is too
short and too rare to trade after latency, and it is not shown to be executable.

### The Backtester's Pair Arbitrage Example

`prediction-market-backtesting`'s `BookBinaryPairArbitrageStrategy` example (4 BTC 5m rounds,
2026-04-26) made two entries. The profitable-looking one bought Up at 0.47 and Down at 0.4936 **at
different times** (`pairing_mode: sequential`), which is two directional bets, not an arbitrage.
Net of fees it made $0.007.

Re-run over one full hour (12 rounds from 2026-05-29 14:00 UTC, `andy_pair_fee_compare.py`), with
the strategy's own default of fees in the entry signal and with the example's fees-off setting:

| Fees in signal | Pairs entered | Per-market total PnL |
| --- | --- | --- |
| On (strategy default) | 9 | -$1.80 |
| Off (example setting) | 12 | -$3.18 |

Because the second leg is bought later at whatever the price has moved to, the pairs cost 0.66 + 0.40
= 1.06, 0.21 + 0.83 = 1.04, 0.20 + 0.81 = 1.01, 0.97 + 0.07 = 1.04 and so on: almost never under $1,
and fees turn the rest into losses. Turning fees off only adds more losing pairs.

Its two totals disagree: the per-market table says -0.085 and the portfolio summary says -10.09. The
fills explain it exactly: the four legs cost $9.87 plus about $0.22 of taker fees, so the portfolio
line counts every position as worthless. It never books the $1 payout on the two winning legs
(2 x 5 shares = $10.00), because the engine does not process the market resolution. -10.09 + 10.00
is about -0.09, matching the table. **Trust the per-market table for hold-to-resolution strategies.**

## Finding 2: Late-Round Prices Are Calibrated, With No Fee-Proof Edge

`calibration.py` takes the last traded Up price at fixed times before each round closes and scores it
against the outcome.

| Round | Time left | Rounds | Market Brier | Skill vs coin flip |
| --- | --- | --- | --- | --- |
| 15m | 600 s | 64,941 | 0.1907 | 0.24 |
| 15m | 300 s | 65,375 | 0.1223 | 0.51 |
| 15m | 60 s | 57,472 | 0.0562 | 0.78 |
| 15m | 30 s | 55,320 | 0.0416 | 0.83 |
| 5m | 240 s | 66,362 | 0.2167 | 0.13 |
| 5m | 60 s | 65,843 | 0.1098 | 0.56 |
| 5m | 10 s | 64,074 | 0.0550 | 0.78 |

Overfitting guard: bins were selected on rounds ending before 2026-03-01 (63,672 rounds) and
re-scored on rounds from 2026-03-01 (70,581 rounds). Up won 49.81% and 49.98% of rounds in the two
periods, so neither period had a directional tailwind.

Of 73 bins profitable after fees on last-traded prices in the train period, 6 held up in test
(EV > 0, z > 2), nearly all "buy the Up favorite at 0.95 or more near the close", worth about 0.26
cents per share. That is the classic favorite-longshot pattern, but the last trade can be a sale at
the bid. Re-scored at executable prices (Up cost = last taker BUY fill; Down cost = 1 minus the last
taker SELL fill):

| Round | Time left | Side | Test EV per share | Test z |
| --- | --- | --- | --- | --- |
| 5m | 10 s | Up | -0.0011 | -0.86 |
| 5m | 10 s | Down | -0.0043 | -3.21 |
| 5m | 30 s | Up | -0.0043 | -2.89 |
| 5m | 30 s | Down | -0.0039 | -2.63 |
| 15m | 30 s | Up | +0.0012 | 0.44 |
| 15m | 300 s | Up | +0.0050 | 1.57 |

No bin is positive in both periods on both sides, and the only positive test values are not
significant. **At prices a buyer actually pays, late favorites do not beat the fee.**

## Open Question: The Fee Rate

Andy uses feeRate 0.07 (Polymarket docs, checked 2026-09-18). PMXT trade events in April to July
carry `fee_rate_bps = 1000`. That may be the maximum rate signed into orders rather than the rate
charged, or the fee may have changed. Every complete-set result above is reported at both 0.07 and
0.10; the conclusion does not depend on which is right, because a higher fee only removes more edges.

## Limits

- PMXT and the live record do not overlap, so the reading-skew conclusion rests on the timing of the
  Sep 20 collector change plus the historical episode evidence, not on a same-round comparison.
- The replay samples once per second, so a gap shorter than a second is invisible to it; a gap that
  short would not be tradeable by a collector that samples at that speed either.
- `quant.parquet` ends 2026-06-18, and about 40% of 5m rounds have no fills in it.
- Calibration uses fills in the 30 seconds before each checkpoint; thin rounds drop out.

## Reproduce

```
python research/polymarket_history/build_market_list.py
python research/polymarket_history/extract_updown_trades.py
python research/polymarket_history/coverage.py
python research/polymarket_history/calibration.py
python research/pmxt_crosscheck/replay_complete_set.py <hours...>
python research/pmxt_crosscheck/summarize_replay.py
python research/pmxt_crosscheck/gap_episodes.py
python research/pmxt_crosscheck/episode_trades.py btc-updown-5m-1780711500 35 14
python research/pmxt_crosscheck/episode_one_sided.py
```
