---
title: User-driven regression gate -- one feature catalog, agentic bug bashes at every level of the delivery loop, findings that can block a release
status: draft
author: unattributed
created: 2026-10-29
last-audited: 2026-10-29
audited-at: d32862446c
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: User-driven regression gate

- Status: draft. Acceptance requested from a maintainer; the status flips to
  `accepted` when one records the decisions in section 6.
- This document changes no code. The pull request that lands it adds this file
  and one row in [README.md](README.md).
- Measured at `origin/main` commit `d32862446c` (2026-10-29). Every repository
  claim names a file or a symbol.

## 1. Summary

Today a Kiro Crew change is judged mostly by what the code does: unit tests,
lint, and AI review lanes that read the diff. Nothing on the path to a release
asks the question a user asks: "does the thing I used yesterday still work, and
does the new thing work the way the release says it does?"

The pieces to ask that question already exist. The
[feature map](../feature-map/README.md) indexes every dashboard feature, the
[GUI user test](../build/gui-user-test.md) drives a real browser as a
first-time user, and its friction channel records what confused that user.
They are not wired together, they run only at night, and nothing they find can
stop a release.

This RFC proposes:

1. **One feature catalog** that names every user-facing feature, its user
   story, its *current* behaviour and, while a change is in flight, its
   *expected* behaviour (section 3.1).
2. **An update contract**: a pull request that changes what a user sees
   updates the catalog in the same PR, and a release diffs the catalog against
   the previous release (section 3.2).
3. **Agentic bug bashes at five levels** of the delivery loop, from the
   author's pod to the release candidate, each with a stated scope and
   blocking power (section 3.3).
4. **A finding contract**: every finding becomes a triaged issue with a class
   and the level that should have caught it; two classes block a release
   (section 3.4).
5. **A retroactive UX audit** that runs the same system over features already
   shipped, to find what is unintuitive rather than broken (section 3.5).


## 2. What exists today

| Piece | Where | What it does | Gap |
|---|---|---|---|
| Feature map | `docs/feature-map/README.md` | One table per area: feature, how a user reaches it, page and handler. | A navigation index by its own contract: it says nothing about behaviour. Updated only when a feature is added or removed (`scripts/check_feature_map.py`). |
| Feature inventory | `test/gui_user/features.json`, rendered to `FEATURES.md` | 272 features, each with a user story, entry path, seed, priority (P0 10, P1 37, P2 162, P3 63) and runnable tier. | A one-time snapshot generated at `2f71d47e6c`. Nothing keeps it current. No expected outcome. |
| Scenarios | `test/gui_user/scenarios/*.yaml` | 44 scripted user tasks (33 smoke, 11 nightly), each tied to a `feature` and a `user_story`. | Cover a small share of the 272 features. |
| GUI user test lane | `.github/workflows/gui-user-test.yml` | A model sees only screenshots and drives mouse and keyboard against a real gateway on a fake ACP backend. Nightly plus `workflow_dispatch`. A red night opens or updates one issue. | No `pull_request` trigger, deliberately (credential boundary). `pr-readiness.yml` does not read it. The release runbook does not mention it. |
| Friction channel | `test/gui_user/friction.py` | The new-user persona files `report_friction` entries (blocker / slows-down / cosmetic), deduplicated across nights by `(feature, element)`. | Advisory only. Nothing routes friction into release decisions. |
| Release runbook | `docs/build/release.md` "Runbook: promoting an RC to stable" | The stable tag must name a commit that already shipped on insider. | The insider soak is the only user-level check. |
| Review loop RFC | [rfc-review-loop-and-escaped-bug-reduction.md](rfc-review-loop-and-escaped-bug-reduction.md) | Accepted: PASS/BLOCK review verdicts and escaped-bug measures. | Reads the diff. This RFC is its user-facing counterpart. |

## 3. Proposal

### 3.1 One feature catalog, two views

Keep the two files that exist, and give each one job.

| File | Job |
|---|---|
| `docs/feature-map/README.md` | **Where** a feature lives: user path, page, handler. Unchanged contract: updated on add or remove. |
| `test/gui_user/features.json` | **What** a user can do and **what should happen**. Read by the bug-bash agents. Becomes a maintained source, not a snapshot. |

