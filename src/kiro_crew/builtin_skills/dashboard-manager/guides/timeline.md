# timeline

## What a reader learns

What each **agent** was doing, and - the question no other view answers - what
ran at the same time as what.

This is not `roadmap` at a different scale. `roadmap` answers "when did each
ITEM happen" from one board. This answers "what was each AGENT doing" across
every board the slot reaches.

## Types its blocks bind

One fold, `workstreams`, which holds the joins the `work` fold does not: which
tasks were billed to one worker session, and which task's dispatch created a
nested board.

| field | type | source |
|---|---|---|
| agent rows | `number` | `workstreams` sessions drawn |
| peak at once | `number` | the most tasks running simultaneously |
| span | `number`, `unit: "h"` | end to end |
| window start, end | `timestamp` | |
| running now | `number` | |
| quiet over 2h | `number` | |
| never reported | `number` | sessions with no report at all |
| busiest agent | `text` | its alias |

**Four readings of an open task, and the order of the branches is the
invariant.** `blocked` or `question` means a person is the next move and is read
BEFORE any stamp, so a blocked worker that reported a minute ago does not read
as one that is working. Then a recent report is working, an old one is idle, and
**no report at all is an absence rather than a long silence**.

## Block layout

| block | type | holds |
|---|---|---|
| headline | - | "Nine agents at peak. Two quiet." |
| the window | `timeline` | start, end |
| who is in what state | `bars` | running, quiet, never reported |
| peak | `stat` | tasks at once, caption "running together" |
| busiest | `note` | the agent, with its span set inside |

## When it fits

The crew is big enough that overlap is a real question - roughly five agents up.
Below that, the reader can hold the whole crew in their head and `org-chart`
answers more.

## For a different subject

"Agent" generalises to anyone doing the work: an interviewer, a contractor, a
venue. Keep the peak figure - "how many at once" is the view's reason to exist.
**No session key reaches the page**: the fold aliases it, and a card names the
board and item a host resolves a session from instead.
