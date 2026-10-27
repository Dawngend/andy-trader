# Polymarket Strategy Scout Report (2026-10-27)

## Executive Summary

This report evaluates trading strategies deployed by consistently profitable Polymarket accounts, cross-referencing on-chain data pulled directly from the public Polymarket Data API and the local scout database (`scout_wide.db`) against industry documentation, developer repositories, and published analytical literature.

The primary objective is to separate genuine, replicable structural edges from unreplicable speed games, negative-skew traps, and whale-scale conviction wagers, and to formulate concrete paper-trading rules for a small live book.

### Key Empirical Findings

1. **The 99¢ "Bonding" Trap (Catastrophic Skew)**: Naively buying contracts priced at 99¢ and above (`0.99_and_above`) to pick up the final 1¢ payout creates severe negative skew. In the complete 10,000 closed-position dataset for `sharky6999` (`0x751a2b86cab503496efd325c8344e10159349ea1`), 7,355 positions (73.55% of markets and 94.69% of total capital: $90.41M of $95.48M) were deployed at 99¢+, winning 99.39% of trades but yielding a negligible **+$31,865.00 PnL (+0.035% return on cost)**. A single -$73,966.70 loss on a 99.06¢ Bitcoin contract erased over 1,163 average wins ($63.56 average win vs. -$1,810.37 average loss; loss-to-win ratio: 28.48). In account `THEHIGHLIFE` (`0x8c0b024c17831a0dde038547b7e791ae6a0d7aa5`), 115 positions at 99¢+ resulted in **-$25,446.12 net PnL (-1.78% RoC)** despite an 86.96% win rate.
2. **Consensus Bonding (80¢ to 95¢) Reality Check (Held vs. Mitigated Positions)**: While accounts entering high-probability outcomes in the 80¢ to 95¢ band show positive aggregate returns, position-level analysis reveals that this edge **cannot be captured by naively holding positions to binary resolution** unless win rates exceed 97%:
   - `sharky6999`: 502 positions in 0.80–0.95 (wtd avg entry $0.916), **+$51,516.00 PnL, 96.81% win rate, +5.97% RoC** (7-month window). Win rate exceeded entry price, generating positive hold-to-resolution yield.
   - `THEHIGHLIFE`: 67 positions in 0.80–0.95 (wtd avg entry $0.8894), **+$71,067.92 PnL, 83.58% win rate, +8.15% RoC** (6-month window). If held to binary resolution ($0 payout on loss), an 83.58% win rate at an 0.8894 entry is mathematically losing (-5.42% RoC, -$47,272 PnL). Data shows all 11 losing positions were mitigated early (average loss -7.8%, worst loss -12.4%), avoiding full wipeout.
   - `flawfence`: 67 positions in 0.80–0.95 (wtd avg entry $0.8963), **+$70,874.03 PnL, 80.60% win rate, +5.86% RoC** (6-month window). Similarly, losing positions were exited or sold early (-0.7% to -44.6% drawdown), never held to total wipeout.
3. **Buying NO on Far Price Targets ("Reach" or "Dip" Markets)**: Retail participants systematically overpay for out-of-the-money milestone lottery tickets ("Will Bitcoin dip to $60k?", "Will ETH reach $5,000?"). In `sharky6999`'s 10,000 closed positions, fading price milestones on the NO side delivered **+$71,147.55 PnL on 1,448 positions (91.16% win rate, +3.64% RoC)**. Entries on NO between 80¢ and 98¢ achieved a **97.80% win rate and +3.35% RoC on 772 positions**. However, chronological split analysis demonstrates decay: first-half performance was 93.92% win rate (+6.23% RoC), declining in the second half to 88.40% win rate (+2.69% RoC), with the latest 1,000-position sample experiencing overall negative PnL (-$30,683.78) due to tail losses.
4. **Scout Wide Report Account Composition**: The wide report (`scout_wide.db`, 30 accounts) contains **0 non-market-maker, non-bot, non-thin accounts**. Of the 30 accounts:
   - 20 are labelled `market_maker` (due to automated rules flagging any account with `inventory_heavy` or `maker_rebate_count > 0`, even with only 1–3 rebates in 500 events).
   - 10 are non-market-maker (9 `mixed`, 1 `longshot`, 0 `bond`).
   - Of these 10 non-market-maker accounts, 8 are flagged `bot_like` (`Papeasy`, `HomeRunHazard`, `UpTheBlues` with 1,178 buys/hr, `takeormake`, `balthazar`, `omnibus-076daa`, `anoin123`, `monkeymashingkeyboard`), and 2 are flagged `thin_sample` (`eventalpha26` with n=2, `dromedaryy` with n=65).
5. **Reclassification of `primm` (Rejection of Passive MM Hypothesis)**: `primm` (`0xd38b...5029`) was previously mischaracterized as capturing spreads via passive market making. Analysis of 500 activity events reveals **387 BUY trades, 0 SELL trades, 104 REDEEMS, and only 3 MAKER_REBATES (0.60%)**. Across 151 distinct markets, there is **zero two-sided quoting**. Primm is an outright directional bettor (principally esports and sports, generating +$4.36M in the 0.50–0.80 band with an 86.56% win rate and +91.54% overall RoC), not a market maker.

---

## Strategy Table

