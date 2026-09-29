<!-- Written by Codex (read-only pass over shallow clones at: polybot 51e5362, PolyWeather 43e658b,
polymarket_lp_tool 32f7799, Prediction-Markets-Trading-Bot-Toolkits b8a31fb, polymarket-mcp-server 21f65f3),
2026-09-29. Claude spot-checked the Toolkits stub claims, the zero fee_rate_bps config, and the MCP installer
key handling against the source; all accurate. -->

# Strategy Reading Notes (Phase 3)

## Summary Table

| Repo | Strategies found | Core edge mechanism | Data needed | Needs wallet/keys? | Paper-testable in Andy Trader? | Verdict (Worth testing / Maybe / Skip) |
|---|---|---|---|---|---|---|
| polybot | Two-sided complete-set quoting, inventory skew, taker top-up | Maker spread capture while assembling YES+NO below $1 | CLOB books/fills, Gamma metadata, Polygon logs | Paper: no. Live: yes | Yes | Maybe |
| PolyWeather | DEB temperature buckets, intraday METAR/TAF update, model-market gap | Better calibrated settlement probability than market price | Forecast ensembles, official observations, market history | No for research | Yes | Worth testing |
| polymarket_lp_tool | Reward-band limit-order maintenance | Liquidity rewards plus spread, net of adverse selection | CLOB book, reward parameters, fills, reward payouts | Paper: no. Live: yes | Partly | Maybe |
| Prediction-Markets-Trading-Bot-Toolkits | Copy trading implemented, nine advertised strategies are stubs | Follow a wallet before price adjustment | Polygon logs, Gamma, CLOB books, settlement | Paper data: no. Live: yes | Yes | Maybe |
| polymarket-mcp-server | Read tools, naive market recommendation, order execution helpers | No demonstrated forecasting edge | Gamma, CLOB, optional account data | Read-only: no. Trading: yes | Data collection only | Skip |

## Per Repo

### polybot

**What code does:** The engine calculates passive prices from bid, ask, spread and tick size, then requires planned UP+DOWN quote cost to clear a minimum edge (`strategy-service/src/main/java/com/polybot/hft/polymarket/strategy/service/QuoteCalculator.java:34-60,167-172`). It quotes both outcomes, skews for inventory, can cross the ask for a lagging-leg top-up, and otherwise holds the complete set toward settlement (`GabagoolDirectionalEngine.java:178-235,262-384`). Research labels fills as directional or complete-set-like, but admits decision-time top-of-book lag near 62 seconds and missing unfilled orders (`docs/STRATEGY_RESEARCH_GUIDE.md:90-112,155-173`).

**Strategies:** 1. Maker complete-set: enter equal UP and DOWN resting bids when planned cost is below $1; replace/cancel as books move; exit economically by completing the pair and settling. 2. Inventory repair: after one fill, buy the lagging leg at ask, either near expiry or shortly after a fill. 3. Symmetric taker mode: take one side and quote the other. Contrary to the guide's claim that symmetric taker mode is disabled (`docs/STRATEGY_RESEARCH_GUIDE.md:47`), checked-in development config enables it (`strategy-service/src/main/resources/application-develop.yaml:52-55`).

**Assessment and data:** Maker spread capture can avoid fees, but fill selection and leg risk dominate. Taker top-ups must include Andy's per-leg fee, and the repo's zero or near-zero edge thresholds do not. Andy's re-quotes already killed every observed complete-set edge within about one second, so only maker-first shadow fills merit testing. Gamma, CLOB, Data API and Polygon data are public. Live mode uses keys (`.env.example:9-24`); PAPER is the development default and settlement is dry-run (`executor-service/src/main/resources/application-develop.yaml:1-33`).

### PolyWeather

**What code does:** This is weather intelligence, not a trading bot. The README says execution, private thresholds and sizing are absent (`README.md:56-62`). DEB blends forecasts, applies lead and temperature-stratum bias, and turns a normal residual model into integer settlement buckets (`src/analysis/deb_probability.py:1-17,258-324`). Intraday logic removes impossible buckets below the observed high, shifts the mean using METAR trend, and suppresses upside in rain/cloud signals (`src/analysis/dynamic_forecast.py:1-16,144-217`). It also produces explicit confirmation and invalidation rules (`web/services/intraday_meteorology.py:228-310`).

**Strategies:** 1. Pre-event value: enter YES when calibrated bucket probability exceeds executable implied probability by fees and slippage; exit when the gap closes or the forecast invalidates. 2. Intraday observation update: after official settlement-source METAR/TAF changes the distribution, trade only a persistent post-refresh gap; exit after convergence, two no-new-high observations, or settlement. 3. Impossible-bucket fade: once the observed maximum rules out lower buckets, buy the complement or sell stale lower-bucket YES where executable.

**Assessment and data:** This is the strongest independent forecasting thesis, but claimed calibration is not Andy's proof. Evaluate lifetime and recent Brier skill, then cost-adjusted calls. Open-Meteo and aviationweather.gov METAR are free; optional providers need keys (`.env.example:52-77,214-216`). Market history is public. The repository also contains unrelated payment and deployment surfaces.

### polymarket_lp_tool

**What code does:** It manages existing manual orders, it does not originate the first order. The active policy keeps, cancels, or cancel-reposts the same remaining size (`README_EN.md:55-70`; `passive_liquidity/order_manager.py:90-210`). Coarse-tick orders use actual same-side book levels inside the reward band and cancel if too few levels exist (`simple_price_policy.py:742-887`). Tokens with any inventory are skipped, while older inventory and fill-risk logic remains in the repo but is not in the main loop (`README_EN.md:62-68`).

