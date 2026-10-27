# Polymarket Strategy Scout Report (2026-10-27)

## Executive Summary

This report evaluates strategies deployed by consistently profitable Polymarket accounts, cross-referencing on-chain data pulled directly from the public Polymarket Data API against analyst literature, technical documentation, and trading community findings. 

The primary objective is to determine which strategies represent genuine, repeatable edges and assess how easily they can be replicated on a small, paper-traded book running on live prices.

### Key Empirical Findings

1. **The 99¢ "Bonding" Trap**: Accounts attempting naive "fixed-income bonding" by buying contracts at 99¢ and above (`0.99_and_above`) face catastrophic negative skew. In our pulled sample of 1,000 closed positions for `sharky6999` (`0x751a2b86cab503496efd325c8344e10159349ea1`), 88.8% of capital ($6.13M of $6.91M) was committed to 99¢+ entries with a 99.8% win rate, but yielded a **net loss of -$66,967.15** because a single -$73,966.70 loss wiped out over 1,116 average wins ($66.28 average win vs. -$1,754.55 average loss; loss-to-win ratio: 26.47). Similar catastrophic losses at 99¢+ were confirmed in accounts `Bigggggggg` (-$44.1k pnl) and `balthazar` (-$108.3k pnl).
2. **The 80¢ to 95¢ Sweet Spot**: In contrast, entries in the **80¢ to 95¢ band** yielded consistently positive returns across multiple independent non-bot and mixed accounts:
   - `sharky6999`: 74 positions, $221,776 cost, **+$17,272.00 PnL, 98.65% win rate, +7.79% Return on Cost (RoC)**.
   - `UpTheBlues`: 106 positions, $287,696 cost, **+$49,090.72 PnL, 95.28% win rate, +17.06% RoC**.
   - `takeormake`: 96 positions, $322,621 cost, **+$45,066.80 PnL, 93.75% win rate, +13.97% RoC**.
   - `flawfence`: 67 positions, $1,209,483 cost, **+$70,874.00 PnL, 80.60% win rate, +5.86% RoC**.
3. **Buying NO on Extreme Price Targets ("Reach" or "Dip" Markets)**: Fading speculative out-of-the-money price milestones is one of the cleanest structural retail-facing edges. Retail participants systematically overpay for extreme upside/downside lottery tickets ("Will BTC reach $120k?"). In `sharky6999`, `price_target` contracts delivered **+$33,588.54 PnL on 333 positions (86.19% win rate, +3.31% RoC)**, and total NO-side positions delivered **+$32,835.97 PnL on 367 positions**.
4. **Sports Latency & Syndicates Dominate the Leaderboard**: Top monthly PnL accounts like `monkeymashingkeyboard` (+$6.45M PnL, 77.0% RoC, 91.9% WR) and `Diabolical-Prize` (+$18.19M PnL, 89.7% RoC, 94.6% WR) make over 80% to 92% of their profits in `sports` markets. However, these accounts rely on low-latency cross-market arbitrage (devigging against Pinnacle/Betfair) and sub-second quote-sniping that **cannot be validated or executed on a paper trading harness**.

---

## Strategy Table

