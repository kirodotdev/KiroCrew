---
title: Conductors and Bots keep one line of work on one worker by default and re-split each round
status: accepted
author: kirocrew agent session, directed by the repository owner
created: 2026-11-22
last-audited: 2026-11-22
audited-at: 4ed7bae83
doc-pr: null
implementation-prs: [40682]
tracking-issues: []
supersedes: []
superseded-by: [rfc-bot-workers-conduct.md]
---

# RFC: No split by default for conductors and Bots

Status: accepted. The repository owner made this decision on 2026-11-22 in the conductor chat that dispatched the guidance rewrite; the ruling is summarised in section 4. Nothing is built yet: the implementation is [#40682](https://github.com/kirodotdev/KiroCrew/pull/40682), which changes prompt and skill text only.

It lands on its own because it changes how every conductor and Bot decomposes a goal by default, and the First Principles lane reads that decision from the base branch.

Related: [`rfc-conductor-plan-review-hourly-check.md`](rfc-conductor-plan-review-hourly-check.md) (plan reviewer and item budget, unchanged), [`rfc-conductor-feature-acceptance.md`](rfc-conductor-feature-acceptance.md) (acceptance model, unchanged).

## 1. Problem

The conductor's split guidance read as "one PR means one worker" and gave no route for parallel parts that land in one output. It also never said what an extra worker costs, so nothing argued against splitting small work. Each worker costs its own setup: for code, a repository copy, a build, a hand-off and a CI and review round.

## 2. Decision

1. **North star.** The goal's Outcome and Done when guide every round. The plan can start incomplete.
2. **Loop.** Each round: decompose, combine the results, verify acceptance, re-split what is left. Adjust as you learn.
3. **Default: do not split.** One line of work goes to one worker, which orders its own steps.
4. **When to split.** Only a large item whose parts can run in parallel, no two writing the same thing. When unsure, do not split.
5. **Nested split.** A large item that splits into parallel parts goes to a child conductor, within the depth cap, so the split is decided near the work. A plain worker cannot dispatch.
6. **Integration.** When parallel parts make one output, one integrator owns the shared artifacts, checks each part's hand-over and lands the output.
7. **Unchanged.** One owner per shared artifact, one integrator per output, and review or testing by a different worker than the author.
8. **Generic rules.** The shared rules use no coding words. Code specifics (separate files, branches, version bump, one PR) live in their own clearly marked part of the goal-conductor skill.
9. **Bots.** A Bot follows the same default. Its split choices, worker counts and costs stay out of what it tells its user unless the user asks. Nothing else changes in how a Bot talks.

## 3. Non-goals

- No new acceptance kind and no code change.
- No change to the pipeline or security conductors.

## 4. Owner ruling

On 2026-11-22 the owner chose "default no split, few rules, dynamic re-split under a north star" over a fan-out-first rewrite, then added that the rules stay generic with coding as a separate part, that Bots get the same default without exposing split internals, and that this decision is recorded as an RFC rather than a review override.
