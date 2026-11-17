# Copy book token concentration, 2026-11-17

Paper only. Wallets `dromedaryy` and `sharky6999`.

## Finding

The copy book opened 100 shares for every buy a wallet made, and both wallets buy the same token many times. So the
settled results are a few large positions, not many separate ideas.

- `dromedaryy`: 104 settled copies on 13 tokens, +$3,192.53. One token has 51 of the copies and +$2,535.84 of the
  profit. Without it the total is +$656.70. 4 of the 13 tokens made money.
- `sharky6999`: 60 settled copies on 14 tokens, +$68.58. One token has 17 of the copies. 10 of the 14 tokens made money.

Counting only the first copy on each token:

| Wallet | Tokens | Wins | Pnl, first copy only | Pnl, all copies | Pnl, later copies |
|---|---|---|---|---|---|
| dromedaryy | 13 | 4 | -$70.92 | +$3,192.53 | +$3,263.45 (91 copies, 72 wins) |
| sharky6999 | 14 | 10 | -$123.96 | +$68.58 | +$192.54 (46 copies, 41 wins) |

All 72 later wins of `dromedaryy` are on two events: 50 on the esports game and 22 on the two Texas governor tokens.

## Data

- `copy_book_positions` in `D:\Andy Trader\crypto_observations.db`, rows with `settled_at` set, read
  2026-11-17 06:33 UTC. Database opened read only.
- `dromedaryy` copies were opened from 2026-10-29 to 2026-11-14 and settled on 2026-11-14 and 2026-11-15.
  `sharky6999` copies were opened from 2026-10-28 to 2026-10-30 and all settled on 2026-11-14.
- "First copy" is the copy with the smallest `their_timestamp` on a token. It is also the first row stored for that
  token in every case.
- Each copy is 100 shares. Pnl is after the fee.

## Settled copies per token, dromedaryy

| Title | Outcome | Copies | Pnl | Average fill | Wins |
|---|---|---|---|---|---|
| LoL: Bilibili Gaming vs Gen.G - Game 4 Winner | Bilibili Gaming | 51 | +$2,535.84 | 0.485 | 51 |
| Will the Democrats win the Texas governor race in 2026? | Yes | 17 | +$1,059.85 | 0.360 | 17 |
| Billionaire one-time wealth tax passes in California election 2026? | Yes | 9 | -$249.22 | 0.263 | 0 |
| Will the Republicans win the Texas governor race in 2026? | No | 7 | +$433.82 | 0.364 | 7 |
| Will the Republicans win the Florida governor race in 2026? | No | 7 | -$256.15 | 0.350 | 0 |
| Will the Republicans win the Nebraska Senate race in 2026? | No | 4 | -$113.51 | 0.270 | 0 |
| Will an independent win the Nebraska Senate race in 2026? | Yes | 2 | -$58.82 | 0.280 | 0 |
| Will the Democrats win the Florida governor race in 2026? | Yes | 2 | -$73.19 | 0.350 | 0 |
| Nuggets vs. Suns | Nuggets | 1 | +$7.48 | 0.920 | 1 |
| Will the Democrats win the Florida Senate race in 2026? | Yes | 1 | -$13.79 | 0.130 | 0 |
| Will the Republicans win the Florida Senate race in 2026? | No | 1 | -$13.79 | 0.130 | 0 |
| Will the Republicans win the Michigan Senate race in 2026? | Yes | 1 | -$28.38 | 0.270 | 0 |
| UFC 334: Jared Gordon vs. Melquizael Costa (Lightweight, Early Prelims) | Jared Gordon | 1 | -$37.61 | 0.360 | 0 |
| All, 13 tokens | | 104 | +$3,192.53 | | 76 |

## Settled copies per token, sharky6999