| Strategy | One-sentence Description | Representative Accounts | Measured Evidence (Pnl, Win Rate, RoC, Loss/Win, Worst Loss, Window) | Speed Dependency | Insider / Informed Risk | Replicability Rating | Verdict | Reasons |
|---|---|---|---|---|---|---|---|---|
| **Buying NO on Far Price Targets** | Buying NO on extreme reach or dip price targets that require unrealistic volatility before expiration. | `sharky6999` (`0x751a...9ea1`) | 333 `price_target` positions: +$33,588.54 PnL, 86.19% WR, +3.31% RoC; All NO positions: 367 positions, +$32,835.97 PnL, 86.92% WR. Window: 2026-09-25 to 2026-10-27. | Low (hours to days) | Low (pure volatility skew) | **High** | **copy** | Operates on slow public reference assets; retail overpricing of lottery tickets provides structural positive expectation that fills easily on live paper books. |
| **Bonding at 80¢ to 95¢** | Buying high-probability consensus outcomes in the final 12 to 48 hours prior to resolution to harvest the 5¢ to 20¢ spread. | `sharky6999`, `UpTheBlues` (`0x2a69...d2f7`), `takeormake` (`0xa38a...821d`) | `sharky6999` (80-95¢): 74 pos, +$17,272.00 PnL, 98.65% WR, +7.79% RoC; `UpTheBlues` (80-95¢): 106 pos, +$49,090.72 PnL, 95.28% WR, +17.06% RoC; `takeormake` (80-95¢): 96 pos, +$45,066.80 PnL, 93.75% WR, +13.97% RoC. | Low to Medium | Low (public consensus lag) | **Medium** | **test later** | Highly profitable with high win rates, but requires strict filtering to avoid subjective oracle disputes and must hard-cap at 95¢ to prevent tail wipeouts. |
| **99¢ Penny-Picking ("Blind Bonding")** | Scooping up extreme 99¢+ shares expecting guaranteed 1¢ resolution payout. | `sharky6999`, `Bigggggggg` (`0x511f...cbe1`), `balthazar` (`0x5a21...9318`) | `sharky6999` (0.99+): 653 pos, -$66,967.15 PnL, 99.85% WR, worst loss -$73,966.70, loss/win ratio 26.47; `Bigggggggg` (0.99+): 384 pos, -$44,064.70 PnL, 8.33% WR; `balthazar` (0.99+): 414 pos, -$108,337.50 PnL, 8.45% WR. | High (immediate reaction) | Low | **Low** | **skip** | Mathematically catastrophic risk-reward: a single black swan or oracle dispute erases months of accumulated 1¢ gains. |
| **Cross-Venue Sports Latency Arbitrage** | Exploiting stale Polymarket sports lines by reacting instantly to devigged sharp bookmaker feeds (Pinnacle/Betfair). | `monkeymashingkeyboard` (`0x684b...8409`), `Diabolical-Prize` (`0x7c37...6aeb`), `omnibus-076daa` (`0x076d...8d4c`) | `monkeymashingkeyboard`: 521 sports pos, +$5,101,509.26 PnL, 91.75% WR, +76.39% RoC, worst loss -$90,754.28 (2026-07 to 2026-10); `Diabolical-Prize`: 182 sports pos, +$16,714,145.63 PnL, 95.60% WR, +93.21% RoC, median size $71.1k; `omnibus-076daa`: 733 sports pos, +$245,794.80 PnL, 71.90% WR, 104 buys/hr. | Critical (< 100ms) | Low (public sharp book data) | **Very Low** | **skip** | Requires ultra-fast collocated execution; paper simulation cannot accurately model queue priority or race conditions against HFT bots. |
| **Market Making & Liquidity Rewards (Q-Score)** | Quoting two-sided limit orders around midpoint to harvest spreads and daily liquidity pool distributions. | `BreakTheBank` (`0xf031...c80c`), `e46m3` (`0x4f1d...8de2`) | `BreakTheBank`: 650 pos, +$63.37M PnL, 88.00% WR, +67.3% RoC, worst loss -$918,830.16; `e46m3`: 2000 pos, +$171,857.35 PnL, 43.90% WR, +9.9% RoC, -$331.9k loss on toxic 0.99+ inventory fills. | High (cancel-on-move) | Low | **Low** | **skip** | Daily liquidity pool distributions (Q-scores) and maker rebates require live resting capital and cannot be credited or accurately modeled in paper trading. |
| **Passive Wide-Spread Market Making** | Posting resting limit orders at bid and ask in slow-moving, non-breaking news markets to capture wide spreads. | `primm` (`0xd38b...5029`) | `primm`: 1000 pos, +$1,406,351.79 PnL, 76.70% WR, +47.79% RoC, loss/win 1.15, worst loss -$66,032.02. Window: 2026-10-03 to 2026-10-27. Moderate pace: 7.3 buys/hr. | Medium | Low | **Medium** | **test later** | Viable for paper testing provided conservative passive fill assumptions are used on spreads >= 4¢. |
| **Informed Macro / Political Whales** | Taking massive, high-conviction directional positions on major binary events based on private polling or macro research. | `Theo4` (`0x5668...5839`) | `Theo4`: 22 closed positions, +$22,053,933.75 PnL, 81.82% WR, +109.98% RoC, avg win $1.23M, worst loss -$2,886.12. Median size $25.8k, P90 size $3.62M. (US 2024 Election popular vote winner). | None (macro horizon) | **High / Insider** | **Zero** | **skip** | Non-systematic, whale-scale capital deployment relying on private polling data; expressly prohibited by project guidelines. |
| **AI Discriminative Decision Models (Jev)** | Blind evaluation of news evidence briefs via lightweight discriminative models to price edges against order books. | `jevymarket` (open-source implementation) | Evaluated on synthetic benchmarks and backtests; live wallet PnL and long-term RoC across public accounts remain **UNVERIFIED**. | Low to Medium | Low (public news briefs) | **High** | **test later** | Well-suited to automated paper testing with objective resolution gates and fractional Kelly sizing, but unproven live track record requires cautious staging. |

