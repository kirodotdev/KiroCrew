# goal-board

## What a reader learns

The ledger one conductor is running: the goal, the round it is on, and how its
items are distributed across states. The plainest of the board views - no
columns, no clock, just the board.

## Types its blocks bind

One fold, `work`, keyed by the conductor's slot.

| field | type | source |
|---|---|---|
| goal | `text` | `work.conductor.goal` |
| round | `number` | `work.conductor.round` |
| entries | `number` | `work.conductor.entries` |
| opened | `timestamp` | `work.conductor.created_at` |
| last write | `timestamp` | `work.conductor.last_entry_at` |
| open, accepted, rejected | `number` | counted from `work.items` |
| omitted | `number` | `work.omitted` |
| top item title | `text` | the one row worth naming |
| its state | `enum`, `choices: ["open","accepted","rejected","abandoned"]` | |

## Block layout

| block | type | holds |
|---|---|---|
| headline | - | the goal, trimmed to <= 10 words |
| the state split | `bars` | open / accepted / rejected |
| where it is | `stat_band` | round, entries, omitted |
| the item in front | `note` | its title, with its state set inside |
| when | `table` | opened, last write |

## When it fits

One board, one conductor, and a reader who wants the state of play rather than a
schedule. Also the right skeleton to start from when you are not yet sure which
board view the subject wants.

## For a different subject

The four `choices` are the real content: shortlisted / interviewing / offered /
declined for a hiring loop, drafted / reviewed / signed for a contract queue.
Keep the `bars` block - a share reads faster than four counts. The original drew
one card per item with its decision and verdict; a package cannot bind that
list, so name the one item in front and leave the rest to the board itself.