| Strategy | One-sentence Description | Representative Accounts | Evidence Level | Measured Evidence (Pnl, Win Rate, RoC, Loss/Win, Worst Loss, Window) | Speed Dependency | Insider / Informed Risk | Replicability Rating | Verdict | Reasons |
|---|---|---|---|---|---|---|---|---|---|
| **Buying NO on Far Price Targets** | Buying NO on extreme reach or dip price targets that require unrealistic volatility before expiration. | `sharky6999` (`0x751a...9ea1`) | **Moderate** | `sharky6999`: 1,448 NO positions in dip/reach markets: +$71,147.55 PnL, 91.16% WR, +3.64% RoC; 772 NO positions at 0.80–0.98: +$42,040.23 PnL, 97.80% WR, +3.35% RoC. Window: 2026-03-16 to 2026-10-27 (225 days). First-half RoC +6.23% vs second-half RoC +2.69%. | Low (hours to days) | Low (public spot pricing) | **High** | **test later** | Clear structural edge exploiting retail lottery bias on transparent spot crypto assets; tested in one deep account, but requires staged paper testing given recent performance decay. |
| **High-Probability Consensus Bonding (80¢–95¢)** | Buying consensus outcomes at 80¢–95¢ and managing risk with strict early exit rules rather than holding blind to resolution. | `sharky6999`, `THEHIGHLIFE` (`0x8c0b...7aa5`), `flawfence` (`0x23d8...0288`) | **Weak to Moderate** | `sharky6999`: 502 pos, +$51,516.00 PnL, 96.81% WR, +5.97% RoC; `THEHIGHLIFE`: 67 pos, +$71,067.92 PnL, 83.58% WR, +8.15% RoC; `flawfence`: 67 pos, +$70,874.03 PnL, 80.60% WR, +5.86% RoC. Multi-account windows: 178 to 225 days. | Low to Medium | Low (public consensus lag) | **Medium** | **test later** | Holding to resolution with 80–84% win rates produces negative expectation (-5.42% RoC); viable only if paper rules enforce aggressive stop-loss / early profit-taking. |
| **Directional Mid-Probability Value Betting (50¢–80¢)** | Purchasing undervalued favorites in non-breaking sports and cultural events where implied market odds lag model probabilities. | `primm` (`0xd38b...5029`), `THEHIGHLIFE`, `flawfence` | **Moderate** | `primm`: 945 pos in 0.50–0.80 band: +$4,359,943.17 PnL, 86.56% WR, +62.84% RoC; `THEHIGHLIFE`: 669 pos: +$1,641,155.00 PnL, 81.76% WR, +32.78% RoC; `flawfence`: 166 pos: +$1,573,619.18 PnL, 80.12% WR, +31.25% RoC. Multi-account windows: 180 to 427 days. | Low to Medium | Low to Medium (domain modeling) | **Medium** | **test later** | Accounts generated the bulk of their profits in this band with win rates (80–87%) substantially exceeding entry prices (55–63%); requires domain odds modeling. |
| **99¢ Penny-Picking ("Blind Bonding")** | Scooping up 99¢+ shares expecting guaranteed 1¢ resolution payout. | `sharky6999`, `THEHIGHLIFE` | **Strong (Negative)** | `sharky6999`: 7,355 pos, $90.41M cost, +$31,865.00 PnL, 99.39% WR, +0.035% RoC, worst loss -$73,966.70, loss/win 28.48; `THEHIGHLIFE`: 115 pos, $1.43M cost, -$25,446.12 PnL, 86.96% WR, -1.78% RoC. Windows: 178 to 225 days. | High (speed race for 1¢) | Low | **Low** | **skip** | Catastrophic negative skew. A single tail loss or resolution dispute destroys months of accumulated pennies. |
| **Passive Wide-Spread Market Making** | Posting resting two-sided limit orders around midpoint on wide-spread markets to capture bid-ask spreads. | None confirmed in leaderboard sample | **None / Unverified** | `primm` was mislabeled by automated scout: activity shows 387 buys, 0 sells, and only 3 maker rebates (0.60%). No two-sided quoting found in leaderboard accounts. | High (resting queue priority) | Low | **Medium** | **skip** | Zero empirical evidence in analyzed accounts; theoretical maker models fail to account for adverse selection without confirmed live data. |
| **Informed Sports Syndicate / Line Betting** | High-conviction, large-size betting on sporting event outcomes based on proprietary odds models. | `monkeymashingkeyboard` (`0x684b...8409`), `Diabolical-Prize` (`0x7c37...6aeb`) | **Moderate** | `monkeymashingkeyboard`: 521 sports pos, +$5,101,509.26 PnL, 91.75% WR, +76.39% RoC, worst loss -$90,754.28; `Diabolical-Prize`: 182 sports pos, +$16,714,145.63 PnL, 95.60% WR, +93.21% RoC, median size $71,148.70, 1.8 buys/hour. | Low to Medium (1.8 to 43.6 buys/hr contradicts HFT) | **High (Informed / Syndicate)** | **Very Low** | **skip** | Relies on institutional bankroll ($71k median sizing) and proprietary sports betting feeds; impossible to validate or replicate on a small paper book. |
| **Macro / Political Whale Conviction Betting** | Taking massive directional positions on major binary political or macro events based on private polling or conviction research. | `Theo4` (`0x5668...5839`) | **Weak** | `Theo4`: 22 closed positions, +$22,053,933.75 PnL, 81.82% WR, +109.98% RoC, avg win $1,225,448 vs avg loss -$1,032.84, worst loss -$2,886.12. Median size $25,845, P90 size $3,624,802. Window: 2024-10-14 to 2025-01-01 (78 days). | None (macro horizon) | **High / Whale Conviction** | **Zero** | **skip** | Non-systematic, whale-scale capital deployment on single binary election events; prohibited by project mandate. |
| **AI Discriminative Decision Models (Jev)** | Evaluating structured news briefs via non-autoregressive decision models (e.g. TypeSafe AI Jev) to price odds edges. | None verified on public leaderboard | **None** | Evaluated in developer benchmarks and synthetic repos (`jevymarket`); live wallet PnL and multi-month return on cost on public Polymarket accounts remain **UNVERIFIED**. | Low to Medium (70-500ms inference per developer specs) | Low (public news feeds) | **UNVERIFIED** | **skip** | Purely unverified hypothesis with zero confirmed live wallet track record; omitted from Top 3 candidates in favor of empirically measured strategies. |

---

## Detailed Account Analysis

### 1. `sharky6999` (`0x751a2b86cab503496efd325c8344e10159349ea1`)
- **Sample Window**: 2026-03-16 07:11:00 UTC to 2026-10-27 10:11:00 UTC (10,000 closed positions pulled).
- **Aggregate Performance**: Total Cost: $95,481,322.69 | Realized PnL: +$208,842.54 | Win Rate: 97.52% (9,752 wins, 227 losses, 21 flat) | Return on Cost: +0.219%.
- **Win/Loss Metrics**: Average Win: $63.56 | Average Loss: -$1,810.37 | Loss-to-Win Ratio: 28.48 | Worst Loss: -$73,966.70 (on Bitcoin Up or Down - May 10, 2026, entry 0.9906).
- **Position Sizing**: Median Sizing: $228.40 | P90 Sizing: $10,480.00.
- **Sample Stability & Period Splits**:
  - *Full 10,000 Positions Chronological Split (Count Midpoint)*:
    * First Half (5,000 pos, 2026-03-16 to 2026-05-24): Cost: $45,975,497.63 | PnL: +$134,462.96 | Win Rate: 98.54% | RoC: +0.292%
    * Second Half (5,000 pos, 2026-05-24 to 2026-10-27): Cost: $49,505,825.06 | PnL: +$74,379.58 | Win Rate: 96.50% | RoC: +0.150%
  - *Full 10,000 Positions Time Midpoint Split (Midpoint 2026-07-06)*:
    * First Half (6,471 pos, 2026-03-16 to 2026-07-06): Cost: $70,862,170.04 | PnL: +$194,639.35 | Win Rate: 97.88% | RoC: +0.275%
    * Second Half (3,529 pos, 2026-07-06 to 2026-10-27): Cost: $24,619,152.65 | PnL: +$14,203.20 | Win Rate: 96.85% | RoC: +0.058%
  - *Latest 1,000 Positions Sample (2026-09-25 to 2026-10-27)*: Total Cost: $6,917,177.73 | Realized PnL: -$30,683.78 | First Half PnL: +$17,808.61 | Second Half PnL: -$48,492.38. Reflects severe drawdown from tail wipeouts.
