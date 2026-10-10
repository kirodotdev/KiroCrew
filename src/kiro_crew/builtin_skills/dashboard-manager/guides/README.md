# Reference guides

Thirteen worked examples, each one answering a question no other guide here
answers. The table below is that list, and it is the reason the set is this
size: not symmetry with the retired template set, but one worked composition per
question a reader actually opens the page to ask.

Twelve of those questions were reachable before, because a template answered
them, and a person who had that page needs the composition that still answers it
under the one-screen limits. `task-pipeline-card` answers the thirteenth, the
fleet view no template covered.

Each guide names the composition it fixes: the view it draws, which scalar
fields it binds, and what the retired shape did that section 2a refuses.

**Every one is a starting point.** Mix two, change the blocks, drop half of one,
or invent a view no guide here describes. A guide tells you what a reader learns
from that shape and which types it binds. Whether the shape fits *this* subject
is your call, and a page wearing the wrong guide is worse than one you composed
from the catalog yourself.

Each guide is five short sections: what a reader learns, which types its blocks
bind, the block layout, when it fits, and what to change for a different
subject. Read one in seconds.

| guide | the question its reader asks |
|---|---|
| [`project-report`](project-report.md) | what did this crew do, what did it cost, what needs me |
| [`office`](office.md) | what landed today, what waits on my eye, what the week cost |
| [`goal-board`](goal-board.md) | what is on the board and what state is each item in |
| [`work-kanban`](work-kanban.md) | which column is each item in, including what waits on me |
| [`swimlane`](swimlane.md) | that board again, but which round does each item belong to |
| [`standup`](standup.md) | what changed since I last looked |
| [`flow`](flow.md) | is the board keeping pace, and where is work piling up |
| [`roadmap`](roadmap.md) | when did each item happen, and what is still running |
| [`timeline`](timeline.md) | what was each agent doing, and what ran at the same time |
| [`org-chart`](org-chart.md) | who dispatched whom, and what did each worker last do |
| [`session-ledger`](session-ledger.md) | what is this long session carrying, and where would it resume |
| [`pr-watch`](pr-watch.md) | why is it still red, and who clears it |
| [`task-pipeline-card`](task-pipeline-card.md) | where is every piece of work in the pipeline right now |

## The constraint that reshapes all twelve

The old templates bound a fold's `items` array whole and drew the rows in their
own script. **A package field is one scalar**, and no block takes a series. So
every list in these guides becomes: its length as a `number`, the top one to
three rows as separate `text` fields, and each row's state as an `enum`.

That is not a loss of fidelity so much as the hard limits applied earlier. A
screen holding one `table` of 24 rows was already failing section 2a. The
retired `project-report` rendered **2029 CSS px tall** at 1280 wide, against a
limit of 800 with no scroll -- so a guide is not a copy of that page, it is the
composition that answers the same question inside the limits.
