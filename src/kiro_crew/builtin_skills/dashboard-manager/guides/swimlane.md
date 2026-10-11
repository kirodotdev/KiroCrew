# swimlane

## What a reader learns

The same columns as `work-kanban`, grouped into a lane per round. A flat board
answers how much sits in each column; this also answers **which round** it
belongs to - the question a conductor running several rounds at once has.

## Types its blocks bind

One fold, `work`. The lane is the item's own round, never an invented epic
field.

| field | type | source |
|---|---|---|
| current round | `number` | `work.conductor.round` |
| rounds on the board | `number` | distinct rounds among `work.items` |
| this round: open, waiting, accepted | `number` | counted within the current round |
| earlier rounds still open | `number` | the number that matters most here |
| generation | `text` | `work.conductor.generation` |
| the oldest open round | `number` | |

## Block layout

| block | type | holds |
|---|---|---|
| headline | - | "Round 3 running. 1 item left in round 1." |
| this round | `stat_band` | open, waiting, accepted |
| rounds still open | `bars` | one bar per round, open count |
| the laggard | `note` | the oldest round with work left |
| board | `table` | current round, rounds, generation |

## When it fits

Several rounds are genuinely live at once and an item left behind in an old
round is the thing worth catching. With one round on the board, this is
`work-kanban` with an extra label - use that instead.

## For a different subject

A lane does not have to be a round. Make it the week, the cohort, the city, the
release: anything that groups work into batches a reader thinks about
separately. The `bars` block carrying one bar per lane is the whole idea; keep
it even if you change everything else. An acceptance verdict and a CI result
stay labelled apart - a green board with a failed acceptance is exactly the case
to see.
