# work-kanban

## What a reader learns

Which column every item sits in, and - the reason this view exists - that one of
those columns is **waiting on the reader's own verdict**.

## Types its blocks bind

One fold, `work`.

| field | type | source |
|---|---|---|
| in progress | `number` | open items, worker still reporting |
| awaiting verdict | `number` | open items whose worker reported `done` |
| accepted | `number` | items the conductor accepted |
| rejected or left | `number` | rejected plus abandoned |
| goal, round | `text`, `number` | `work.conductor.*` |
| the item waiting longest | `text` | its title |
| its column | `enum`, `choices: ["in progress","awaiting verdict","accepted","rejected"]` | |

**A column is chosen from the item's state AND its worker's last status.** An
item still open whose worker reported `done` is waiting on the conductor, and
neither its state nor its status says that alone. Get this wrong and the reader
cannot see their own queue.

## Block layout

| block | type | holds |
|---|---|---|
| headline | - | "3 of 10 accepted. 2 wait on you." |
| the four columns | `stat_band` | the four counts |
| the shape of the board | `bars` | the same four as shares |
| waiting on you | `note` | the oldest item in that column |
| which column | `pills` | its column, as a chip |

Four blocks. The `bars` and the `stat_band` say the same thing twice - keep
whichever the reader reads faster and spend the room elsewhere.

## When it fits

The reader is the conductor, or the person the conductor escalates to. Choose
this over `goal-board` the moment "waiting on me" is a real queue rather than an
occasional event.

## For a different subject

Rename the four columns, keeping the one that means "waiting on the reader":
screening / loop done / offer out / passed for hiring; booked / confirmed /
paid / cancelled for a trip. If nothing ever waits on the reader, this view is
the wrong one - use `goal-board`.