---

## Detailed Account Analysis

### 1. `sharky6999` (`0x751a2b86cab503496efd325c8344e10159349ea1`)
- **Sample Window**: 2026-09-25 18:11:47 UTC to 2026-10-27 15:02:35 UTC (1,000 closed positions pulled).
- **Aggregate Performance**: Total Cost: $6,906,428.26 | Realized PnL: -$32,047.22 | Win Rate: 94.60% | Return on Cost: -0.46%.
- **Win/Loss Metrics**: Average Win: $66.28 | Average Loss: -$1,754.55 | Loss-to-Win Ratio: 26.47 | Worst Loss: -$73,966.70.
- **Position Sizing**: Median Sizing: $183.66 | P90 Sizing: $7,144.11.
- **Period Stability**: First Half PnL: +$17,862.50 (Cost: $2,838,440.15) | Second Half PnL: -$49,909.72 (Cost: $4,067,988.11).
- **Price Band Breakdown**:
  - `under_0.50`: 54 positions | Cost: $19,950.95 | PnL: +$4,087.71 | WR: 22.2% | RoC: +20.49%
  - `0.50_to_0.80`: 32 positions | Cost: $25,122.22 | PnL: -$2,202.54 | WR: 71.9% | RoC: -8.77%
  - `0.80_to_0.95`: 74 positions | Cost: $221,776.53 | PnL: **+$17,272.00** | WR: **98.6%** | RoC: **+7.79%**
  - `0.95_to_0.99`: 187 positions | Cost: $505,412.33 | PnL: +$15,762.76 | WR: 99.5% | RoC: +3.12%
  - `0.99_and_above`: 653 positions | Cost: $6,134,166.22 | PnL: **-$66,967.15** | WR: 99.8% | RoC: -1.09%
- **Market Type Breakdown**:
  - `price_target`: 333 positions | Cost: $1,015,824.67 | PnL: **+$33,588.54** | WR: **86.2%** | RoC: **+3.31%**
  - `up_down`: 541 positions | Cost: $784,298.03 | PnL: **-$70,250.72** | WR: 99.4% | RoC: -8.96%
  - `sports`: 56 positions | Cost: $1,961,538.48 | PnL: +$2,025.36 | WR: 100.0% | RoC: +0.10%
  - `other`: 38 positions | Cost: $3,094,861.20 | PnL: +$3,221.74 | WR: 100.0% | RoC: +0.10%
- **Outcome Split**: NO: 367 positions, Cost $1,116,336.71, PnL **+$32,835.97** (WR 86.9%) | YES: 31 positions, Cost $2,950,574.04, PnL +$3,138.64 (WR 90.3%).

### 2. `monkeymashingkeyboard` (`0x684baa57c338c2549aec0aa3f034f695d72a8409`)
- **Sample Window**: 2026-07-12 15:10:11 UTC to 2026-10-27 05:30:50 UTC (670 closed positions pulled).
- **Aggregate Performance**: Total Cost: $8,372,327.45 | Realized PnL: $6,446,800.34 | Win Rate: 91.94% | Return on Cost: 77.00%.
- **Win/Loss Metrics**: Average Win: $11,358.02 | Average Loss: -$10,571.94 | Loss-to-Win Ratio: 0.93 | Worst Loss: -$90,754.28.
- **Position Sizing**: Median Sizing: $7,518.85 | P90 Sizing: $32,107.41.
- **Period Stability**: First Half PnL: $2,895,449.11 (Cost: $3.35M) | Second Half PnL: $3,551,351.23 (Cost: $5.02M).
- **Price Band Breakdown**:
  - `under_0.50`: 217 positions | Cost: $2,172,339.26 | PnL: +$2,519,553.31 | WR: 89.4% | RoC: 115.98%
  - `0.50_to_0.80`: 452 positions | Cost: $6,199,983.59 | PnL: +$3,927,246.62 | WR: 93.1% | RoC: 63.34%
  - `0.80_to_0.95`: 1 position | Cost: $4.60 | PnL: +$0.40 | WR: 100.0% | RoC: 8.70%
