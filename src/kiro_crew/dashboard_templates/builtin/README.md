# Built-in dynamic-dashboard templates

Templates in the format `kiro_crew.dashboard_templates.manifest` defines: one
directory per template, holding `manifest.json` and `template.html`. This is the ONLY
directory the registry discovers, and `load_template` is its only gate: a page that
renders on a crewmate's Dashboard tab is a page that went through review here, because
it runs its own script against that crewmate's task titles and summaries inside a frame
that can navigate itself. Twelve ship: the first four are below, five more are in "The round-7 set"
further down, then `office` in "A desk that ships decks and mail" and the last two in
"The two views".

| id | folds it reads | what it answers |
|---|---|---|
| `project-report` | `workstreams` | what this crewmate did, what each thing cost, what came of it, what needs the reader |
| `goal-board` | `work` | the work ledger it conducts: items, states, verdicts |
| `work-kanban` | `work` | that same board as columns, including the one waiting on the conductor's own verdict |
| `session-ledger` | `ledger` | what a long-running session is carrying, and what it would resume from |

`project-report` is the DEFAULT: a crewmate that never adopted a template renders it,
so the first thing anybody sees is the report rather than an empty frame
(`kiro_crew.dashboard_templates.instance.DEFAULT_TEMPLATE_ID`). The three round-3 pages
it replaced -- `crewmate-overview`, `work-map`, `spend-and-tools` -- were each one slice
of the same question asked three ways, and a reader had to assemble the answer from
three pages of engineering counters. One page answering the four questions in order
replaces them.

## The two rules every page here follows

**Absent is not zero.** Each fold reports how many turns reported a measurement next to
the measurement itself: `usage.turns.credits_reported`, `usage.turns.tokens_reported`,
`usage.turns.duration_reported`, and `usage.credits_by_source.*.reported`. A total of 0
beside a reporter count of 0 means nobody said, so the page draws a dash. Printing 0
would state an amount the record never claimed. The same applies to a fold's empty
string, which is a key the fold declares and nothing has filled.

Session-wide credits gate on the SUM of `usage.credits_by_source.*.reported`, not on
`usage.turns.credits_reported`. A crewmate that only delegates has a turn-scoped
reporter count of zero beside a real total.

**A truncated list says so.** Every fold here is bounded and reports what it dropped
(`timeline.dropped`, `tools.names_omitted`, `work.omitted`). Each page draws that number
beside the list it belongs to, so a partial picture is never shown as a whole one.

## How a page gets its values

The host fills every `data-dashboard-field` element by `textContent` and sets
`window.kirocrew = {fields, agentic, seq, stale}`. A field whose type is `array` or
`object` therefore reaches its bound element as JSON, which is unreadable in a cell, so
each page overwrites that one cell with a count and draws the real thing from
`window.kirocrew.fields` in its own script. Every page renders on load and again on each
`message`, because the order of the host's first fill against the script is not the
page's to assume.

No page fetches anything: the frame's CSP blocks the network, so a chart drawn from a CDN
renders as a hole. The charts are plain elements sized in the page's own JS.

## Agentic fields

`session-ledger` declares exactly one, `headline`: the crewmate's own one-sentence read
of where the work stands. No fold records a human-facing headline, which is the bar for
an agentic field. The page marks it from `window.kirocrew.agentic` rather than from its
own belief, so a value the crewmate wrote never looks like one folded from the record.

`project-report` declares three for the same reason. `for_you` is the lines the lead
wants its reader to act on next: the record holds which items are blocked or asking a
question, and the page folds those itself; what it cannot fold is the lead's own
judgment about what matters, so that half is written and marked as written. `verdict` is
the lead's one-line read of where the work stands, with the one thing most in the way --
the lead already knows which of six reds is the one that matters, and no fold ranks them.
`ci` is the lead's last reading of a code host's check board, which no fold polls. The
page draws the `crewmate wrote this` tag only when the host says the field IS agentic, so
a value arriving some other way cannot borrow the label.

`goal-board` and `work-kanban` declare none, because every number they show has a fold.

## Checking and rendering them

```
env -u KIROCREW_HOME .venv/bin/pytest -q -n 0 test/test_dashboard_templates_builtin.py
python scripts/render_dashboard_builtin.py \
    src/kiro_crew/dashboard_templates/builtin/<id> \
    test/fixtures/dashboard_templates/pod_folds.json out.png
```

The test resolves every declared `{fold, path}` against
`test/fixtures/dashboard_templates/pod_folds.json`, the eight folds a real pod session
served. Nothing in `load_template` can tell whether a path EXISTS, so a plausible path
that resolves to nothing would otherwise ship and render a blank cell forever.

## Adding one