| Title | Outcome | Copies | Pnl | Average fill | Wins |
|---|---|---|---|---|---|
| Will Ethereum reach $2,500 in October? | No | 17 | +$109.54 | 0.931 | 17 |
| Will Ethereum reach $2,600 in October? | No | 6 | +$22.97 | 0.959 | 6 |
| Will Ethereum reach $2,700 in October? | No | 6 | +$14.56 | 0.974 | 6 |
| Will XRP reach $1.40 in October? | No | 5 | +$132.53 | 0.721 | 5 |
| Will Solana reach $110 in October? | No | 5 | +$27.33 | 0.942 | 5 |
| Will Ethereum dip to $2,200 on October 29? | No | 5 | -$225.79 | 0.439 | 0 |
| Will XRP dip to $1.20 in October? | No | 3 | +$97.54 | 0.659 | 3 |
| Will Bitcoin reach $85,000 in October? | No | 3 | +$19.11 | 0.932 | 3 |
| Will there be no change in Fed interest rates after the October 2026 meeting? | Yes | 3 | +$11.66 | 0.958 | 3 |
| Will Bitcoin reach $82,000 on October 29? | No | 2 | +$4.33 | 0.977 | 2 |
| Will Ethereum dip to $2,200 in October? | No | 2 | -$31.45 | 0.148 | 0 |
| Will Bitcoin dip to $78,000 on October 28? | No | 1 | +$5.46 | 0.942 | 1 |
| Will Bitcoin dip to $77,000 on October 29? | No | 1 | -$24.29 | 0.231 | 0 |
| Will Bitcoin dip to $79,000 on October 29? | No | 1 | -$94.92 | 0.946 | 0 |
| All, 14 tokens | | 60 | +$68.58 | | 51 |

## First copy on each token

| Wallet | Title | Outcome | Fill | Their price | Delay, seconds | Win | Pnl |
|---|---|---|---|---|---|---|---|
| dromedaryy | Will the Republicans win the Texas governor race in 2026? | No | 0.355 | 0.362 | 1060 | 1 | +$62.92 |
| dromedaryy | Will the Democrats win the Texas governor race in 2026? | Yes | 0.360 | 0.360 | 1025 | 1 | +$62.39 |
| dromedaryy | Nuggets vs. Suns | Nuggets | 0.920 | 0.810 | 372 | 1 | +$7.48 |
| dromedaryy | Will the Republicans win the Michigan Senate race in 2026? | Yes | 0.270 | 0.240 | 1172 | 0 | -$28.38 |
| dromedaryy | Will the Republicans win the Nebraska Senate race in 2026? | No | 0.280 | 0.270 | 726 | 0 | -$29.41 |
| dromedaryy | Will an independent win the Nebraska Senate race in 2026? | Yes | 0.280 | 0.263 | 723 | 0 | -$29.41 |
| dromedaryy | Billionaire one-time wealth tax passes in California election 2026? | Yes | 0.270 | 0.240 | 1210 | 0 | -$28.38 |
| dromedaryy | Will the Democrats win the Florida governor race in 2026? | Yes | 0.350 | 0.350 | 71632 | 0 | -$36.59 |
| dromedaryy | Will the Republicans win the Florida governor race in 2026? | No | 0.350 | 0.340 | 64188 | 0 | -$36.59 |
| dromedaryy | Will the Democrats win the Florida Senate race in 2026? | Yes | 0.130 | 0.120 | 25156 | 0 | -$13.79 |
| dromedaryy | Will the Republicans win the Florida Senate race in 2026? | No | 0.130 | 0.120 | 25153 | 0 | -$13.79 |
| dromedaryy | LoL: Bilibili Gaming vs Gen.G - Game 4 Winner | Bilibili Gaming | 0.480 | 0.470 | 66 | 1 | +$50.25 |
| dromedaryy | UFC 334: Jared Gordon vs. Melquizael Costa (Lightweight, Early Prelims) | Jared Gordon | 0.360 | 0.100 | 318 | 0 | -$37.61 |
| dromedaryy, 13 tokens | | | | | | 4 | -$70.92 |
| sharky6999 | Will Bitcoin dip to $78,000 on October 28? | No | 0.942 | 0.856 | 433 | 1 | +$5.46 |
| sharky6999 | Will there be no change in Fed interest rates after the October 2026 meeting? | Yes | 0.957 | 0.951 | 1397 | 1 | +$4.01 |
| sharky6999 | Will Ethereum reach $2,500 in October? | No | 0.850 | 0.870 | 1450 | 1 | +$14.11 |
| sharky6999 | Will Ethereum reach $2,700 in October? | No | 0.974 | 0.967 | 1248 | 1 | +$2.43 |
| sharky6999 | Will Ethereum reach $2,600 in October? | No | 0.955 | 0.953 | 988 | 1 | +$4.20 |
| sharky6999 | Will XRP reach $1.40 in October? | No | 0.721 | 0.701 | 862 | 1 | +$26.47 |
| sharky6999 | Will Bitcoin reach $85,000 in October? | No | 0.870 | 0.860 | 57 | 1 | +$12.21 |
| sharky6999 | Will Solana reach $110 in October? | No | 0.930 | 0.940 | 1137 | 1 | +$6.54 |
| sharky6999 | Will Bitcoin dip to $79,000 on October 29? | No | 0.946 | 0.919 | 1265 | 0 | -$94.92 |
| sharky6999 | Will XRP dip to $1.20 in October? | No | 0.670 | 0.650 | 504 | 1 | +$31.45 |
| sharky6999 | Will Ethereum dip to $2,200 on October 29? | No | 0.977 | 0.284 | 454 | 0 | -$97.86 |
| sharky6999 | Will Bitcoin reach $82,000 on October 29? | No | 0.979 | 0.930 | 258 | 1 | +$1.96 |
| sharky6999 | Will Bitcoin dip to $77,000 on October 29? | No | 0.231 | 0.200 | 145 | 0 | -$24.29 |
| sharky6999 | Will Ethereum dip to $2,200 in October? | No | 0.148 | 0.100 | 143 | 0 | -$15.72 |
| sharky6999, 14 tokens | | | | | | 10 | -$123.96 |

