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

# Optional research and paper-only monitoring paths
python -m andy_trader.training --instrument BTC-USD --check-schedule
python -m andy_trader.portfolio --predictor baseline:momentum --instrument BTC-USD
python -m andy_trader.dashboard
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
| `coingecko` | **NULL** | yes | yes | 1h fallback. Its request-time quote is folded into the current hourly bucket |
| `coinbase` | yes | yes | yes | 1h only, no 4h granularity |
| `kraken` | yes | yes | yes | Registered, out of the default |

Two traps worth knowing:

**`degraded = 0` means the fetch succeeded, not that the bar is complete.** CoinGecko returns close
and volume only, and its NULL open/high/low is a legitimate partial observation. Any feature built
from highs or lows must filter on `high IS NOT NULL` rather than assuming.

**A failed fetch becomes a degraded row, never a skipped one.** A gap in the data is itself a fact
about what we could observe, and losing it makes a backtest quietly optimistic later.

## Storage

The two core audit tables are append-only in spirit. Supporting tables hold
signals, model-training decisions, and simulated paper-account state.

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
| Positioning and sentiment predictors | evaluated, none beat the base rate |
| CT-07 walk-forward retraining and promotion gate | built; first candidate rejected |
| CT-08 paper portfolio | built, long-or-flat and simulated only; manual opt-in |
| CT-09 local dashboard | built, read-only on `127.0.0.1:8787` |

186 tests. `python -m pytest tests/ -q`.

## Walk-forward result

Measured on 2026-09-05 using the existing BTC-USD 1h history with a 100-bar minimum expanding window,
1h horizon, 10 bps fee and 5 bps slippage on both entry and exit. This produced
120 genuinely out-of-sample windows. The series uses the same deterministic
one-row-per-open-time venue selection as live prediction; it does not erase the
small Bybit USDT/USD basis.

Calibration scores every forecast window. PnL uses one capital unit and only
compounds non-overlapping positions, so a 4h trade opened from 1h bars must
settle before that equity can enter another trade. This prevents four adjacent
4h forecasts from multiplying the same capital four times.

| Predictor | Brier skill | Reliability | Gross return | Net return | Trades | Max drawdown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Coin flip | -0.0070 | 0.0017 | 0.00% | 0.00% | 0 | 0.00% |
| Base rate | -0.0075 | 0.0002 | +2.51% | -28.52% | 120 | 28.52% |
| EMA crossover 12/26 | -0.0084 | 0.0012 | -1.01% | -30.98% | 120 | 30.98% |
| Momentum | -0.0349 | 0.0127 | -1.54% | -31.35% | 120 | 31.35% |
| **PyTorch MLP, seed 1729** | **-0.0354** | **0.0143** | **-4.55%** | **-33.45%** | **120** | **33.45%** |
| Random, seed 1729 | -0.4553 | 0.1254 | -7.59% | -35.58% | 120 | 35.58% |

The PyTorch candidate lost honestly. Its 0.0143 reliability term and 0.1079
expected calibration error show material miscalibration despite chronological
temperature scaling; its 40.8% hit rate is not the headline. It trails the
coin flip, base-rate, and EMA baselines on calibration and the no-trade coin
flip after costs. It is therefore available only with `--include-model` for
continued evaluation and is not part of the default backtest or live call.

```bash
python -m andy_trader.backtest --instrument BTC-USD --horizon 1h
python -m andy_trader.backtest --instrument BTC-USD --horizon 1h --include-model
```

## Signal predictor result

Signal predictors use a rolling percentile over their own instrument and abstain
outside the outer quartiles. A signal also abstains when it is missing, has fewer
than 20 historical observations, or is older than its source-specific freshness
limit. These rules were fixed before running the comparisons.

Measured on 2026-09-05, the existing database produced 120 BTC-USD windows and 113 DOGE-USD windows at
the 1h horizon. Ten bps fee and five bps slippage were charged on both entry and
exit.

