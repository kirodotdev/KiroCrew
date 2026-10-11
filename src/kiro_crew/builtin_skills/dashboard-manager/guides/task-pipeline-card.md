# task-pipeline-card

The one guide with no retired template behind it. The old set had no fleet view:
every board view answered for ONE board, and a person running several at once
had to open several pages.

## What a reader learns

Where every piece of work in the fleet is right now, as one pipeline the work
moves along. Not "what is on board X" but "what is anywhere, and which stage is
backed up".

## Types its blocks bind

One fold, `workstreams` - the only one whose population is the whole crew rather
than one board. For a crew that nests, the tree-keyed `worktree` fold reaches
further: a root board, every worker it bound, and every board those workers
conduct, to closure.

| field | type | source |
|---|---|---|
| working | `number` | open, a worker reporting |
| needs you | `number` | blocked or asking - read FIRST |
| in review | `number` | worker reported `done`, no verdict yet |
| ready | `number` | accepted, nothing left to do |
| boards | `number` | how many pipelines feed this |
| the backed-up stage | `enum`, `choices: ["working","needs you","in review","ready"]` | |
| the oldest card there | `text` | its title, <= 6 words |
| its age | `number`, `unit: "h"` | |
| past the cap | `number` | `workstreams.omitted` |

**One stage per card, and `needs you` is read before any other.** A card that
could be counted in two stages must land in the one that names the next mover.
A blocked worker that reported two minutes ago belongs in `needs you`, not in
`working` - otherwise the stage the reader must act on is the one stage that
looks healthy.

## Block layout

| block | type | holds |
|---|---|---|
| headline | - | "Four ready. Two need you." |
| the pipeline | `stat_band` | working, needs you, in review, ready |
| where it is piling up | `bars` | the same four as shares |
| the oldest card | `note` | its title, with its age set inside |
| the fleet | `table` | boards, past the cap |

The `bars` block is what makes this a pipeline rather than four counters: a
stage holding most of the work is visible as a long bar before the reader reads
a single label.

## When it fits

Several boards are live and the reader owns all of them. This is the fleet
default - reach for it before `work-kanban` whenever "which board" is not the
reader's question.

## For a different subject

The four stages are a pipeline, so name them in the subject's own flow and keep
them in order: sourced / screening / onsite / offer for a hiring loop; drafted /
in review / approved / published for a launch week. Keep exactly one stage that
means "a person is the next mover", and keep it read first.
