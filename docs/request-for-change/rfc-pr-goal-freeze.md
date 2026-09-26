---
title: Frozen PR goal — a fixed goal every review round is judged against
status: in-progress
author: zejiangg
created: 2026-09-26
last-audited: 2026-09-26
audited-at: 7b2da39c53
doc-pr: 14265
implementation-prs: [14181, 14161]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Frozen PR Goal

> **Status:** `in-progress`. Nothing is on main yet. Verified at `7b2da39c53`:
> `.github/PULL_REQUEST_TEMPLATE.md` has no `**Goal:**` line or `## Not a goal`
> section, `fork-pr-description.yml` still reads "Fork-only by design", and
> prepare-pr's `SKILL.md` still posts the `<!-- prepare-pr-intent -->` comment.
> The design is implemented by
> [#14181](https://github.com/kirodotdev/KiroCrew/pull/14181) (frozen sections,
> gate, prepare-pr),
> [#14161](https://github.com/kirodotdev/KiroCrew/pull/14161) (First Principles
> lens) and a follow-up Intent Lock PR not yet opened. This document records
> the maintainer's decisions so the First Principles lane can trace them.

## Summary

Every PR body opens with a short goal that is written once and then left
alone. Review rounds rewrite only the description of the change. Reviewers,
the prepare-pr retrospective and the First Principles lane judge the diff
against that fixed goal, so a PR cannot quietly grow its own scope.

## Motivation

The prepare-pr loop rewrites the whole PR body every round to match the diff.
So the body's "why" drifts to describe whatever the loop just added. Reviewers
and the every-third-round retrospective read that text as the intent, and the
loop's own additions become part of the goal. PRs over-grow with nothing fixed
to measure growth against.

The separate `<!-- prepare-pr-intent -->` comment was meant to be that fixed
point, but it is rarely written. Of 25 recent PRs, 7 had the intent comment,
and 1 had the `mechanism:` / `self-added:` disposition lines the rounds view
reads. The largest grew by +747 lines over 7 dispositions.

## Goals

- A PR's goal is fixed when it opens and stays fixed.
- Every review surface judges the diff against that goal.
- The change fits the original goal as tightly as possible and does not expand
  (minimality).

## Non-goals

- Limiting PR size by a number.
- A new review lane.
- Back-filling old PRs by hand; they update on their own next event (Rollout).

## Design

### 1. Frozen sections

Every PR body opens with, in order:

- `## Problem / Motivation`, whose first line is a one-sentence
  `**Goal:** …`;
- `## Why it matters`;
- `## Not a goal`.

They are written once, when the PR opens. Agents never edit them on their own,
only when a human explicitly asks. Everything from `## What changed` down is
still rewritten each round as a snapshot of the whole diff. New evidence goes
in PR comments, not in the frozen sections.

### 2. Description gate: same-repo and fork PRs

The PR description gate checks same-repo AND fork PRs, with one shared rule
set. This deliberately reverses the "Fork-only by design" rule in
`fork-pr-description.yml`: the drift comes from maintainer and agent PRs, which
are same-repo. Bot-authored PRs
(`dependabot[bot]`, and this repo's own automation PRs such as
`add-contributor.yml` and `test-durations.yml`) are exempt: they are not
where the drift comes from, and their generated bodies carry no sections. A
body that fails is blocked, and the message points to
`.github/PULL_REQUEST_TEMPLATE.md`. A body edit clears it; no push is needed.

### 3. prepare-pr

- The `<!-- prepare-pr-intent -->` comment is removed. The frozen sections are
  the intent.
- The retrospective still runs every 3 rounds. It judges the FULL diff against
  the frozen goal. For each mechanism it asks: is it beyond the goal, is that
  justified, and should the PR revert to an earlier head and redo the work by
  a smaller path.
- "Revert to `<head>` and redo" joins remove / smaller replacement / keep as a
  verdict.
- A finding outside the goal is rebutted or deferred. It is never absorbed by
  widening the goal.

### 4. Intent Lock (follow-up PR)

A CI check on same-repo and fork PRs. When a PR opens, the bot posts a baseline
hash of the frozen sections. Any later change to them turns the check red until
a maintainer with write access comments `/intent approve <head-sha>`.

- It reads only the PR body.
- PRs opened before it lands are skipped.
- PR Readiness counts it as awaiting maintainer approval.
- prepare-pr treats it as a human wait. Agents never post `/intent approve`.

### 5. First Principles lens

The lane judges the Goal from the frozen sections, not from `## What changed`.
A mechanism outside the goal that is not justified is a Subtraction item. A PR
with no `**Goal:**` line (older PRs) is judged against its whole description.

## Rollout

Open PRs go red on the description gate at their next event. The fix is adding
the frozen sections, which is a body edit with no push. The maintainer-side auto-approval cron
keeps its own copy of the required sections outside this repository; it must
be updated when #14181 lands, or fork PRs with older bodies lose workflow
approval. Order: this RFC, then
#14181 and #14161, then the Intent Lock PR.

## Backward compatibility

Same-repo PRs gain a required-section check they did not have. No code or
config key changes meaning.

## Security considerations

Intent Lock approval is limited to maintainers with write access, and agents
never post it, so an agent cannot unlock its own goal. The gate and the lock
read only the PR body.

## Alternatives considered

- **Size-based retrospective triggers.** Size says nothing about scope; a small
  PR can drift and a large one can be on goal.
- **Compare against round 0 or a stored anchor commit.** Round 0 is often wrong
  too; the goal is the stable thing, not a tree.
- **A new dedicated over-engineering CI lane.** Duplicates First Principles;
  the lens change covers it.
- **Freeze only a new `## Intent` section.** It overlaps `## Problem /
  Motivation`; two places to state the goal drift apart.

## Open questions

None.
