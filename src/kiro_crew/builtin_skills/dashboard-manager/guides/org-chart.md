# org-chart

## What a reader learns

Who dispatched whom, and what each worker last actually did. The crew as the
shape it really has, drawn from binds the record already holds: a board's
conductor is a lead, each of its tasks is a dispatch, and a board whose parent
names a task of another board hangs under it.

## Types its blocks bind

One fold, `workstreams`, for the same two joins `timeline` needs.

| field | type | source |
|---|---|---|
| leads | `number` | boards with a conductor |
| sub-leads | `number` | agents that answer to one and dispatch others |
| worker sessions | `number` | |
| depth | `number` | how far the tree goes |
| working, needs you, quiet, no report | `number` | one per reading |
| the agent needing a person | `text` | its alias |
| its reading | `enum`, `choices: ["working","needs you","quiet","no report","accepted","rejected"]` | |
| tasks past the cap | `number` | `workstreams.omitted` |

**A sub-lead is its own role and one node.** It both answers to someone and
dispatches others; calling it a worker hides half the structure, and drawing it
as a board node plus a task node puts one agent on the page twice.

**A latest-run card is picked by the worker's own last word**, never by a close
stamp - that is the conductor ruling rather than the worker working.

## Block layout

| block | type | holds |
|---|---|---|
| headline | - | "Two leads, 13 workers. One needs you." |
| the shape | `stat_band` | leads, sub-leads, workers, depth |
| what everyone is doing | `pills` | the readings, each a chip with a word |
| who needs you | `note` | the agent, with its reading set inside |
| past the cap | `stat` | tasks the fold dropped |

A graph is not a package block. The hierarchy itself lives behind a link; what
one screen can carry is the counts and the one node that needs a person.

## When it fits

The reader does not know the crew's shape - a new manager, a handover, a crew
that grew by nesting. Once the shape is known, `timeline` and
`task-pipeline-card` answer more per pixel.

## For a different subject

The roles generalise cleanly: hiring manager / recruiter / interviewer, producer
/ stage manager / crew. Keep the three-role split and the "no report" reading,
which is an absence rather than a silence and needs its own word. Never draw a
reporting line nobody bound.
