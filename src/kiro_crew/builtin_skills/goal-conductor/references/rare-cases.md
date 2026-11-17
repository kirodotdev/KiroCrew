# Rare cases in verification

Read this before you act on an item flagged `pending_overdue`, and before you
settle a `human_approval` item.

## A `pending` acceptance has a budget

An item whose `pending_overdue` flag is true has read `pending` for 24 hours on end: a check that never reports, a runner that never frees up, a fork run nobody approves. Evaluate it once more with the rest of the batch, so the evidence you show is current. If it still answers `pending`, do not ask. The first time, keep waiting: write its bar again with `action=accept` (that ends the run, so the next `pending` verdict starts a fresh 24 hours), record `action=decide` with a reason that starts `pending overdue: re-armed once`, and say so in your report, quoting the evidence and saying it is closed if it goes overdue again. When it goes overdue again and its row's `decision` field (on every `work_ledger_read` row, compact included) still starts `pending overdue: re-armed once`, close it `abandoned` and report it with the evidence. Write no other `decide` on the item while it waits, so the marker stays. Either step clears the flag, so after a restart or a compaction there is nothing to redo. Any verdict other than `pending` ends the run too. A `human_approval` item never goes overdue, because it is settled by asking.

## A `human_approval` item is verified by asking

The evaluator answers `pending` for it forever, so the answer comes from the user. In a turn nobody typed (it opens with `[auto-nudge cycle`, `[work-ledger wake]`, `[sent by session` or `[headless run`, or a cron fired it), never call `ask_question`: record the ask with `work_ask_record`, put it in `needs_you` or an `[OPTIONS:]` reply, keep patrolling the other items, and record the verdict in the turn the user answers. In a turn the user typed, ask with `ask_question`. That call BLOCKS until the user responds and returns the answers as its tool result, so your turn does not end and no later patrol cycle has to pick the answer up: read the result and record the verdict in the same turn. Nothing else runs in this session meanwhile, so keep patrol work out of the turn that asks. The card waits up to 30 minutes; if the result says it expired, was dismissed, or that the user replied in chat instead, act on that outcome (re-ask, or read their next message) rather than inventing an answer.

A result saying the card was withdrawn (dashboard restarted, tab reset) is not an answer: re-issue it.
