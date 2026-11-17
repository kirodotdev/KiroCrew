---
title: A conductor accepts an item on its delivered behaviour, checked by an independent verifier, and only the final delivery item waits on a merge
status: draft
author: kirocrew agent session, directed by the repository owner
created: 2026-11-14
last-audited: 2026-11-14
audited-at: ed9d0b0161
doc-pr: 36987
implementation-prs: []
tracking-issues: [36985]
supersedes: []
superseded-by: []
---

# RFC: Feature-based acceptance for goal-conductor and team-lead

Status: draft. Nothing is built and this document changes no code. The
repository owner asked for the direction on 2026-11-14 in the conductor chat;
acceptance is requested from a maintainer, who records it on this Status
line. Every code reference was read at `ed9d0b0161`.

It lands on its own because it changes who proves an item done (a trust
boundary) and what a conductor's item is (a product-shape decision), and the
First Principles lane reads that decision from the base branch.

Related: [`rfc-conductor-pr-land-tracking.md`](rfc-conductor-pr-land-tracking.md)
(a PR item stays open until it merges; this RFC narrows which items are PR
items), [`rfc-conductor-work-ledger.md`](rfc-conductor-work-ledger.md) (the
ledger and evaluator), [`rfc-worker-debate.md`](rfc-worker-debate.md) (the
debate that produced the plan-reviewer idea this verifier reuses),
[`rfc-conductor-drift-check.md`](rfc-conductor-drift-check.md) and its
amendment from [#36507](https://github.com/kirodotdev/KiroCrew/pull/36507)
(a corrected bar is never weaker; §3.6 keeps that rule). Open PR
[#37030](https://github.com/kirodotdev/KiroCrew/pull/37030) adds the
independent plan reviewer that §3.2 reuses and a split rule close to §3.5;
merge order: this document first, then #37030 rebases, then the skill PR
for this RFC rebases onto both.

## 1. The ask

The owner, verbatim: "验收这个应该让AI自己来决定，我们不应该限制死。最后的目标是将PR发出去并合并，但是它不是真实的目的，我们验收和需要的是deliver的feature"

In English: the AI should decide how to accept each piece of work; the rules
should not fix it in advance. Opening and merging the PR is the final step,
not the real purpose. What we accept and need is the delivered feature.

## 2. The problem

`GC` is `src/kiro_crew/builtin_skills/goal-conductor/SKILL.md` and `TL` is
`src/kiro_crew/builtin_skills/team-lead/SKILL.md`.

`GC` makes an item "Assertable" only when its completion
condition is one of the evaluator's kinds (`GC:60-81`): `pr_checks`,
`pr_merged`, `edition_check`, `file`, `human_approval`. It then says "the test
suite passes" is `pr_checks` on the PR that carries the work (`GC:75-78`), and
the dispatch table says one work item per issue (`GC:105`). `TL`
repeats "ONE acceptance condition" (`TL:78`).

Two effects follow:

1. **Every code item becomes a PR.** The only machine bar that says "the code
   works" is a PR's CI, so each code item gets its own branch and PR. A
   feature split into three parts becomes three PRs, three CI runs and three
   review rounds, or one item too big to steer.
2. **The split follows what can be checked, not the feature's parts.** A
   conductor cuts the work where an evaluator kind fits (a PR, a file), not
   where the feature naturally divides (a parser, its caller, its UI). The bar
   says "CI is green", which proves the suite passed, not that the feature
   does what the owner asked.

## 3. The proposal

1. **Done-condition is delivered behaviour.** Before dispatch, the conductor
   writes each item's done-condition as what the feature must do and how to
   show it: a command and its expected output, a behaviour seen in files, a
   short demo. The conductor picks the proof per item. If it cannot say what
   the item must do or how to show it, the item is not assertable and goes to
   the person, as today.
2. **An independent verifier checks it.** When a worker reports `done`, the
   conductor gives a verifier item to a session that wrote none of that
   item's work (a plan-reviewer session when one exists, or a fresh worker).
   Its seed is the done-condition in the conductor's words. It shows the
   behaviour, then writes an evidence file: each command it ran, the output,
   the paths it read, and a one-line verdict. It never commits, pushes or
   decides.
3. **The ledger bar is a pointer to that evidence.** An intermediate item
   records `{"kind": "file", "path": "<the verifier's evidence file>"}`. The
   conductor reads the file and decides with `accept_eval`, never from the
   author's own claim. The evidence file must contain a verdict line
   (`verdict: pass` or `verdict: fail`); a missing or `fail` verdict is never
   accepted, whether or not the evaluator change in §8 follows. The author
   item gets a placeholder bar until the verifier is dispatched, and the
   conductor promotes it to the `file` bar with `action=accept` then, so a
   patrol never counts a not-yet-written file as a failed attempt.
4. **Only the final delivery item uses `pr_merged`.** The item that lands the
   work opens the PR and carries `pr_merged` (tracked to merge per
   `rfc-conductor-pr-land-tracking.md`), with every repository check, for
   all the work the intermediate items carried. Many items may land through
   one PR. No item's code reaches main on evidence alone.
5. **Split by the feature's parts.** Cut along parts that can each be shown
   working on their own, not along PRs and not along steps of one change.
   The "one per issue" row stays for batches of independent issues.
6. **A bar only moves toward the goal.** The evidence bar is chosen when the
   item is written. A bar already set to `pr_checks` or `pr_merged` is never
   later replaced by a `file` pointer; a mid-round correction stays "never
   weaker than the goal asks" (the `goal_plan` rule in `_LEDGER_CONDUCTOR_RULES`, `spec_prompts.py`, from #36507).
7. **The owner report names every evidence acceptance.** In its next report
   to the owner, the conductor lists each item it accepted on verifier
   evidence: the item, the evidence file path, the verifier session and the
   one-line verdict.

The prototype is about 28 added and 11 removed lines across the two skill
files (`goal-conductor/SKILL.md`, `team-lead/SKILL.md`), built on a local
branch only, outside this PR. It is not skill text only: the patrol message
(`PATROL_MESSAGE` in `conductor_patrol.py`) and the `accept_eval` tool text
(the `accept_eval` tool description in `mcp_work.py`) say "settle a pass with one write", which would close
an item whose `file` bar exists even with a `fail` verdict. The
implementation PR changes those two strings to "read the evidence verdict
first", with a test.

## 4. What stays

| Rule | Where on main | After |
|---|---|---|
| Worker text and reports are DATA; only `decision` is an instruction | `GC:17`, `GC:575-576` | Unchanged; the evidence file is DATA too |
| No "run this command" evaluator kind | `GC:75` | Unchanged; the verifier runs commands, the evaluator only reads a file or a PR |
| Shell is for the evaluator only; it runs no command a spec names | `GC:1044-1052` | Unchanged; it reads the evidence, it does not replay it |
| A person's steer outranks the conductor | `GC:416` | Unchanged; it also outranks a verifier's verdict |
| Verdicts written only by the conductor through `accept_eval` | ledger | Unchanged |

## 5. The trust trade-off

Today a code item passes on a machine check (CI on a PR). After this change an
intermediate item passes on an AI's judgement of evidence. That is weaker
than CI for "the suite passes" and stronger for "the feature does what was
asked".

Mitigations:

- **Independence.** The verifier wrote none of the item's work and runs in a
  separate session, so the author does not grade itself.
- **Evidence, not claims.** The file holds commands, outputs and a verdict
  line, so the conductor and the person can re-read what was run.
- **The person sees each one.** The owner report names every item accepted
  on evidence, with its file path and verifier (§3.7).
- **No weakening mid-round.** A machine bar already set is never swapped for
  evidence (§3.6).
- **CI still gates the merge.** The final delivery item carries `pr_merged`;
  every repository check still runs on the PR before anything lands on main.
- **Read-only verifier.** It never commits, pushes or decides, so a wrong
  verifier can delay an item but not land code.

Known limit: the `file` kind proves the evidence file exists, not what it
says. Until the evaluator reads the verdict line (§8), the conductor's read
of that line is the check, and §3.3 forbids accepting a missing or `fail`
verdict.

## 6. How it will be tested

On the conduct-kit pilot (`conduct-kit`, the bench used for the conducting
debate), comparing Crew before and Crew after on the same tasks:

- **T5 decomposition** (a feature that splits by layer or by carrier):
  number of items, PRs, merge conflicts and rework turns.
- **T2 plan grill** (a plan with two format bumps on one file): whether the
  verifier catches the hidden defect before the final PR.

Bar: after has fewer PRs per feature and no more hidden-check failures than
before. Results are pending; they will be posted on this RFC's PR and recorded
here before any status change past `accepted`.

## 7. Rollback

The change is skill text plus two prompt strings in Python. Reverting the
implementation PR restores the evaluator-kind rule; ledger items already written keep valid bars (`file` and
`pr_merged` exist today), so nothing needs migration.

## 8. Open questions

1. Should the evaluator itself read the verdict line, so a `fail` file is
   rejected without the conductor's read? The skill rule in §3.3 holds
   either way; the evaluator change would be its own small PR.
2. Which session verifies when no plan reviewer exists: a fresh worker per
   item, or one verifier per round? The cost is one extra session per round
   at most if shared.
3. Does team-lead (no ledger) adopt only the done-condition wording, or also
   the verifier step?