- **Price Band Breakdown (10,000 Positions)**:
  - `0.00_to_0.50`: 242 positions | Cost: $53,015.00 | PnL: +$63,470.00 | WR: 53.72% | RoC: +119.72%
  - `0.50_to_0.80`: 261 positions | Cost: $634,556.00 | PnL: +$6,855.00 | WR: 77.39% | RoC: +1.08%
  - `0.80_to_0.90`: 187 positions | Cost: $226,891.00 | PnL: +$13,852.00 | WR: 93.58% | RoC: +6.11%
  - `0.90_to_0.95`: 315 positions | Cost: $636,435.00 | PnL: +$37,664.00 | WR: 98.73% | RoC: +5.92%
  - `0.95_to_0.98`: 788 positions | Cost: $2,212,671.00 | PnL: +$44,184.00 | WR: 98.60% | RoC: +2.00%
  - `0.98_to_0.99`: 852 positions | Cost: $1,309,221.00 | PnL: +$10,952.00 | WR: 99.41% | RoC: +0.84%
  - `0.99_and_above`: 7,355 positions | Cost: $90,408,533.00 | PnL: +$31,865.00 | WR: 99.39% | RoC: +0.035%
  - *Sum across price bands*: 242 + 261 + 187 + 315 + 788 + 852 + 7,355 = **10,000 positions**.
- **Market Type Breakdown (10,000 Positions)**:
  - `up_down`: 5,993 positions | Cost: $15,552,487.39 | PnL: +$37,272.04 | WR: 99.22% | RoC: +0.24%
  - `price_target` (reach/dip/hit): 1,810 positions | Cost: $6,651,238.47 | PnL: **+$77,971.56** | WR: **91.60%** | RoC: **+1.17%**
  - `sports`: 783 positions | Cost: $2,581,854.00 | PnL: +$6,509.00 | WR: 98.72% | RoC: +0.25%
  - `above_below`: 775 positions | Cost: $2,237,315.00 | PnL: +$1,750.00 | WR: 98.32% | RoC: +0.08%
  - `other` (macro, entertainment, unclassified): 639 positions | Cost: $68,458,429.00 | PnL: +$85,340.00 | WR: 97.34% | RoC: +0.12%
  - *Sum across market types*: 5,993 + 1,810 + 783 + 775 + 639 = **10,000 positions** (Cost: $95,481,322.69 | PnL: +$208,842.54).
- **Outcome Breakdown (10,000 Positions)**:
  - `Up`: 3,008 positions | Cost: $8,035,637.69 | PnL: +$90,063.28 | WR: 99.50% | RoC: +1.12%
  - `Down`: 2,985 positions | Cost: $7,516,849.70 | PnL: -$52,791.24 | WR: 98.93% | RoC: -0.70%
  - `No`: 2,240 positions | Cost: $18,177,474.38 | PnL: +$91,475.77 | WR: 93.79% | RoC: +0.50%
  - `Yes`: 1,041 positions | Cost: $24,078,588.23 | PnL: +$32,673.50 | WR: 97.12% | RoC: +0.14%
  - `Other / Named Teams`: 726 positions | Cost: $37,672,772.69 | PnL: +$47,421.22 | WR: 95.59% | RoC: +0.13%
  - *Sum across outcomes*: 3,008 + 2,985 + 2,240 + 1,041 + 726 = **10,000 positions** (Cost: $95,481,322.69 | PnL: +$208,842.54). Note: `Up` and `Down` sum to 5,993 positions, $15,552,487.39 cost, and +$37,272.04 PnL, matching `up_down` market type exactly.
- **Price Target ("Reach" / "Dip") Detailed Breakdown (1,810 Positions)**:
  - Outcome `No`: 1,448 positions | Cost: $1,957,456.29 | PnL: **+$71,147.55** | WR: **91.16%** | RoC: **+3.635%**
  - Outcome `Yes`: 345 positions | Cost: $4,468,448.49 | PnL: +$6,639.12 | WR: 93.33% | RoC: +0.15%
  - Outcome Other (baseball player "hit" props): 17 positions | Cost: $225,333.69 | PnL: +$184.89 | WR: 88.24% | RoC: +0.08%
  - *Sum across price target outcomes*: 1,448 + 345 + 17 = **1,810 positions** (Cost: $6,651,238.47 | PnL: +$77,971.56).
- **Price Target NO Positions by Entry Band (1,448 Positions)**:
  - `0.00_to_0.50`: 168 positions | Cost: $29,326.00 | PnL: +$16,805.00 | WR: 50.00% | RoC: +57.30%
  - `0.50_to_0.80`: 142 positions | Cost: $88,076.00 | PnL: +$9,730.00 | WR: 82.39% | RoC: +11.05%
  - `0.80_to_0.85`: 47 positions | Cost: $64,968.00 | PnL: +$4,609.00 | WR: 85.11% | RoC: +7.09%
  - `0.85_to_0.90`: 78 positions | Cost: $102,283.00 | PnL: +$1,582.00 | WR: 94.87% | RoC: +1.55%
  - `0.90_to_0.96`: 260 positions | Cost: $517,287.00 | PnL: +$21,682.00 | WR: 98.46% | RoC: +4.19%
  - `0.96_to_0.98`: 387 positions | Cost: $569,723.00 | PnL: +$14,247.00 | WR: 99.48% | RoC: +2.50%
  - `0.98_to_0.99`: 229 positions | Cost: $164,854.00 | PnL: +$1,798.00 | WR: 99.56% | RoC: +1.09%
  - `0.99_and_above`: 137 positions | Cost: $420,939.00 | PnL: +$696.00 | WR: 99.27% | RoC: +0.16%
  - *Sum across NO bands*: 168 + 142 + 47 + 78 + 260 + 387 + 229 + 137 = **1,448 positions**.