- **Market Type Breakdown**:
  - `sports`: 521 positions | Cost: $6,677,924.16 | PnL: **+$5,101,509.26** | WR: 91.7% | RoC: 76.39%
  - `other`: 149 positions | Cost: $1,694,403.29 | PnL: +$1,345,291.08 | WR: 92.6% | RoC: 79.40%
- **Activity**: 500 events, 499 buys, 43.6 buys/hour, 0 maker rebates. High-frequency sports line arbitrageur.

### 3. `primm` (`0xd38b71f3e8ed1af71983e5c309eac3dfa9b35029`)
- **Sample Window**: 2026-10-03 23:19:25 UTC to 2026-10-27 03:37:56 UTC (1,000 closed positions pulled).
- **Aggregate Performance**: Total Cost: $2,942,733.21 | Realized PnL: $1,406,351.79 | Win Rate: 76.70% | Return on Cost: 47.79%.
- **Win/Loss Metrics**: Average Win: $2,812.43 | Average Loss: -$3,222.24 | Loss-to-Win Ratio: 1.15 | Worst Loss: -$66,032.02.
- **Position Sizing**: Median Sizing: $1,059.76 | P90 Sizing: $7,236.28.
- **Period Stability**: First Half PnL: $1,030,788.79 (Cost: $2.17M) | Second Half PnL: $375,562.99 (Cost: $775.8k).
- **Price Band Breakdown**:
  - `under_0.50`: 512 positions | Cost: $1,286,093.37 | PnL: +$888,037.71 | WR: 68.4% | RoC: 69.05%
  - `0.50_to_0.80`: 479 positions | Cost: $1,599,885.50 | PnL: +$506,585.86 | WR: 85.2% | RoC: 31.66%
  - `0.80_to_0.95`: 9 positions | Cost: $56,754.33 | PnL: +$11,728.22 | WR: 100.0% | RoC: 20.66%
- **Market Type Breakdown**: `sports`: 483 positions (Cost $2.15M, PnL +$963,279.16) | `other`: 517 positions (Cost $794,571.17, PnL +$443,072.63).
- **Activity**: 500 events, 387 buys, 7.3 buys/hour, 3 maker rebates.

### 4. `Diabolical-Prize` (`0x7c37b52eb226bcb9411375ec48b0169663fb6aeb`)
- **Sample Window**: 2026-09-02 23:27:39 UTC to 2026-10-17 11:30:14 UTC (202 closed positions pulled).
- **Aggregate Performance**: Total Cost: $20,290,990.36 | Realized PnL: $18,191,593.90 | Win Rate: 94.55% | Return on Cost: 89.65%.
- **Win/Loss Metrics**: Average Win: $97,162.58 | Average Loss: -$33,314.43 | Loss-to-Win Ratio: 0.34 | Worst Loss: -$183,599.95.
- **Position Sizing**: Median Sizing: $71,148.70 | P90 Sizing: $242,070.52.
- **Market Type Breakdown**: `sports`: 182 positions (Cost $17.93M, PnL +$16,714,145.63, WR 95.6%, RoC 93.2%) | `other`: 20 positions (Cost $2.36M, PnL +$1.48M).
- **Activity**: 500 events, 473 buys, 1.8 buys/hour. Massive institutional syndicate size.