Add the directory with both files, add its id to `EXPECTED_IDS` in the test, and check
the packaging globs still cover it (`[options.package_data]` in `setup.cfg` and the
`recursive-include` lines in `MANIFEST.in`). A template absent from the wheel leaves the
loader shipped and working while the registry discovers nothing, with every test green.

## The round-7 set

Five more, each answering ONE question a reader asks out loud rather than one slice of
the record. The questions and their counts come from `hci/REPORT.md`, which read them
out of real transcripts instead of guessing; the elements come from Linear, Jira, GitHub
Projects, Grafana, Datadog, Notion, Obsidian and Azure DevOps, cited per trick in that
folder's `templates-v2/REFERENCES.md`.

| id | folds it reads | the question it answers | agentic |
|---|---|---|---|
| `pr-watch` | `work`, `approvals`, `status` | why is it still red, and who clears it | `lanes` |
| `standup` | `work`, `timeline`, `status`, `ledger`, `approvals` | what changed since you last looked | `headline` |
| `roadmap` | `work` | when did each thing happen, and what is still running | none |
| `flow` | `work` | is the board keeping pace, and where is work piling up | none |
| `swimlane` | `work` | that board as columns, one lane per round | none |

Three of the five declare NO agentic field, which is the point of putting them beside the
two that do. `roadmap`, `flow` and `swimlane` are computed entirely from stamps the work
fold already writes, so nobody has to keep them current and a reader takes a chart for
the record safely. The two that do declare one declare it for a fact no fold holds:

`pr-watch`'s `lanes` is the crewmate's own last reading of a code host's check board.
No fold reads GitHub, so a folded CI state would be the record claiming something it
never recorded. The page marks the strip as written and draws the mark from the host's
own `agentic` list -- which is a LIST of names, so it is read with `indexOf` and not as
an object property. A page that asks that list for a property answers "nobody wrote
this" on every render while looking correct in review.

`standup`'s `headline` is the crewmate's one-sentence read of where the work stands,
the same reason `session-ledger` declares its own.

### The distinctions these five exist to keep

Each of these is a case where two different facts would otherwise render identically,
and each is pinned by a case in that template's own test file:

- **A check that failed and a check that never ran** are separate readings with separate
  words (`pr-watch`). One needs a fix and the other needs a re-run, so one red dot for
  both spends a reader's attention on nothing.
- **Checks green and mergeable** are not one word (`pr-watch`).
- **An acceptance verdict and a CI result** are labelled apart (`pr-watch`, `swimlane`).
  The first is the acceptance evaluator's ruling; a green board with a failed acceptance
  is exactly the case to see.
- **A worker's `done` and an accepted item** are different columns (`standup`,
  `swimlane`). An item still open whose worker reported done waits on the conductor, and
  neither its state nor its status says that alone.
- **Work time and silence time** are different measurements (`standup`, `swimlane`).
  Every other element shows how long something took, which a worker that died and one
  that is thinking share.
- **An un-drawable chart and an empty board** say different things (`roadmap`, `flow`).
  Both end in no bars, and only one of them means there is no work.

### Where a value is missing

Where a fold has no value, the page draws a dash and says what is absent, rather than
drawing a zero or going blank. `pr-watch` names an owner it was not given
(`owner not said`), `swimlane` marks a worker that never reported (`no report yet`), and
`roadmap` names an item it cannot place (`no open time stamped`) instead of dropping it
and leaving its own row count wrong.

## A desk that ships decks and mail

| id | folds it reads | the question it answers | agentic |
|---|---|---|---|
| `office` | `workstreams` | what my crewmate shipped today, what is waiting on my eye, what the week cost | `drafts`, `steps` |

`office` is for a person whose crewmate writes the deck, the email and the report rather
than the code. It binds the same four `workstreams` paths `project-report` does, and
derives four sections from them: delivered today, waiting for your review, in progress,
and credits this week.

Its two lists are the SAME shape drawn from different facts, which is the one error a
page about someone else's work must not make. "Delivered today" is the conductor's own
verdict (`state == accepted`); "Waiting for your review" is the writer's claim about
itself (an open item whose worker reported `done` or `question`). The split is pinned
against `kiro_crew.work_vocab` rather than against the page's own list, so a state the
product adds has to land somewhere deliberate.

Its two agentic fields each answer a question no fold has a field for. `drafts` is keyed
by task id and carries the artifact's type, its file name and the one line of meta a
reader needs to decide whether to open it. `steps` is keyed by board id and carries the
step that board is on right now. A row with no entry still renders from the record, so a
crewmate that never wrote either loses the file name and the step and nothing else. A
type nobody wrote draws no tag at all, rather than a hollow one a reader has to know a
convention to read.