- **Price Target NO Chronological Half-Period Split**:
  - *First Half (724 pos, 2026-03-16 to 2026-07-10)*: Cost: $521,972.34 | PnL: +$32,524.28 | Win Rate: 93.92% | RoC: +6.231%
  - *Second Half (724 pos, 2026-07-11 to 2026-10-27)*: Cost: $1,435,483.95 | PnL: +$38,623.27 | Win Rate: 88.40% | RoC: +2.691%
  - *Key Takeaway*: Sizing increased nearly 3x in the second half while win rate dropped 5.5% and RoC fell by more than half, showing edge compression over time.
- **Activity**: 3,500 events (2026-10-15 to 2026-10-27), 2,633 buys, 471 sells, 9.5 buys/hour, 88.9% of buys at >= $0.95.

---

### 2. `THEHIGHLIFE` (`0x8c0b024c17831a0dde038547b7e791ae6a0d7aa5`)
- **Sample Window**: 2026-05-01 17:44:40 UTC to 2026-10-26 23:36:54 UTC (2,000 closed positions pulled, 178.2 days).
- **Classification in Scout**: Label `market_maker`, flags `[]`. Note: Mislabeled `market_maker` because activity feed had 2 maker rebates in 500 events (0.40%). Actual activity is directional sports betting.
- **Aggregate Performance**: Total Cost: $13,697,587.67 | Realized PnL: +$4,676,214.32 | Win Rate: 76.10% | Return on Cost: +34.14%.
- **Win/Loss Metrics**: Average Win: $4,506.73 | Average Loss: -$4,595.83 | Loss-to-Win Ratio: 1.02 | Worst Loss: -$31,489.02.
- **Position Sizing**: Median Sizing: $3,782.59 | P90 Sizing: $14,719.81.
- **Period Stability**: First Half PnL: +$711,047.46 | Second Half PnL: +$3,965,166.86.
- **Price Band Breakdown (2,000 Positions)**:
  - `under_0.50`: 1,085 positions | Cost: $5,809,453.33 | PnL: +$2,997,001.79 | WR: 70.51% | RoC: +51.59% | Wtd Avg Entry: $0.2909
  - `0.50_to_0.80`: 669 positions | Cost: $5,006,242.89 | PnL: +$1,641,155.00 | WR: 81.76% | RoC: +32.78% | Wtd Avg Entry: $0.5502
  - `0.80_to_0.95`: 67 positions | Cost: $872,474.04 | PnL: **+$71,067.92** | WR: **83.58%** | RoC: **+8.15%** | Wtd Avg Entry: $0.8894
  - `0.95_to_0.99`: 64 positions | Cost: $582,941.28 | PnL: -$7,564.27 | WR: 84.38% | RoC: -1.30% | Wtd Avg Entry: $0.9830
  - `0.99_and_above`: 115 positions | Cost: $1,426,476.13 | PnL: **-$25,446.12** | WR: 86.96% | RoC: **-1.78%** | Wtd Avg Entry: $0.9954
  - *Sum across price bands*: 1,085 + 669 + 67 + 64 + 115 = **2,000 positions**.
- **Held vs. Mitigated Analysis in 0.80–0.95 Band**:
  - In the 0.80–0.95 band, winning positions averaged $13,563.74 in cost (total $759,569.51), while losing positions averaged $10,264.05 in cost (total $112,904.53 across 11 losses).
  - All 11 losses suffered modest percentage losses (-0.3% to -12.4% drawdown) due to early exits or non-total settlement. If all 11 losses had been held to binary resolution ($0 payout), the gross loss would have totaled -$112,904.53, turning the band's net PnL from +$71,067.92 into **-$47,272.23 (-5.42% RoC)**.
- **Market Type Breakdown (2,000 Positions)**:
  - `sports`: 1,877 positions | Cost: $12,134,756.35 | PnL: +$4,643,676.75 | WR: 76.19% | RoC: +38.27%
  - `price_target`: 8 positions | Cost: $9,854.11 | PnL: +$9,353.86 | WR: 100.0% | RoC: +94.92%
  - `other`: 115 positions | Cost: $1,552,977.21 | PnL: +$23,183.72 | WR: 73.04% | RoC: +1.49%
  - *Sum across market types*: 1,877 + 8 + 115 = **2,000 positions**.
- **Activity**: 11.57 buys/hour, 2 maker rebates in 500 events (0.40%).

---

### 3. `flawfence` (`0x23d81ba9371e576015c1e562db09c689f56b0288`)
- **Sample Window**: 2026-04-29 20:23:02 UTC to 2026-10-27 14:44:27 UTC (467 closed positions pulled, 180.8 days).
- **Classification in Scout**: Label `market_maker`, flags `[]`. Note: Mislabeled `market_maker` because activity feed had 1 maker rebate in 500 events (0.20%).
- **Aggregate Performance**: Total Cost: $9,615,654.26 | Realized PnL: +$4,063,501.33 | Win Rate: 77.09% | Return on Cost: +42.26%.
- **Win/Loss Metrics**: Average Win: $11,849.92 | Average Loss: -$2,045.17 | Loss-to-Win Ratio: 0.17 | Worst Loss: -$56,000.00.
- **Position Sizing**: Median Sizing: $5,020.31 | P90 Sizing: $42,158.81.
- **Period Stability**: First Half PnL: +$2,566,279.31 | Second Half PnL: +$1,497,222.02.
- **Price Band Breakdown (467 Positions)**:
  - `under_0.50`: 204 positions | Cost: $2,007,570.20 | PnL: +$2,399,358.39 | WR: 72.55% | RoC: +119.52% | Wtd Avg Entry: $0.2799
  - `0.50_to_0.80`: 166 positions | Cost: $5,034,850.71 | PnL: +$1,573,619.18 | WR: 80.12% | RoC: +31.25% | Wtd Avg Entry: $0.5797
  - `0.80_to_0.95`: 67 positions | Cost: $1,209,483.09 | PnL: **+$70,874.03** | WR: **80.60%** | RoC: **+5.86%** | Wtd Avg Entry: $0.8963
  - `0.95_to_0.99`: 28 positions | Cost: $1,346,858.25 | PnL: +$19,541.88 | WR: 82.14% | RoC: +1.45% | Wtd Avg Entry: $0.9765
  - `0.99_and_above`: 2 positions | Cost: $16,892.00 | PnL: +$107.85 | WR: 100.0% | RoC: +0.64% | Wtd Avg Entry: $0.9936
  - *Sum across price bands*: 204 + 166 + 67 + 28 + 2 = **467 positions**.