## What the data cannot show

- 13 and 14 tokens are too few to say whether either wallet has an edge, with or without the cap. The first-copy
  totals (-$70.92 and -$123.96) are as weak as the headline totals.
- The tokens are not a spread of markets. For `dromedaryy`, 10 of the 13 are on the 2026 US elections, one is a single
  game of one esports series, one is an NBA game and one is a UFC fight. For `sharky6999`, 13 of the 14 are crypto
  price targets for the last days of October and one is the Fed meeting.
- The tokens are not independent either. Going by the titles, the 13 `dromedaryy` tokens are 9 events: the Texas
  governor, Florida governor, Nebraska Senate and Florida Senate races each have two tokens that settle together.
  The cap is per token, so one race can still hold two copies.
- The order book price and delay for the later copies. The 51 copies on the esports game were opened within 14
  seconds in one pass, each filled at 0.48 or 0.49 from its own order book read. A paper fill does not take shares
  out of the book, so whether 5,100 shares were on offer at that price is not recorded. With the cap, later buys
  are skipped before the order book read, so from now on no price or delay is stored for them.
- 4 first copies and 18 later copies of `dromedaryy` were opened more than 35 minutes after the wallet's buy, up to
  19.9 hours. They are from before the `stale` rule and would not be copied today.
- Whether scaling in is the wallet's skill. All of the `dromedaryy` profit is in the later copies (+$3,263.45), on two
  events. That fits a wallet that adds to its best ideas, and it fits one esports game and one governor race going
  its way. Two events cannot tell these apart. The cap gives up that profit if it was skill.

## Code change

`MAX_COPIES_PER_TOKEN = 1` in `andy_trader/copy_book.py`. `copy_wallet_trades` skips a buy with the reason
`token_cap` when `copy_book_positions` already has a copy, open or settled, for that wallet and token. The check runs
before the order book read. Open copies from before the change are left as they are (`dromedaryy` has 19 open copies
on 6 tokens, up to 7 on one token); new buys on those tokens are skipped.
