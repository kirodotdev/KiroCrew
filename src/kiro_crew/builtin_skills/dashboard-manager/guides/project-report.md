# project-report

## What a reader learns

All five answers on one page: what the crew did, what each thing cost, what came
of it, and what still needs them. The widest of the twelve, and the closest
thing to the manager view the skill describes.

## Types its blocks bind

Everything here comes from the `workstreams` fold - the crew's own work across
every session - plus three values you write yourself.

| field | type | source |
|---|---|---|
| in flight, accepted, needs you | `number` | `workstreams` item counts |
| spent | `number`, `unit: "credits"` | `workstreams` credits |
| accepted of total | two `number` | for the `gauge` |
| last entry | `timestamp` | `workstreams.last_entry_at` |
| omitted | `number` | `workstreams.omitted` |
| headline, verdict, the one in the way | `text` | `{"agentic": true}` |

The verdict is yours because no fold ranks six reds and says which one matters.

## Block layout

| block | type | holds |
|---|---|---|
| headline | - | the conclusion, <= 10 words |
| the five answers | `stat_band` | in flight, accepted, needs you, spent |
| progress | `gauge` | accepted over total |
| needs you | `list` | at most 3 titles, each <= 6 words |
| the one in the way | `note` | the blocker, as the title |
| freshness | `table` | last entry, omitted |

Five blocks. Cut `freshness` into the `note`'s caption if the page runs long.

## When it fits

The default choice when someone asks for "a dashboard" with no further steer,
and the right choice whenever the reader is the person the work reports to.

## For a different subject

Rename every count in the subject's own words: candidates / offers out for a
hiring loop, deliverables / shipped for a launch. Drop `spent` where nobody is
paying attention to cost and give the room to `needs you`. The original drew a
full epic-story-task tree with per-row credits, which a package cannot bind and
one screen cannot hold - keep the `gauge` and link the tree as an artifact.
