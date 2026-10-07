---
title: Crewmate dynamic dashboard -- a project report beside every crewmate's chat
status: in-progress
author: chenmingwei23, with kirocrew-lead
created: 2026-10-03
last-audited: 2026-10-03
audited-at: f31e2f7091
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Crewmate dynamic dashboard -- a project report beside every crewmate's chat

- Status: in-progress. This document ships INSIDE its implementing pull
  request: the repository takes no standalone RFC pull requests, so `doc-pr` is
  null and the implementation is the one named in `implementation-prs`. The
  First Principles lane reads an RFC's status off the **base** branch, so until
  this document is on main it reads as absent to that lane; clearing the lane is
  a maintainer's call (an override on the final head, or merging this document
  first), not this pull request's.
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

