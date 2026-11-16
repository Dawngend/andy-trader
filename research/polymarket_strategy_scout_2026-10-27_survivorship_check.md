# Polymarket Strategy Scout, 2026-10-27

Read-only research. No orders, no logins, no credentials. Paper-trading only. Numbers come from the public
data API (`data-api.polymarket.com`) pulled on 2026-10-27, unless a row says otherwise. Web claims are leads until
an API pull backs them. Companion file: `research/wallet_scout_wide_2026-10-27.md` (the raw 30-account scout table). A report with the name `polymarket_strategy_scout_2026-10-27.md` was already committed by an earlier run, so this one is saved as `polymarket_strategy_scout_2026-10-27_survivorship_check.md` and the old file is untouched.

## The one finding that changes how to read every scout table

**The `closed-positions` endpoint hides most losing positions.** A market that resolves worthless stays in
`/positions` as an unredeemed row with `curPrice` 0 and never shows up in `closed-positions`. So win rate and
return on cost from the closed-positions sample look far better than the truth.

Proof, both pulled from the API:

| Account | Closed-positions only | Add unredeemed resolved positions | All-time leaderboard PnL |
|---|---|---|---|
| Diabolical-Prize | 202 positions, 94.6% win, +$18.19M | 414 positions, 49.5% win, +$1.87M (195 worthless rows hid -$16.33M) | +$1.67M |
| SmartPredictOrNot | 4 positions, 100% win, +$3.49M | 7 positions, 57.1% win, -$0.61M | -$0.68M |

I re-ran 20 accounts with the unredeemed resolved rows added back. Where the closed sample covers the account's whole
history (fewer than 2,000 rows, so the 2,000-row cap did not cut it), the corrected total matches the all-time
leaderboard PnL closely: primm 3.439M vs 3.433M, Ended. 48,019 vs 48,019, Jsram 2.25M vs 2.35M,
dromedaryy 0.839M vs 0.836M, monkeymashingkeyboard 0.767M vs 0.750M, flawfence 1.11M vs 1.05M,
juice-fruit 0.79M vs 0.84M, BreakTheBank 11.95M vs 10.99M. I call those **reconciled**. Accounts that hit the
2,000-row cap are **truncated**: their corrected totals are biased negative (old wins fell out of the window, old
losses stayed in) and I do not use their win rates.

Method limit: the open-position side has no close date, so reconciled means "total matches", not "every row dated".
Also, the existing scout table (`WALLET_SCOUT.md` method) has this same bias. Its win rates and return on cost
should not be used to pick accounts. **This also affects the committed `research/polymarket_strategy_scout_2026-10-27.md`
(the 2026-10-28 draft):** its figures for Diabolical-Prize (95.6% win, +93.21% on cost), primm (86.56% win in the 0.50 to
0.80 band, +91.54% overall) and monkeymashingkeyboard (91.75% win, +76.39%) are closed-positions only. Corrected, Diabolical-Prize is
49.5% win and +5.1% on cost, primm is 46.8% win and +16.2%, monkeymashingkeyboard is 53.6% win and +5.5%. Its sharky6999 figures hold up
because that account has almost no unredeemed rows. I did not edit that file; this report sits beside it.

Other scout observations (30 accounts, MONTH leaderboard):
- No account got the `bond` or `price_target_no` label. The `market_maker` label fires on any single MAKER_REBATE row, so 20 of 30 got it. It is too broad to mean anything.
- Almost all top-30 profit is sports and event trading in the 0.30 to 0.80 entry range, not 0.80 to 0.98 bonding.
- Ten of the 30 have sample windows of five days or less (2,000 rows in a few days = high-frequency bots).

## Strategy table

Evidence levels: **A** = reconciled to all-time leaderboard PnL from the public API. **B** = large academic dataset
(figures were extracted by a tool from the PDF, re-check before quoting). **C** = one web source, not verified.
**D** = promotional or snippet-only lead.