- **Held vs. Mitigated Analysis in 0.80–0.95 Band**:
  - The 67 positions had 54 wins, 11 losses, and 2 flat.
  - Across the 11 losses, loss drawdowns ranged from -0.7% to -44.6% of position cost (e.g., $404,162 position lost -$2,957; $31,495 position lost -$4,291). None of the positions were held to a total wipeout ($0 payout).
- **Market Type Breakdown (467 Positions)**:
  - `sports`: 85 positions | Cost: $2,050,461.70 | PnL: +$1,614,610.35 | WR: 92.94% | RoC: +78.74%
  - `price_target`: 6 positions | Cost: $245,863.54 | PnL: +$23,493.24 | WR: 100.0% | RoC: +9.56%
  - `up_down`: 1 position | Cost: $283.51 | PnL: +$5.79 | WR: 100.0% | RoC: +2.04%
  - `above_below`: 4 positions | Cost: $34,720.76 | PnL: -$13,486.79 | WR: 0.00% | RoC: -38.84%
  - `other`: 371 positions | Cost: $7,284,324.75 | PnL: +$2,438,878.75 | WR: 73.85% | RoC: +33.48%
  - *Sum across market types*: 85 + 6 + 1 + 4 + 371 = **467 positions**.
- **Activity**: 8.93 buys/hour, 1 maker rebate in 500 events (0.20%).

---

### 4. `primm` (`0xd38b71f3e8ed1af71983e5c309eac3dfa9b35029`)
- **Sample Window**: 2025-08-25 15:15:47 UTC to 2026-10-27 03:37:56 UTC (1,930 closed positions pulled, 427.5 days).
- **Classification in Scout**: Mislabeled `market_maker` (flags: `[]`).
- **Aggregate Performance**: Total Cost: $12,855,379.55 | Realized PnL: +$11,768,176.54 | Win Rate: 79.43% | Return on Cost: +91.54%.
- **Win/Loss Metrics**: Average Win: $8,707.03 | Average Loss: -$3,889.37 | Loss-to-Win Ratio: 0.45 | Worst Loss: -$81,649.06.
- **Position Sizing**: Median Sizing: $1,440.00 | P90 Sizing: $8,800.00.
- **Period Stability**: First Half PnL: +$8,690,793.72 | Second Half PnL: +$3,077,382.82.
- **Price Band Breakdown (1,930 Positions)**:
  - `under_0.50`: 967 positions | Cost: $5,796,570.60 | PnL: +$7,384,345.30 | WR: 72.08% | RoC: +127.39%
  - `0.50_to_0.80`: 945 positions | Cost: $6,938,255.39 | PnL: **+$4,359,943.17** | WR: **86.56%** | RoC: **+62.84%**
  - `0.80_to_0.95`: 17 positions | Cost: $112,026.36 | PnL: +$23,439.27 | WR: 100.0% | RoC: +20.92%
  - `0.95_to_0.99`: 1 position | Cost: $8,527.20 | PnL: +$448.80 | WR: 100.0% | RoC: +5.26%
  - `0.99_and_above`: 0 positions | Cost: $0.00 | PnL: $0.00 | WR: N/A | RoC: 0.00%
  - *Sum across price bands*: 967 + 945 + 17 + 1 + 0 = **1,930 positions**.
- **Activity Feed Analysis (500 Events)**:
  - 387 BUY trades, **0 SELL trades**, 104 REDEEMS, 3 MAKER_REBATES (0.60%), 3 TAKER_REBATES, 3 REWARDS.
  - Across 151 distinct markets, exactly 0 markets exhibited both BUY and SELL transactions.
  - **Reclassification**: `primm` is an outright directional value bettor (buying under 0.80 and redeeming winning shares at $1.00), not a two-sided passive market maker.

---

### 5. `monkeymashingkeyboard` (`0x684baa57c338c2549aec0aa3f034f695d72a8409`)
- **Sample Window**: 2026-07-12 15:10:11 UTC to 2026-10-27 05:30:50 UTC (670 closed positions pulled, 106.6 days).
- **Classification**: Mixed / bot-like (flags: `['bot_like']`).
- **Aggregate Performance**: Total Cost: $8,372,327.45 | Realized PnL: +$6,446,800.34 | Win Rate: 91.94% | Return on Cost: +77.00%.
- **Win/Loss Metrics**: Average Win: $11,358.02 | Average Loss: -$10,571.94 | Loss-to-Win Ratio: 0.93 | Worst Loss: -$90,754.28.
- **Position Sizing**: Median Sizing: $7,518.85 | P90 Sizing: $32,107.41.
- **Period Stability**: First Half PnL: +$2,895,449.11 | Second Half PnL: +$3,551,351.23.
- **Market Breakdown**: Sports accounts for 521 of 670 positions (77.8% of volume, +$5,101,509.26 PnL, 91.75% WR).
- **Activity**: 500 events, 499 buys, 43.6 buys/hour, 0 maker rebates. Represents an automated directional sports betting operation.

---

### 6. `Diabolical-Prize` (`0x7c37b52eb226bcb9411375ec48b0169663fb6aeb`)
- **Sample Window**: 2026-09-02 23:27:39 UTC to 2026-10-17 11:30:14 UTC (202 closed positions pulled, 44.5 days).
- **Classification**: Mislabeled `market_maker` in scout (flags: `[]`, 3 maker rebates in 500 events).
- **Aggregate Performance**: Total Cost: $20,290,990.36 | Realized PnL: +$18,191,593.90 | Win Rate: 94.55% | Return on Cost: +89.65%.
- **Win/Loss Metrics**: Average Win: $97,162.58 | Average Loss: -$33,314.43 | Loss-to-Win Ratio: 0.34 | Worst Loss: -$183,599.95.
- **Position Sizing**: Median Sizing: $71,148.70 | P90 Sizing: $242,070.52.
- **Market Breakdown**: Sports accounts for 182 of 202 positions (88.4% of cost, +$16,714,145.63 PnL, 95.60% WR).
- **Activity**: 500 events, 473 buys, 1.8 buys/hour. Massive institutional syndicate sizing ($71.1k median position).

