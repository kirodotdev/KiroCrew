# Pages to start from

Working pages, each answering a different reader question. **Every one is
optional.** Copy one and adapt it, mix two, or write a page none of them
describes. The runtime loads none of them by id; this is source to read.

## Whole packages, in `gallery/`

Each is a complete `kind="dashboard"` package: `view.html` carries the markup,
the CSS and the script, and `model` lists the fields it may be sent. Copy the
directory, change `bound_to`, change what it binds, then `dashboard_save` it and `dashboard_apply` with no arguments.

| package | the question its reader asks |
|---|---|
| `gallery/dispatch-tree/package.json` | how does one goal branch into the sessions doing it |
| `gallery/approvals/package.json` | what waits on me now, and what just arrived |

Two entries today, and each is an example on the same footing as everything below:
never the default page, never assumed by a prompt or a test, never required for
a dashboard to be complete. A page that shows no tree at all is a correct page.

Read the dispatch tree for three things any page you write will need. Where it binds
a cell it uses `data-dashboard-field="roots"`; where it draws the tree it uses
`window.kirocrew.subscribe('boards', draw)`. It builds every row by creating
elements and setting `textContent`, because a board's goal is whatever a
conductor wrote in the crew log, so a template string assigned to `innerHTML`
is how a recorded value becomes markup on your page. And every name its markup
binds appears in its `model`, which is also the list the controller pushes.

Read the approvals page for motion on a dashboard, done with the `living-ui`
toolkit: on a change, a count follows to its new value and a newly arrived
approval's row rises and fades once; then the page is still, with no idle tour.
Its counts are bound cells, so they are right before its script runs, and under
reduced motion every change is a jump.

## Pages that shipped

These are in the tree as source to read. Nothing resolves one by name at run
time, so read one for its information architecture and its bindings, then write
the page you were actually asked for.

| page | the question its reader asks |
|---|---|
| `project-report` | what did this crew do, what did it cost, what needs me |
| `office` | what landed today, what waits on my eye, what the week cost |
| `goal-board` | what is on the board, and what state is each item in |
| `work-kanban` | which column is each item in, including what waits on me |
| `swimlane` | that board again, but which round does each item belong to |
| `standup` | what changed since I last looked |
| `flow` | is the board keeping pace, and where is work piling up |
| `roadmap` | when did each item happen, and what is still running |
| `timeline` | what was each agent doing, and what ran at the same time |
| `org-chart` | who dispatched whom, and what did each worker last do |
| `session-ledger` | what is this long session carrying, and where would it resume |
| `pr-watch` | why is it still red, and who clears it |

## Crew drawer boards

Three human-written panel boards, reviewed markup fed by crew data. Read them
for the split that makes free HTML safe -- the page is the presentation and the
data arrives separately -- and for a dense board that still reads at 320px:

| board | shape |
|---|---|
| default | one row per item, state word plus a line on where it stands |
| conductor | a goal, its round, and the items the round is holding |
| pipeline conductor | lanes with what is red and who clears it |

## Task dashboards people liked

Pages published as `kind="dashboard"` artifacts on real long-running work. They
are the best evidence of what the design freedom in section 2 is for: no two
share a layout, and each was shaped by the work it reported. Read a recent one
from the Artifacts library rather than from a list that would go stale here.

## Reading one well

Three things to take, and one not to:

- **Take the information architecture.** What goes first, what folds away, what
  earns colour.
- **Take the bindings.** Which fold path each cell reads, and where a length is
  bound rather than a row.
- **Take the honesty.** How the page says "waiting on you" without inventing a
  percentage.
- **Do not take the shape as a requirement.** These pages answer a dozen
  different questions. Yours answers the one you were asked.