**Strategy:** Manually post maker orders inside the reward half-band. Reprice toward selected depth, pause on midpoint jumps or recent fills, cancel when the band thins, and stop after inventory appears. Rewards plus spread survive zero maker fees only if they exceed adverse selection, inventory loss and queue disadvantage. Public books and reward parameters support a shadow queue model, but historical reward attribution is needed. Live use derives API credentials from a private key (`passive_liquidity/clob_factory.py:11-45`).

### Prediction-Markets-Trading-Bot-Toolkits

**What code does:** Source explicitly states only copy trading is wired end to end (`src/bot/mod.rs:1-5,52-68`). It subscribes to Polygon fills, filters markets, sizes a copy, signs an order, and runs TP/SL (`src/bot/copy_trading.rs:1-25`). Despite the README's multi-wallet claim, it selects only the first wallet (`copy_trading.rs:48-54`). It ignores whale sells, enters whale buys with a buffered FAK, then exits on its own midprice TP/SL (`src/service/order_executor.rs:89-92,290-302`; `src/service/position_monitor.rs:20-30,94-146`).

**Strategies:** Implemented copy trade: enter after a watched maker fill, exit at configured TP/SL. Advertised BTC arb, cross-venue arb, spread farming, sports execution, resolution sniper, order-book imbalance, market making and multi-whale signal only log "in development" (`src/bot/arbitrage.rs:1-13`, `cross_market_arb.rs:1-13`, `spread_farming.rs:1-11`, `resolution_sniper.rs:1-12`, `market_maker.rs:1-11`, `whale_signal.rs:1-15`). Directional arb has parameters but no loop (`directional_arb.rs:33-68`).

**Assessment and data:** Copying after an on-chain fill is structurally late and often pays spread plus taker fee. The sample config sets `fee_rate_bps` to zero (`config.json:31`), invalid for Andy's crypto markets. Polygon logs, Gamma, books and resolutions are public, so wallet selection can be tested without keys. Reject any wallet whose out-of-sample, delay-adjusted copies fail the paper gate.

### polymarket-mcp-server

**What code does:** It exposes market, portfolio and live order tools. Its "AI-powered" opportunity analysis is a fixed rule: low liquidity or wide spread means AVOID, while a spread below 2 percent can produce BUY at 65 confidence, with no outcome forecast (`src/polymarket_mcp/tools/market_analysis.py:474-576`). Price suggestion merely chooses ask, bid, mid, or ten percent into the spread (`src/polymarket_mcp/tools/trading.py:582-742`). Smart trade parses words such as "buy" and "quick" and may split orders (`trading.py:1080-1221`).

**Assessment:** Tight spread is execution quality, not positive expected value. There is no falsifiable predictive edge, fee model or calibrated probability, so skip its recommendations. Its credential-free read tools could collect public books, but add no strategy. Trading stores a private key and can post real orders; confirmation defaults are safer but not a paper boundary (`src/polymarket_mcp/config.py:24-33,62-99`).

## Ranked Candidates For Andy Trader

1. **Intraday weather repricing:** Official settlement observations improve bucket probabilities before the market fully adjusts. Paper test timestamped forecasts, METAR/TAF, executable quotes and settlement, with frozen calls. Kill if either lifetime or recent Brier skill is non-positive, fewer than 200 independent settlements qualify, or hit rate is below fee break-even. Effort: L.
2. **Pre-event calibrated weather buckets:** DEB probabilities beat implied probabilities after cost. Replay only predeclared lead times and settlement stations. Kill on the same paper gate or unstable city/lead calibration. Effort: L.
3. **Selective delayed wallet copying:** A public wallet's buys retain alpha after realistic block, API and fill delay. Replay at the first executable post-log quote with Andy fees. Kill if alpha vanishes under one-second delay, recent Brier skill fails, or copied hit rate misses break-even. Effort: M.
4. **Maker-first paired quoting:** Resting YES and NO bids can form complete sets below $1 without taker fees often enough to overcome one-leg fills. Shadow queue position and require both simulated fills. Kill if fewer than 200 independent pairs settle profitably, or any assumed taker repair removes lifetime profit. Effort: L.
5. **Reward-band passive quoting:** Liquidity rewards exceed adverse-selection loss. Shadow order eligibility, conservative queue fills and published rewards. Kill if net rewards are unavailable, inventory-adjusted PnL is negative, or results depend on optimistic fills. Effort: L.

## Security Findings

- Do not run `polymarket_lp_tool/readme_ip.md:4`: it pipes a third-party remote installer into shell. The bot can also cancel every order from an authorized Telegram chat (`passive_liquidity/telegram_command_poller.py:202-235`) and cancel/repost live orders (`order_manager.py:108-210`).
- Do not run the MCP live installers or trading server with a real wallet. The Windows installer reads the key visibly, defaults autonomous trading to true, and writes the key into both `.env` and client configuration (`polymarket-mcp-server/install.bat:168-221,280-288`). The Unix installer also writes the key into `.env` and passes it in process arguments before storing it in client config (`install.sh:256-321,370-412`). `SETUP_GUIDE.md:64-74` prints a newly generated private key to the terminal.
- Do not run polybot's live executor or Toolkits with credentials. Polybot accepts live order requests after a header acknowledgement (`polybot/executor-service/src/main/java/com/polybot/hft/executor/web/LiveTradingGuardFilter.java:31-59`). Toolkits can post signed orders when both live flags permit it (`Prediction-Markets-Trading-Bot-Toolkits/src/config.rs:251-252`; `src/service/order_executor.rs:178-192`).
- No obfuscated payload or explicit private-key transmission to a non-Polymarket endpoint was found by static inspection. That is not a runtime security audit.