---

### 7. `UpTheBlues` (`0x2a69660046d7acc4ab204d7cc5ba78b0776cd2f7`)
- **Sample Window**: 2026-10-26 03:33:45 UTC to 2026-10-27 15:20:20 UTC (2,000 closed positions pulled, 1.49 days).
- **Classification**: High-frequency bot sample (flags: `['bot_like']`, label: `mixed`).
- **Aggregate Performance**: Total Cost: $2,777,422.98 | Realized PnL: +$201,897.65 | Win Rate: 62.50% | Return on Cost: +7.27%.
- **Activity Pace**: 500 events in ~25 minutes, 1,178.5 buys/hour.
- **Evaluation**: The 36-hour sample window and extreme execution pace (1,178 buys/hr) characterize an automated algorithmic bot. Because the sample lacks multi-week persistence and depends on high execution speed, it is excluded from primary evidence for manual/paper trading.

---

### 8. `Theo4` (`0x56687bf447db6ffa42ffe2204a05edaa20f55839`)
- **Sample Window**: 2024-10-14 22:30:57 UTC to 2025-01-01 08:27:52 UTC (22 closed positions pulled, 78.4 days).
- **Classification**: Macro whale conviction bettor.
- **Aggregate Performance**: Total Cost: $20,052,204.64 | Realized PnL: +$22,053,933.75 | Win Rate: 81.82% | Return on Cost: +109.98%.
- **Position Sizing**: Median Sizing: $25,844.59 | P90 Sizing: $3,624,802.27.
- **Market Focus**: Exclusively concentrated in 2024 US Presidential Election markets.
- **Evaluation**: Whale-scale directional betting on a single macro political cycle. Disqualified from systematic replication.

---

## Top 3 candidates

### Candidate 1: Far Price Target NO Buying (Negative Skew Harvesting)

- **Thesis**: Retail participants systematically overpay for extreme out-of-the-money price milestones on crypto assets ("Will Bitcoin reach $150k this month?", "Will ETH dip to $1,800?"). Buying the NO contract extracts steady yield from this retail lottery bias.
- **Empirical Evidence**: In `sharky6999`'s 10,000 closed positions, fading price milestones on the NO side produced **+$71,147.55 PnL across 1,448 positions (91.16% win rate, +3.64% RoC)**. Entries between 80¢ and 98¢ delivered a **97.80% win rate and +3.35% RoC on 772 positions**.
- **Performance Caveat & Edge Decay**: Chronological splitting of the 1,448 NO positions shows clear compression over time:
  - First Half (724 positions, Mar–Jul 2026): 93.92% win rate, **+6.23% RoC** (+$32,524.28 PnL).
  - Second Half (724 positions, Jul–Oct 2026): 88.40% win rate, **+2.69% RoC** (+$38,623.27 PnL).
  - In the latest 1,000 positions overall, tail losses caused a net negative PnL (-$30,683.78). Paper book implementation must strictly cap sizing and enforce tail risk rules.
- **Target Markets**:
  - Active Polymarket markets tagged or formatted as price milestones (`dip`, `reach`, `hit`, `touch`).
  - Underlying instrument must possess transparent, high-liquidity spot reference pricing (e.g., BTC, ETH, SOL).
  - Expiration horizon between 3 days and 21 days.
- **Filter Criteria**:
  - Volatility buffer: Current spot price must sit at least 2.5 standard deviations away from the target price based on 30-day realized spot volatility.
  - Reject any market where spot price is within 8% of the target threshold.
- **Entry Rules**:
  - Buy NO at market ask between **$0.85 and $0.98**.
  - Permissible bands: 90¢ to 96¢ (+4.19% RoC, 98.46% WR) and 96¢ to 98¢ (+2.50% RoC, 99.48% WR).
  - **Hard prohibition**: NEVER enter at or above **$0.98**. The risk-reward collapses above 98¢ (dropping to +0.16% RoC at 99¢+ while exposing the position to total loss).
  - Order execution: Limit order posted at the best bid, or taker order if the ask spread is <= 1¢ and resting depth exceeds $500.
- **Sizing Rules**:
  - Maximum 1.5% of total paper portfolio equity per contract.
  - Maximum aggregate exposure across all correlated crypto NO positions capped at 15.0% of portfolio equity.
- **Exit & Invalidation Rules**:
  - Hold to resolution ($1.00 payout) under normal market conditions.
  - **Emergency stop-loss**: Close position immediately at market bid if the NO ask falls below **$0.70** (indicating spot price is rapidly approaching the strike and volatility is expanding).

---

### Candidate 2: High-Probability Consensus Convergence (80¢ to 94¢ Window) with Active Stop-Loss

- **Thesis**: Purchasing consensus outcomes in markets approaching finality captures the remaining 6¢ to 18¢ discount.
- **Critical Risk Finding (Downgraded Evidence Level)**: Holding high-entry positions blindly to resolution is mathematically unviable when win rates are in the 80–84% range. If an account buys at an average price of $0.89 and experiences an 83.58% win rate (like `THEHIGHLIFE`), holding losing positions to 0 results in a net return of **-5.42%**. `THEHIGHLIFE` (+8.15% RoC) and `flawfence` (+5.86% RoC) only made profit because their losing trades were closed early at partial drawdowns (-0.7% to -44%), rather than held to zero.
- **Empirical Evidence**:
  - `sharky6999`: 502 positions, +$51,516.00 PnL, 96.81% win rate, +5.97% RoC (high win rate enabled positive yield).
  - `THEHIGHLIFE`: 67 positions, +$71,067.92 PnL, 83.58% win rate, +8.15% RoC (all 11 losses mitigated early; wtd avg entry $0.8894).
  - `flawfence`: 67 positions, +$70,874.03 PnL, 80.60% win rate, +5.86% RoC (all 11 losses mitigated early; wtd avg entry $0.8963).
- **Target Markets**:
  - Markets where the decisive real-world event has concluded (e.g., official economic data releases, conceded elections, concluded sports fixtures).
- **Filter Criteria**:
  - **Resolution safety gate**: Market resolution rules must depend on an unambiguous primary source (e.g., BLS.gov, SEC.gov, AP race call). Reject any market with ambiguous wording, subjective criteria, or active dispute rumors.
- **Entry Rules**:
  - Enter YES or NO at market ask between **$0.80 and $0.94**.
  - **Hard cutoff**: Exclude all entries at or above **$0.95**. The negative skew above 95¢ erodes the edge.