| # | Strategy | Who (public name) | Measured evidence | Speed needed? | Informed or insider? | Replicability (small paper book, live prices) | Level | Verdict |
|---|---|---|---|---|---|---|---|---|
| 1 | **Bonding**: buy at 0.90 to 0.98, hold to resolution | Ended. (83 entries at 0.90+), flawfence (63), BreakTheBank (29), anoin123 (388) | Ended.: 88.0% win, +0.79% on cost, +$7,545, worst -$15,341, 2026-01-04 to 2026-10-24, all-time PnL only +$48,019. flawfence: 77.8% win, +1.49%, +$32,133, worst -$7,330. BreakTheBank: 79.3% win, +1.95%, +$70,943, worst -$45,500. **anoin123: 50.3% win, -8.93%, -$3.03M, worst -$651,479.** arXiv (588M trades): favorites at 0.90 and up earned +0.83% pooled (95% CI 0.60 to 1.07) and +0.277% equal-weighted per market, 2022-11 to 2026-03. Our own 2026-09-30 report: 95%+ crypto up/down favorites turn negative after taker fee at paid prices. | No | No | High. Easy to run on live asks. Edge is thin (about 0 to 2%), one loss erases 19 to 32 wins at 0.95 to 0.97. | A + B | **Copy, with filters** (candidate 1) |
| 2 | **Delayed follow of sports accounts with a long reconciled record** | primm, flawfence, Jsram | primm: 3,279 positions, 2025-08-25 to 2026-10-27, 46.8% win, +16.2% on cost, avg win $8,427 vs avg loss $5,429, worst -$225,995, +$3.44M. Under-0.50 entries: 1,912 positions, 36% win, +30.4% on cost. flawfence: 757 positions, 47.8% win, +8.9%, avg win $11,784 vs loss $8,149, worst -$236,785, +$1.11M. Jsram: 304 positions, 54.9% win, +8.5%, worst -$782,427, +$2.25M. | Partly. They fill early at quoted prices; a follower pays worse prices. Paper test must use the price at the delay, not their price. | Unknown. Sports sharps may just be better informed; cannot rule out. | Medium. Needs their new trades from `/activity`; edge can decay or vanish after delay. | A | **Test** (candidate 2) |
| 3 | **Large sports edge, no clear pattern** | BreakTheBank (+$11.95M corrected), kch123 (+$11.35M all-time) | BreakTheBank: 1,350 positions, 42.4% win, +8.2%, avg win $123,131 vs loss $76,445, worst -$1,143,612. kch123: all-time +$11.35M on $293M volume (3.9%), but his corrected sample is truncated so no win rate used. | Unknown | Possibly informed or very sharp | Low. Position sizes are 5 to 6 figures; moves the book. | A (BTB) | **Skip** for now |
| 4 | **Buy NO on far price targets** ("will X hit $Y") | sharky6999 (found in the committed 2026-10-28 report; I re-pulled it myself) | Re-pulled 10,000 closed rows plus 10 unredeemed resolved rows (no survivorship problem here: only 21 open rows in total). NO side at 0.80 and up in dip/reach/hit titles: 1,147 positions, 98.3% win, +2.53% on cost, +$47,027, avg win $62 vs avg loss -$1,209 (about 19 wins per loss), worst -$8,623. All entries at 0.99 and up: 7,352 positions, 99.4% win, +0.04% on cost, worst -$73,967. Entries 0.80 to 0.95: 507 positions, 96.8% win, +6.1%. Sample is capped at 10,000 rows (about 7 months); all-time leaderboard PnL is +$948,723 vs +$209,029 in the sample, so older history is not covered. The earlier report also saw the return fade from the first half to the second half. | No | No | High, and it is just bonding on one market type | A for survivorship, one account only | **Test later** as a tagged slice of strategy 1. Do not enter at 0.99 and up. |
| 5 | **Market making and rebates** | Looks like many, but the label is unreliable (see above). e46m3 is inventory heavy, 77.9 buys per hour | Rebates are real: maker rebates are funded by taker fees, 20% in crypto and 15% in sports shares of fee, 25% most others, minimum $1 payout (Polymarket docs, opened). No account I checked is a clean maker with reconciled data. e46m3 corrected sample is truncated and negative (-$879,628 on 6,505 rows, ~1 day). | Yes. Needs queue position and fast cancel; paper fills are not trustworthy. | No | Low | C | **Skip** |
| 6 | **Crypto 5 and 15 minute up/down bots, latency or arbitrage** | Claims only (a Yahoo article about a "98% win" bot and "$2.2M in two months", snippets only) | Not verified. Our own 2026-09-30 research found no fee-proof edge in 134,253 resolved rounds and that complete-set "arbitrage" was reading skew. | Yes | No | Low | D | **Skip** |
| 7 | **Jev decision-model trading** | markusbug/jevymarket (GitHub); nobody with a verified account | Repo is live-by-default ("places real orders with real money"), shows no PnL. Only measured item is a model score going from 0.27 to 0.78 when given a research brief, which is not a trading result. A dev.to Jev article is about an FX scalper, not Polymarket. | No | No | Cannot test without an API key and paid calls | C | **Skip**: nothing measured |
| 8 | **"Nothing ever happens" / bet against change** | An unnamed trader said to have made $412K (search snippet of a Reddit post) | Could not open Reddit. No account named. | No | No | Same as strategy 4 | D | **Skip** (unverifiable) |
| 9 | **Early election / news informed whales** | Theo4 (+$22.05M all-time), Fredi9999 (+$16.62M) | Both samples are 22 and 66 closed positions from 2024-08 to 2025-01, concentrated in a few huge bets. | No | Likely informed or concentrated | None | A (facts), nothing replicable | **Skip** (informed) |
| 10 | **Copy-trading marketing figures** | SmartPredictOrNot | polycopy.app shows "+$987K, 80% win" for soccer 30 days. API: all-time -$677,106 (MONTH +$807,348); corrected sample 57.1% win on 7 positions, -$614,439. | n/a | n/a | n/a | D | **Skip**: promotion, misleading |