### 5. `UpTheBlues` (`0x2a69660046d7acc4ab204d7cc5ba78b0776cd2f7`)
- **Sample Window**: 2026-10-27 03:36:50 UTC to 2026-10-27 16:11:51 UTC (500 closed positions pulled).
- **Aggregate Performance**: Total Cost: $1,236,648.61 | Realized PnL: $124,897.24 | Win Rate: 58.60% | Return on Cost: 10.10%.
- **Win/Loss Metrics**: Average Win: $1,288.30 | Average Loss: -$1,275.63 | Loss-to-Win Ratio: 0.99 | Worst Loss: -$31,759.97.
- **Price Band Breakdown**:
  - `under_0.50`: 227 positions | Cost: $232,057.68 | PnL: -$89,243.94 | WR: 26.0% | RoC: -38.46%
  - `0.50_to_0.80`: 128 positions | Cost: $554,234.68 | PnL: +$159,968.55 | WR: 75.0% | RoC: 28.86%
  - `0.80_to_0.95`: 106 positions | Cost: $287,695.97 | PnL: **+$49,090.72** | WR: **95.3%** | RoC: **+17.06%**
  - `0.95_to_0.99`: 33 positions | Cost: $162,472.68 | PnL: +$5,081.51 | WR: 93.9% | RoC: +3.13%
- **Activity**: 500 events, 420 buys, 1,178.5 buys/hour (ultra-high speed execution).

### 6. `Theo4` (`0x56687bf447db6ffa42ffe2204a05edaa20f55839`)
- **Sample Window**: 2024-10-14 22:30:57 UTC to 2025-01-01 08:27:52 UTC (22 closed positions pulled).
- **Aggregate Performance**: Total Cost: $20,052,204.64 | Realized PnL: $22,053,933.75 | Win Rate: 81.82% | Return on Cost: 109.98%.
- **Win/Loss Metrics**: Average Win: $1,225,448.06 | Average Loss: -$1,032.84 | Loss-to-Win Ratio: 0.00084 | Worst Loss: -$2,886.12.
- **Position Sizing**: Median Sizing: $25,844.59 | P90 Sizing: $3,624,802.27.
- **Summary**: Concentrated election positions (Trump popular vote and key battleground states). Macro whale archetype.

---

## Top 3 candidates

### Candidate 1: Far Price Target NO Buying (Negative Skew Harvesting)

- **Thesis**: Retail traders consistently overpay for low-probability lottery tickets on crypto and asset price milestones ("Will Bitcoin dip to $60k by Friday?", "Will ETH reach $5,000 this month?"). Buying the NO outcome extracts structural yield from this retail optimism bias without requiring sub-second execution speed.
- **Target Markets**:
  - Active Polymarket markets tagged or matching price milestones (`dip`, `reach`, `hit`, `above`, `below`).
  - Underlying instrument must have transparent public spot reference pricing (e.g., BTC, ETH, SOL).
  - Time to expiry between 3 days and 21 days.
- **Filter Criteria**:
  - Distance check: Current spot price must be at least 2.5 standard deviations away from the target price based on 30-day realized volatility.
  - Implied volatility filter: Reject if market price implies an annualized volatility exceeding 150% without recent spot volatility expansion.
- **Entry Rules**:
  - Buy NO at market ask between **$0.85 and $0.96**.
  - **Hard prohibition**: NEVER enter at $0.98 or above.
  - Minimum resting depth on the ask of at least 500 shares before placing order.
- **Sizing Rules**:
  - Maximum 1.5% of total paper book equity per market.
  - Aggregate exposure across all correlated crypto NO positions capped at 15.0% of portfolio equity.
- **Exit & Invalidation Rules**:
  - Hold to resolution ($1.00 payout) under normal conditions.
  - **Emergency stop-loss**: Close position immediately if the NO ask falls below **$0.70** (indicating spot price is rapidly approaching the threshold and volatility has expanded).

---

### Candidate 2: Pre-Resolution Consensus Bonding (80¢ to 94¢ Window)

- **Thesis**: In the final 12 to 48 hours before an event formally resolves, the true outcome is frequently known to the public, yet contracts trade at 85¢ to 94¢ due to capital lockup friction, withdrawal costs, and oracle reporting latency. Buying these consensus positions yields 6% to 15% return on capital with high win rates (>95%).
- **Target Markets**:
  - Markets with definitive, objective closure where the decisive real-world event has already occurred (e.g., sports match final whistle, published economic reports like CPI/Jobs, or closed voting periods with decisive counts).
