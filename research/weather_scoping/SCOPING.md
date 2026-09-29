# Weather Repricing: Scoping Note

Status: scoping only, 2026-09-30. Nothing built, nothing collected beyond two read-only checks.
Source idea: Phase 3 notes (`research/strategy_notes/phase3_strategy_notes.md`), candidate 1.

## Why This One

The two ideas tested overnight (complete sets, late favorites) had no edge because the market already
prices everything they use. Weather markets settle on a physical measurement that updates on a public
schedule, so an edge can come from **reading the settlement station faster or better than the
market reprices**, not from the market's own prices.

## Size (from `weather_market_inventory.py`, public markets table)

| Year | Markets | Events | Volume |
| --- | --- | --- | --- |
| 2024 | 47 | 10 | $23.0M |
| 2025 | 6,124 | 887 | $112.5M |
| 2026 (to July) | 82,059 | 7,758 | $651.8M |

Almost all are daily "Highest temperature in <city>" buckets. Largest series by volume: London
($87.9M), NYC ($72.2M), Seoul ($48.8M), Hong Kong ($32.5M), Shanghai ($25.9M), Paris ($20.8M).

## Data Availability (Checked)

- Free official observations: aviationweather.gov METAR API returned live reports for KLGA (New
  York LaGuardia) and EGLC (London City Airport) with no key, at least hourly, KLGA with a tenths
  temperature remark.
- Market history: the public markets table has every weather market with outcomes; fills are in the
  same Hugging Face dataset (not yet extracted for weather).

**Settlement confirmed from the official rules text** (`settlement_rules.py`, Gamma API, July 2026
events):

| City | Settlement source and station | Precision |
| --- | --- | --- |
| London | Wunderground, London City Airport (EGLC) | whole deg C |
| NYC | Wunderground, LaGuardia (KLGA) | whole deg F |
| Seoul | Wunderground, Incheon Intl (RKSI) | whole deg C |
| Shanghai | Wunderground, Pudong Intl (ZSPD) | whole deg C |
| Paris | Wunderground, Paris-Le Bourget (LFPB) | whole deg C |
| Hong Kong | Hong Kong Observatory "Absolute Daily Max" (Daily Extract) | 0.1 deg C |

Wunderground's airport history is built from the same METAR reports the free feed serves, so METAR
is effectively the settlement data for five of the six. Rules also say a market cannot resolve until
the next day's first data point is published, and revisions count until then. Unit conversion and
rounding (METAR reports whole or tenths deg C; NYC settles in whole deg F) must be replicated exactly
in any replay. Hong Kong needs the Observatory's own data instead.

## Proposed Paper Test (Fits The Existing Gate)

1. **Hypothesis:** after a new METAR report raises the day's observed high, the buckets it rules out
   (below the new high) stay mispriced for long enough to buy the complement at a net-of-fee edge.
2. **Collector:** poll METAR for the top 6 cities and snapshot the matching Polymarket books on every
   new report; log calls as Andy Trader predictions (no orders).
3. **Kill if:** fewer than 200 independent settled calls, non-positive lifetime or recent Brier skill,
   hit rate below fee break-even, or the gap closes before a realistic polling delay.
4. **Historical first:** replay 2026 fills against historical METAR to see whether impossible buckets
   ever traded after the report that ruled them out, before building anything live.

Effort: medium for the historical replay, large for a live collector.
