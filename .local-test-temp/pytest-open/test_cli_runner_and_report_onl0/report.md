# Polymarket AI Approval Gate Report

Data cut: 2026-10-29T12:00:00+00:00

## Summary

| Book | Approved | Skipped | Not Scored | Appr Settled | Appr Open | Appr Avg PnL | Appr 95% CI | Skip Settled | Skip Open | Skip Avg PnL | Skip 95% CI |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| bond_book | 1 | 0 | 0 | 0 | 1 | n/a | n/a | 0 | 0 | n/a | n/a |
| price_target_book | 0 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | n/a | n/a |
| Total | 1 | 0 | 0 | 0 | 1 | n/a | n/a | 0 | 0 | n/a | n/a |

## Verdict

not enough settled positions yet (approved 0 of 100, skipped 0 of 30)

## Details

- Settled rows excluded (null pnl or unparseable settled_at): 0
- Not scored rows: 0. Not scored positions are book rows without a decision row whose opened_at is at or before now minus 30 minutes and at or after the earliest decided_at across all decisions. This includes rows that were never eligible, rows whose model call failed, and rows pushed out by pass limits.

## Accepted Limitations

1. The model may know an event's outcome from general knowledge or a web tool; scoring within 30 minutes of opening reduces that but cannot remove it.
2. Approved and skipped positions may settle at different speeds, so the settled-only comparison can be biased; the open counts are shown beside it.
3. The verdict is looked at repeatedly as data grows, so it can give false alarms; it is advisory, and deciding to rely on the gate is the operator's choice.
4. Thirty skipped positions is a thin sample.
5. A candidate that was never scored is excluded; the report counts such rows as not scored, for any reason, without splitting the reasons.
6. Whether --sandbox stops the model from reading files outside its working directory is not established; the empty working directory only hides the repository from a model that does not go looking, and the databases hold paper data only.
7. Facts text from the book is untrusted; the sanitiser and the closing instruction reduce prompt injection but cannot remove it.
