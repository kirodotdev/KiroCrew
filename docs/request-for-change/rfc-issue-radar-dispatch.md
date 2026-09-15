---
title: Issue Radar dispatch — let an agent complete an issue, not just investigate it
status: draft
author: zezhexu
created: 2026-08-12
last-audited: 2026-08-12
audited-at: 9fe52b10a
doc-pr: 2920
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Issue Radar dispatch — let an agent complete an issue, not just investigate it

Issue Radar can already put an agent on an item and write its conclusion back
onto the card. It cannot ask the agent to *do* the item. This proposes the third
verb: dispatch an issue to an implementation attempt, contained in a worktree and
a pull request, and accept or reject it on evidence the environment produced
rather than on a diff the human reads.

## Summary

An issue is already a task specification. So dispatch needs no new authoring
surface: it is one more button next to Investigate and Review.

The agent works in its own git worktree, opens a pull request, and the pull
request's machine-produced signals are rendered as that issue's acceptance card,
alongside an explicit list of what the machine could not judge. Merging stays a
human action. Nothing about the existing Investigate and Review paths changes.

## Motivation

### What exists today

`website/src/apps/issue-radar/lib/agentSession.ts` is a shared "open an agent
session for one provider item" orchestration: resolve the per-repo chat folder,
create a slot, seed and auto-run the first turn, link the per-item record so a
repeat click resumes instead of duplicating, navigate to chat. Investigate
(`lib/investigate.ts`) and Review (`lib/review.ts`) differ only in the seed
prompt and the slot title.

The per-item record is the same store on both sides, namespaced by item kind, and
`store._merge_findings` merges findings per key. The agent writes into it through
the `issue_radar_record_investigation` MCP tool rather than a raw HTTP call,
because an agent session holds no dashboard credential
(`docs/system-specs/modules/issue-radar.md`, "Recording findings").

So the dispatch machinery, the per-item record, and the write-back path are all
already built. What both existing verbs produce is a *reading*: a verdict, a root
cause, a summary.

### Three gaps this closes

**No verb produces work.** The loop an issue lives in is: someone reports it,
someone implements it, someone accepts it. Issue Radar covers triage and review
of a change that already exists. The step in the middle is the one a human still
does by hand, and it is the step that determines how many issues a person can
carry at once.

**Issues and pull requests are two boards with no join.** The spec describes the
pull-request section as reusing the same shape as the issue section. Nothing
links an issue to the change that resolves it inside the app, so even today a
human who investigates an issue and then reviews its fix is holding the
connection in their head.

**The card's conclusion is a self-report.** `issue_radar_record_investigation`
takes `verdict`, `root_cause`, `suggested_labels`, `next_action` and `summary` —
all authored by the agent. The signals the environment produced (the automated
checks on the head commit, merge readiness on the list row) live on the
pull-request side and are not fused into an accept-or-reject decision. A
self-report is weaker evidence than a check that ran, and the difference matters
exactly when the human has stopped reading the diff.

### Why a button and not a compose box

The obvious alternative is a "describe what you want" input. That is the wrong
shape here: an issue already carries the goal, usually with reproduction steps
and sometimes with acceptance criteria, and it carries a stable identity that the
resulting change can be linked to. A compose box would create a second, weaker
task object next to the one the forge already maintains.

## Goals

- One click on an issue starts an implementation attempt.
- The attempt is contained: its own worktree, its own branch, its own pull
  request. Nothing lands without a human.
- The issue's card carries the attempt's state and its evidence.
- Acceptance is decided on evidence the environment produced, with the parts the
  machine could not judge named explicitly rather than omitted.
- Reuse the organs that exist: the shared agent-session orchestration, the
  dashboard's worktree handler, and the worktree-gate-propose shape that
  `auto_improvement` already runs for self-improvement candidates.

## Non-goals

- Automatic merging, or arming provider auto-merge on green. The pull request is
  the containment boundary that makes this safe; removing it is a separate
  argument with a separate risk profile.
- Multiple competing attempts on one issue. Phase 4 sketches it and the record
  shape leaves room, but phase 1 dispatches one attempt.
- Issues that are questions, discussions, or product decisions. Dispatch is for
  items where "done" can be demonstrated.