- **Sizing Rules**:
  - Maximum 1.5% of paper portfolio equity per position.
  - Maximum 5 active bonding positions simultaneously (7.5% total capital commitment).
- **Exit & Invalidation Rules (Mandatory Active Loss Mitigation)**:
  - If contract price drops by more than **8¢ from entry** or falls below **$0.75**, immediately close the position at market bid. **Never hold a losing consensus position to resolution.**
  - If contract reaches **$0.98**, exit early to lock in profit rather than bearing tail settlement risk for the final 2¢.

---

### Candidate 3: Directional Mid-Probability Value Betting (50¢ to 80¢ Band)

- **Thesis**: Fading public bias and mispriced favorites in non-breaking sports, cultural, and political events in the 50¢ to 80¢ band, where implied probabilities lag true odds.
- **Empirical Evidence**: Multi-account data shows that the 50¢ to 80¢ band generates the largest and most sustainable profits across seasoned accounts:
  - `primm`: 945 positions, $6,938,255.39 cost, **+$4,359,943.17 PnL, 86.56% win rate, +62.84% RoC** (wtd avg entry ~$0.58).
  - `THEHIGHLIFE`: 669 positions, $5,006,242.89 cost, **+$1,641,155.00 PnL, 81.76% win rate, +32.78% RoC** (wtd avg entry $0.5502).
  - `flawfence`: 166 positions, $5,034,850.71 cost, **+$1,573,619.18 PnL, 80.12% win rate, +31.25% RoC** (wtd avg entry $0.5797).
- **Target Markets**:
  - Scheduled sports fixtures (soccer, tennis, esports) and scheduled political/cultural milestones.
  - Exclude all live in-play sports and fast-breaking news markets.
- **Filter Criteria**:
  - Independent benchmark verification: Underlying event must have liquid reference odds from external betting exchanges (e.g., Pinnacle, Betfair) or statistical models.
  - Required edge: Polymarket implied price must be at least **4¢ lower** than the devigged consensus probability.
- **Entry Rules**:
  - Enter directional position at market ask between **$0.50 and $0.78**.
  - Minimum resting order book depth >= $1,000 within 2¢ of ask.
- **Sizing Rules**:
  - Maximum 1.0% of portfolio equity per trade.
  - Maximum 10 concurrent active positions (10% aggregate exposure).
- **Exit & Invalidation Rules**:
  - Hold to resolution ($1.00 payout) if the external model edge remains valid.
  - If external market probability reverses by >= 8¢, flatten position at market bid.

---

## Could not verify

The following claims encountered in analyst commentary, social media, or forum discussions could not be verified against public on-chain data or primary sources:

1. **Sharky6999 Dedicated Polygon RPC / Node Setup**:
   - *Claim*: Reports on community forums and tracking sites claim account `sharky6999` operates a private Polygon validator/RPC node to execute trades without latency delays.
   - *Verification Status*: **UNVERIFIED**. While `https://polbots.com/wallets/sharky6999` documents transaction volume and market-maker characteristics, it provides no evidence of proprietary node infrastructure or RPC setup. On-chain transaction records do not disclose validator or node configurations.
2. **Sports Latency Arbitrage & Pinnacle/Betfair Devigging**:
   - *Claim*: Industry commentary asserts top sports accounts (`monkeymashingkeyboard`, `Diabolical-Prize`) operate sub-100ms latency arbitrage engines devigging live odds against Pinnacle and Betfair exchanges.
   - *Verification Status*: **UNVERIFIED**. On-chain transaction data pulled from the API shows `Diabolical-Prize` executed at a modest **1.8 buys/hour** with a massive **$71,148.70 median position size**, and `monkeymashingkeyboard` placed **43.6 buys/hour**. Neither profile matches high-frequency sub-second quote sniping; their metrics reflect large-scale informed syndicate betting. The specific external data sources and devigging algorithms cannot be verified from public blockchain records.
3. **Theo4 / Fredi9999 Private Polling and Multi-Account Linkage**:
   - *Claim*: Media reports (Business Insider, crypto.news) claim French trader "Theo4" commissioned private non-public polling and controlled a four-wallet cluster (`Fredi9999`, `PrincessCaro`, `Michie`, `Theo4`) deploying over $40M on 2024 election outcomes.
   - *Verification Status*: **UNVERIFIED**. While public data API queries for wallet `0x56687bf447db6ffa42ffe2204a05edaa20f55839` confirm +$22.05M PnL across 22 closed positions on Trump election markets, the multi-wallet clustering and the use of private polling cannot be verified using public read-only blockchain endpoints.
4. **Insider / Political Leak Advantage for Political Whales**:
   - *Claim*: Social media rumors suggested whale accounts had advance access to non-public political information.
   - *Verification Status*: **UNVERIFIED**. No evidence of insider leaks exists in public data. Trades reflected high-conviction macro wagers on public political events, but non-public communication or polling cannot be verified from public records.
5. **Live Real-Money Profitability of Jev Decision Model Bots (`jevymarket`)**:
   - *Claim*: Bots deploying TypeSafe AI's Jev model generate 30%+ annualized alpha trading live prediction markets.
   - *Verification Status*: **UNVERIFIED**. Developer documentation (`https://systemonemodels.org/examples/for/trading-and-markets/`) explains the integration of non-autoregressive decision models into trading pipelines, and open-source GitHub repositories (`https://github.com/jev-market/`) demonstrate sample agent workflows. However, no verified public Polymarket wallet has been linked to a live Jev implementation with a proven track record.
6. **Viral Social Media Claims of 500% to 1000% ROI on Automated Arbitrage**:
   - *Claim*: Anonymous social media posts claim simple Python cross-market scripts achieve 500% to 1000% monthly returns risk-free.
   - *Verification Status*: **UNVERIFIED / LIKELY PROMOTION**. No verifiable on-chain wallet addresses or transaction logs were provided to substantiate these claims. Discussion on algorithmic trading forums (`https://www.reddit.com/r/PredictionMarkets/`) indicates that statistical arbitrage mispricings (`YES + NO < $1.00`) are heavily targeted by automated latency bots, and taker fees, slippage, and execution legging risk eliminate retail arbitrage edges.
7. **Risk-Free Settlement Arbitrage Between Polymarket and Kalshi**:
   - *Claim*: Simultaneous offsetting positions on Polymarket and Kalshi yield guaranteed 3¢ to 5¢ risk-free profits.
   - *Verification Status*: **UNVERIFIED**. Discussions on prediction market trading forums note that differences in contract settlement language, expiration times, and resolution oracles (UMA decentralized voting vs. CFTC/Kalshi internal panel) expose cross-platform positions to catastrophic single-leg divergence risk. No confirmed live wallet records validate persistent risk-free execution.