| Instrument | Signal predictor | Brier skill | Gross return | Net return | Trades |
| --- | --- | ---: | ---: | ---: | ---: |
| BTC-USD | funding_contrarian | -0.0221 | -4.21% | -15.07% | 40 |
| BTC-USD | fear_greed_contrarian | -0.0268 | +1.66% | -5.69% | 25 |
| BTC-USD | crowd_contrarian | -0.0367 | -5.40% | -24.72% | 76 |
| BTC-USD | crowd_momentum | -0.0371 | +5.52% | -16.01% | 76 |
| DOGE-USD | crowd_momentum | -0.0038 | +7.10% | -17.52% | 87 |
| DOGE-USD | funding_contrarian | -0.0113 | -0.06% | -17.55% | 64 |
| DOGE-USD | fear_greed_contrarian | -0.0506 | +2.69% | -4.74% | 25 |
| DOGE-USD | crowd_contrarian | -0.0750 | -6.91% | -28.34% | 87 |

Every signal predictor had negative Brier skill, so none beat the hindsight base
rate used by the calibration report. None beat the zero-trade coin flip net of
costs either. DOGE crowd momentum ranked above its walk-forward base-rate
predictor on Brier skill, but remained below zero skill and lost 17.52% after
costs. The 77.7% DOGE long ratio is therefore not treated as an edge: against
DOGE's own history it carries different information than BTC near 53.2%.

```bash
python -m andy_trader.backtest --instrument BTC-USD --horizon 1h --include-signals
python -m andy_trader.backtest --instrument DOGE-USD --horizon 1h --include-signals
```

## Retraining and promotion result

CT-07 adds a deterministic rolling retraining command, backward as-of joins for
funding, positioning, open interest, and Fear and Greed, a chronological
holdout, and a registry that records every promotion decision. Promotion
requires positive holdout Brier skill and a win over the base-rate predictor on
the identical holdout. A rejected candidate never enters the default live or
paper path.

The first real BTC-USD run used 168 training bars and 24 holdout bars with seed
1729. It scored **-0.0074 holdout Brier skill**, versus **-0.0061** for the
base-rate predictor, and was rejected. Its calibration temperature was 5.9667.
No weights were saved and no model is promoted. The registry records the loss;
the scheduled cycle continues to log only the five baseline predictors.

```bash
python -m andy_trader.training --instrument BTC-USD --horizon 1h
python -m andy_trader.training --instrument BTC-USD --horizon 1h --walk-forward
```

## Paper portfolio and monitor

CT-08 keeps persistent simulated cash, one long-or-flat position per predictor
and instrument, trading costs, immutable trade rows, and an equity curve. It
does not auto-select a predictor and is not part of the unattended cycle. The
current database contains one `baseline:momentum` BTC-USD portfolio at its
$10,000 starting value, flat, with zero trades. That is setup state, not a
performance result.

CT-09 serves the same database on localhost only. It shows collection health,
prices, predictions, calibration, model promotion decisions, and paper equity.
It does not place trades. The score display and CLI exclude, and explicitly
count, predictions whose call-time reference data exceeded the configured
freshness ceiling.

```bash
python -m andy_trader.portfolio --predictor baseline:momentum --instrument BTC-USD --horizon 1h
python -m andy_trader.dashboard
```

## September 5 collection incident

The local network presented an untrusted certificate chain for Bybit,
Coinbase, and Kraken. Certificate verification remains enabled: there is no TLS
bypass, DNS override, or proxy in this project. The unattended cycle now stops
retrying a certificate failure, journals each phase to `.cycle-run.jsonl`, and
uses CoinGecko only for instruments missing a usable 1h primary result.

At 09:32 PHT the real scheduled task completed successfully under that path:
all 16 Bybit requests failed, CoinGecko recovered seven of eight instruments,
70 fresh calls were recorded, LINK-USD abstained, and the task returned 0.
Calls created earlier during the outage include 230 references older than 90
minutes. Those rows remain immutable audit evidence, but the scorer's quality
gate excludes and reports them rather than pretending they were honest 1h/4h
forecasts. Future live and paper decisions also abstain on stale input. Each
network request is capped at one eight-second attempt because the next
15-minute scheduled pass is the retry; this bounds a fully hung cycle below its
cadence instead of compounding retries inside retries.

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
