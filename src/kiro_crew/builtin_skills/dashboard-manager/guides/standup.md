# standup

## What a reader learns

What changed since they last looked. A reader who asks how things are going
twice in one afternoon is asking for a **delta**; a page that restates the whole
board answers a different question.

## Types its blocks bind

Four folds, which is what makes this the widest-reading of the board views:
`work` for the items, `timeline` for the moments, `status` for the turns,
`approvals` for what is pending.

| field | type | source |
|---|---|---|
| finished | `number` | `work` items the conductor accepted |
| claimed done | `number` | open, worker reported `done` |
| still moving | `number` | open, nobody reported a blocker |
| stuck | `number` | blocked, asking, rejected or failed |
| turns completed | `number` | `status.turns_completed` |
| moments dropped | `number` | `timeline.dropped` |
| approvals pending | `number` | `approvals.pending` |
| headline | `text` | `{"agentic": true}` |
| last write | `timestamp` | `work.conductor.last_entry_at` |

The headline is yours: no fold records a one-sentence read of where the work
stands, and that is the bar for writing one yourself.

## Block layout

| block | type | holds |
|---|---|---|
| headline | - | your one-line read, <= 10 words |
| the four buckets | `stat_band` | finished, claimed done, moving, stuck |
| since you looked | `timeline` | last write, plus the newest moments |
| waiting on a person | `stat` | approvals pending |
| dropped | `table` | moments dropped, turns |

## When it fits

The reader checks in repeatedly through a day. Its value is entirely in the
delta, so it needs a `timestamp` the reader can anchor on - without one it
degrades into a worse `goal-board`.

## For a different subject

"Claimed done" and "finished" must stay separate columns whatever you call them:
a worker's `done` is a claim and the item waits on its conductor. For a hiring
loop that is "loop complete" versus "offer approved". Work time and silence time
are also different measurements - an item quiet for two hours is not the same as
one that took two hours.