## Top 3 candidates for a new paper book

These are rules to try, not claims of profit. Judge each against the evidence bar in
`research/real_money_evidence_bar.md` (200 settled calls, fresh window, net of fees and slippage).

### 1. Filtered short-horizon bond book
- Entry: buy the favorite side at best ask between 0.90 and 0.97.
- Skip crypto up/down and "hit/reach" price-target markets in the main book (tag price-target NO as its own slice, strategy 4).
- Resolution within 14 days of entry; resolution source clear in the market text.
- Ask depth of at least the paper size at the quoted price; book the real fill, not the top price.
- Size: no more than 2% of book per position, no more than 5 positions in one event or theme, so one failure costs about 2%, not 19 to 32 wins.
- Record the taker fee and days to resolution on every row; report return per day, not just per trade.
- Success bar: beat the arXiv +0.83% per purchase after fees on at least 200 settled entries; otherwise call it no edge.

### 2. Delayed follow of primm and flawfence (Jsram as third)
- Read only their new BUY rows from `/activity`. Paper-enter at the live ask after a fixed delay, run three delays in parallel (1 minute, 5 minutes, 30 minutes).
- Skip entries above 0.95 and any where the paper fill is more than 3 cents worse than their price.
- Cap 1% of book per entry. Record their price, our price, and the gap; the gap is the main measurement.
- Drop an account if its corrected (closed + unredeemed) PnL stops reconciling with its leaderboard PnL, or after 150 settled paper entries show a negative return.
- Note: an untracked `.copy-book.jsonl` exists in the repo root. I did not open it. This may overlap with work already running.

