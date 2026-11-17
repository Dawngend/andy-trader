# Scout maker label check, 2026-11-17

Paper only. Question: does the rule "any `MAKER_REBATE` row means `market_maker`" in `strategy_label` mislabel wallets
like `dromedaryy`, and can a rebate share replace it?

## Verdict: NO CLEAR GAP, CODE UNCHANGED

- 35 of the 50 wallets in the newest run are labelled `market_maker`. 25 of them are market makers only because of the
  rebate rule. 10 have the `inventory_heavy` flag.
- The rebate share does not separate the two groups. The 10 inventory-heavy wallets have the lowest rebate shares
  (nine at 0.00%, one at 0.20%). The 25 rebate-only wallets run from 0.20% to 2.80% with no break wider than 4 rows
  out of 500.
- The share goes the wrong way. The wallets with the highest rebate share are the slowest ones, and the bot-like ones
  all have a single rebate row. A minimum share would keep the slow wallets as market makers and release the fast ones.
- So no `MAKER_REBATE_SHARE` was added. `strategy_label`, the tests, `copy_candidates` and `profile_flags` are as they
  were. `dromedaryy` stays `market_maker`. No wallet changes label.

The rebate rule does look too wide: it decides 25 of 35 labels on at most 2.8% of the activity rows. But this data
gives no share to replace it with.

## Data

- `D:\Andy Trader\crypto_observations.db`, table `wallet_profiles`, opened read only.
- Newest run only: `8568eb30624b4fe69ea05a29adfd015a`, 2026-11-16 15:13 UTC, leaderboard `MONTH`, 50 wallets.
- Labels in the run: 35 `market_maker`, 12 `mixed`, 3 `longshot`. The stored label matches the current rule for all 50.
- `activity_count` tops out at 500 rows, the size of the one activity page the scout reads. 48 of the 50 wallets are at
  500. The two below are `Abkha` (289) and `8m1` (47).
- 26 wallets have at least one rebate row: the 25 rebate-only ones and `Tokor`, which is also inventory heavy.

## Market makers, sorted by rebate share

Rebate share is `maker_rebate_count / activity_count`. "Window, days" is the time from the oldest to the newest
activity row; it is not asked for in the task but explains the result. Names that are an address are shortened.

