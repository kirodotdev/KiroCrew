# roadmap

## What a reader learns

When each item happened, and what is still running. One row per item on one
clock, so a long-running item is visible as a long bar rather than as a row that
looks like every other row.

## Types its blocks bind

One fold, `work`, for its two stamps per item.

| field | type | source |
|---|---|---|
| board opened | `timestamp` | `work.conductor.created_at` |
| first write | `timestamp` | `work.conductor.first_entry_at` |
| last write | `timestamp` | `work.conductor.last_entry_at` |
| still running | `number` | open items |
| closed | `number` | |
| longest open item | `text` | its title |
| its age | `number`, `unit: "h"` | |
| cannot be placed | `number` | items with no open stamp |

**Name what you cannot place.** An item with no stamped open time has no
position on the clock. Counting it and saying so is right; dropping it leaves
the row count wrong, which is worse than an honest gap.

## Block layout

| block | type | holds |
|---|---|---|
| headline | - | "One item open 6 days." |
| the span | `timeline` | opened, first write, last write |
| running vs closed | `bars` | the two counts |
| the longest | `note` | its title, with its age set inside |
| not placed | `stat` | the count, caption "no open time stamped" |

## When it fits

Age is the fact that matters: something has been open too long and the reader
needs to see which. Prefer `timeline` over this when the reader's question is
about *agents* rather than items.

## For a different subject

The clock is the whole view, so the subject needs real durations - a hiring
loop's days-since-applied, a trip's booking dates, a launch's weeks-to-ship. Set
`unit` to whatever the reader counts in. The original drew a bar per item
grouped into a swim row per round; a package binds endpoints, not series, so
pick the one item whose age is the point and link the rest.