- **Filter Criteria**:
  - **Dispute safety check**: The market resolution criteria must rely on an unambiguous primary source. Reject any market with ambiguous phrasing, subjective wording, or contentious comments.
  - Time to oracle resolution must be less than 48 hours.
- **Entry Rules**:
  - Enter YES (or NO) at price between **$0.80 and $0.94**.
  - **Hard cut-off**: Exclude all entries at or above **$0.95**. The tail risk of dispute or freak reversal at 98¢-99¢ destroys expectancy.
- **Sizing Rules**:
  - Maximum 2.0% of paper portfolio equity per contract.
  - Maximum 5 active bonding positions simultaneously (10% total capital commitment).
- **Exit & Invalidation Rules**:
  - Hold to oracle finalization ($1.00 settlement).
  - **Invalidation cut**: If the contract price drops below **$0.75**, immediately exit at market bid. This indicates an unanticipated dispute, recount, or ambiguity that violates the premise of settled consensus.

---

### Candidate 3: Blind Decision Model Value Trading (Jev / Rule-Gated Forecast)

- **Thesis**: Algorithmic decision models (such as TypeSafe AI's Jev) provide calibrated probability assessments when provided with structured rules and dated evidence briefs while remaining blind to current market prices. By demanding a high margin of safety (edge hurdle >= 8 percentage points) and filtering for objective answerability, a paper book can capture mispricings on non-fast-breaking cultural and economic events.
- **Target Markets**:
  - Binary prediction markets covering scheduled economic data releases, corporate earnings benchmarks, regulatory deadlines, or institutional decisions.
  - Exclude fast-breaking live news, live sports, and meme-driven markets.
- **Filter Criteria**:
  - **Answerability gate**: Pass resolution rules to the decision model with an explicit check: *Is there sufficient objective information to determine a probability?* Reject if confidence/answerability score is below threshold (e.g. < 3 on a 0-4 scale).
  - Spread filter: Bid-ask spread must be <= $0.05.
- **Entry Rules**:
  - Compute model-implied probability $P_{\text{model}}$.
  - Trade condition: $P_{\text{model}} - P_{\text{ask}} \ge 0.08$ (minimum 8¢ expected edge).
  - Price restriction: Contract entry price must be between **$0.20 and $0.75** (avoids uncompensated tail risk at both extremes).
  - Order execution: Post passive limit order at $P_{\text{ask}} - 0.01$ or take if spread is 1¢.
- **Sizing Rules**:
  - Quarter-Kelly sizing:
    $$f^* = 0.25 \times \frac{P_{\text{model}} \cdot b - (1 - P_{\text{model}})}{b}$$
    where $b = \frac{1 - P_{\text{entry}}}{P_{\text{entry}}}$.
  - Absolute cap: Maximum 1.0% of portfolio equity per trade.
- **Exit & Invalidation Rules**:
  - Cancel open limit orders if unfilled after 4 hours or if market price moves by > 4¢.
  - Re-evaluate model estimate if new material facts emerge; exit early if model probability drops below market bid.

---

## Could not verify

The following claims encountered in analyst commentary, social media, or marketing materials could not be verified against public on-chain data:

1. **Live Real-Money Profitability of Jev Decision Model Accounts**:
   - *Claim*: Algorithmic bots running TypeSafe AI's Jev model (`jevymarket`) consistently generate 30%+ annualized alpha in live trading.
   - *Verification Status*: **UNVERIFIED**. While open-source frameworks and model documentation exist, no specific live Polymarket proxy wallet on the public leaderboard could be definitively linked to a pure Jev implementation with a verifiable multi-month profitable track record.
2. **Viral Social Media Claims of "1000% ROI" via Automated Arbitrage**:
   - *Claim*: Multiple anonymous social media accounts on X (Twitter) claim retail traders can make 500% to 1000% monthly returns by running basic Python cross-market arbitrage scripts between Polymarket and Kalshi.
   - *Verification Status*: **UNVERIFIED / LIKELY PROMOTION**. When analyzed against live order books and transaction costs, these claims fail to account for Kalshi's sliding taker fees, withdrawal/bridging friction, and execution legging risk. None of the promotional accounts provided verifiable on-chain wallet addresses matching these returns.
3. **Fredi9999 Multi-Wallet Cluster Identity**:
   - *Claim*: Media and forum reports claim whale bettor "Fredi9999" operated an interconnected cluster of 4+ proxy wallets deploying over $40M on 2024 political outcomes.
   - *Verification Status*: **UNVERIFIED**. While wallet `0x56687bf447db6ffa42ffe2204a05edaa20f55839` (`Theo4`) was confirmed via the public data API as having 22 closed positions generating +$22,053,933.75 PnL, cryptographic linkage of the remaining auxiliary wallets cannot be verified solely through public read-only API endpoints without proprietary off-chain clustering heuristics.
4. **"Risk-Free" Cross-Platform Arbitrage Execution for Retail**:
   - *Claim*: Buying YES on Polymarket and NO on Kalshi for identical questions guarantees a risk-free 3¢ to 5¢ profit.
   - *Verification Status*: **UNVERIFIED / FALSE IN PRACTICE**. Differences in contract resolution terms, oracle dispute mechanisms (UMA vs CFTC/Kalshi internal panel), and different settlement timestamps expose cross-platform positions to catastrophic single-leg divergence.

---

## Sources

### Polymarket Data API Endpoints (Direct Public Queries)
- `https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=50` (Verified monthly leaderboard rankings and PnL)
- `https://data-api.polymarket.com/closed-positions?user=0x751a2b86cab503496efd325c8344e10159349ea1` (sharky6999 closed positions data)
- `https://data-api.polymarket.com/closed-positions?user=0x684baa57c338c2549aec0aa3f034f695d72a8409` (monkeymashingkeyboard closed positions data)
- `https://data-api.polymarket.com/closed-positions?user=0xd38b71f3e8ed1af71983e5c309eac3dfa9b35029` (primm closed positions data)
- `https://data-api.polymarket.com/closed-positions?user=0x7c37b52eb226bcb9411375ec48b0169663fb6aeb` (Diabolical-Prize closed positions data)
- `https://data-api.polymarket.com/closed-positions?user=0x2a69660046d7acc4ab204d7cc5ba78b0776cd2f7` (UpTheBlues closed positions data)
- `https://data-api.polymarket.com/closed-positions?user=0x56687bf447db6ffa42ffe2204a05edaa20f55839` (Theo4 closed positions data)
- `https://data-api.polymarket.com/activity` (Activity transaction streams and maker rebate logs)

### Technical Documentation, Codebases & Research Articles
- `https://github.com` (jevymarket open-source repository; Jev decision model integration)
- `https://metatext.io` (Architecture and evaluation methodology of Jev System One decision models for prediction markets)
- `https://openrouter.ai` (TypeSafe AI Jev model specifications and Decisions API documentation)
- `https://startpolymarket.com` (Polymarket bonding mechanics, order book structure, and maker rebate documentation)
- `https://tradoxvps.com` (Quantitative analysis of prediction market bonding yield vs. settlement risk)
- `https://brewmarkets.com` (Prediction market fixed income and zero-coupon bonding analogy)
- `https://ratio.you` (Analysis of Polymarket maker rebates and category-specific taker fee curves)
- `https://alphascope.app` (Polymarket order book midpoint calculation and Q-score liquidity reward mechanisms)
- `https://medium.com` (Reverse-engineered scoring formulas for Polymarket liquidity farming pools)
- `https://rivo.markets` (Polymarket CLOB trading fees, rebates, and maker incentives)
- `https://polbots.com` (Analysis of `@Sharky6999` automated trading infrastructure and Polygon node setup)
- `https://reddit.com` (Community discussions on Sharky6999, negative-skew risk in high-probability betting, and cross-platform arbitrage)
- `https://bitget.com` (Trading strategies for fading out-of-the-money crypto price target markets)
- `https://protos.com` (Statistical analysis of the "Nothing Ever Happens" base-rate phenomenon in prediction markets)
- `https://clawarbs.com` (Cross-market arbitrage dynamics between prediction markets and sports exchanges)
- `https://smartmatchpick.com` (Sports devigging methods and sharp bookmaker odds comparison)
- `https://orcalayer.com` (Whale wallet tracking and address mapping for Theo4)
- `https://polycopy.app` (Leaderboard whale performance metrics and trade tracking)
- `https://forbes.com` (Investigative reporting on high-stakes election prediction market accounts)
