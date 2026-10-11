# office

## What a reader learns

What my crew shipped today, what is waiting on my eye, and what the week cost -
for a person whose crew writes the deck, the email and the report rather than
the code.

## Types its blocks bind

One fold, `workstreams`, plus what you write yourself.

| field | type | source |
|---|---|---|
| delivered today | `number` | `workstreams` items the conductor accepted |
| waiting for review | `number` | open items whose worker reported `done` |
| credits this week | `number`, `unit: "credits"` | `workstreams` credits |
| first draft title | `text` | `{"agentic": true}` |
| its kind | `enum`, `choices: ["doc","deck","sheet","mail"]` | `{"agentic": true}` |
| its file | `text` | `{"agentic": true}` |

The file name and the kind are yours: no fold records what a writer named its
output.

**Keep the two lists apart.** "Delivered" is the conductor's own verdict;
"waiting for review" is the writer's claim about itself. Collapsing them tells
the reader something was approved when nobody approved it.

## Block layout

| block | type | holds |
|---|---|---|
| headline | - | what landed, <= 10 words |
| the three answers | `stat_band` | delivered, waiting, credits |
| waiting on you | `list` | at most 3 draft titles |
| what each is | `pills` | the kind of each draft |
| this week | `bars` | credits per workstream |

## When it fits

The reader manages writing, not shipping: a chief of staff, a finance lead, a
comms owner. Reach for it when "done" means a file exists and somebody has to
read it.

## For a different subject

The `enum` of kinds is the part worth rewriting: contract / invoice / filing for
a legal desk, lesson / quiz / report for a course. Swap `credits` for whatever
the reader actually watches - hours, headcount, spend. If no one reviews
anything, drop `waiting on you` and the page becomes a simpler delivery log.
