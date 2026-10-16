# Evidence Bar Before Any Real Money

Written 2026-10-16 so "enough proof of profit" is a number and not a feeling. Dawn decides; this is the minimum
the paper record must show first. Claude does not place real orders. Dawn places any real order herself.

Per coin, judged one coin at a time:

1. Passes the skill gate: at least 200 settled non-overlapping calls that beat `baseline:base_rate` on Brier skill.
2. Passes the recent-window check on its latest 100 settled calls.
3. Holds up on a fresh window that was not used to pick it (at least 100 new settled calls after selection).
4. Net of fees and slippage, the paper equity is above start, and max drawdown stays under the 25% kill-switch limit.
5. The result survives the shuffled-order replay (`scaling_replay`), so it is not one lucky ordering.
6. At most the 3 best coins, each sized as tuition money (PHP 1,000 each, PHP 3,000 total), 1x leverage, no averaging down.

If fewer than 3 coins pass, fewer coins get real money. If none pass, none do, and the honest report is "no edge".