- Replacing Investigate. Reading an issue before working it stays useful, and
  dispatch can consume an existing investigation record when one is present.

## Design

### 1. A third verb, built like the first two

A new `lib/implement.ts` plus `implement.prompt.ts` calling the existing
`useAgentSession`, and a button that shares presentation with the other two
through `AgentSessionButton`. The prompt lives in its own `*.prompt.ts` module
for the same reason the investigation prompt does: that suffix is a declared
model-facing boundary the i18n gate ignores, which keeps the surrounding module
fully covered.

### 2. A local checkout becomes a requirement (the one genuinely new constraint)

Issue Radar today needs no clone. Every read goes through the user's own `gh`
session, and that is a large part of why connecting a repo is one dialog. Writing
code does need a working copy, so a connected repo grows an optional local path.

This fails closed. With no local path recorded, the dispatch button is present
but disabled and says why, with the action needed. It must not silently fall back
to any directory the gateway happens to be running in.

The path goes through the same containment and sensitive-path checks Spec Builder
applies to its own worktree creation, and a path that is not a git repository is
refused at settings time rather than at dispatch time.

### 3. One worktree per attempt, never the user's main tree

A worktree is not a clone: `git worktree add` needs an existing repository to
attach to and shares its object store. So the local path from step 2 and the
per-attempt worktree are a base and a leaf, not alternatives — the path names the
repository worktrees hang off, and each attempt gets its own worktree branched
from it. The user's own working tree is never the thing being edited, which is
worth saying in the UI as well as here: "local checkout" reads like "the directory
the agent edits" and is the wrong mental model.

This also settles why the base is asked for rather than discovered. `worktree add`
MUTATES the repository it attaches to — a branch, plus an entry under
`.git/worktrees` — so choosing one on the user's behalf is not a discovery problem
but a permission one.

Attempt worktrees live under the app's own data dir, at
`<app_data>/worktrees/<owner>__<repo>/issue-<n>/`. Two placements are rejected on
purpose: inside the user's repository, which would drop untracked directories into
the tree they have open, and `/tmp`, where an attempt would not survive a reboot
that happens mid-run.

Creation goes through the dashboard's worktree handler
(`src/kiro_crew/dashboard/handlers/worktree.py`), which already carries the
hardening this needs, including not letting the repository's own hooks run during
`worktree add`. Spec Builder is the in-repo precedent for an app driving it
(`_create_worktree`, `_remove_worktree`, `_rollback_worktree_if_ours`), including
the rule that matters most: `worktree add` mutates the user's repository, so a
half-created attempt must roll back the worktree *and* the branch, and only when
it was ours to remove.

### 4. State the proof before doing the work

An issue body is prose. Before the agent starts, its first turn proposes the set
of things it will use to demonstrate completion: which existing tests must stay
green, which new tests it will write, and any real-environment check that
applies. The human confirms with one click or edits the set. It is stored on the
record.

This is the smallest useful form of an acceptance contract, and it is what makes
the acceptance card in step 6 possible: without a proof set agreed up front,
"verified" degrades into "the agent says it works".

### 5. The card's state machine

`none → dispatched → working → proposed → verified → accepted | rejected`

`proposed` carries the pull-request number; `verified` carries the evidence
summary; `rejected` carries the human's note and returns the item to `working`.
Because the store merges findings per key, this extends the existing record
without a migration, and an item that was only ever investigated keeps rendering
exactly as it does today.

### 6. The acceptance card

One surface that fuses three things the app already has separately:

- the automated checks on the head commit and merge readiness, which the
  pull-request routes already return;
- the proof set from step 4, each item marked satisfied or not;
- what the machine could not judge, listed explicitly as the human's remaining
  work.

The third list is the load-bearing one. A card that shows only green checks
invites a rubber stamp; a card that names the two things a person still has to
look at is the reason not reading the diff is defensible.

"Open the diff" stays available on this card. It stops being the default action.

### 7. The issue-to-pull-request join

The record carries the pull request the attempt produced, and the pull-request
row carries the issue it came from. Without this join, dispatch produces an
orphan change and the human is back to holding the connection in their head,
which is the problem this RFC opened with.

## Migration plan

Each phase is independently shippable and independently abandonable.