### 3. Passive-bid variant of the bond book
- Same markets and filters as candidate 1, but post a resting paper bid 1 to 2 cents under the ask (for example 0.93 to 0.95).
- Count a fill only when a real trade prints at or below the bid after the order time; never fill on a quote alone.
- Compare to the taker variant on the same markets. arXiv: favorite purchases by posters of offers earned +0.64% against -0.46% for acceptors (equal-weighted per market).
- This cannot prove queue position, so treat any result as an upper bound.

## Claims I could not verify

- Reddit posts: the "$412K by betting nothing changes" trader, the "$40M of arbitrage but only 0.51% of wallets above a grand" study, the "77.9% of wallets" post, and the Jev "24 hour" and "paper $100K" threads. Reddit blocked both Firecrawl and the in-app browser; I saw only search excerpts, not the posts.
- Yahoo article claims ("98% win bot", "$313 into $414,000", "$2.2M in two months"): search excerpts only, no wallet named.
- SSRN paper "Who Wins and Who Loses" (top 1% of users take 76.5% of profits): the fetch returned a 503. Only the search excerpt was seen.
- X posts (Polysights "Nothing Ever Happens" study, sandra's copy-trading thread): the page fetch returned no text. Cost 34 credits, so I stopped using X.
- Bloomberg 34,000 flagged insider-like trades: I opened the 2026-04-28 Bloomberg article but only the headline summary loaded (over 100,000 accounts lost at least $1,000, almost twice as many as gained that much). The 34,000 figure is from a third-party blog excerpt I did not open.
- kch123 web report (polyresearchrobotics.com) says 78,148 trades, $76.5M wagered, 60.9% win, and "+$25,654 at 77.7% win in a strong-conditions filter" for Jan 1 to Mar 30 2026. The site sells datasets. I could not reproduce any of it; the API shows only the all-time +$11.35M and $293M volume.
- arXiv favorite-longshot figures: extracted from the PDF by a tool; I did not read the PDF page by page.
- Whether Diabolical-Prize, primm, flawfence and others trade on information. Nothing public tells.
- Fee formula at 0.90+ prices. A web guide gave a taker-fee formula; I did not confirm it from Polymarket docs, so fee in candidates must be read from the live fee schedule, not assumed.

## Sources I actually opened

- Polymarket public data API: `https://data-api.polymarket.com/v1/leaderboard`, `/closed-positions`, `/activity`, `/positions` (pulled 2026-10-27)
- https://arxiv.org/pdf/2609.12878 (The Favorite-Longshot Bias in Prediction Markets: Evidence from Polymarket)
- https://docs.polymarket.com/programs/maker-rebates
- https://startpolymarket.com/strategies/bonding/ (page says it contains affiliate links)
- https://github.com/markusbug/jevymarket
- https://dev.to/nodefiend/jev-assisted-llm-trading-ofa
- https://www.bloomberg.com/news/articles/2026-04-28/most-prediction-market-traders-are-losing-money-while-bots-rack-up-gains (summary only)
- https://www.polyresearchrobotics.com/reports/kch123 (sells data)
- https://polycopy.app/best-polymarket-traders (sells copy trading with a 0.5% fee)
- https://quantjourney.substack.com/p/why-77-win-rate-traders-go-broke (paid newsletter; no data source stated)
- Local: `research/REPORT_2026-09-30_polymarket_edges.md`, `research/real_money_evidence_bar.md`, `research/WALLET_SCOUT.md`

## Promotion flags

polycopy.app (copy-trading fee), polyresearchrobotics.com (data sales), startpolymarket.com (affiliate links),
quantjourney (paid newsletter), the Yahoo/BeInCrypto bot article (bot marketing). Treat all four as leads, not evidence.

## How to reproduce

Scripts are in the session scratchpad, not the repo:
`...\scratchpad\verify.py` (account profile) and `...\scratchpad\corrected.py` (closed plus unredeemed resolved rows,
reconciled against the ALL leaderboard). Raw output: `C:\Users\Dawn\Temp\claude-out\poly\corrected_summary.txt` and
`C:\Users\Dawn\Temp\claude-out\poly\scout_wide.db`.