Every `features.json` row keys to a feature-map row by the same id, and a
check fails when either side has an id the other lacks.

Each row gains two outcome fields:

- `current`: what a user sees today on `main`, in one to three sentences a
  tester can verify from the screen. This is the regression baseline.
- `expected`: present only while a change is in flight (an open PR, or merged
  but not yet released): what a user will see once it ships, plus the PR
  number. On release, `expected` replaces `current` and is cleared.

Scenario YAML stays the "how to check it" layer: a scenario names the feature
id, and its success criteria are written against `current` or `expected`.

Behaviour stays out of the feature map on purpose: the map's own contract says
a map edited on every behaviour change stops being read.

### 3.2 The update contract

**Per pull request.** A PR that changes something a user can see (a
`website/src/` change, a dashboard handler, a user-facing string, a default)
does one of two things in the same PR:

- updates `expected` for every feature it changes, and adds or edits a
  scenario when a P0 or P1 feature has none; or
- states `User-visible change: none` in the PR body, with one line saying why.

An extension of `scripts/check_feature_map.py` fails a PR that touches those
paths and does neither. The judgment half, "is the `expected` text true to the
diff", joins the existing blocking `feature-map-correctness` rule in
`AUTOSDE.yaml`.

**Per release.** The release PR:

1. promotes `expected` to `current` for every row whose PR is in the release;
2. diffs the catalog against the previous release tag; the changed rows are the
   release's **user-facing scope**; and
3. uses that scope as the list the release-candidate bug bash must cover, and
   as the source of the user-facing release notes, so the notes and the tests
   describe the same thing.

### 3.3 Bug bashes at five levels

| Level | When | Scope | Blocks? |
|---|---|---|---|
| L0 Author | Before push, on a pod (`pod_up`) | Touched features: their `expected` outcomes plus `current` outcomes of neighbouring features in the same area | No. Evidence goes in the PR's Tests section. |
| L1 Pull request | Every PR that changes a user-visible path | Smoke tier for the touched features, `expected` and `current` | Advisory first; blocking for P0 regressions once the false-red rate meets D4's bar. |
| L2 Nightly | Every night on `main` | All features with a scenario | A confirmed P0/P1 regression stops the next RC cut. |
| L3 Release candidate | On each RC, before promotion | Every feature in the release's user-facing scope plus all P0/P1 features | Yes, per 3.4. Promotion needs a green L3 run on the exact commit being tagged. |
| L4 Retroactive | Weekly, and after each stable release | All runnable features, friction only | No. Produces the UX backlog (3.5). |