**Phase 0 — the local path, and a refusal that tells the truth.**
Add the optional local path to a connected repo, plus the disabled dispatch
button. *Exit criteria:* dispatching on a repo with no local path shows an
actionable refusal naming what to set; a repo with a valid path shows an enabled
button; a path that is not a git repository is refused when saved; no agent runs
in this phase.

**Phase 1 — dispatch into a worktree.**
Dispatch creates a worktree and branch, opens the seeded session, and moves the
record to `working`. *Exit criteria:* one issue produces a branch carrying at
least one commit and a card reading `working`; a forced failure during
`worktree add` leaves no stray worktree and no stray branch; a second click
resumes the same session rather than starting a second attempt.

**Phase 2 — the proof set.**
The first turn proposes the proof set; the human confirms or edits it; it is
stored. *Exit criteria:* the set is visible before work starts, an edit changes
what the agent later asserts, and the stored set is readable from the record.

**Phase 3 — pull request, join, and acceptance card.**
The attempt opens a pull request; the record and the pull-request row link both
ways; the acceptance card renders checks, proof set, and the un-judgeable list.
*Exit criteria:* from the issue card a human can accept or reject without opening
the diff; rejecting returns the item to `working` with the note attached; the
pull request is reachable from the issue and the issue from the pull request.

**Phase 4 — competing attempts (optional, gated on phases 1-3 being used).**
Dispatch N attempts, render their evidence side by side, pick one. *Exit
criteria:* two attempts on one issue show their evidence separately, and picking
one removes the others' worktrees and closes their pull requests.

## Backward compatibility

The record extends per key, so existing investigation and review records keep
rendering. Both existing verbs are untouched. The app remains
`defaultEnabled: false`. A repo with no local path keeps working exactly as it
does today, minus the new button.

## Security considerations

The new capability is "an agent writes code in a worktree and opens a pull
request". Its blast radius is bounded by the pull request: the change is
reviewable, revertible, and cannot land without a human. That containment is
load-bearing, which is why automatic merging is a non-goal rather than a later
convenience.

Three constraints follow:

- **Do not widen the internal-secret path list.** Only the full
  `/api/apps/issue-radar/investigation` path is in `_MIXED_INTERNAL_API_PATHS`,
  deliberately not the `/api/apps/issue-radar` prefix, which would also admit the
  forge-write routes. Dispatch must not be reachable with the internal secret.
- **The local path is an input, so validate it like one.** Containment and
  sensitive-path checks at settings time, and a refusal rather than a fallback
  when it is missing or unusable.
- **Roll back only what we created.** A half-created attempt removes its own
  worktree and branch and nothing else, following Spec Builder's
  rollback-if-ours rule.

## Alternatives considered

**A compose box for arbitrary work.** Rejected: the issue is already the
specification, and a second task object would compete with the forge's.

**A separate app.** Rejected: it would duplicate the board, the record, and the
provider clients, and it would recreate the issue-to-pull-request split inside a
new boundary.

**Arm auto-merge when the checks are green.** Rejected for now: it removes the
one boundary that keeps this proposal's risk small, and it should be argued
separately once the acceptance card has been used enough to know how often its
un-judgeable list is non-empty.

**Let the agent work in the user's checkout.** Rejected: concurrent attempts and
a human editing at the same time both corrupt that assumption, and the worktree
handler already exists.

## Open questions

1. **What if the machine holds no copy of the repository at all?** Phase 0 refuses,
   which is honest but leaves a connected repo that can never be worked on. The
   obvious answer is to offer to clone it into the app's data dir, and the reason
   that is not phase 1 is that it changes the shape of the feature rather than
   extending it: a clone needs push credentials of its own, costs minutes on first
   dispatch, and duplicates a repository the user probably already has. Worth
   deciding once the gate has been used enough to know how often the refusal
   actually fires.
2. What makes an issue eligible for dispatch? A label, a heuristic on the body,
   or always-available with a refusal when the agent judges the item unactionable
   in its first turn.
3. Should the agreed proof set be posted back to the issue as a comment, so the
   reporter can see what "done" was taken to mean?
4. In a monorepo with several connected repos pointing at one checkout, is a
   worktree per attempt still the right unit, or per repo per attempt?
5. When an investigation record already exists, should dispatch consume it as
   context automatically, or should the human choose?
