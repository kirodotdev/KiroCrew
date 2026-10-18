---
title: Crewmate dynamic dashboard -- a project report beside every crewmate's chat
status: partial
author: chenmingwei23, with kirocrew-lead
created: 2026-10-03
last-audited: 2026-10-15
audited-at: 40139dc512
doc-pr: 19204
implementation-prs: [17507, 17998, 18014, 18994]
tracking-issues: [18642]
supersedes: []
superseded-by: []
---

# RFC: Crewmate dynamic dashboard -- a project report beside every crewmate's chat

- Status: partial. P1's template contract, its instance store, the agentic write
  path and the crewmate Dashboard tab are on main, under the pull requests named
  in `implementation-prs`. On main the dynamic dashboard is still offered only
  behind the "Dynamic Dashboard" Feature Preview, whose removal the second
  amendment of 2026-10-12 records; with it off, a crewmate's Dashboard tab draws
  the published panel. P2 onward are open, and the agent-written page line is
  in flight under
  [issue #18642](https://github.com/kirodotdev/KiroCrew/issues/18642). This
  document is itself on main now, as the pull request named in `doc-pr`, so the
  First Principles lane -- which reads an RFC's status off the **base** branch --
  reads it as a recorded decision rather than as absent. The earlier reading of
  this bullet, that the repository takes no standalone RFC pull request and so
  this document could only ship inside its implementation, no longer holds: the
  amendment of 2026-10-11 below landed on its own.
- Where this document and its amendments disagree, **the amendment holds**. The
  one of 2026-10-11 supersedes §Migration plan's P2 row and the three passages
  that withheld an agent-written page until this gateway minted a wrapper
  document around it. The first of 2026-10-12 after it records three further
  decisions -- a save stages a page rather than landing it, the catalog flow is
  retired in favour of a reference gallery, and the approval gate is its own
  module -- and names the passage each one supersedes. The second of 2026-10-12,
  last, records that the dynamic dashboard is the Dashboard tab for every reader,
  with the "Dynamic Dashboard" Feature Preview removed: it closes §Open
  questions 3, supersedes §Backward compatibility's account of the published
  page, and sets two rules for the catalog that hold only until the first one's
  retirement of it lands.
- The amendment of 2026-10-15 drops the preview step. A `builtin` or `user` template and the owner's own `kind="dashboard"` package both go live on apply. Every apply writes a version, rollback is one call, `dashboard_write` keeps working on a bound package, and the frame's containment does not change. `RENDERABLE_SOURCES` and `AUTHORED_PAGE_REFUSAL` do not change, and a guard test holds that every renderable template writes values as text only.
- Author: written up by the crew that implements it.
- Created: 2026-10-03
- Related:
  [`../system-specs/modules/dashboard-instances.md`](../system-specs/modules/dashboard-instances.md)
  (the shipped contract for templates, instances, versions and snapshots),
  [`../reference/crew-log/fold-paths.md`](../reference/crew-log/fold-paths.md)
  (every value a session fold renders),
  [rfc-append-only-ledger.md](rfc-append-only-ledger.md) (the crew log the page
  reads), [rfc-conductor-work-ledger.md](rfc-conductor-work-ledger.md) (the
  boards and work items the report rolls up),
  [rfc-composable-layout-mechanism.md](rfc-composable-layout-mechanism.md) (the
  sibling design for assigning a view to a surface).

## Summary

A crewmate's work lives in its chat. To learn what it did, what it spent and
what it needs from you, you scroll the transcript. This design puts that answer
on one page in the side panel, next to the chat:

- **what the crewmate is doing**: every workstream it runs, and every task
  inside each one
- **what it cost**: credits per task, per workstream, per hour
- **what came out**: accepted, rejected, still in flight
- **what it needs from you**: blocked tasks, open questions, and the lines the
  crewmate itself wants you to act on

The page is a template the crewmate adopts, filled from the crew log. The
crewmate can also write its own values into it, and those are marked as its own.

Every reading this document claims about the page has a render behind it, taken at
the side panel's width and wide, in both themes. They are attached to the pull
request that carries the pages rather than committed here, because review evidence is
an attachment in this repository (see `.gitignore`). The recipe is in the tree:
`scripts/shoot_dashboard_evidence.sh` takes every one of them from a fixture in
`test/fixtures/dashboard_templates/` through `scripts/render_dashboard_builtin.py`,
with the clock pinned by the fixture, so a reader can regenerate any picture rather
than taking it on trust.

## Motivation

Three things a reader wants about a crewmate are each recoverable today, and
each costs a scroll:

1. **What is in flight.** The work ledger holds it, and the only rendering of it
   is a transcript the conductor and its workers wrote to each other.
2. **What it cost.** The usage fold holds per-session spend. Attributing spend to
   a *task* means knowing which worker session that task bound, which no surface
   joins.
3. **What needs a human.** A worker's `blocked` or `question` report is one line
   in a log that keeps growing past it.

The transcript is the record and should stay the record. What is missing is a
view over it that is current without a model call.

## Goals

- One page per crewmate, in the side panel beside its chat, answering the four
  questions above.
- Every number on it comes from a fold, so opening the page costs no model call
  and no full-log scan.
- A crewmate may fill a value no fold records, and the page must mark such a
  value as the crewmate's own rather than as a recorded fact.
- The page is data, not code: a template is loadable at run time and shareable
  between installs.

## Non-goals

- A template editor, a picker, or any authoring UI. The format is loadable; the
  controls to choose and edit a page are a later phase.
- Sharing a page, freezing one, or reading its version history.
- Replacing the transcript. The page is a view over the same log.
- Cross-crewmate rollup. One page is one crewmate.

## Design

### One page for everything the crewmate runs

The default view is **All**. A row of pills at the top holds `All` plus one pill
per epic, so switching between the different things the crewmate is doing is one
click.

| Block | What it answers |
|---|---|
| Headline | How many tasks are in flight, total spend, how many need you |
| Four cards | In flight, accepted (with progress bar), spent (with trend line), needs you |
| Credits per hour | Spend over time across every workstream, with tasks accepted each hour |
| Where it went | A donut of spend by workstream |
| Needs you | One card per item: tasks asking a question or blocked, plus the crewmate's own notes |
| Every epic | One tree of the work, with done/total, a progress bar and a cost on every row |

### The work as a tree

`project-report` drew the board list flat and put their tasks on a stage board,
so the work's own shape was nowhere on the page. It draws one tree of four
levels instead -- a board's goal is an EPIC, a round of it a STORY, a work item
a TASK, an item on a board that task's own worker runs a SUB -- beside a
two-ring sunburst of the same money.

Each row carries a level tag in its own tint, every tint a theme variable, so
both themes render the levels the product's own way rather than a hard-coded
hex. Leaves keep the result pills (Accepted / Rejected / Decide / Blocked /
Awaiting crewmate review), their pull-request chip and their duration. Epics and
the newest story
of each start open; tasks start shut, so an epic opens onto its rounds rather
than onto every item at once.

The rings are always the two levels UNDER the tree's root: epic inside and story
outside in the global view, story inside and task outside scoped to one epic.
That is what keeps "inside" and "outside" meaning one thing in both views -- a
share of the root, and a share of that share -- instead of a drill-down into a
different vocabulary.

### One workstream, task by task

Clicking a workstream pill narrows the same page to that one goal. Needs-you
comes first. Below it, every task gets one row: what it produced, what it cost,
how long it ran, and its result.

The narrowed view has no shot of its own: the pill is client state inside the
sandboxed document, which the fixture renderer cannot click, so what holds this
claim is the template's own test rather than a picture.

A task whose worker reported no cost shows a dash, never `0`. A zero would claim
the work was free.

### The three readings a user audit asked for

A study of what a person actually asks their conductor sessions -- fifteen asks over
one week of real transcripts -- found the page answered the two most common ones only
by being read, and three of them not at all. These are the three it now answers in a
line each.

**A verdict, written by the crewmate.** One line above everything else: a health word,
the headline, and the one thing most in the way. Written rather than computed, because
the lead already knows which of six reds is the one that matters and no fold ranks
them.

Four values, and the fourth is not padding. `on_track`, `needs_you` and `blocked` are
the lead's to pick; `no_word` is the page's own fallback, raised when the fold has
advanced since the verdict was written. A verdict with no honest silence state becomes
a stale lie the moment a lead dies mid-run, which on a box whose gateway restarts often
is the ordinary case rather than the edge one -- so the page downgrades it itself and
names the lead's last word underneath as a past reading rather than hiding it. The
comparison is against the record's own write time, never a time inside the value: a
stamp the writer supplied would be the writer's claim about its own freshness, which is
the claim in doubt.

**The build, on the PR chip and in a strip of the red lanes.** The chip carries the
state beside the number, and under the verdict a strip lists ONLY the lanes a reader
has to do something about -- a real board has ninety green ones, and a strip that
listed them is a strip nobody reads. Each red lane is tagged with who can clear it:
`yours` is one the reader can act on, `maintainer` one they can only wait for, and a
lane that never started is neither party's finding so it is tagged with the act it
needs instead. That third state is why this exists: drawing a failure to launch and a
real finding as the same red spends a reader's attention deciding which kind of red it
is, every time.

The lane board is an agentic field for now, written by the worker that is already
reading that exact board every cycle and currently throwing it away. A fold that polls
the forge is the correct source and is the next round's work; shipping the judgment
field first is what gets the reading in front of a reader this week.

**How long each row has been quiet, and whether the fold is advancing at all.** A
row's duration is WORK time, so a worker thinking for twenty minutes and a worker that
died twenty minutes ago read identically in it. Each in-flight row now carries the time
since its own worker last produced anything, amber once that is worth checking, and
above the page a band appears when the log itself has stopped reaching the fold.

The band is a different statement from the frame's own: that one says a FIELD did not
resolve, this one says every field resolved and none of them is being produced any
more. A restarted gateway leaves every number on the page intact and correct as of a
time nothing else on it names, so without this the report reads as current forever.

One workstream on its own carries all three the same way.

Every render behind the readings above is produced by
`scripts/shoot_dashboard_evidence.sh` from fixtures in the repository, and each
fixture pins the page's clock, so two runs on different days render the same picture.
An image nobody can regenerate is a claim rather than a reading.

### One task on its own, and how the work moves

Two more readings, from a study of an open-source agent kanban board whose ideas
these are. The implementation is ours and shares no code with it.

**A drawer, for one task.** Clicking a task row opens its own record beside the page:
what the worker last said, what the conductor decided, the verdict, the result, this
task's red lanes, and its own events newest first. Everything it shows is already in
the document, because the frame it is minted into has `connect-src 'none'` -- a drawer
that wanted to fetch an item's detail could not. That is why the fold carries a
bounded tail per task rather than the page asking for one.

Every block says what WOULD be there when it is empty. A blank area reads as a page
that failed to load rather than as work that has not landed, and the question a reader
opens a finished task with is what it produced.

At the side panel's own width the drawer covers the tree rather than narrowing it to
nothing.

The events are bounded by one ring across the whole fold, not per task: twelve boards
of forty tasks at twenty events each is neither a state worth checkpointing nor a page
a frame will mint. So a task also carries how many events it has HAD, and the drawer
says "20 of 34" or "no events recorded" -- a tail alone cannot tell a task that did
nothing from one whose lines the fold has moved past, and saying the first about the
second is a lie about the work.

**A pipeline, per epic.** One workstream's tasks as the three stages they move
through: with a worker, ruled by the acceptance check, accepted by the lead. Each task
sits in the furthest stage it has reached and never in two, so the three counts sum to
the total and the progress bar and the stages are one statement. The gate between two
stages is named rather than left to an arrow.

A worker is named by the render's own opaque alias, never by its session key: this
page is a document any dashboard caller can read, and the conductor ledger's rule is
that no reader but the conductor sees a key.

**And the page says whether it is being fed.** A dot and a read time, which are two
different facts: the dot is the fold's own advance -- the same reading the band above
is raised from, so the two cannot disagree -- and the time is when this page last
read. A page showing only the second looks current while its feed is dead.

A row whose own drawn values moved is marked for a moment on the refresh that moved
it, and not on first paint -- a page that marked every row the moment it opened would
be saying everything just changed. Under `prefers-reduced-motion` the same rows get a
rule instead of a flash, because the mark means something and only its motion is
optional. A refresh leaves an expanded row expanded and an open drawer open, showing
that task's new values: this page's whole point is that it advances, so a drawer that
shut whenever the fold moved would be unusable.

### Where the numbers come from

Every number comes from a **fold**: a small, always-current view the gateway
keeps over the crew log, projected in
[`../../src/kiro_crew/crew_log/projection.py`](../../src/kiro_crew/crew_log/projection.py).
The page never scans the whole log.

| Source | Who writes it | Used for |
|---|---|---|
| `workstreams` fold (new) | The gateway | Every board, its tasks, and each task's credits, joined from the bound worker's usage |
| Crew-log folds (`usage`, `timeline`, `work`, `ledger`, ...) | The gateway | Spend, activity, task state |
| Agentic fold | The crewmate, through `dashboard_write` | Values no fold has yet, such as "what I want you to look at next" |
| `mistakes` fold (new) | The gateway | Every refused write, so the crewmate sees its past errors before it writes again |

The `workstreams` fold carries two keys a reader cannot derive. A nested board
carries `parent: {board, item_id, title}`, read at RENDER rather than recorded
when the bind is folded: a bind can reach the fold after the sub-board's own
entries, and a link written at step time would then be missing for exactly the
boards that have one. A board whose own task bound its own slot is not its own
subtask, and a worker bound to two tasks names one parent -- the first, in board
then item order -- so the tree has one shape on every read.

Each task row also carries the `spender` it was costed through, and
reports that worker's whole spend, which is the fold's own posture: that is what
the task cost to run. One session bound to two tasks therefore appears twice at
the same amount, so a tree that ADDED its rows up would bill that session once
per task it served. A node therefore does not carry a number; it carries the SET
of spenders its subtree was billed through, and the number is that set's values
summed. A subtree nobody measured draws a dash.

`spender` is an opaque alias minted per render, not the worker's session key.
This payload is embedded in a page any dashboard caller can read, and the
conductor ledger's rule is that no reader but the conductor sees a session key.
The alias keeps the one property the roll-up needs, two rows billed to the same
worker carrying the same token, while naming nobody; because it is minted per
render, the same worker is a different token in the next read, so the tokens
cannot be accumulated across reads into one worker's history.

### Agentic values

The crewmate is encouraged to read folds first, but it may fill values in
itself through
[`../../src/kiro_crew/dashboard_agentic.py`](../../src/kiro_crew/dashboard_agentic.py).
What it writes is type-checked against the template's contract. A wrong write is
refused, recorded in `mistakes`, and the crewmate retries. On the page its values
carry a dashed **crewmate wrote this** tag, so the reader can tell a recorded
fact from the crewmate's own judgment. In the attached renders of the Needs-you
card, three of the five cards were written by the crewmate itself during a real turn.

### Templates are copied, versioned and shared

A template is one `manifest.json` (fields, types, which fold each comes from)
plus one `template.html` (layout and JS). It holds no server code, so the
gateway can load it at run time.

| Part | What it does | Where |
|---|---|---|
| Registry | Built-in templates ship in the repo, and in P1 they are the only ones a crewmate can be given | [`../../src/kiro_crew/dashboard_templates/catalog.py`](../../src/kiro_crew/dashboard_templates/catalog.py) |
| Manifest | Declares each field, its type and its fold path | [`../../src/kiro_crew/dashboard_templates/manifest.py`](../../src/kiro_crew/dashboard_templates/manifest.py) |
| Adopt and versions | The crewmate copies a template into its own instance; each edit is a new version, and rollback is a new version too | [`../../src/kiro_crew/dashboard_templates/instance.py`](../../src/kiro_crew/dashboard_templates/instance.py) |
| Share | Export to one file; import adds it to the registry | later: it ships with the chooser that calls it |
| Snapshot | Freezes the template, its version and the fold values at that moment | later: it ships with the UI that offers it |

A path in this document is a LINK once the file is in the tree and plain code until
then. This change ships as a stack, so a reader of an early pull request meets a
design whose later modules have not landed yet, and a link to a file that does not
exist is worse than a name: it reads as a broken document rather than as work still
to come.

Sharing and snapshots are the far end of that. They are designed here and built
behind the surfaces that call them, because a route with no caller is a surface
nobody can review against a use -- so they land with their chooser and their export
button rather than ahead of them.

Built-ins today: `project-report` (the default, shown above), `work-kanban`,
`goal-board` and `session-ledger`, documented in
[`../../src/kiro_crew/dashboard_templates/builtin/README.md`](../../src/kiro_crew/dashboard_templates/builtin/README.md).
`work-kanban` is a stage-column board with one row per work item.

### Relationship to the dev-time template package already on main

`src/kiro_crew/dashboard_templates/` exists before this change as a **dev-time**
contract model: a template is four files plus a registration line in
[`../../src/kiro_crew/dashboard_templates/registry.py`](../../src/kiro_crew/dashboard_templates/registry.py),
its data half is built by typed Python whose return type mypy checks, and its
`REGISTRY` is deliberately empty because the machinery shipped before the first
template. This design adds a second source to the same package rather than
replacing that one: a template described entirely by `manifest.json` plus
`template.html`, loadable without a release.

What the two share is the invariant that made the first one worth having. Both
go through
[`../../src/kiro_crew/dashboard_templates/parity.py`](../../src/kiro_crew/dashboard_templates/parity.py):
the set of `data-dashboard-field` names in the HTML must equal the set of fields
the contract declares, in both directions. For a manifest-described template
`check_parity` runs at load, so a malformed template is refused rather than
rendered with empty cells.

What differs is who may author one and when it is checked, and that is the part
this document asks maintainers to accept. The security posture does not move:
the gateway still runs no agent-authored code and evaluates no agent-authored
expression. A crewmate supplies typed VALUES through `dashboard_write`, checked
against the manifest's declared types, and a template's HTML is inert layout the
frame renders with the network blocked.

### How the page is drawn

The page runs in a sandboxed iframe with scripts allowed and the network
blocked, minted by
[`../../src/kiro_crew/dashboard_frame.py`](../../src/kiro_crew/dashboard_frame.py)
and mounted by
[`../../website/src/pages/members/CrewDynamicDashboard.tsx`](../../website/src/pages/members/CrewDynamicDashboard.tsx).
The host injects the fold values as `window.kirocrew` and fills each
`data-dashboard-field` slot. If a field stops resolving, the page keeps its last
good value and shows a stale banner.

## Migration plan

| Phase | Scope | Exit criteria |
|---|---|---|
| P1 (this pull request) | Template format, `workstreams` and `mistakes` folds, agentic writes, the owner-only page read, the tab that mounts a page, live refresh, and the twelve built-in pages with the project report as the default | A crewmate writes a value and the open tab shows it with no reload, and the side panel shows real workstreams and real spend with no model call to update a number |
| P2 | Template picker UI; version history; custom templates, behind a wrapper document this gateway mints so authored markup renders without the frame's own navigation | A reader switches a crewmate's page without an operator, and a page somebody wrote renders with no path to the values but the one the host gives it |
| P3 | Snapshots to shared storage; whole-page and region screenshots | A shared link opens a frozen report |
| P4 | Select a block and have the crewmate rewrite only that block | Such an edit keeps the contract check green |
| P5 | The page changes shape with the work: plan, execute, review | One session shows three layouts across its life |

Each phase is independently shippable and independently abandonable. P4 and P5
are blocked on open question 1 below, because both multiply the number of
refused writes a crewmate has to recover from.

P1 carries the contract and the pages together, because neither is reviewable
alone. A contract with no page is a format nobody has built against, and twelve
pages with no contract are twelve files nothing loads. Reading them in one place
is also what makes the format's own claims checkable: every one of the twelve
goes through the same `load_template`, so "the registry refuses a malformed
template" is a sentence with twelve witnesses rather than an assertion.

### What P1 ships ahead of its UI

The store that records which template a crewmate adopted ships here, because the
page read resolves a default through it and the write path validates against the
same record.

Four of its management routes ship with it, and they do so because P1 now has a
caller for them that is not a button: the AGENT. `templates`, `preview`, `apply`
and `rollback` sit on `/api/agent-panel/dashboard/`, behind the same
strict-internal gate as `fields` and `write`, and the six together are the
`kirocrew-panel` tool surface a crewmate reaches through MCP. A person who wants
another page says so in chat, and the crewmate they said it to carries it out --
which is a caller, and a reviewable one, without a picker existing.

The rest stay parked and are still P2 and P3: `adopt`, `edit`, `export`,
`import`, `snapshot` and `snapshots`. `adopt` and `edit` are not missing
capability -- they are the store functions BEHIND `apply`, which is one route
because the person said one thing. A history route is parked for a different
reason: the rows ride along on the `fields` read an agent already makes, capped
at ten, so a second round trip would buy nothing.

So in P1 nobody PICKS a page off a list in the UI. A crewmate that has adopted
nothing renders the default -- the project report this change carries -- and a
crewmate that wants another one asks for it in chat and is shown it first.

### The agent surface for the page itself

The flow is three phrases, and the route split follows them rather than the
store's verbs:

| the person says | the tool | what it writes |
| --- | --- | --- |
| "show me another" / "one that shows cost" | `dashboard_templates` | nothing |
| (the agent offers one) | `dashboard_preview` | a staged page beside the record |
| "keep this one" | `dashboard_apply` | one new instance version |
| "go back" | `dashboard_rollback` | one new instance version |

**A preview records nothing.** No version, no history row, and the page the
person is currently reading is untouched. That is what makes "show me another"
free when the answer is no, and it is why previewing is a separate verb rather
than a flag on apply.

**Apply takes no argument.** What it installs is the page that was STAGED, so
what lands is provably the page the person looked at. An argument here would let
the thing applied differ from the thing shown, and the agent in between is the
one place that difference would be invisible to everybody.

**Only a page that shipped with the product can be staged.** `dashboard_preview`
takes a `template_id` and nothing else; a call carrying a `manifest` and `html`
the agent wrote is refused, and the refusal says that custom templates come
later. The reason is the frame, not the format: a dashboard page runs its own
script against the crewmate's task titles and summaries, and the dashboard's own
`frame-src` admits the hosts its artifact previews need, so a page that runs can
navigate itself to one of them with those values in the URL. A closed
`connect-src` does not stop that. Scripts ARE the format -- a chart is script or
it is nothing -- so the rule has to be about the author: a directory in this repo
went through review, and a page an agent produced in the turn before did not.

That also means the registry reads one place. Scanning a writable directory as
well would let whatever can write that directory choose what a crewmate's
dashboard executes, which is the same hole by a slower route.

A page somebody writes themselves is **P2**, and it needs a wrapper document this
gateway mints: one that carries the authored markup as data inside a document the
product wrote, with the frame's own navigation withheld from it, plus a surface
where a person looks at the page before it runs. Until both exist, the honest
answer to "show me the one I wrote" is a refusal that says so.

These six verbs are auto-approved for a crewmate, on the same ownership test
`panel_publish` is granted by: the page is the calling crewmate's own, resolved
from the calling session and never from an argument, and every earlier version
stays on disk. The person's "yes" is part of the FLOW -- preview, ask, apply, and
the `dashboard` skill that drives it -- rather than an approval dialog, which
would ask them to confirm a page the agent has not shown them yet.

## Backward compatibility

The crewmate Dashboard tab rendered the crewmate's own `panel_publish` document,
and it now renders `project-report`. The page read resolves the default template
for a crewmate that has adopted nothing, so the body always carries a page and
the tab always draws the dynamic dashboard. Nothing in the frontend renders the published document any more:
`panel_publish` still accepts and stores, `GET /api/members/{slug}/panel` still
serves, and `CrewDashboardFrame` keeps no production caller. That is the decision
this document asks maintainers to record, and it is the one part of P1 that
removes a user-facing surface rather than adding one.

Both paths resolve the SAME default, and they have to. The page read falls back
to `default_instance` for a crewmate that adopted nothing, so `read_instance`
resolves it for that state too: the default page's one agentic field is the
"needs you" answer, the conductor skill writes that field every cycle, and no
surface in P1 calls the adopt route. A write path that refused the unadopted
state would refuse every one of those writes and name adopting a template as the
remedy, with no control in P1 able to carry it out. `error` stays refused, being
the one state where no manifest parses.

The alternative shape is to leave the default unadopted, so the tab keeps
falling back to the published page and the dynamic dashboard appears only for a
crewmate that adopted a template. That shape needs an adopt caller, which P1
does not ship. A phase that ships one can take it, and §Open questions 3 records
the choice as open.

`panel_publish` keeps its route and its store and has no reader. The conductor
skill sends an item needing a person to `dashboard_write`, and the drawer that
drew the published document has no production caller, so each cycle that
publishes a panel spends tokens on a document nothing displays. P1 leaves the
route accepting rather than removing it, because a crewmate mid-run may already
have published one and a removed route turns that into an error; the tool stays
advertised for the same reason. Retiring it is the follow-up this document asks
maintainers to record alongside the tab change, and §Open questions 3 is where
the decision belongs.

No wire contract is rejected, renamed or removed. The three new crew-log entry
types are additive: an older reader that does not declare them stops folding,
which is why they are declared in the same change that writes them.

## Security considerations

- A template is data, and data from an untrusted author is still data: the
  template holds no server code, and the gateway never executes it outside the
  frame.
- The frame's own policy is `default-src 'none'` with scripts allowed and the
  network blocked, so a template cannot reach a host, an endpoint, or another
  crewmate's values.
- Only a `builtin` template may become a live dashboard, and `builtin` means a
  directory in this repo. A closed `connect-src` stops a page from sending, but
  the dashboard's own `frame-src` admits the hosts its artifact previews need, so
  a page that runs can still navigate itself to one of them with the values in
  the URL. Scripts are the format, so the rule is about the author rather than
  the capability: a page an agent wrote, and a page a sender exported, are both
  refused at preview and at adopt. Making either renderable is P2 work that needs
  a wrapper document this gateway mints plus a surface where somebody reviews the
  page, and the registry scans no writable directory in the meantime.
- An agentic write is redacted for credentials and exfiltration URLs, bounded in
  nesting depth as well as serialized size, then re-validated against the
  manifest's declared type before it is stored.
- An agentic write is type-checked against the manifest's declared contract
  before it is stored, and a refused write is recorded rather than dropped.
- Fold values are read per crewmate slot. A page cannot name another slot's
  fold, because the handler resolves the slot from the request's own identity.

## Alternatives considered

- **A fixed page in the frontend.** Cheapest to build and impossible to share or
  version. It also forces every new number through a frontend release, which is
  the cost this design exists to remove.
- **Let the crewmate write the whole page each turn.** Then every number costs a
  model call and can be wrong. Folds exist precisely so a number is current
  without being re-derived.
- **Record a nested board's parent link when the bind is folded.** Rejected: a
  bind can reach the fold after the sub-board's entries, so the link would be
  missing for exactly the boards that have one.
- **Sum each task row's cost up the tree.** Rejected: it bills one worker session
  once per task it served. The union is what makes a roll-up equal the board
  total.

## Open questions

1. How many retries a refused agentic write gets before the crewmate asks the
   human (3, 5, or unlimited). It is a constant for now.
2. Whether a task's cost should include the conductor's own turns, or only what
   its bound worker spent. This change counts worker spend only.
3. Whether the published `panel_publish` page keeps the Dashboard tab when a
   crewmate has adopted nothing, or the default template takes the tab outright.
   P1 resolves the default, which takes the published page's one rendering
   surface; the alternative needs a reachable adopt, which P1 does not ship.
   Phase 2's template picker is where that caller would land.

## Verification

The fold is pinned by
[`../../test/test_workstreams_fold.py`](../../test/test_workstreams_fold.py),
the format by
[`../../test/test_dashboard_template_manifest.py`](../../test/test_dashboard_template_manifest.py),
the frame by
[`../../test/test_dashboard_frame.py`](../../test/test_dashboard_frame.py), the
write path and its refusals by
[`../../test/test_dynamic_dashboard.py`](../../test/test_dynamic_dashboard.py),
and the read route by
[`../../test/test_member_dashboard_routes.py`](../../test/test_member_dashboard_routes.py).
Each built-in page is pinned by its own file: the default report by
[`../../test/test_dashboard_template_project-report.py`](../../test/test_dashboard_template_project-report.py)
and the registry's built-ins together by
[`../../test/test_dashboard_templates_builtin.py`](../../test/test_dashboard_templates_builtin.py),
which resolves every declared `{fold, path}` against the eight folds a real pod
session served. Nothing in `load_template` can tell whether a path EXISTS, so a
plausible path that resolves to nothing would otherwise ship and render a blank
cell forever.

Six of the report's cases are negative-controlled: each rule was broken in turn
and the matching case required to fail -- a roll-up that adds instead of
unioning, a header that re-reads the board row, a cost total starting at `0`, a
nesting index that ignores `parent`, an `All` pill that counts boards, and a
headline card that counts boards.

The committed sample fixtures are checked as evidence in their own right,
because the renders above are taken from them: exactly one board nested under a
retained task, at least one session billed through two costed rows, and at least
one task with no cost at all. Without the shared-worker row a union and a sum
render the same number, so the case asserts the two totals actually differ and
that the fold's own board total equals the union.
[`../../scripts/render_dashboard_builtin.py`](../../scripts/render_dashboard_builtin.py)
renders any built-in from a fixture at both widths and both themes, and
[`../../scripts/shoot_dashboard_evidence.sh`](../../scripts/shoot_dashboard_evidence.sh)
reshoots every one of them in one command.

The route's own masking is checked against the whole rendered document rather
than the one field a reader would think to look at, because a worker's session
key reaches a page by three routes -- the field value, the board id a card is
keyed on, and a parent link -- and a per-field assertion sees one of them.

The tab's failure states are host chrome rather than page pixels, so they are
composed from the real component against a built bundle rather than from a
fixture. There are six, each shot at both themes:

| State | What the tab shows |
|---|---|
| unavailable | The dashboard could not be served at all, with its Retry |
| stale band | Some values did not resolve, so the band NAMES them rather than counting them, and says the rest is current |
| kept last good | A newer page failed to load while an older one was on screen, so the older one is kept and the band says so |
| mint failure | The document could not be minted into its sandbox at all, so there is no page to draw under the band |
| nothing adopted | A registry answering with no default. A state, not a failure -- see below |
| drawing | The read has answered and no document exists yet |

The twelve images are attached to the **pull request**, not committed. A
screenshot is evidence about one revision, and a repository that carries it pays
for it in every clone forever while the assertion it supports is already in
`website/src/test/CrewDynamicDashboard.test.tsx`.

None of these borrows the published-page frame's copy. That frame says
"published view", which this tab does not show, so each state has its own
`dashboard_*` string in all thirteen locales.

A newer page that failed to MINT while an older one was on screen reads the same
sentence as the kept-page band above, because it is the same situation: the page
on screen is the last one that loaded. Two wordings for one state left a reader
deciding whether they were two different problems, so there is one.

Nothing adopted, which a build whose registry serves no default renders. It is a
state and not a failure, so it carries no error styling and no retry: re-reading
returns the same answer, and a button that cannot change anything reads as a fault
the reader could clear. With the built-in pages shipped a crewmate that adopted
nothing renders `project-report` instead, so this state is the one behind a registry
that answers with no default rather than the one every crewmate meets.

The first thing a brand-new crewmate opens on, which is the state most readers
meet first: no workstream, nothing waiting, nothing spent, and a line saying
what will appear here once it has a goal. The accounting line is hidden rather
than printed as a row of zeroes.

## Amendment 2026-10-11 -- an agent-written page renders with no minted wrapper

**This section records an accepted decision, not a proposal.** It was decided on
2026-10-11 by this document's author for the dashboard line tracked in
[issue #18642](https://github.com/kirodotdev/KiroCrew/issues/18642), and it
lands on main on its own so that the decision is readable off a base branch
rather than proposed inside each pull request that depends on it.

It SUPERSEDES three passages above, each of which held an agent-written page
behind a wrapper document this gateway mints with the frame's own navigation
withheld: §Migration plan's **P2** row and its exit criterion, the
"Only a page that shipped with the product can be staged" passage in §The agent
surface for the page itself, and the "Only a `builtin` template may become a
live dashboard" bullet of §Security considerations.
The body above is left as written; where the two disagree, this section is what
holds.

### The decision

A page the owner agent wrote renders in the dashboard frame **without a
gateway-minted navigation-blocking wrapper**. The host loads the package's own
`view.html` into the frame and fills the fields the page subscribed to. No
wrapper document is minted around the authored markup, and no
navigation-stripping rewrite is applied to it. A page an agent wrote is
therefore renderable, which is what P2 refused.

Four reasons, and the fourth carries a condition:

1. **Only the owner agent writes the HTML.** The page is a file inside a
   `kind="dashboard"` package the calling agent owns, resolved from the calling
   session and never from an argument. No other agent, no sender and no reader
   can put markup there. The author test P2 was built to enforce -- a directory
   in this repository went through review, and a page an agent produced in the
   turn before did not -- is answered by ownership rather than by a wrapper: the
   page a crewmate renders is the page that crewmate's own owner agent wrote.
2. **Pushed values are text-only, so crew-log content cannot inject code or a
   URL.** Every value the controller pushes is a string, a number or a boolean,
   type-checked against the model the package declares, redacted, and bounded in
   size and nesting depth. A value is DATA the page reads; it is never markup,
   never a script source and never a navigable target. So a crew-log entry a
   third party influenced cannot become code or a host inside the page, which is
   the exfiltration route a wrapper was supposed to close.
3. **The frame holds only the `subscribed_fields` allowlist.** The push surface
   is the set of field names the package's model declares and the page
   subscribed to. A field the model does not declare has no path to the page at
   all -- the save gate refuses the binding, and the controller resolves every
   value through the manifest built from that same model. The frame carries no
   fold reader, no other crewmate's slot, and no name a page can ask for that
   the model did not already admit.
4. **The owner agent can already reach the network with its own tools.** A
   navigation-blocking wrapper exists to stop values leaving through a URL. The
   agent that wrote the page is the same agent that holds the values and already
   has network tools of its own, so the wrapper withholds from the page a
   capability its author has outside it. It buys no confidentiality against that
   author; it only makes charts, which are script or nothing, unbuildable.

### The admin-ceiling condition

Reason 4 holds only while the owner agent really has network access. When an
**admin ceiling DENIES the agent network access**, reason 4 fails: the wrapper
would then be withholding a capability the author does not have, and a page that
runs script would hand it one.

Under such a ceiling the host renders the agent-written `view.html` **without
`allow-scripts`**. The page is static, its subscribed fields are still filled by
the host, and the document gains no new egress path. Everything else about the
render is unchanged: the same file, the same frame, the same fields, the same
ownership resolution. A page whose drawing needs script degrades to its static
markup under that ceiling rather than being refused, because the reader's answer
is in the fields and a refusal would show them nothing.

### Ownership, and the flow a change goes through

The reasons above rest on the page having exactly one author, so the write path
is part of the decision rather than a detail beside it:

- **Only the owner agent may write a value into the page or change its HTML.**
  Both are resolved from the calling session's own ownership of the package, not
  from an argument.
- **The path is stage -> preview -> a person approves -> apply.** A write that
  changes the page stages it and shows it; it does not take effect on write. The
  person who reads the page is the one who approves the page they read, which is
  what `dashboard_preview` and `dashboard_apply` already separate: a preview
  records nothing, and apply installs the page that was STAGED, so what lands is
  provably the page the person looked at.
- **The controller pushes values only AFTER apply.** A staged page is never fed,
  so a page nobody approved never receives a value.

This is also the resolution of the preview-gate question raised against the
save-side pull request: a save-style write is held behind preview-ask-apply
rather than taking effect immediately, and a save-time lint on a hard-coded
number WARNS rather than refuses. Keep this text when that work lands -- the
flow is the premise the wrapper exemption above was granted on, so removing the
gate would reopen the decision rather than simplify it.

## Amendment 2026-10-12 -- a save stages, the catalog flow is retired, and the gate is its own module

**This section records three accepted decisions, not proposals.** All three were
decided on 2026-10-12 by this document's author for the dashboard line tracked in
[issue #18642](https://github.com/kirodotdev/KiroCrew/issues/18642), and they land
on main on their own so that each is readable off a base branch rather than
proposed inside the pull request that depends on it. Where this section and the
body above disagree, this section holds.

It leaves the amendment of 2026-10-11 standing. Decision 1 below is the gate that
amendment's exemption was granted on, stated as a rule of its own rather than left
as a sentence inside a paragraph about ownership.

### Decision 1 -- a save STAGES a page; it does not make it live

This supersedes §The agent surface for the page itself's verb table, which gives
staging to `dashboard_preview` and the undo to `dashboard_rollback`.

A save writes a page into the owner agent's own package as a **staged** copy. It
does not make that page live, it cuts no version, and the controller pushes it no
values. The live path is one sequence of four steps:

```
stage -> preview -> a person approves -> apply
```

- **stage** is the save. The agent asks the person in chat for the page they want,
  then saves what they agreed to; the save records the page and nothing else.
- **preview** shows the staged page to the person who will decide on it. A preview
  records nothing, which is what makes "show me" free when the answer is no.
- **a person approves** the page they have just read.
- **apply** cuts a version from the STAGED page and takes no page argument, so what
  lands is provably the page the person looked at.

The undo is `artifact_revert` on the package artifact rather than a
dashboard-specific rollback verb. A page is a version of an artifact, so the
artifact store's own revert is what puts an earlier page back, and a second
rollback mechanism over the same versions would be two answers to one question.

**The 2026-10-11 amendment's exemption rests on this gate, and that dependency is
recorded here rather than inferred.** That amendment lets an agent-written page
render with no gateway-minted navigation-blocking wrapper, and its reasons are
reasons about the AUTHOR: only the owner agent writes the HTML, pushed values are
text-only, the frame holds only the subscribed-fields allowlist, and the author
already reaches the network with its own tools. Not one of them says a page nobody
approved is safe to render. What makes the author test worth anything to a reader
is that the page in front of them is a page a person said yes to. So: **remove the
stage-preview-approve-apply gate and the no-wrapper decision is reopened, not
simplified.** A change that would render a page no person approved needs this
document amended first; it is not a local simplification of a save path.

### Decision 2 -- the catalog flow is retired; the pages become a reference gallery

This supersedes §Non-goals' "A template editor, a picker, or any authoring UI ...
the controls to choose and edit a page are a later phase"; §The agent surface for
the page itself's "show me another" row, which routes that request to
`dashboard_templates`; §Templates are copied, versioned and shared's Registry row
and its built-ins paragraph; and §Open questions 3's closing sentence, which parks
the caller in "Phase 2's template picker".

A crewmate **composes its own page**. There is no template picker, and the
`dashboard_templates` id flow is retired: **nothing is loaded by id at run time**,
and no run-time path resolves a page from a catalog entry.

The pages that exist are not thrown away. They change role:

| what it was | what it becomes |
| --- | --- |
| the twelve built-in dashboard templates | reference pages in a gallery |
| the three panel templates | reference pages in the same gallery |
| the good task-dashboard pages | reference pages in the same gallery |

A gallery page is READ MATERIAL for the agent that writes a page: it copies from
one and adapts it to what the person asked for. A gallery page is therefore not a
template -- it has no instance, no adopt, no id anything resolves, and nothing a
reader sees passes through it. The picker §Non-goals parked as a later phase is
not a later phase any more; it is cancelled, because the caller it existed for --
a person choosing one of our pages off a list -- is now a person asking the agent
for the page they want.

### Decision 3 -- the approval gate is its own module, and a renderer may name a package it does not publish

This supersedes §Backward compatibility's "The page read resolves the default
template for a crewmate that has adopted nothing, so the body always carries a
page and the tab always draws the dynamic dashboard", wherever that is read as
naming a page being the same act as publishing it.

The gate of decision 1 and the renderer are **two modules**, and each owns one
half:

| half | the module that owns it | what it may do |
| --- | --- | --- |
| the approval gate | the save module (`dashboard_save`) | stage a page, show it, take the person's yes, and apply -- and apply is the only thing that cuts a version |
| the render path | the dashboard renderer and its host | serve an APPLIED version of the package this dashboard is bound to, and fill the fields that page subscribed to |

From that split follows the thing worth writing down: **a renderer may hold the
name of a package it does not publish.** A renderer carrying a binding -- the slug
of the package this dashboard renders, `bound_package_slug` -- publishes nothing by
carrying it, because the renderer serves an applied version and nothing is applied
until a person has approved it. A bound package with no applied version renders
nothing at all. Naming and publishing are different acts: the binding answers
WHICH package a reader would see, and the gate answers WHETHER there is yet
anything to see.

The ordering this permits is one-way, and both directions are part of the
decision. A renderer may land before the gate, because until a version is applied
it has nothing to serve. The gate may not be dropped afterwards, because decision
1 is the premise the no-wrapper exemption of 2026-10-11 was granted on.

## Amendment 2026-10-12 (second) -- the dynamic dashboard is the Dashboard tab for every reader

**This section records four accepted decisions, not proposals.** All four were
decided on 2026-10-12 by a maintainer for the dashboard line tracked in
[issue #18642](https://github.com/kirodotdev/KiroCrew/issues/18642), and they
land on main on their own, so the pull requests that remove the preview and
carry the crew panel over can read them off the base branch. This is the second
amendment of that day. The first one -- a save stages, the catalog flow is
retired, and the gate is its own module -- stands, and so does the amendment of
2026-10-11.

It CLOSES §Open questions 3. It SUPERSEDES §Backward compatibility where that
section speaks of the published page: the tab change it asks maintainers to
record, the alternative that keeps the published page in the tab, and the
`panel_publish` follow-up. That section's other two points stand: both paths
resolve the same default, and no wire contract is removed. Where this section
and the body above disagree, this section holds.

It does not reopen the first amendment of 2026-10-12. That amendment's
decision 2 retires the catalog flow, and the retirement stands. Until the change
that carries it lands, the catalog still serves the dynamic dashboard's pages,
and after decision 1 below every reader sees them. Decisions 3 and 4 are rules
for that window only. Each ends when the catalog does, and neither is a reason
to keep it.

### Decision 1 -- the dynamic dashboard takes the tab outright, for every reader

The "Dynamic Dashboard" Feature Preview is removed. Every reader gets the
dynamic dashboard in a crewmate's Dashboard tab and in a root session's
Dashboard view in the side panel, and every reader is offered the All Dashboards
page and the lock switch on the Profile card. A crewmate that has adopted
nothing shows its default page (decision 4).

No surface renders the `panel_publish` document any more, and the tab never
falls back to it. On main a reader with the preview off still sees that
published view in the tab, which §Backward compatibility describes as gone;
after this decision no reader does. Open question 3 closes this way because a
fallback is a rule a reader cannot see: one crewmate would show one page or
another depending on whether anybody ever adopted one. The preview is a rule of
the same kind, a switch kept on each device, so it goes too.

The preview decided where the dashboard is offered, never what a page may do.
The frame, its policy and the rule for where a page's bytes come from are the
same with it on or off. Removing it changes who sees a page, and nothing about
how a page runs.

Kept: the person's layout lock, a root session's own page, preview and apply, a
way back to an earlier page, and the version history. Kept means the
capability, not today's module. Where the first amendment of 2026-10-12 moves
one -- preview and apply into the save module's gate, the way back into
`artifact_revert` -- that move stands.

The switch for automatic session cards, which the preview's card also holds,
stays in Settings > Developer as a setting of its own, off by default.

No status row above the chat composer comes back. It is retired, and its guard
test stays.

### Decision 2 -- the published panel is data a page may read

`panel_publish` keeps accepting on the same route, record and crew-log entry, so
no wire contract changes. What it publishes lands in the `panel` slot fold. A
page reads it like any other fold, `{"fold": "panel", "path": "data"}`, resolved
against the record of the crewmate whose page it is and never another crew's. A
page that shows it says the crewmate wrote it.

The tool is retired in a later change, after the catalog flow retires. That
change first gives the pipeline board's card a producer of its own, because
that card is built from a published panel today.

### Decision 3 -- an operator's own page renders, from one folder

A template directory the operator places in the data home's `panel-templates`
folder is served with source `user`. That folder already refuses every agent
write path, for the panel's own overrides, so a page there is one the person put
there, not one an agent wrote. A page there never shadows a shipped id, and a
linked entry is refused.

This narrows the rule that the registry scans no writable directory -- "the
registry reads one place" in §The agent surface for the page itself, and the
same clause in §Security considerations -- rather than reversing it. The rule
keeps an agent from choosing what a dashboard runs, and no agent can write this
folder. It leaves the amendment of 2026-10-11 as it is: an agent still cannot
place a page here.

When the catalog flow retires, this folder becomes gallery material: an agent
reads it, and nothing loads from it by id.

### Decision 4 -- a crewmate's default page may carry its name

A crewmate that has adopted nothing gets the page whose id is its own slug, when
the catalog serves one, and the default page otherwise. This carries over the
panel's rule that a template named after a crew is that crew's view, side effect
included: a crewmate named like a shipped page gets that page. It goes when the
catalog goes.

### What the catalog's retirement must carry

Piece 10 of #18642 removes the instance records. Before it does, the package
path needs a home for two things decision 1 keeps: the person's layout lock, and
a root session's own page.

Piece 7 of #18642 leaves a crewmate with no saved page on an empty state. It
waits until a composed page renders end to end. Until then, such a crewmate
keeps a default page rather than an empty tab, because after decision 1 every
reader sees that tab.


## Amendment 2026-10-15 -- a page goes live on apply, with no preview step

**This section records an accepted decision, not a proposal.** It was decided on 2026-10-15 by maintainer iamwhatever for the dashboard line tracked in [issue #18642](https://github.com/kirodotdev/KiroCrew/issues/18642), and it settles [issue #20354](https://github.com/kirodotdev/KiroCrew/issues/20354). It lands on main on its own, so the pull request that removes the step reads it off the base branch. Where this section and the text above disagree, this section holds. The three earlier amendments stand except where this one names a passage.

### Why the step goes

The preview was asked to be a safety gate, and it cannot be one. A page that leaks values does so in script. A person looking at the drawn page sees charts and cells, not the script, so a page that leaks looks the same as one that does not. A "yes" given after looking proves nothing about safety. It is only an extra step.

What does keep a page safe is unchanged by this amendment:

| part | what it holds | where |
| --- | --- | --- |
| who wrote a template's script | only a `builtin` page (reviewed in this repo) or a `user` page (the operator put it in `panel-templates`) renders as a template | `RENDERABLE_SOURCES`, checked by `_check` in `src/kiro_crew/dashboard_templates/instance.py` |
| who wrote a package's script | only the owner agent, resolved from the calling session | the package binding, `_page_binding` in `src/kiro_crew/dashboard/handlers/agent_panel_dashboard.py` |
| what reaches the page | typed values only, as text, never markup or a URL | `dashboard_write`, type-checked, redacted, bounded |
| what the page can reach | `allow-scripts` only, `connect-src 'none'`, `frame-src 'none'`, `form-action 'none'` | `src/kiro_crew/dashboard_frame.py` |

### Decision 1 -- apply makes a page live, and it always writes a version

This supersedes §The agent surface for the page itself's four-row verb table, its "A preview records nothing" and "Apply takes no argument" paragraphs, and the sentence on the person's "yes" being part of the flow. It supersedes the first amendment of 2026-10-12's decision 1 (stage -> preview -> a person approves -> apply), and the 2026-10-11 amendment's "Ownership, and the flow a change goes through" bullets that require it.

| the crewmate does | the tool | what it writes |
| --- | --- | --- |
| uses a shipped or operator page | `dashboard_apply` with a `template_id` | one new instance version |
| uses the page it composed | `dashboard_save`, then `dashboard_apply` of that package | one new package version, now bound and live |
| goes back | `dashboard_rollback` (template) or `artifact_revert` (package) | one new version |

- Apply installs the page now. There is no staged file, and no Keep or Discard button between apply and the tab.
- The person can press the same apply, and the same one-press rollback, in the Dashboard tab.
- Every apply and every rollback writes a new version. Earlier versions stay, so any page can be put back.
- Rollback re-runs the same checks as apply, so a page that no longer passes is not restored.
- `dashboard_write` keeps working on a bound package. Binding a package does not stop the crewmate's own fields.
- Owner only, as before. The page is the calling crewmate's own, resolved from the calling session and never from an argument. A worker still cannot write a package bound to another crewmate.

### Decision 2 -- the 2026-10-11 exemption is re-granted without the gate

The first amendment of 2026-10-12 says removing the gate reopens the 2026-10-11 decision that an owner-written page renders with no minted wrapper. This amendment reopens it and closes it again, the same way. The 2026-10-11 reasons were reasons about the author: only the owner writes the page, values reach it as text, the frame holds only the subscribed-fields allowlist, and the owner agent already reaches the network with its own tools. None of them depended on a person looking first, and the look added nothing, for the reason above. So the exemption stands on those four reasons alone, and its admin-ceiling condition stands with it: under a network-deny ceiling the page renders without `allow-scripts`.

### Decision 3 -- what does NOT change

- `RENDERABLE_SOURCES` stays `{builtin, user}`. A `shared` template and a template an agent wrote are still refused at apply and at rollback.
- `AUTHORED_PAGE_REFUSAL` stays. A template call that hands the store a `manifest` or `html` is refused. Its text names `dashboard_preview`, so the sentence is reworded to name `dashboard_apply`; what it refuses does not change. `EDITED_PAGE_REFUSAL` stays for the same reason. An agent's own page goes through the package path, not the template path.
- The frame's containment does not change: same sandbox, same policy, same CDN allowlist.
- The catalog's link and junction refusal for the operator folder stays, and a `user` page never shadows a `builtin` id.
- The layout lock stays. A locked crewmate's apply and rollback are refused, as today.

### Decision 4 -- a guard test holds the "values are text" premise

A reviewed template is safe to run because it never turns a value into markup or code. Checked at `40139dc512`: the 13 directories under `src/kiro_crew/dashboard_templates/builtin/` contain none of the calls below. Until now nothing enforced that. The pull request that removes the step adds the guard in the same change:

| where | what it does |
| --- | --- |
| a test over every `builtin/*/template.html` | fails if a page contains a listed call |
| the same check in the catalog scan, for a `user` page | the page is reported as a problem and not served, like a bad manifest |

The listed calls: `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `document.writeln`, `eval(`, `Function(`, `setTimeout` or `setInterval` with a string, `createContextualFragment`, `DOMParser`, `setHTMLUnsafe`, `srcdoc`. One list in one module, read by both the test and the scan, so they cannot drift.

It is a text match, not a parser. It can refuse a page that only names a call in a comment, which is the safe way to be wrong. It cannot prove a page is safe. It keeps the one route by which a value becomes code from being added quietly. A page with a real need for a listed call goes through review with the list changed in the same pull request.

### Cross-platform

Nothing here is platform-specific. Instance versions stay under `data_home()`, package versions stay in the artifact store, and the operator folder keeps its link and junction refusal through `platform_compat.is_link_or_junction`. Linux, macOS and Windows behave the same. The frame runs in the Electron shell's Chromium on all three, and in the reader's browser.

### Out of scope

- A gateway-minted wrapper document around an authored page, an outer document whose own `frame-src 'none'` would stop the page navigating itself. Decision 2 rests on the author, not on a wrapper, so this amendment does not build one.
- Pages over the `view.html` size cap, as #20354 says.