L1 runs in the `workflow_run` shape that the GUI user test doc already
prescribes (phase 2 of
[#9578](https://github.com/kirodotdev/KiroCrew/issues/9578)): harness from the
trusted base, PR-built gateway under a separate unprivileged user, so PR code
never runs beside a cloud credential.

L3 uses two personas. The **new user** (exists, `scenarios.PERSONAS["new-user"]`)
checks the expected experience is findable. A **returning user**, seeded with a
home from the previous release, checks the existing experience survived the
upgrade: settings kept, sessions readable, controls where they were.

### 3.4 What happens to a finding

Every finding becomes a GitHub issue, deduplicated by `(feature, element)` as
friction already is, triaged by the captain within 24 hours. It carries a class
and an **escape level**: the lowest level (L0 to L3) that should have caught it.

| Class | Meaning | Release effect |
|---|---|---|
| `regression` | A `current` outcome no longer holds. | Blocks the RC. Fix, revert, or an owner-signed waiver. |
| `expected-miss` | An `expected` outcome does not hold. | Blocks the RC if the feature is in the release notes; otherwise ship dark or drop the notes line. |
| `friction-blocker` | The tester could not continue without guessing, on a P0/P1 feature. | Owner decision before promotion, recorded in `docs/decisions/`. |
| `friction` | Slows down or cosmetic. | Backlog, ranked by 3.5. |
| `harness` | The tester, seed or fixture was wrong. | Never blocks. Counted as noise. |

Rules:

- A finding blocks only when it reproduces on the harness's second attempt.
- A finding closes when a later run stops seeing it, not when the fix merges.
- A waiver names the issue, the owner, the reason and the release it expires
  at, and is posted on the release PR. The same finding cannot be waived twice.

### 3.5 Retroactive UX audit

L4 runs the new-user persona over every runnable feature and only collects
friction. Each feature gets a score:

```
ux_debt = sum over open friction rows of severity_weight x nights_seen
severity_weight: blocker 5, slows-down 2, cosmetic 1
```

The top of that list, weighted by priority, is the UX backlog. A fix PR for a
friction row updates `current`, and the row closes when the next L4 run stops
seeing it. This judges already-shipped, already-"working" features by whether a
person can use them, not by whether their tests pass.

## 4. From tech-driven to user-driven

| Step | Today | Proposed |
|---|---|---|
| Start of work | An issue describes a bug or a code change | The issue names the feature id and the `expected` outcome |
| Review | Diff against PR body | Plus built product against `expected` (L0, L1) |
| Release scope | Commits since the last tag | Catalog rows that changed since the last tag |
| Release notes | Written from PR titles | Written from changed rows' `expected` text |
| Release gate | CI green, insider soak | Plus a green L3 bug bash |
| Measure of quality | Tests pass | Users can do what the catalog says, without friction |

Code-level gates stay; they answer "is the code right". The new gates answer
"is the product right for the person".

## 5. Measures

Reuse the escaped-bug measures of
[rfc-review-loop-and-escaped-bug-reduction.md](rfc-review-loop-and-escaped-bug-reduction.md)
D8 and add:

- user-visible regressions reported after each stable release;
- escapes per level: findings whose escape level is below the level that
  actually caught them, which shows the weakest gate;
- coverage: share of P0/P1 features with `current` and a scenario;
- false-red rate per level (findings later classed `harness`);
- open `friction-blocker` rows on P0/P1 features, and total `ux_debt`;
- cost and wall time of an L3 run (the nightly default budget is $60 per run;
  measure the first RC run before fixing a budget).

## 6. Decisions requested

| # | Decision | Recommendation |
|---|---|---|
| D1 | Behaviour lives in `features.json` (`current` / `expected`), not in the feature map. | Yes. |
| D2 | Every user-visible PR updates `expected` or declares none, enforced by a check. | Yes. |
| D3 | The release PR promotes `expected` to `current`; the catalog diff is the release's scope and notes source. | Yes. |
| D4 | L1 ships advisory and blocks P0 regressions after 30 days with a false-red rate under 5%. | Yes. |
| D5 | L3 is a required runbook step; `regression` and in-notes `expected-miss` block promotion unless waived. | Yes. |
| D6 | A confirmed P0/P1 regression on nightly stops the next RC cut. | Yes. |
| D7 | Add a returning-user persona with an upgrade seed. | Yes. |
| D8 | Every finding records an escape level. | Yes. |
| D9 | L4 runs weekly; its ranking is the UX backlog. | Yes. |

## 7. Risks

- **Flaky testers block releases.** Second-attempt rule, `harness` class, L1
  advisory first.
- **Catalog rot.** D2's check and the id cross-check in 3.1.
- **Credential boundary.** L1 only in the `workflow_run` shape.
- **Cost.** Bounded per level by scope; L3 is the only full run per release.
- **Tester model drift.** Pin the tester model per release and record it in
  the L3 summary.

## 8. Not a goal

- Replacing unit, integration or E2E tests.
- Native-only and needs-secret features at L1 to L3 until the harness can
  reach them.
- Making ordinary friction a blocking signal.

## 9. Phases

1. Add `current` / `expected` to `features.json`, write `current` for the 47
   P0/P1 features, add the id cross-check.
2. D2 check and the PR body line; L0 recipe in the worktree verification guide.
3. L3 in the release runbook via `workflow_dispatch` on the RC ref; the
   nightly stop-the-line rule; returning-user persona.
4. L1 in the `workflow_run` shape, advisory.
5. L4 weekly run, UX debt ranking and escape-level reporting.
6. Flip L1 to blocking for P0 regressions when D4's bar is met.
