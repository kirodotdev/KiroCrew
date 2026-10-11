# pr-watch

## What a reader learns

Why a piece of work is still red, and who clears it. Red and never-ran lanes
come first, with an owner each.

## Types its blocks bind

`work` for the items, `approvals` for what waits on a person, `status` for the
session - and the lane reading from you, because **no fold reads a code host**.
A folded CI state would be the record claiming something it never recorded.

| field | type | source |
|---|---|---|
| red | `number` | `{"agentic": true}` |
| did not run | `number` | `{"agentic": true}` |
| green | `number` | `{"agentic": true}` |
| the blocking lane | `text` | `{"agentic": true}` |
| its owner | `enum`, `choices: ["yours","maintainer","rerun it","nobody"]` | `{"agentic": true}` |
| waiting on you | `number` | `approvals.pending` |
| requested | `number` | `approvals.requested` |
| head | `text` | the sha you read the board at |
| read at | `timestamp` | when you read it |

## The distinctions this view exists to keep

- **A check that failed and a check that never ran** are separate readings with
  separate words. One needs a fix, the other a re-run, so one red dot for both
  spends the reader's attention on nothing.
- **Checks green and mergeable** are not one word.
- **An acceptance verdict and a CI result** are labelled apart. The first is the
  acceptance evaluator's ruling; a green board with a failed acceptance is
  exactly the case to see.
- **An owner you were not given** is "owner not said", not a guess.

## Block layout

| block | type | holds |
|---|---|---|
| headline | - | "2 red, 1 never ran. A maintainer clears it." |
| the lanes | `stat_band` | red, did not run, green, waiting on you |
| the one in the way | `note` | the lane, with its owner set inside |
| who clears it | `pills` | the owner, as a chip |
| read at | `table` | head, read at |

Because every lane number is agentic, the page must say so and must say when:
a stale CI reading looks exactly like a fresh one.

## When it fits

Something is blocked on a machine verdict and the reader's next move depends on
which verdict. It is narrow by design - it answers one of the five questions
well and the other four not at all, so pair it or use it as a second page.

## For a different subject

A lane generalises to any external check the reader does not control: a
background check, a permit, a credit approval, a customs hold. Keep the
failed-versus-never-started split and keep the owner enum - "who clears it" is
the whole value.
