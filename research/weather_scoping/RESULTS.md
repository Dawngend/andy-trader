# Weather Repricing: Historical Test Results

2026-09-30. Paper research only: no wallet, no keys, no orders. Reproduce with the scripts in this folder
(order below). Raw data in `research_data/` (gitignored, on D:).

## Bottom Line

**This is the first real, measurable edge in the overnight research, but it is small and it is a race.**
The public airport readings (METAR) decide thousands of daily-high buckets hours before the market
closes, and they are almost never wrong. The market does reprice after each new reading, but not
instantly: a trader who reacted within about 5 minutes could have bought already-decided outcomes at a
profit. After about 15 minutes the opportunity is mostly gone, and after an hour it is gone.

| Reaction delay | Markets with profitable fills | Gross | Net of 0.07 fee |
| --- | --- | --- | --- |
| 5 min | 502 | $3,826.86 | $3,612.53 |
| 15 min | 235 | $735.15 | $686.69 |
| 1 h | 107 | $8.77 | $6.49 |

Over about 15 months (Jan 2025 to Apr 2026) of five cities. That is an upper bound for one extra
trader, because each historical fill can only be taken once.

## Data

| Source | What | Coverage |
| --- | --- | --- |
| Polymarket markets table | 8,659 resolved daily-high buckets, 1,119 daily events | London, NYC, Seoul, Shanghai, Paris; 2025-01 to 2026-04-16 |
| Polymarket fills | 4,465,848 fills, $79.9M | Same markets |
| Iowa State ASOS archive | about 105,000 METAR reports | EGLC, KLGA, RKSI, ZSPD, LFPB, 2025-01-01 to 2026-04-20 |

Settlement stations and rounding come from each market's official rules text (see `SCOPING.md`).

## Step 1: METAR Reproduces Settlement

The local-day high rebuilt from METAR, in the market's own unit, reproduces **99.07%** of real
resolutions: 100% for London, NYC and Shanghai, 97.3% for Seoul, 91.6% for Paris.

The high can only rise during the day, so a bucket is decided the moment the running high passes it.
Requiring the running high to clear the bucket edge by one whole degree (margin 1), METAR decides
**3,893 markets before the day ends, and 3,892 (99.97%) match the real resolution.**

## Step 2: Did The Market Still Offer Decided Outcomes?

For each decided market, fills after the deciding report plus a reaction delay are checked for a price
a buyer of the certain side could have paid (a taker SELL at YES price p means NO was buyable at 1 - p;
a taker BUY at p means YES was buyable at p).

Two artifacts had to be removed to get the honest numbers above:

1. **Day-boundary disputes.** The raw test showed $17,220 at a 5-minute delay, but $13,260 of it
   came from two NYC markets on 2026-03-07, decided by a reading at 23:51 local time on the night
   US clocks changed. They traded the next day at 0.71 while it was unclear which day that reading
   belonged to. That is resolution risk, not mispricing, so decisions after 23:00 local are excluded.
2. **Wrong calls are losses.** The one market METAR called wrong is kept as a full loss (a strategy
   cannot know in advance which call will fail), and the "skip sub-2-cent prints" filter applies to
   the price a trader sees, so losses are never filtered out.

By city at a 5-minute delay: London $2,914 (avg 15.6 cents per share), NYC $568 (7.7), Paris $345
(5.9, including the wrong call), Seoul and Shanghai $0.

At 15 minutes, 99% of the remaining profit comes from 5 markets (median market $4.38), so what is
left after the first few minutes is a handful of stale quotes, not a steady edge.

## What It Means For Andy Trader

- **Worth a paper test**, focused on London and NYC, where it concentrates.
- **It is a speed game.** The edge lives in the first few minutes after each METAR, so a collector
  must poll METAR every minute or faster and react immediately. METAR is published a few minutes
  after its observation time; the replay measures delay from the observation time, so real-world
  delays are slightly worse than shown.
- **Capacity is small:** a few thousand dollars across 15 months and five cities at best. It fits a
  paper-to-small-live progression, not a large allocation.
- **Paper gate still applies:** 200+ independent settled calls, positive lifetime and recent skill,
  hit rate above fee break-even, before any real money.

## Limits

- Fill prices tell what traded, not what a new order would have gotten; queue position and slippage
  are not modeled.
- The weather fee rate was not verified; results are shown at 0 and 0.07.
- Wunderground rounding and revision rules are approximated by rounding METAR to whole degrees and a
  one-degree margin. That holds for London (0 of 108 days where METAR's high differed from the winning
  exact-degree bucket), Shanghai (0 of 30) and NYC (100% of buckets reproduced), but not for Seoul (14
  of 110 days, 13%) or Paris (25 of 53 days, 47%, three by 2 C or more). The paper collector
  (`andy_trader.weather`) therefore trades London, NYC and Shanghai only.
- Delay is measured from the METAR observation time, not its publication time.

## Reproduce

```
python research/weather_scoping/weather_market_inventory.py
python research/weather_scoping/settlement_rules.py
python research/weather_scoping/build_weather_market_list.py
python research/polymarket_history/extract_trades_for_markets.py weather_markets.parquet weather_trades 1735689600
python research/weather_scoping/fetch_metar_history.py
python research/weather_scoping/weather_certainty.py 1
python research/weather_scoping/decided_bucket_trades.py
python research/weather_scoping/inspect_decided_prints.py NYC
```
