# Andy Trader

A calibrated-forecast harness for short-horizon crypto directional prediction.

The point of this project is not the model. It is the measurement.

## The one design decision that matters

**The evaluation layer was built before any strategy existed.**

Every prediction is written to durable storage *before* its outcome can be known, carrying its
timestamp, the reference price, the probability, and a snapshot of what the predictor saw. A separate
settlement job resolves it later, and that job **never reads the prediction** — it looks up the price
and compares it to the reference. Keeping the outcome computation blind to the call is what stops a
settlement bug from quietly flattering the score.

That ordering cannot be retrofitted. Build a predictor first and measure it later, and you have no
way to prove the calls were not adjusted after the fact. So the harness came first, and the first
predictors it scored were deliberately stupid ones.

## What it does

```
collect  ->  observe            append-only OHLCV from keyless public venues
predict  ->  log in advance     one call per predictor, before the outcome exists
settle   ->  resolve blind      price lookup only, never reads the prediction
score    ->  Brier + skill      is this better than the base rate, and by how much
```

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env

python -m andy_trader.collector --instruments BTC-USD,ETH-USD --intervals 1h,4h
python -m andy_trader.predict predict
# wait for a horizon to elapse
python -m andy_trader.predict settle
python -m andy_trader.predict score
```

No API keys. Every venue is a keyless public endpoint, and nothing here can place an order.

## Reading the score

The headline is **Brier skill score**, not accuracy.

A raw Brier of 0.24 sounds respectable and is worthless if predicting the base rate alone scores
0.24. The skill score is the ratio against that baseline: positive means the predictor knows
something the base rate does not, zero means it does not, negative means it is actively worse.

The report also gives Murphy's decomposition, `Brier = reliability - resolution + uncertainty`:

- **reliability** — 0 is perfectly calibrated. When you say 70%, it happens 70% of the time.
- **resolution** — higher is more informative. How far predictions move away from the base rate
  *correctly*. A model can be perfectly calibrated and completely useless by always predicting the
  base rate; resolution is what catches that.
- **uncertainty** — fixed by the data, not the model.

**Hit rate is reported but is not the headline.** It throws away the confidence, so a predictor that
is 51% right while claiming certainty scores the same as one that is 51% right while saying so.

### Degenerate samples

If every settled outcome went the same way, predicting the base rate is already perfect and the skill
ratio divides by zero. The report flags this as `degenerate` rather than returning 0.0, which would
rank a perfect predictor and a useless one identically. With few settled calls, an all-up run is
common, and it means *not enough evidence*, not *no skill*.

## The baselines

Any real model has to beat these out of sample, net of costs. The bar is the best of them, which in
practice is almost always `base_rate`.

| Baseline | What it does |
| --- | --- |
| `coin_flip` | Constant 0.5. The zero-information floor; Brier lands on exactly 0.25 |
| `random` | Uniform random, seeded. Badly calibrated on purpose, so reliability has something to catch |
| `base_rate` | The historical share of up-moves. **The one that is hard to beat** |
| `momentum` | Follows the last move, confidence scaled by its size |
| `ema_crossover_12_26` | Fast over slow EMA |

Baselines are capped at 0.65 confidence. A baseline emitting 1.0 is a strawman that Brier punishes so
hard that beating it proves nothing.

## Data sources

| Venue | open / high / low | close | volume | Notes |
| --- | --- | --- | --- | --- |
| `bybit` | yes | yes | yes | Default lead. Only venue giving full bars at both 1h and 4h. USDT-quoted |
| `coingecko` | **NULL** | yes | yes | Fallback. Never blocked in testing |
| `coinbase` | yes | yes | yes | 1h only, no 4h granularity |
| `kraken` | yes | yes | yes | Registered, out of the default |

Two traps worth knowing:

**`degraded = 0` means the fetch succeeded, not that the bar is complete.** CoinGecko returns close
and volume only, and its NULL open/high/low is a legitimate partial observation. Any feature built
from highs or lows must filter on `high IS NOT NULL` rather than assuming.

**A failed fetch becomes a degraded row, never a skipped one.** A gap in the data is itself a fact
about what we could observe, and losing it makes a backtest quietly optimistic later.

## Storage

Two tables, both append-only in spirit.

`crypto_observations` is keyed by a content hash of the bar's values, with `first_seen_at`,
`last_seen_at` and `times_seen`. Re-fetching an unchanged closed candle bumps the counter. A candle
whose values actually changed lands as a **new row**, because that is the honest record: we saw two
different things and kept both.

`crypto_predictions` is an audit record. Rows are written once and only ever have their `settle_*`
columns filled in. Any other update to that table is a bug.

## Status

| Task | State |
| --- | --- |
| CT-01 collectors, append-only observations | done |
| CT-02 prediction log and calibration harness | done |
| CT-03 dumb baselines | done |
| CT-04 walk-forward backtest, fees and slippage | done |
| CT-05 PyTorch predictor | evaluated, did not clear CT-03; remains opt-in |

89 tests. `python -m pytest tests/ -q`.

## Walk-forward result

Run on the existing BTC-USD 1h history with a 100-bar minimum expanding window,
1h horizon, 10 bps fee and 5 bps slippage on both entry and exit. This produced
100 genuinely out-of-sample windows. The series uses the same deterministic
one-row-per-open-time venue selection as live prediction; it does not erase the
small Bybit USDT/USD basis.

| Predictor | Brier skill | Reliability | Gross return | Net return | Trades | Max drawdown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| EMA crossover 12/26 | -0.0051 | 0.0027 | +0.56% | -25.54% | 100 | 25.54% |
| Base rate | -0.0088 | 0.0005 | +4.52% | -22.59% | 100 | 22.59% |
| Coin flip | -0.0101 | 0.0025 | 0.00% | 0.00% | 0 | 0.00% |
| Momentum | -0.0420 | 0.0152 | +1.25% | -25.02% | 100 | 25.02% |
| **PyTorch MLP, seed 1729** | **-0.0434** | **0.0218** | **-6.79%** | **-31.00%** | **100** | **31.00%** |
| Random, seed 1729 | -0.4541 | 0.1227 | -4.38% | -29.21% | 100 | 29.21% |

The PyTorch candidate lost honestly. Its 0.0218 reliability term and 0.1367
expected calibration error show material miscalibration despite chronological
temperature scaling; its 38% hit rate is not the headline. It trails the best
calibration baseline (EMA crossover), the base-rate baseline, and the no-trade
coin flip after costs. It is therefore available only with `--include-model`
for continued evaluation and is not part of the default backtest or live call.

```bash
python -m andy_trader.backtest --instrument BTC-USD --horizon 1h
python -m andy_trader.backtest --instrument BTC-USD --horizon 1h --include-model
```

## Honest expectations

The likely outcome of CT-05 is that the model does **not** beat the base rate net of costs. That is a
real result and this repository is built to record it rather than to tune until the number looks
good. Most apparent short-horizon edge turns out to be round-trip cost that was never subtracted.

The durable value here is the engineering: scheduled collectors, append-only history, blind
settlement, walk-forward evaluation, and calibrated prediction logging. That survives either outcome.

## Scope

This project reads public market data and scores forecasts. It does not connect to an account, hold
credentials, or place orders. Execution is deliberately out of scope and gated behind a risk
interlock that does not exist yet.
