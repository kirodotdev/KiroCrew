# flow

## What a reader learns

Whether the board is keeping pace with itself, and where work is piling up. Two
readings of the same board over time: how many items are still open, and how
many sat in each state.

## Types its blocks bind

One fold, `work`, read for its stamps rather than its titles.

| field | type | source |
|---|---|---|
| accepted | `number` | items closed accepted |
| still open | `number` | the burndown's current value |
| rejected or left | `number` | |
| opened in all | `number` | the top of the band |
| first write | `timestamp` | `work.conductor.first_entry_at` |
| last write | `timestamp` | `work.conductor.last_entry_at` |
| omitted | `number` | `work.omitted` |
| pace | `enum`, `choices: ["ahead","on pace","behind"]` | your reading |

A package cannot bind the per-step series the original charted - a field is one
scalar. Bind the endpoints and let `bars` carry the distribution; the shape over
time belongs behind a link, not on one screen.

## Block layout

| block | type | holds |
|---|---|---|
| headline | - | "Two items behind pace." |
| where it stands | `stat_band` | accepted, open, rejected |
| the distribution | `bars` | the three states as shares |
| how far along | `gauge` | accepted over opened |
| span | `timeline` | first write, last write |

## When it fits

The board has enough history for "pace" to mean anything, and the reader cares
about rate rather than content. On a board three days old it says nothing a
`gauge` does not.

## For a different subject

Pace needs a denominator the subject actually has: a launch date, a close of
quarter, a candidate's start date. Without one, drop the `pace` enum rather than
invent a target. **An un-drawable chart and an empty board say different
things** - both end in no bars, and only one means there is no work. Say which.