---

## Sources

### Polymarket Data API Endpoints (Direct Public Queries Pulled)
- `https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=50` (Public monthly leaderboard rankings, volume, and PnL)
- `https://data-api.polymarket.com/closed-positions?user=0x751a2b86cab503496efd325c8344e10159349ea1&limit=50&offset=<0..9950>&sortBy=TIMESTAMP&sortDirection=DESC` (sharky6999 closed positions data, 200 pages, 10,000 rows fetched across offsets 0 to 9,950)
- `https://data-api.polymarket.com/activity?user=0x751a2b86cab503496efd325c8344e10159349ea1&limit=500&offset=<0..3000>` (sharky6999 activity data, 7 pages, 3,500 rows fetched across offsets 0 to 3,000)
- `https://data-api.polymarket.com/closed-positions?user=0x8c0b024c17831a0dde038547b7e791ae6a0d7aa5&limit=50&offset=<0..1950>&sortBy=TIMESTAMP&sortDirection=DESC` (THEHIGHLIFE closed positions data, 40 pages, 2,000 rows fetched across offsets 0 to 1,950)
- `https://data-api.polymarket.com/activity?user=0x8c0b024c17831a0dde038547b7e791ae6a0d7aa5&limit=500` (THEHIGHLIFE activity data, 1 page, 500 rows fetched)
- `https://data-api.polymarket.com/closed-positions?user=0x23d81ba9371e576015c1e562db09c689f56b0288&limit=50&offset=<0..450>&sortBy=TIMESTAMP&sortDirection=DESC` (flawfence closed positions data, 10 pages, 467 rows fetched across offsets 0 to 450)
- `https://data-api.polymarket.com/activity?user=0x23d81ba9371e576015c1e562db09c689f56b0288&limit=500` (flawfence activity data, 1 page, 500 rows fetched)
- `https://data-api.polymarket.com/closed-positions?user=0xd38b71f3e8ed1af71983e5c309eac3dfa9b35029&limit=50&offset=<0..1900>&sortBy=TIMESTAMP&sortDirection=DESC` (primm closed positions data, 39 pages, 1,930 rows fetched across offsets 0 to 1,900)
- `https://data-api.polymarket.com/activity?user=0xd38b71f3e8ed1af71983e5c309eac3dfa9b35029&limit=500` (primm activity data, 1 page, 500 rows fetched)
- `https://data-api.polymarket.com/closed-positions?user=0x7c37b52eb226bcb9411375ec48b0169663fb6aeb&limit=50&offset=<0..200>&sortBy=TIMESTAMP&sortDirection=DESC` (Diabolical-Prize closed positions data, 5 pages, 202 rows fetched across offsets 0 to 200)
- `https://data-api.polymarket.com/activity?user=0x7c37b52eb226bcb9411375ec48b0169663fb6aeb&limit=500` (Diabolical-Prize activity data, 1 page, 500 rows fetched)
- `https://data-api.polymarket.com/closed-positions?user=0x684baa57c338c2549aec0aa3f034f695d72a8409&limit=50&offset=<0..650>&sortBy=TIMESTAMP&sortDirection=DESC` (monkeymashingkeyboard closed positions data, 14 pages, 670 rows fetched across offsets 0 to 650)
- `https://data-api.polymarket.com/activity?user=0x684baa57c338c2549aec0aa3f034f695d72a8409&limit=500` (monkeymashingkeyboard activity data, 1 page, 500 rows fetched)
- `https://data-api.polymarket.com/closed-positions?user=0x2a69660046d7acc4ab204d7cc5ba78b0776cd2f7&limit=50&offset=<0..1950>&sortBy=TIMESTAMP&sortDirection=DESC` (UpTheBlues closed positions data, 40 pages, 2,000 rows fetched across offsets 0 to 1,950)
- `https://data-api.polymarket.com/activity?user=0x2a69660046d7acc4ab204d7cc5ba78b0776cd2f7&limit=500` (UpTheBlues activity data, 1 page, 500 rows fetched)
- `https://data-api.polymarket.com/closed-positions?user=0x56687bf447db6ffa42ffe2204a05edaa20f55839&limit=50&offset=0&sortBy=TIMESTAMP&sortDirection=DESC` (Theo4 closed positions data, 1 page, 22 rows fetched)
- `https://data-api.polymarket.com/activity?user=0x56687bf447db6ffa42ffe2204a05edaa20f55839&limit=500` (Theo4 activity data, 1 page, 500 rows fetched)

### Web Documentation & Analytical Sources (Opened & Verified URLs)
- `https://web3.bitget.com/en/academy/polymarket-trading-strategies-how-to-make-money-on-polymarket` (Comprehensive analysis of Polymarket trading mechanics, price target fading, and liquidity rewards)
- `https://startpolymarket.com/strategies/` (Documentation of prediction market bonding concepts, convergence trading, and high-probability skew risks)
- `https://startpolymarket.com/strategies/market-making/` (Mechanics of two-sided limit quoting, spread capture, and liquidity pool reward structures)
- `https://ratio.you/blog/best-polymarket-trading-strategies` (Quantitative examination of Polymarket maker fee rebates, order book dynamics, and inventory management)
- `https://polbots.com/wallets/sharky6999` (Profile of @Sharky6999 on Polymarket, documenting automated volume and high-probability market maker characteristics)
- `https://systemonemodels.org/examples/for/trading-and-markets/` (Technical documentation on deploying Jev System One decision models in automated trading loops and prediction market pricing)
- `https://github.com/jev-market/` (Open-source developer repositories detailing high-speed non-autoregressive decision model integrations for prediction markets)
- `https://www.reddit.com/r/PredictionMarkets/` (Trader discussions on endgame bonding yield, UMA dispute risks, internal statistical arbitrage erosion by HFT bots, and cross-platform Kalshi arbitrage frictions)
- `https://www.businessinsider.com/polymarket-whale-banked-48-million-profits-trump-president-election-win-2024-11` (Investigative reporting on the French whale trader Theo4, trade sizing, and 2024 election market positions)
- `https://crypto.news/polymarket-french-whale-theo4-nets-21m-off-trump-win/` (Detailed breakdown of Theo4's realized PnL and popular vote wagers on Polymarket)