| Wallet | Rebate rows | Activity rows | Rebate share | Inventory heavy | Half-price share | Split/merge share | Buys per hour | Sample size | Window, days |
|---|---|---|---|---|---|---|---|---|---|
| wokerjoesleeper | 14 | 500 | 2.80% | no | 0.0% | 8.4% | 0.31 | 2000 | 25.98 |
| UnderperformingForALiving | 10 | 500 | 2.00% | no | 7.8% | 1.8% | 0.21 | 373 | 13.24 |
| 0x492442...3782 | 9 | 500 | 1.80% | no | 2.2% | 0.0% | 0.11 | 1766 | 143.70 |
| juice-fruit | 9 | 500 | 1.80% | no | 0.0% | 0.0% | 1.69 | 71 | 11.52 |
| 0xa2cd4c...2ba0 | 6 | 500 | 1.20% | no | 0.0% | 0.0% | 0.13 | 19 | 147.77 |
| dromedaryy | 5 | 500 | 1.00% | no | 7.9% | 0.2% | 4.49 | 89 | 4.42 |
| ArielFilipino | 5 | 500 | 1.00% | no | 0.8% | 0.0% | 1.13 | 119 | 14.40 |
| 0x448861...e319 | 4 | 500 | 0.80% | no | 1.2% | 0.0% | 4.31 | 2000 | 4.08 |
| Abkha | 2 | 289 | 0.69% | no | 0.0% | 0.0% | 0.26 | 4 | 44.21 |
| Diabolical-Prize | 3 | 500 | 0.60% | no | 2.0% | 0.0% | 0.61 | 202 | 31.00 |
| PacersThreeandahalf | 3 | 500 | 0.60% | no | 0.8% | 1.4% | 5.28 | 2000 | 3.13 |
| nb1985 | 3 | 500 | 0.60% | no | 2.2% | 0.0% | 0.27 | 45 | 66.25 |
| paddaa | 3 | 500 | 0.60% | no | 0.2% | 1.2% | 3.85 | 2000 | 3.33 |
| Mooseborzoiii | 2 | 500 | 0.40% | no | 0.0% | 0.0% | 9.99 | 51 | 2.04 |
| justwins | 2 | 500 | 0.40% | no | 8.7% | 0.0% | 12.47 | 585 | 1.56 |
| rdba | 2 | 500 | 0.40% | no | 3.9% | 0.6% | 0.00 | 255 | 14.59 |
| Woaifacai | 2 | 500 | 0.40% | no | 0.4% | 0.0% | 6.46 | 523 | 2.48 |
| 0x2c3350...0563 | 1 | 500 | 0.20% | no | 0.9% | 0.0% | 27.82 | 2000 | 0.66 |
| monkeymashingkeyboard | 1 | 500 | 0.20% | no | 4.6% | 0.0% | 19.63 | 883 | 1.03 |
| robloxgamblemaster-2008-10-18 | 1 | 500 | 0.20% | no | 2.8% | 0.2% | 28.68 | 2000 | 0.72 |
| Tokor | 1 | 500 | 0.20% | yes | 0.1% | 10.0% | 16.60 | 2000 | 0.96 |
| Contestable-Parfait | 1 | 500 | 0.20% | no | 0.0% | 8.6% | 30.04 | 502 | 0.63 |
| kaxel78 | 1 | 500 | 0.20% | no | 0.1% | 0.6% | 10.02 | 2000 | 0.95 |
| gmpm2 | 1 | 500 | 0.20% | no | 4.5% | 0.8% | 41.85 | 1000 | 0.48 |
| 0xe16d3f...5e30 | 1 | 500 | 0.20% | no | 0.0% | 0.0% | 21.50 | 1338 | 0.93 |
| CUTJALENHURTS | 1 | 500 | 0.20% | no | 1.4% | 1.6% | 15.07 | 2000 | 1.13 |
| e46m3 | 0 | 500 | 0.00% | yes | 0.1% | 46.4% | 34.44 | 2000 | 0.24 |
| balthazar | 0 | 500 | 0.00% | yes | 0.2% | 16.4% | 93.01 | 2000 | 0.15 |
| xtxpleasecallme | 0 | 500 | 0.00% | yes | 0.1% | 30.8% | 378.39 | 2000 | 0.04 |
| 0xb0e74d...39d2 | 0 | 500 | 0.00% | yes | 0.1% | 30.8% | 0.00 | 2000 | 0.49 |
| 0x62d56a...3d4f | 0 | 500 | 0.00% | yes | 0.0% | 23.2% | 0.00 | 2000 | 1.33 |
| Lucerys | 0 | 500 | 0.00% | yes | 0.1% | 11.6% | 54.86 | 2000 | 0.21 |
| 8m1 | 0 | 47 | 0.00% | yes | n/a | 68.1% | 0.00 | 0 | 14.59 |
| 0x9b129a...c2d5 | 0 | 500 | 0.00% | yes | 0.0% | 12.6% | 40.35 | 2000 | 0.37 |
| qwen300.fund | 0 | 500 | 0.00% | yes | 0.0% | 12.0% | 155.88 | 2000 | 0.10 |

Rebate-only wallets: 25. Inventory-heavy wallets: 10, all through the split/merge share (10.0% to 68.1%); none reaches
the 20% half-price share.

## Why no share is set

The task asks for the rebate share that separates clear market makers (inventory heavy, high split/merge or half-price
share, bot-like pace) from rebate-only ones. There is none.

- Clear market makers are at the bottom of the table, not the top. Nine of the ten inventory-heavy wallets have no
  rebate row and the tenth has one. Any share above 0.20% is passed by rebate-only wallets alone.