The page wears `project-report`'s sheet rule for rule -- the same card radius and header,
the same KPI card, the same Needs-you card and kind token, the same result pill and the
same epic-row progress bar -- so the family reads as one product. Every colour on it is a
theme variable: it draws no chart and no sunburst, so a literal here would paint a colour
the product never serves.

The two buttons on a review row put a line in the person's chat box and never send it:
the page posts `kirocrew-dashboard:act`, the host fills the composer, and the person
presses send.

`scripts/render_dashboard_builtin.py --no-head` drops the harness's own caption band
(template, fold seq, theme, width) for a shot whose subject is how the page itself looks.
The caption stays on by default, because a screenshot that cannot say what produced it is
not evidence.
++ b/src/kiro_crew/dashboard_templates/builtin/README.md
## The two views: who was working when, and who dispatched whom

Two more, read as a pair and sharing one palette, because a reader opens them one after
the other and a colour that means "needs a person" on the first has to mean it on the
second.

| id | folds it reads | the question it answers | agentic |
|---|---|---|---|
| `timeline` | `workstreams` | what was each agent doing, and what ran at the same time | none |
| `org-chart` | `workstreams` | who dispatched whom, and what did each worker last actually do | none |

Both declare NO agentic field. Every mark on either page -- a bar's two ends, a role, a
status, a stamp, a cost -- has a fold behind it, which is the bar for a chart a reader
takes for the record: the one thing a timeline must not do is place a bar where nothing
happened, and the one thing an org chart must not do is draw a reporting line nobody
bound.

**They are not `roadmap` at a different scale.** `roadmap` answers "when did each ITEM
happen" from one board's `work` fold. These answer "what was each AGENT doing", across
every board the slot reaches, which needs the `workstreams` fold's two joins: `spender`,
which says two task rows were billed to one worker session, and `parent`, which says
which task's bind created a nested board. Neither join is in the `work` fold.

### The fold grew two stamps for this

`created_at` and `closed_at` per task row, spelled exactly as the `work` fold spells its
own. Nothing the fold already carried could place a worker's row on a clock:
`duration_ms` is the worker SESSION's total work time -- one figure however many tasks
that session served, naming no instant -- and `last_report_at` is a single instant.
`_workstreams_span_opened` writes the opener once, by whichever entry reaches the item
first; `_workstreams_span_closed` reads the item's resulting STATE rather than the
action's name, so a `close` that sets `open` does not end a bar and a reopened item
loses its close stamp. `test/test_workstreams_spans.py` pins both, including that a
worker's report never stamps a close -- a worker's `done` is a claim, and the item waits
on its conductor.

### The distinctions these two exist to keep

- **Four readings of an open task** get four words, and the order of the branches is
  the invariant: `blocked` or `question` means a PERSON is the next move and is read
  BEFORE any stamp, so a blocked worker that reported a minute ago does not read as one
  that is working. Then a recent report is `working`, an old one is `idle`, and **no
  report at all is an absence rather than a long silence** -- drawn as an outline rather
  than a fill, because the only other grey on the page is `abandoned`.
- **Twenty minutes and two hours** are two silences, in two colours. The twenty is
  `project-report`'s own `QUIET_MS`, asserted against that file, so two pages of one
  dashboard cannot disagree about the same worker.
- **The silence itself is drawn**, as a hatched tail from the last report to the bar's
  live end -- so idle has a length rather than only a label -- and only while the task
  is OPEN. A closed task's gap between its last report and its close is the conductor's
  time, and hatching it would blame a worker for a wait it did not own.
- **A lead's row is an extent, not a session.** No fold records a conductor's own span,
  so it is a thin rule labelled as the hull of its tasks. The overlap strip above the
  ruler counts worker tasks only, since counting an extent would count its own tasks
  twice and roughly double the peak.
- **A sub-lead is its own role, and one node.** It both answers to someone and
  dispatches others; calling it a worker hides half the structure, and drawing it as a
  board node plus a task node puts one agent on the page twice.
- **A latest-run card is picked by the worker's own last word**, never by a close stamp,
  which is the conductor ruling rather than the worker working.
- **A rejected run and a run waiting on a person** are not neighbouring shades: one is
  finished and the other is the reason to put the page down.

### No session key reaches either page

`spender` is an opaque per-render alias, and the conductor ledger's rule is that no
reader but the conductor sees a session key. These documents are embedded in a page any
dashboard caller can read, so a card names the **board and item** a host resolves the
session from -- the same pair the card-action route takes as lookup keys -- and never
the key. A live click-through needs a host message and handler, which is a host change
rather than a page one. `test_dashboard_template_views.py` asserts neither page names
`worker_session_key` or binds a `slot`.