- The rebate-only shares have no break. The distinct values are 0.20%, 0.40%, 0.60%, 0.69%, 0.80%, 1.00%, 1.20%, 1.80%,
  2.00% and 2.80%. The two widest steps are 3 rows (1.20% to 1.80%) and 4 rows (2.00% to 2.80%). A line in the first
  would rest on the four wallets above it, a line in the second on one.
- The wallets above those steps do not look like market makers on the other measures. The top four buy 0.11 to 1.69
  times an hour; none is bot-like or inventory heavy. `wokerjoesleeper` comes nearest, with 8.4% split/merge, under the
  10% limit.
- The five bot-like rebate-only wallets all have one rebate row. All nine rebate-only wallets that buy more than 10
  times an hour have one or two.
- The rebate count follows the length of the window, not the trading. Rebate-only wallets whose 500 rows cover under
  2 days have 1 or 2 rebate rows; 2 to 5 days, 2 to 5 rows; 5 days or more, 2 to 14 rows. No wallet has more rebate
  rows than its window has days, rounded up (`dromedaryy`: 5 rows in 4.42 days). That fits a rebate paid about once a
  day to any wallet with some maker fills, which is a guess from the counts: the table stores no rebate rows or
  amounts. If so, with 500 rows fixed, the share measures how slowly a wallet trades.

What each share would do to the 25 rebate-only wallets, for the record:

| Rebate rule applies at share | Rebate-only wallets still `market_maker` |
|---|---|
| any row (now) | 25 |
| 0.40% | 17 |
| 0.60% | 13 |
| 0.80% | 8 |
| 1.00% | 7 |
| 1.20% | 5 |
| 1.80% | 4 |
| 2.00% | 2 |
| 2.80% | 1 |

## dromedaryy

Address `0x6575759174ad221bba9947bc3f18750ec769b198`, leaderboard rank 40.

| Item | Value |
|---|---|
| Label | `market_maker`, from the rebate rule only |
| Flags | `thin_sample` |
| Rebate rows | 5 of 500, share 1.00%, sixth highest of the 25 rebate-only wallets |
| Activity window | 4.42 days, 476 buys, 4.49 buys per hour (bot limit is over 20) |
| Half-price share | 7.9% (limit 20%) |
| Split/merge share | 0.2% (limit 10%) |
| Sample size | 89 closed positions (minimum 200) |
| Wins | 74.2% |
| Return on cost | 43.6% ($1,658,660 on $3,805,800) |
| Loss to win ratio | 0.69 |

Nothing but the five rebate rows points to market making. With the rebate rule off its label would be `mixed`. It
would still not be a copy candidate in this run: `thin_sample` excludes it, and its 74.2% wins are above the 65% limit.

With the rebate rule off entirely, 23 of the 25 rebate-only wallets would be `mixed`, `wokerjoesleeper` `bond` and
`kaxel78` `longshot`. The run has no copy candidates now; three wallets would pass the filters (`0x448861...e319`,
`UnderperformingForALiving`, `paddaa`). Dropping the rule is outside this task and was not done.

## What the data cannot show

- A 500-row activity window. Among the 35 market makers it covers 0.04 days for the fastest wallet and 147.77 days
  for the slowest, so rebate counts and shares are not measured over the same time from one wallet to the next.
- A single scout run of 50 wallets. Shares move by 0.2% per rebate row, so one more or one fewer row reorders the
  table. Earlier runs were not read.
- How much of a wallet's trading is maker fills. The table stores the count of rebate rows only, not their amounts or
  the wallet's maker volume.
- Whether rebates are paid once a day. The counts fit it; the rows that would confirm it are not stored.
- Whether a rebate-only wallet is safe to copy. `dromedaryy`'s copy record is not in this table and was not checked.
- Whether the inventory-heavy wallets earn rebates outside their window. Their 500 rows cover 0.04 to 1.33 days
  (`8m1`: 47 rows over 14.59 days).

## Code change

None. The data shows no clear gap, so step 3 of the task does not apply. `andy_trader/wallet_scout.py` and
`tests/test_wallet_scout.py` are untouched.
