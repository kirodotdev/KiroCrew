---
title: Fewer review loops and fewer escaped bugs -- a PASS/BLOCK review gate, lint first, and six smaller changes
status: accepted
author: unattributed (blameless study write-up)
created: 2026-10-19
last-audited: 2026-10-19
audited-at: 2cf3d0d69
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Fewer review loops and fewer escaped bugs

- Status: accepted. The repository owner recorded all eight decisions on
  2026-10-20 in section 2.2: D1 with an amendment, D2 to D8 as written. Each of
  the eight decisions in section 2 is separable. Nothing is built yet.
- This document changes no code. The merged pull request adds this file and one
  row in [README.md](README.md).
- Measured at `origin/main` commit `2cf3d0d69` (2026-10-19). Every repository
  claim names a file and a line (or a symbol). Line numbers are written beside
  the path, not as a backticked `path:NNN` token, because
  `scripts/docs_lint.py` reports that shape as a rotting citation. Lines move;
  the symbol and the commit above are the stable reference.
- The evidence comes from a study of 1,980 merged pull requests (merged
  2026-10-07 to 2026-10-18) and six design documents that grew out of it. The
  study files are working files, not committed here. Where a number comes from
  them, the text names its source: `aggregate.json` plus a key, `SYNTHESIS`
  plus a section, or `design A` to `design F` plus a section. Section 3.7 lists
  what the study cannot show.
- The text is blameless on purpose. It names no person. A pull request number
  appears only as an example of a pattern.

## 1. Summary

Review loops (a push made because feedback arrived) cost the project a mean of
3.31 loops per merged pull request, and 150 pull requests (7.6%) hold 46.9% of
all loops. Separately, 24.3% of bug-fix pull requests repair a risk that a
review had already raised and the author had set aside. In 71.3% of those cases
the review lane had labelled the risk CONCERNS, FINDING or advice, a label that
never gates. Only 3.2% were a BLOCK when raised.

This RFC asks the team to decide eight things. The headline one (D1) is whether
the CI review verdict should be only PASS or BLOCK, each with a short reason,
instead of today's PASS, CONCERNS, BLOCK and advisory FINDING. The other seven
are smaller: run lint before the heavy CI jobs (D2), a PR-body sibling list on
fix pull requests (D3), a proof line inside the existing Tests section (D4), a
class sweep on every fix round with an automatic strategy switch (D5), a size
signal with an owner override (D6), an optional and advisory pre-push tool (D7),
and a set of measures that tell us whether any of it worked (D8).

The RFC does not propose mandatory local checks, a human stop at the third
patch, or a central deferral registry. Section 6 says why.

## 2. Decisions requested

### 2.1 The decision table

Lead with this table. Section 4 holds the detail for each row.

| # | Decision | What changes | Main evidence | Main cost | Rollout |
|---|---|---|---|---|---|
| D1 | **Review gate is PASS or BLOCK** (headline) | Every lane prints PASS or BLOCK plus a 1 to 3 sentence reason. A BLOCK must state a trigger and an observable wrong outcome. Style notes leave the gate. A BLOCK leaves only by a fix or an override record with a class. Security, data-loss, crash and guard-removal blocks need a writer other than the PR author. | 282 risks set aside and later shipped as bugs: 201 (71.3%) were advisory, 9 (3.2%) were BLOCK. 97% of deferrals and 98.2% of overrides were written by the PR author. | More BLOCKs, so possibly more rounds. The false-block rate is unknown. | Replay on history, then shadow, then enforce lane by lane. Superseded by the D1 amendment in section 2.2: direct enforcement on all lanes, safety is a revert by pull request. |
| D2 | **Lint first** | Heavy CI jobs wait for the two lint jobs, through one small aggregate job | 25,571 heavy job-minutes sat in 88 pushes that lint would have stopped (10.5-day window). Lint median 8.1 min | Every green push waits about 8.1 min more before heavy jobs start | Repository variable, off then on |
| D3 | **Sibling sweep in the PR body** | New `## Sibling sweep` section on `fix`/`revert` pull requests, checked for presence and shape (exit 23) | 786 `partial_fix` loops (12.0%); 234 `partial_fix` bugs (20.1% of bug-fix PRs) | One more section on a fix body | Warn 14 days, then red |
| D4 | **Proof inside `## Tests`** | One `Proof:` line under the existing heading on `fix` pull requests (exit 24). No new heading | 36.0% of bug-fix bodies claim a test fails without the fix; nothing checks it | One `prove.py` run per fix | Warn 14 days, then red |
| D5 | **Class sweep each fix round, automatic strategy switch** | `sweep_check.py` per fix round; on the 3rd fix of one span the mate must change approach; before a 5th it must narrow or defer with an issue. No human stop. Exit 30 reshaped | 142 repeat-only fix rounds in 519 PRs (12.2% of fix rounds); 72 are third-or-later | A sweep file and one `git grep` pass per push | Warn, then refuse |
| D6 | **Size signal with owner override** | Flag at 80 or more files, or 168 h open. Red `PR Size` check at 80 or more files until an owner comments `/pr-size approve`. Plus `regen_shared.py` | 3.3% of PRs hold 26.9% of rebase loops | A red check on 0.9% of pull requests | Shadow, then red |
| D7 | **Pre-push tool, optional and advisory** | `pre_push_gate.py` and a `Gate proof:` line. No `require` mode anywhere | 192 avoidable pushes (2.4% of pushes), 86% of them on cheap checks | None forced | Ships as a tool |
| D8 | **Measure it** | Metrics M1 to M5 in `fix_loop_metrics.py` | Baselines in section 4.8 | About 10,000 REST calls a week, unless the organisation already has Actions usage reports | Release 0 |

### 2.2 Decision record

Filled in by the maintainers when they decide. The status above flips to
`accepted` when at least one row is recorded here, and `partial` follows the
rules in [README.md](README.md).

| # | Outcome (accept / reject / amend) | Recorded by (role) | Date | Amendment text |
|---|---|---|---|---|
| D1 | amend | repository owner | 2026-10-20 | Amended 2026-10-20: the rollout in section 4.1 (the replay on history, the shadow period with its kill rule and the `REVIEW_VERDICT_ENFORCED` switch, and enforcement one lane at a time) is replaced by direct enforcement on all lanes from the first merge. No replay, no shadow mode, no kill switch, no auto-rollback. The safety is a revert by pull request. The change is observed for one week after the merge. The same replacement applies to every other row that describes that rollout: the D1 rows in sections 2.1 and 2.3, the file-list, replay, shadow, enforce, cost, rollout and rollback rows of sections 4.1.6 to 4.1.8, the D1 rows of the alternatives table in section 5, releases 0, 1 and 3 and reasons 1 and 4 in section 7, and the answer to question 1 in section 8. Each is marked and its original text is kept beside the marker. |
| D2 | accept | repository owner | 2026-10-20 | None. |
| D3 | accept | repository owner | 2026-10-20 | None. |
| D4 | accept | repository owner | 2026-10-20 | None. |
| D5 | accept | repository owner | 2026-10-20 | None. The RFC text already matches: class sweep every fix round, strategy switch on the 3rd fix of one span, narrow or defer with an issue forced on the 5th, no human stop. |
| D6 | accept | repository owner | 2026-10-20 | None. |
| D7 | accept | repository owner | 2026-10-20 | None. The RFC text already matches: optional and advisory, no `require` mode. |
| D8 | accept | repository owner | 2026-10-20 | None. |

### 2.3 Dependencies between decisions

| Decision | Needs first | Why |
|---|---|---|
| D3, D4, D5 | Release 0 (SKILL.md byte budget move) | `SKILL.md` of `kirocrew-prepare-pr` is 55,986 of 56,000 bytes (`test/test_review_repair_routing_skill.py` line 116), so 14 bytes remain |
| D3, D4, D7 | The exit-code map in section 7.3 | Three new exit codes of `diff_signals.py --check-body`, assigned once |
| D1 | Nothing in CI to start the replay; a shadow period before any gate changes | The replay and the shadow period change no gate. Superseded by the D1 amendment in section 2.2: direct enforcement on all lanes, safety is a revert by pull request. |
| D2, D6, D1 | None of the others | Each stands alone |
| D8 | Nothing | Measures the rest; useful even if all others are rejected |

## 3. Problem

### 3.1 Loops

A loop is a push made because feedback arrived (a lane comment, a CI failure, a
conflict). Source: `aggregate.json` keys `totals`, `loops_per_pr`,
`loops_by_feedback_source`.

| Measure | Value |
|---|---|
| Merged pull requests studied | 1,980 (of 10,814 merged non-bot pull requests in the window) |
| Loops | 6,560 (mean 3.31 per PR; median 1; p90 8; p99 35; max 131) |
| Pull requests with no loop | 676 (34.1%) |
| Pull requests with 10 or more loops | 150 (7.6%), holding 3,078 loops (46.9%); median 60.2 h to merge against 8.2 h for the rest (`runaway_prs`) |
| Median hours from creation to merge | 9.1 |

| Where the feedback came from | Loops | Share |
|---|---|---|
| Bot review lanes | 3,288 | 50.1% |
| CI failure | 1,923 | 29.3% |
| Merge conflict | 966 | 14.7% |
| Human review | 281 | 4.3% |
| Human comment | 102 | 1.6% |

### 3.2 Top causes

Source: `aggregate.json` key `loops_by_normalized_category`.

| Cause | Loops | Share | Rows of this RFC that address it |
|---|---|---|---|
| `rebase_conflict` | 1,017 | 15.5% | D6 (size, regen) |
| `partial_fix` | 786 | 12.0% | D3, D5 |
| `convention_unknown` | 688 | 10.5% | D2, D7 |
| `finding_text_unrecoverable` | 603 | 9.2% | none (a data gap, section 3.7) |
| `main_branch_red` | 573 | 8.7% | none (Rule 4 stays backlog, section 6) |
| `contract_drift` | 419 | 6.4% | D3, D5 |
| `scope_or_design_conflict` | 405 | 6.2% | D1 |
| `ux_copy_or_layout` | 298 | 4.5% | D1 |
| `race_or_ordering` | 271 | 4.1% | D5 |
| `flaky_or_isolation` | 263 | 4.0% | none |
| `canonical_path_tests` | 256 | 3.9% | D4, D5 |

### 3.3 Who owns the loop

Source: `aggregate.json` keys `main_moving_vs_author_owned` and `preventable`.

| Group | Loops | Share |
|---|---|---|
| Author-owned (everything else) | 3,745 | 57.1% |
| Main moved (`rebase_conflict`, `main_branch_red`) | 1,590 | 24.2% |
| Cause unknown (text or CI cause lost) | 835 | 12.7% |
| Flaky tests or CI infrastructure | 390 | 5.9% |

Among loops with a known cause, 65.4% are author-owned. The record's own
`preventable` field marks 3,471 loops (52.9%) and 3,305 of the author-owned
ones (88.3%) as preventable by a rule, a pre-push check or a lesson. That figure
is a ceiling set by each record's writer, not a measured effect of any rule.

### 3.4 Bug origins

Source: `aggregate.json` keys `totals`, `bug_origins`,
`bugs_by_normalized_failure_class`.

| Measure | Value |
|---|---|
| Bug-fix pull requests | 1,162 (58.7%) |
| Traced to an introducing pull request | 1,007 (86.7%), naming 946 distinct pull requests |
| Traces with `high` confidence | 475 of 1,162 (40.9%) |

| Failure class of the bug | Bugs | Share of bug-fix PRs |
|---|---|---|
| `deferred_risk` (a review raised it; it was set aside) | 282 | 24.3% |
| `partial_fix` | 234 | 20.1% |
| `contract_drift` | 195 | 16.8% |
| `race_or_ordering` | 85 | 7.3% |
| `canonical_path_tests` | 69 | 5.9% |
| `flaky_or_isolation` | 64 | 5.5% |
| `platform_difference` | 59 | 5.1% |

### 3.5 How the 282 set-aside risks left the pull request

Source: design D section 1 and 3. Each case was labelled by reading the record,
the introducing pull request's comments and its check runs. The path labels are
the designer's reading, not a field in the data.

| Release path | Bugs | Share |
|---|---|---|
| (a) Lane said BLOCK, a person overrode it | 6 | 2.1% |
| (a2) Lane said BLOCK at merge, no valid override (an adjudication downgrade, or merged past red) | 3 | 1.1% |
| (b) Lane said CONCERNS, FINDING or PASS-with-advice, so it never gated | 201 | 71.3% |
| (c) A human reviewer noted it, no gate exists | 15 | 5.3% |
| (d) Text lost, reviewer unknown | 5 | 1.8% |
| (e) The author declared it ("Not a goal", a code comment, commit text), or nobody raised it | 52 | 18.4% |

Who writes the release record (design D sections 3.3 and 1):

| Record | Total | Written by the PR author |
|---|---|---|
| Deferral dispositions that could be parsed | 77 | 75 (97%) |
| `ai-review-human-override` records (208 PRs) | 833 | 818 (98.2%) |

Inside the 201 advisory cases, 108 ended in an `accepted-and-deferred` reply, 49
had no reply, 28 were rebutted or scoped out, 10 were declared in the PR body, 5
sat in an approval note. 135 of the 282 bugs (47.9%) had a tracked issue and
shipped anyway, so an issue link alone does not stop the bug (design D section
11).

On the current rubric, a sample of 100 of the 282 root causes read as: 18% in a
class the rubric already blocks (security, credential, crash, data loss on a
path the diff changes), 8% in the same classes but with a remedy outside the
diff, and 74% outside any blocking class (copy, contract drift, silent
failure, resource leak). That is a lead with a wide interval, not a measurement
(design D section 4.3).

### 3.6 CI waste from avoidable pushes

Source: design F section 1. Definition: a push is *avoidable* when every red job
is a job that has a local command, and the author pushed again later.

| Measure | Value |
|---|---|
| Observed pushes in 1,878 pull requests | 7,876 (4.2 per PR) |
| Avoidable pushes | 192 in 152 PRs (7.7%): 2.4% of pushes |
| Job-minutes of those pushes | 57,613 (1.5% of all job-minutes; about 91 runner-hours per day over the 10.5-day window) |
| Share on cheap checks only (the "fast tier") | 166 of 192 (86%) |
| Heavy test and build jobs inside the 57,613 | 38,691 (67%) |
| Of the 192, pushes whose red jobs include Backend or Frontend Lint | 88, with 25,571 heavy job-minutes (measured for this RFC from design F's saved data, `lint_first_savings.py`) |

### 3.7 What the evidence cannot show

| Limit | Effect |
|---|---|
| Only 12 days of merges, and none of the 95 closed-unmerged lists | Bugs introduced in the window may not be found yet (right-censoring). Trends compare batches with different surviving evidence. |
| The study contains bugs only. It has no list of "concerns that were harmless". | **The false-block rate of D1 is unknown.** D1 section 4.1.7 proposes the measurement. |
| 41.7% of pull requests lost earlier heads to force-push; 603 loops (9.2%) have no recoverable finding text because lane comments are edited in place | Loop counts are lower bounds. Per-head lane verdicts are partly unreadable. |
| `preventable` and `prevention` are each batch author's judgement | The 3,471 figure is a ceiling. No rule was tested. |
| Only 40.9% of bug traces have `high` confidence | Origin counts carry error. |
| Severity words "at raise" come from the batch author's text and the surviving comment; 23 cases are inferred from the lane's final state | Path (b) is the best reading available, not a record. |
| Human-approval wait time is not in the records | Time cost of D1's independent-writer rule is unknown. |
| Cost of model runs, tokens and runner dollars | Unknown. Section 8 lists what would settle each. |


## 4. The decisions in detail

Each decision has the same facets: what happens today (with file and line),
the proposal, the evidence, the cost, the risk with a false-positive example,
the rollout (shadow, warn, enforce), the rollback, the proposed owner (a role),
and the success metric with its baseline.

### 4.1 D1: the review gate is PASS or BLOCK

**Decision asked.** Replace the lane vocabulary PASS / CONCERNS / BLOCK and
BLOCKING / FINDING with exactly two verdicts, PASS and BLOCK. This is the
headline of the RFC and can be accepted or rejected without D2 to D8. The
repository owner accepted D1 with an amendment on 2026-10-20 (section 2.2):
the rollout described below is replaced by direct enforcement on all lanes.
The same replacement applies to the rows in sections 2.1, 2.3, 4.1.6 to 4.1.8, 5, 7 and 8 that describe the replay, the shadow period, the kill rule, the `REVIEW_VERDICT_ENFORCED` switch or enforcement one lane at a time. Each is marked, and its original text stays beside the marker as the record.

#### 4.1.1 What happens today

Every lane already has a gate. The gate reads only the word that means block;
every softer word leaves the check green.

| Lane (check name) | Words it emits | What gates | Where (file, line) |
|---|---|---|---|
| GPT 6.1 Review | `BLOCKING`, `FINDING` lines; markers `[GPT-REVIEWED] <sha>`, `[BLOCK-MERGE] <sha>` | Only `[BLOCK-MERGE]`. A conditional Opus adjudication pass can clear it. | Prompt: `.github/review-prompts/gpt-review-core.md` lines 12-50 (finding bar, two labels, closed "WHAT BLOCKS" list); `.github/review-prompts/gpt-output-contract.md` lines 10-44. Gate: `.github/workflows/codex-review.yml` header line 9 ("BINARY"), final gate step lines 2300-2360. |
| Opus 5.5 Review | same two words, same markers | same | Prompt: `.github/review-prompts/opus-validate.md` lines 162-208 (at most 5 BLOCKING and 6 FINDING). Gate: `.github/workflows/claude-review.yml` line 629. |
| Design Review | `Design-Verdict: PASS / CONCERNS / BLOCK` | BLOCK exits 1; PASS and CONCERNS exit 0 | `.github/workflows/design-review.yml` lines 612-626 (rules), 714 (output line), 1791-1900 (gate). CONCERNS draws a job annotation only (lines 1051-1110, 1871-1890). |
| UX Review | `UX-Verdict: PASS / CONCERNS / BLOCK` | BLOCK | `.github/workflows/ux-review.yml` lines 1164-1176 (rules, "choose CONCERNS" tie-breaker), 1232 (output line), 2427-2483 (gate). |
| First Principles Review | `First-Principles-Verdict: PASS / CONCERNS / BLOCK` | BLOCK | `.github/review-prompts/first-principles.md` lines 9-10, 398-428, 512; gate step at `.github/workflows/first-principles-review.yml` line 1528. |
| Security Scope Review | `Scope-Verdict: PASS / CONCERNS / BLOCK` | BLOCK (needs a script-confirmed regression) | `.github/review-prompts/security-scope.md` lines 192-224; `.github/workflows/security-scope-review.yml` line 2702. |
| Fork copies | the six `fork-*-review.yml` lanes mirror these | same | `.github/workflows/fork-design-review.yml`, `fork-ux-review.yml`, `fork-first-principles-review.yml`, `fork-security-scope-review.yml`, `fork-gpt-review.yml`, `fork-opus-review.yml`; parity pinned by `test/test_ai_review_workflows_fork_verdict.py` |

How the verdicts reach the merge gate:

- `PR Readiness` aggregates the lane checks (`.github/workflows/pr-readiness.yml`
  lines 20-40 list them). A lane check is red only on BLOCK (or a missing
  verdict for the GPT and Opus lanes).
- The helper that finds an unanswered CONCERNS, `unanswered_concern_lanes` in
  `src/kiro_crew/builtin_skills/kirocrew-dev/kirocrew-prepare-pr/scripts/_review_contract.py`
  (line 1148), says in its docstring that it is "LOCAL ONLY": the server-side
  required status deliberately does not stop on CONCERNS.
- The verdict words are parsed by one regex in the same file (lines 927-929,
  `PASS|CONCERNS|BLOCK`) and read in `pr_status.py` (`evaluate_reviewer_markers`
  and `main`).
- A BLOCK leaves a pull request by a new head or by
  `/ai-review override <lane> <sha>: <reason>`. The handler checks that the
  actor is a repository writer, the sha is the current head, and the reason is
  non-empty and at most 500 characters
  (`.github/workflows/ai-review-human-override.yml` lines 64-80, 146-150). "PR
  authors receive no exemption" (`docs/ci/ci-and-reviews.md` lines 3850-3880),
  which also means the PR author may override.
- The rule that a writer's agent never overrides a security, data-loss,
  corruption, crash or removed-guard finding is written in prose only
  (`docs/ci/ci-and-reviews.md` lines 2865-2890). No script enforces it.
- A deferral reply (`accepted-and-deferred`) is checked for an open issue with
  the `deferred-finding` label, an assignee and a due date, but the check is
  advisory (`.github/workflows/disposition-deferral-check.yml` header lines
  1-17, tests at lines 120-133).
- Design Review, UX Review and First Principles keep a tie-breaker that says to
  choose CONCERNS when unsure (`design-review.yml` line 612ff,
  `ux-review.yml` line 1176, `first-principles.md` line 428). The GPT prompt
  already says "there is no possible-issue tier" and requires a concrete
  trigger, call path and wrong outcome for any finding
  (`gpt-review-core.md` lines 12-27), and its BLOCKING lines already carry an
  "input -> call path -> observable failure" chain plus an `Anchor:`
  (`gpt-output-contract.md` lines 12-20). The new contract extends that bar to
  every lane and removes the soft tier that sits beside it.

#### 4.1.2 What the data says

See section 3.5. In short: of 282 bugs whose risk had been raised and set aside,
201 (71.3%) were labelled with a word that does not gate, 9 (3.2%) carried a
BLOCK word when raised, and 52 (18.4%) were declared by the author. The release
record is written by the PR author in 97% of parsed deferrals and 98.2% of
override records. Two of the 201 are plain calibration misses: a lane wrote
"non-blocking" for Discord and Telegram data loss, and for a credential that
stays split through the redactor, against rubric text that already calls both
BLOCKING (design D section 3.2, path b). The owner's position is that a soft
tier is not a usable signal and that it leaves the door open for bugs. The data
fits that position; it does not prove that removing the tier lowers the bug
count.

#### 4.1.3 Proposal: the contract

1. **Two verdicts.** Each lane prints one verdict line and a reason of one to
   three sentences:

   ```text
   <Lane>-Verdict: PASS
   Reason: <one to three sentences>

   <Lane>-Verdict: BLOCK
   Reason: <one to three sentences: the core problem>
   Trigger: <the input or condition that occurs in practice, and the call path to the changed line>
   Outcome: <the observable wrong result>
   Anchor: <rule id or residual class>     (GPT and Opus lanes keep this field)
   ```

   Several BLOCK items repeat `Trigger:` and `Outcome:`. The GPT and Opus lanes
   keep `[GPT-REVIEWED]`, `[OPUS-REVIEWED]` and `[BLOCK-MERGE]` as the machine
   markers; `[BLOCK-MERGE]` means at least one complete BLOCK item.
2. **A BLOCK without trigger and outcome cannot block.** The parser treats a
   BLOCK item missing either field as PASS, prints an `unsubstantiated BLOCK`
   annotation and counts it. The check is on presence and non-empty text, not
   on truth.
3. **Impact floor.** The wrong outcome must affect a user, an operator, data or
   a security boundary. A preference, a wording choice or a style point is a
   note. This is the most contestable line of the proposal (open question 2).
4. **Style notes leave the gate.** A lane may add a `Notes:` block of at most
   three bullets. The gate, `PR Readiness` and the disposition machinery never
   read it, and no reply is required. `blocking: false` AUTOSDE rules
   (`AUTOSDE.yaml`; 17 of 24 rules carry `blocking: true`) report to `Notes:`
   as they report to FINDING today.
5. **The soft tier is deleted, not renamed.** CONCERNS, FINDING, `Watch items`
   and PASS-with-advice no longer exist as verdicts. An item that was a
   CONCERNS becomes a BLOCK if the lane can write both fields, and a note or
   nothing if it cannot. The "choose CONCERNS when torn" tie-breakers become
   "choose PASS with a note when you cannot write the Trigger and the Outcome".
6. **Rules that stay.** The rule that missing required evidence is a BLOCK, not
   a softer word, is unchanged (`docs/ci/ci-and-reviews.md` lines 2612-2640).
   A lane that does not finish, errors, is overloaded or is refused keeps
   today's behaviour (GPT and Opus fail closed; the whole-design lanes never
   fail on infrastructure: `design-review.yml` lines 1791-1800).
7. **Widening for the line lanes.** Today only defects on the closed "WHAT
   BLOCKS" list block (an AUTOSDE `blocking: true` rule, security, crash,
   data loss, corruption, a removed guard: `gpt-review-core.md` lines 29-43).
   Under D1, any defect that passes the existing three-part bar (trigger, call
   path, observable wrong outcome, on code the diff adds or changes) is a
   BLOCK. The closed list stays, but only to name the **sensitive classes** of
   4.1.4. The FIX BAR that today drops an advisory finding whose remedy needs
   untouched code (`gpt-review-core.md` lines 46-50) goes away for BLOCK items,
   as it already has for the closed list; the adjudication pass weighs
   proportionality (`gpt-review-core.md` lines 53-63).

#### 4.1.4 How a BLOCK leaves a pull request

| Way out | Rule |
|---|---|
| Fix | A new head. The lane re-runs and prints PASS. |
| Override record with a class | `/ai-review override <lane> <sha> <class>[ #N]: <reason, 20 to 500 characters>`. The class table is in Appendix C. The workflow stores the class, the issue and whether the actor is the PR author in a second hidden line of the bot comment; the first line keeps today's shape so every reader of the marker keeps working (`OVERRIDE_MARKER_RE`, `_review_contract.py` line 176). |
| Deferral | Class `deferred`. The same command names `#N`, and the workflow checks in that run that the issue is open, labelled `deferred-finding`, assigned and dated (the four tests already in `disposition-deferral-check.yml`, moved into a shared `issue_problems()` function). Otherwise the override is refused. |
| Sensitive classes | A BLOCK anchored `residual/security`, `residual/crash-data-loss-corruption` or `residual/guard-removal` needs an override from a repository writer who is not the PR author. An `agent:` reason is refused on these anchors. |
| Fallback for the sensitive classes | If no independent writer acts within a stated time (proposed: 24 hours), an owner may record class `owner-decision` instead. The actor is a CODEOWNERS member, and the record links that owner's own decision comment. The handler allows the actor to be the author of the linked comment. Whether that owner may also be the PR author is open question 4. The weekly audit lists each one. The 24 hours is a placeholder; the real wait time for a second writer is not in the data (open question 4). |

A downgrade by the adjudication pass (path a2 in section 3.5, two of nine
cases) stays as a mechanism for non-sensitive BLOCKs, but it must print a
`Downgraded:` line with the reason in the lane comment, so the release has a
record. Security-class findings are already withheld from adjudication
(`codex-review.yml` line 1104).

#### 4.1.5 Alternatives, and what each costs

| | (a) Keep three tiers, add a `Bug-likely: yes/no` tag | (b) PASS or BLOCK (proposed) |
|---|---|---|
| What the lane prints | Same words as today. Every CONCERNS and FINDING line ends with a tag. | Two verdicts. No tag. |
| What gates | Still BLOCK only. `PR Readiness` also fails on an item tagged `yes` that has no fix and no disposition at this head. | BLOCK only. |
| Lane prompts edited | One line each in six prompts and the fork copies | Rewritten output contract in six prompts and six fork copies |
| Parser and gate code | Move `unanswered_concern_lanes` server-side; new violation type; a flag | Delete the CONCERNS branches; add the trigger/outcome presence check and the override record |
| New work for authors | One disposition per `yes` item. 49 of the 282 bugs (17.4%) had no reply at all. | One fix or one override record per BLOCK |
| Where it can still leak | A mis-tagged `no` ships. The study cannot say how often. | A BLOCK that fails the presence check is treated as PASS. |
| False-block exposure | Smaller: only tagged items gate | Larger: any complete BLOCK gates |
| Needs calibration | Yes (tag accuracy) | Yes (BLOCK precision and recall) |
| Rollback | Remove the tag line and flag | Re-enable the old parser branches (kept for two releases) |
| What it leaves | A vocabulary of four words, one of which means "read this and decide" | One state. Nothing to read and ignore |

Both options need the override record and the independent-writer rule of 4.1.4.
Design D proposed (a) with a shadow period. The repository owner's position is
that concerns do not work as a signal and that the CI verdict should be only
PASS or BLOCK, which is the case for (b). The team should
decide with the replay of 4.1.7 in hand; the cost of (b) is the unknown
false-block rate, and the cost of (a) is that the soft tier stays.

Superseded by the D1 amendment in section 2.2: the owner chose (b) and no replay precedes it. Enforcement is direct on all lanes, and the safety is a revert by pull request.

#### 4.1.6 Files that change

| Area | Files |
|---|---|
| Lane prompts | `.github/review-prompts/gpt-review-core.md`, `gpt-output-contract.md`, `gpt-round-convergence.md`, `gpt-block-adjudication.md`, `opus-validate.md`, `opus-discovery.md`, `first-principles.md`, `security-scope.md`; the inline prompts of `design-review.yml` and `ux-review.yml` and of their fork copies |
| Lane gates | `codex-review.yml`, `claude-review.yml`, `design-review.yml`, `ux-review.yml`, `first-principles-review.yml`, `security-scope-review.yml` and the six `fork-*` copies (verdict parse, remove the CONCERNS digest steps, add the shadow line). The shadow line is superseded by the D1 amendment in section 2.2: no shadow mode exists, enforcement is direct on all lanes. |
| Readiness and local loop | `_review_contract.py` (verdict regex, `release_violations`), `pr_status.py`, `pr-readiness.yml` (a `REVIEW_VERDICT_ENFORCED` variable), `SKILL.md` Phase 4 text. The `REVIEW_VERDICT_ENFORCED` variable is superseded by the D1 amendment in section 2.2: no switch exists, the safety is a revert by pull request. |
| Override handler | `ai-review-human-override.yml` (class grammar, independence rule, record line), new `.github/scripts/release_record.py` |
| Deferral check | `disposition-deferral-check.yml` (shared `issue_problems()`, parity test) |
| Docs and tests | `docs/ci/ci-and-reviews.md` ("One shared binary contract", "Advisory means advisory", "Human override", "AI-review human overrides"), `AGENTS.md` (one subsection), `test/test_review_lane_prompt_parity.py`, `test/test_ai_review_workflows*.py`, new `test/test_release_record.py` |

#### 4.1.7 The honest limit, and how to measure the false-block rate

The study has bugs and nothing else. It cannot say how many CONCERNS and
FINDING lines were harmless, so it cannot say how many of them a BLOCK would
have wrongly held. **The false-block rate of D1 is unknown.** Raising the gate
without measuring it risks adding rounds to a loop that already costs 6,560 of
them.

Proposed measurement, in three steps. All numbers below are proposals; the team
should change them before the replay starts.

Superseded by the D1 amendment in section 2.2: there is no replay, no shadow period, no kill rule and no per-lane enforcement. The steps below, and the cost paragraph after them, stay as the record of what was proposed.

| Step | What | Acceptance to move on |
|---|---|---|
| 1. Replay on history (no CI change) | **Positives:** the introducing pull requests of the 282 bugs (277 have one recorded). **Negatives:** a matched set of about three per positive, from the same merge week, area and size decile, that no bug-fix pull request names as an introducer. Run each lane's new prompt on the stored diff at the merged head. Two readers judge each BLOCK; a third settles a split. | **Recall on stated risks** (positives where the old lane already wrote a trigger and an outcome, paths a, a2, b): at least 90% get a BLOCK carrying the same risk. **Recall on all positives**: report it; proposed floor 50%. **False-block rate**: share of negative pull requests with at least one BLOCK both readers call not a defect: at most 10%, and at most 0.15 false BLOCKs per pull request (0.15 is 4.5% of the 3.31 loops per PR today). Superseded by the D1 amendment in section 2.2: direct enforcement on all lanes, safety is a revert by pull request. |
| 2. Shadow mode | Each lane keeps its current verdict as the gate and prints the new verdict beside it: `Verdict (shadow): PASS or BLOCK, reason` in the comment and the job summary. A weekly job reports the agreement matrix per lane, the share of new BLOCKs with a missing field (target below 5%) and, once overrides exist, the share overridden as `false-positive`. A switch `REVIEW_VERDICT_ENFORCED: "false"` follows the pattern of `issue-gate.yml` (`GATE_ENFORCED`, lines 168-196), where any value other than `"true"` or `"false"` fails the job. | At least 14 days and the replay numbers confirmed on live pull requests. Kill rule: more than 30% of new BLOCKs overridden as `false-positive` by an independent writer withdraws the prompt (30% is a placeholder; the base rate is unknown). Superseded by the D1 amendment in section 2.2: no switch, kill rule or auto-rollback exists; enforcement is direct on all lanes and the only remedy is a revert by pull request. |
| 3. Enforce, one lane at a time | Order: GPT 6.1 and Opus 5.5 first (already two-state at the gate), then Security Scope, First Principles, Design, and UX last (`ux_copy_or_layout` is 298 loops, 4.5%, and the lane judges taste). | Each lane flips on its own date. Superseded by the D1 amendment in section 2.2: direct enforcement on all lanes, safety is a revert by pull request. |

Costs of the replay: about 1,100 pull requests times up to five lanes, so
roughly 5,500 lane runs at a median of 2 to 7 minutes each. Tokens and dollars
are **unknown**; a pilot of 50 positives and 150 negatives settles the cost
before the full run.

Negatives are a lower bound on harm that the study can still miss: a "no known
bug" pull request may carry a bug not found in 12 days. That bias raises the
measured false-block rate, so it errs against D1.

#### 4.1.8 Cost, risk, rollout, rollback, owner, metric

| Facet | Content |
|---|---|
| Cost | Prompt rewrites in 6 lanes and 6 fork copies; parser changes; the override handler (medium); the replay (cost unknown); a weekly shadow report. More BLOCKs means possibly more rounds. A second writer on sensitive blocks adds latency of unknown size. The replay and the weekly shadow report are superseded by the D1 amendment in section 2.2: neither is built. |
| Risk | **False blocks.** A BLOCK the author disagrees with costs a push, an override or both. **Taste lanes.** UX and Design judge experience and shape; "Trigger" and "Outcome" fit them less well than a code path. **Rubric widening.** 74% of sampled bugs fall outside today's blocking classes, so the BLOCK bar widens most where lanes have the least training signal. |
| False-positive example | The UX lane states the trigger "a user opens Settings in a 320 px window" and the outcome "a label wraps onto a second line". Both fields are present and the outcome is observable. Under the impact floor (item 3) this is a note unless the label becomes unreadable. Without the floor, it blocks. The replay decides where the floor sits. |
| Rollout | Replay (no CI change) then shadow then enforce per lane (4.1.7). Old and new override grammar both accepted for six weeks. The replay, shadow and per-lane parts are superseded by the D1 amendment in section 2.2: direct enforcement on all lanes, safety is a revert by pull request. |
| Rollback | Set `REVIEW_VERDICT_ENFORCED` to `false`: the old gate returns, because the old parser branches stay for two releases. Revert the grammar by dropping the class requirement; the second hidden line is ignored by old readers. Superseded by the D1 amendment in section 2.2: no switch, kill rule or auto-rollback exists; enforcement is direct on all lanes and the only remedy is a revert by pull request. |
| Owner (proposed) | The maintainers who own the review lanes (CODEOWNERS team `kirocrew-pr-review`), with CI owners for the workflow edits. |
| Success metric (baseline to target) | Risks released with no record: 49 of 282 path-b cases (17.4%) to 0 for BLOCK items. Overrides carrying a class: 0 of 833 to 100%. Sensitive-class overrides by the PR author: unknown baseline (98.2% of all overrides) to 0. `deferred` overrides with a valid issue: not recorded to 100%. `deferred_risk` bug-fix pull requests: 282 of 1,162 (24.3%) to a lower share; no number is promised, because other changes land at the same time (design D section 9). |

### 4.2 D2: lint runs first

**Decision asked.** Make the heavy jobs of `ci.yml` wait for Backend Lint and
Frontend Lint, in addition to the Fast Gate they wait for today.

| Facet | Content |
|---|---|
| Today | `.github/workflows/ci.yml` has 24 jobs whose `needs` include `await-fast-gate` (first at line 414, last at line 3782), two of them the lint jobs themselves. `await-fast-gate` (line 511) polls the separate `fast-gate.yml` run through `.github/scripts/await-fast-gate.sh` (poll budget 720 s, `TOTAL_BUDGET`). `fast-gate.yml` holds the cheap gates: 198 job-seconds in total, about 44 s wall clock (header lines 6-8). The two lint jobs, `backend-lint` (line 623, timeout 25 min) and `frontend-lint` (line 2535, timeout 20 min), run beside the heavy jobs, not before them. The eight backend test shards alone are 73.7% of CI's job-minutes (`fast-gate.yml` header, line 8). |
| Proposal | Add one small job, `lint-gate`, to `ci.yml`: `needs: [backend-lint, frontend-lint]`, `if: ${{ !cancelled() }}`. It fails when either lint job concluded `failure` and succeeds when each is `success` or `skipped` (each lint job already skips on a diff that cannot change its verdict: lines 630 and 2540). Heavy jobs change `needs: [changes, await-fast-gate]` to `needs: [changes, await-fast-gate, lint-gate]`. A red lint then skips the heavy matrix exactly as a red Fast Gate does today (`ci.yml` comment at line 3122). |
| Alternative: fold lint into Fast Gate | More expensive. Backend lint passes in a median 8.1 min (p90 18.1; 6,515 passing runs, design F section 1.1), against a Fast Gate of about 44 s. Two consumers key on Fast Gate finishing: `await-fast-gate` (poll budget 720 s, job timeout 15 min) and the fork review lanes (`fast-gate.yml` header, lines 16-18). Folding lint in would exceed the poll budget at p90 and would delay every fork review lane by the lint time. The aggregate job changes neither. |
| Variant | Gate only `backend-test` on `lint-gate`. The shards are 73.7% of job-minutes, and the edit touches one job. Frontend and E2E jobs would still start at once. |
| Evidence | Of the 192 avoidable pushes, 88 had Backend or Frontend Lint among the red jobs. Those 88 pushes ran 25,571 heavy test and build job-minutes in the 10.5-day window (computed for this RFC from design F's saved check-runs: 34,525 job-minutes in total, 25,571 of them heavy). The other 104 avoidable pushes ran 13,120 heavy job-minutes, and lint-first would not stop them (their red jobs were PR Hygiene, Bundle Size, Static Ratchet and similar). 25,571 is 0.7% of all job-minutes (3,751,684). Design F's figure of "up to 38,691" counts both groups, so it is an upper bound for this change. |
| Cost | Every push whose heavy jobs would run waits a median 8.1 min (p90 18.1) longer for them to start. CI's median wall clock is about 54 min (`fast-gate.yml` header, line 17; not re-measured here), so the median cost is about 15% of it. A push that is green gains nothing. One new job and 22 one-line `needs` edits (one with the variant). |
| Risk and false-positive example | A lint job that goes red for a reason outside the diff (a runner or network fault during `uv pip install`, `ci.yml` line 674) holds every heavy job until a re-run. Fast Gate has the same exposure today. A push to main skips `await-fast-gate` while `MERGE_QUEUE_ENABLED` is `'true'` (`ci.yml` line 517), so `lint-gate` must follow the same skip rule or the merge queue path stalls. |
| Rollout | Shadow: land `lint-gate` with no job depending on it, and have it print a notice `heavy jobs would have been held` on a red lint. Count the notices for two weeks against design F's 88-push baseline. Enforce: one pull request adds `lint-gate` to the `needs` lists. |
| Rollback | Revert the pull request that added the `needs` edges. The job can stay. |
| Owner (proposed) | CI owners (`ci.yml`, `fast-gate.yml`). |
| Success metric | M2 in section 4.8 (avoidable job-minutes per merged PR: 29.1) and the lint-red heavy job-minutes (25,571 per 10.5 days) to near zero. Counter-metric: median start time of the first heavy job after a push, today about the Fast Gate time, with a ceiling of plus 10 min at the median. |

### 4.3 D3: a sibling sweep in the PR body

**Decision asked.** Fix and revert pull requests carry a `## Sibling sweep`
section listing every other place with the same defect.

| Facet | Content |
|---|---|
| Today | `fix`/`revert` pull requests must carry `## Pattern harvest`, which asks whether the defect class generalises (`.github/PULL_REQUEST_TEMPLATE.md` line 144; CI step "Require Pattern harvest section on fix PRs", `.github/workflows/code-review.yml` lines 492-528). It checks presence only. 1,018 of 1,022 fix pull requests carry it (99.6%, design F section 2.1). Nothing asks for the unchanged siblings. `diff_signals.py --check-body` returns 0, 20, 21 or 22 (`.../kirocrew-prepare-pr/scripts/diff_signals.py` lines 40-42; `return rc or tpl` at line 404). |
| Proposal | New template block directly before `## Pattern harvest`. Grammar and the six cheap checks are in Appendix D. CI side: a new step after `code-review.yml` line 528 runs `.github/scripts/sibling_list_check.py`, read from the base ref so a branch cannot weaken it. Local side: `diff_signals.py --check-body` gains `sibling_check`, exit **23**. The section is **not** added to `REQUIRED_SECTIONS` (`.github/scripts/pr-description-check.sh` lines 28-36): that list is matched on every open pull request, and an outside copy must match. The prepare-pr body builder scaffolds from the template (`SKILL.md` Phase 1), so the template edit reaches agents with no SKILL.md bytes. |
| Evidence | Fix titles are 1,022 of 1,980 pull requests (51.6%). `partial_fix` is 786 loops (12.0%), 479 of them in fix pull requests, and 25.0% of fix pull requests have one; 234 bugs (20.1% of bug-fix pull requests) are `partial_fix`. A mechanical probe for "a removed line still appears elsewhere" fires on 7.2% of fix commits and pointed at the later fix in 7 of 121 cases (5.8%, design A section 0), so the list rests on the author's search, not on the probe. 373 fix bodies (36.5%) already use a sibling word; those pull requests have 0.61 `partial_fix` loops each against 0.39 for the rest, an association confounded by size (3.66 against 2.77 mean loops). |
| Cost | One section on a fix body; one `git grep` the author already runs. No network call in the check. |
| Risk and false-positive example | The list can become boilerplate: `Siblings: none -- single call site`. A share above 60% would say so (metric). A `revert` that has no siblings must write the same line. A completeness error stays invisible: the check never proves the list is complete, and the script header says so, as `pr-description-check.sh` lines 24-25 does for its own limit. |
| Rollout | Template and `sibling_list_check.py` in warn mode (`::warning::`, exit 0) for 14 days. Then red for pull requests opened on or after an `ENFORCE_FROM` date, so no open fix pull request turns red for a section it never had. |
| Rollback | Set the warn mode back, or drop the step. The template block is inert without the step. |
| Owner (proposed) | Skill owners for `kirocrew-prepare-pr`, CI owners for the `code-review.yml` step. |
| Success metric | Fix pull requests carrying the section: 0% to over 95% of new ones. `Siblings: none` share: below 60%. `partial_fix` loops per fix pull request: 0.47 (479 of 1,022). `partial_fix` bugs: 234 at the next study. |

### 4.4 D4: a Proof line inside `## Tests`

**Decision asked.** A `fix:` pull request states, under the existing `## Tests`
heading, whether its new tests fail without the fix. No new heading.

| Facet | Content |
|---|---|
| Today | The template asks only to describe tests (`PULL_REQUEST_TEMPLATE.md` lines 78-80). `## Tests` is one of five required headings, checked for presence (`pr-description-check.sh` lines 32-36). `prove.py` reverts the production hunks in a throwaway worktree and re-runs the changed tests (`.../scripts/prove.py` docstring, lines 1-36); `SKILL.md` lists it in the script table (line 275) but no phase step, gate or workflow runs it (a search of `.github/workflows` finds nothing). The AUTOSDE rule `an-invariant-line-needs-a-mutation-pin` is `blocking: false` (`AUTOSDE.yaml` line 1241). A fail-without-fix result is asked only for a flaky-test fix (`SKILL.md` line 95). |
| Proposal | Template text under `## Tests`: `Proof: PROVEN <the new tests that fail without the fix> (head <sha12>)`, or `Proof: not provable -- <reason from a closed list>` (Appendix D). CI: a new step after the Pattern harvest step runs `.github/scripts/proof_line_check.py` on `fix:` titles only, checking that the line is inside `## Tests` (presence and shape). Local: `diff_signals.py --check-body` gains `proof_check`, exit **24**, which compares a `PROVEN` line with `<git-dir>/prepare-pr-prove.json` for the same tree, so a stale or invented line fails in the agent flow. `prove.py` learns to name a frontend test and to write the line and the record. |
| Evidence | 418 of 1,162 bug-fix bodies (36.0%) state a fail-without-fix result in strict wording and 782 (67.3%) in loose wording; the claim is text and nothing checks it (design C part II). `prove.py` can judge 766 (65.9%) of bug-fix pull requests; 239 (20.6%) are frontend-test pull requests where it cannot (design C part II). `canonical_path_tests` (256 loops) and `missing_test` (69 loops) are 325 loops (5.0%). |
| Cost | One `prove.py` run per fix (one extra pytest run over one or two files). About 30 bytes in `SKILL.md`, which needs release 0 first. |
| Risk and false-positive example | A control test that passes both ways ([#21400](https://github.com/kirodotdev/KiroCrew/pull/21400)) stays valid: the rule asks for one failing new test, not all. A frontend fix writes `Proof: not provable -- frontend test`, which is honest and will describe 20.6% of fixes until a vitest runner exists. A hand-typed `PROVEN` line passes the CI step, as `Reader:` lines do; only the local comparison catches it. |
| Rollout | Warn for 14 days, then red for fix pull requests opened after an `ENFORCE_FROM` date. |
| Rollback | As D3. |
| Owner (proposed) | Skill owners, CI owners. |
| Success metric | Fix pull requests with a `Proof:` line inside `## Tests`: 0% to over 95%. `not provable` share by reason (baseline: frontend 20.6%). `canonical_path_tests` plus `missing_test` loops: 325 (5.0%) to lower. |

### 4.5 D5: a class sweep on every fix round, and an automatic strategy switch

**Decision asked.** On every fix round the mate names the class of each finding
and lists its sites, and a script checks the list at push time. The 3rd fix of
one span forces a change of approach. The 5th fix forces narrowing or a
deferral with an issue. No human is asked at any step.

| Facet | Content |
|---|---|
| Today | `pr_findings.py --rounds` reads the pull request thread and exits **30** "retrospective due" every third round or when a span has been mentioned in 3 rounds (`.../scripts/pr_findings.py` lines 37-38 `RETRO_EVERY`, 85-190 `rounds_view`, `retro_due` at line 140). It counts mentions, so 9 of the 61 pull requests that trip it do so on re-answers only. `SKILL.md` lines 333-360 make the retrospective "a step, not a stop", with four pause reasons for a person; recurrence is never one. The local review runs the GPT and Opus lanes only (`profiles/kirocrew.json` `reviewers`). |
| Proposal | (1) **Sweep every fix round.** Before editing, the mate writes one entry per span in `<git-dir>/prepare-pr-sweep.md` (never committed): a `class:` sentence, a `git grep` pattern, and every site marked `fixed`, `covered-by <test id>` or `not-in-class -- <reason>`. A class with no usable pattern writes `search: none -- <reason>` plus a `branches:` table. Format and the enforced rules are in Appendix E. (2) **`sweep_check.py`** runs inside `push_guard.py` (after `--require-single-on-base`) and refuses the push, exit 40, while the pattern has a hit no site line accounts for. (3) **3rd fix of one span.** Exit 30 fires when a span has been *fixed* in two earlier heads, not merely mentioned, and prints `SEAM REPEAT` with a fixed pick order: remove the second writer or hazard (when this PR added the mechanism); narrow the scope (revert to the last head before the mechanism and redo it smaller); defer with an issue and a pinning test. The mate takes the first option that holds and continues in the same turn. (4) **5th fix of one span.** Only two options remain, narrow or defer-with-issue-and-pin; `sweep_check.py` refuses a fifth `fixed` disposition of the span that records neither. For security, data-loss and corruption classes deferral is not allowed (the GPT lane re-raises them every round, `gpt-round-convergence.md`), so only remove or narrow apply. (5) Exit 30 and the retrospective step stay; design C's exit 31 and its 8-hour wait for a maintainer are dropped. |
| Riders (each can be dropped alone) | Run the Design and First Principles lanes in the local review (the study shows 746 of 1,150 fix rounds include a Design, First Principles or UX finding, and none of those lanes' fix events are repeats); lane prompts list every site of a class and tag same-class findings (`Sites:`); hold a push until every lane of the previous head has posted, with a 45-minute cap (25 of 413 evaluable fix rounds, 6.1%, followed a lost lane verdict); count fix outcomes, not mentions, in `rounds_view`; correct `SKILL.md` line 495, which says the review lanes never run on `edited`, although `codex-review.yml` lists `edited` (line 30). |
| Evidence | In 519 pull requests with parsed span dispositions (2,901 loops, 1,160 fix rounds), 142 fix rounds (12.2%) only re-fix a span already fixed, 4.9% of those loops; 72 are third-or-later fixes. Every repeat fix came from the GPT or Opus lanes (151 and 129 events; 0 from Design, First Principles, UX). The record-level ceiling is wider: `partial_fix`, `contract_drift`, `race_or_ordering` and `unhandled_failure_mode` are 1,549 loops (23.6%). A seam that moves across files shows as new spans, so the span count under-reports it (design E section 3.2). In the 61 pull requests with a span in 3 or more heads, 123 of 999 loops (12.3%) are repeat-only fix rounds (design E section 6.1). |
| Cost | A sweep file per fix round and one to four `git grep` passes per push. Time and tokens per sweep are **unknown**; the warn phase logs them to `<git-dir>/prepare-pr-metrics.jsonl`. No extra push. |
| Risk and false-positive example | The check proves only that the mate's own pattern leaves no unlisted hit. A sibling the pattern never names passes (the mechanical echo probe had 5.8% recall: D3). A pattern that matches 40 harmless lines is refused as too broad until narrowed, which costs a local iteration, not a CI push. A span that carries six different defects ([#17549](https://github.com/kirodotdev/KiroCrew/pull/17549)) gets six different `class:` sentences and does not trigger `SEAM REPEAT`. A refusal the mate cannot satisfy falls under the existing pause reason "every option breaks the frozen goal". |
| Rollout | Release 0 frees the `SKILL.md` bytes; `rounds_view` counts fix outcomes and prints `SEAM REPEAT`, still exit 30. Then `sweep_check.py` in warn mode for 14 days, with `SWEEP_ENFORCED: "false"`. Then refuse. |
| Rollback | `SWEEP_ENFORCED` back to false; `SKILL.md` text reverted from `references/seam-protocol.md`. |
| Owner (proposed) | Skill owners for `kirocrew-prepare-pr`. |
| Success metric | Repeat-only fix rounds: 142 (12.2% of fix rounds) to 6% in two months. Third-or-later fix rounds: 72 to under 30. `partial_fix` loops: 12.0% to 8%. Pull requests with 10 or more loops: 150 (7.6%), 46.9% of loops, to 5% and 35%. Loops per PR p90: 8 to 6. |

### 4.6 D6: a size signal with an owner override

**Decision asked.** Flag the pull requests that are very wide or have been open
a week, and make the widest wait for an owner's explicit approval.

| Facet | Content |
|---|---|
| Today | `.github/workflows/pr-scope.yml` is advisory and "NEVER fails the build", by design: "a hard threshold on it would block legitimate work" (lines 3-11); `test/test_pr_quality_gates.py` (lines 753-797) pins that. The only size rule is a file-length cap of 2,000 lines per hand-written file (`AGENTS.md`, "No hand-written file over 2,000 lines"). `regen_shared.py` does not exist. |
| Proposal | Thresholds in Appendix F. **Hint** (local, prints only): `diff_signals.py` prints `! SIZE` at 50 or more files. **Flag**: 80 or more files, or 168 h open: label `large-pr` or `aging-pr` and one comment listing the options (split, finish today, ask the owner). **Stop**: 80 or more files: a new check `PR Size` is red until an owner comments `/pr-size approve: <reason, 12 to 500 characters>`. **Age** never goes red (waiting for review is not the author's fault: 327 `unknowns` entries mention a wait). Handler `pr-size-override.yml` runs on `issue_comment` from the default branch, validates the actor with the same permission read as `ai-review-human-override.yml` (lines 100-150: `admin`, `maintain` or `write`; an unreadable permission is a refusal), and writes a bot marker with actor, size and reason. The approval holds while files stay at or below the recorded count times 1.25 plus 10. The owner must not be the PR author unless that person is `admin`. Add `PR Size` to the lane list of `pr-readiness.yml` (lines 20-40). New script `regen_shared.py` regenerates the shared generated files last, after a fresh rebase, from a new `regen[]` list in the profile. |
| Evidence | Thresholds are tuned on 1,980 pull requests (Appendix F). `files >= 80 or open >= 168 h` flags 65 pull requests (3.3%) that hold 26.9% of rebase loops and 19.7% of all loops, with 4.6% of them "quiet" (merged within 12 h with at most one loop). Lines are the wrong signal: of 106 pull requests with 5,000 or more lines that merged fast with at most one loop, 94 have "split" in the title. Rebase conflicts name the same files again and again: `docs/feature-map/README.md` (32 loops), `docs/architecture/mcp.md` (16), `test/test_security_gate_liveness.py` (14), `src/kiro_crew/dashboard/chat_runner.py` (13), `test/test_campaign_facades_base_public_names.py` (13), `.github/file-line-cap-baseline.txt` (8) (`SYNTHESIS` section 7). Age is partly an outcome (a pull request stuck in review stays open); the direction is unknown. |
| Cost | One red check on 0.9% of pull requests (17 of 1,980). One `GET pulls/<n>` per run, no checkout. Two new workflows and one script. |
| Risk and false-positive example | A mechanical change that legitimately touches 90 files (a generated-file refresh) goes red until an owner comments; that is the intended cost of the override. This reverses the statement in `pr-scope.yml` for a narrow tail and needs the maintainers' explicit confirmation (open question 11). |
| Rollout | `SIZE_ENFORCED: "false"` first, in the style of `issue-gate.yml` (`GATE_ENFORCED`, lines 168-196): the check logs what would be red and labels. Then red after the shadow period. |
| Rollback | Set `SIZE_ENFORCED` back to false; remove `PR Size` from the lane list. |
| Owner (proposed) | CI owners for the workflows, the maintainers for the override rule. |
| Success metric | Flagged share: 3.3% of pull requests. Rebase loops: 1,017 (mean 0.51 per PR) to lower. Share of rebase loops inside flagged pull requests: 26.9%. Overrides per week, listed in the weekly report; a run of overrides means the limit needs retuning. |

### 4.7 D7: the pre-push tool, optional and advisory

**Decision asked.** Ship a runner that does what the pre-commit prose in
`AGENTS.md` asks, and a line that says what it ran. Nothing refuses a push
because of either.

| Facet | Content |
|---|---|
| Today | `AGENTS.md` ("The gate before you commit", line 269 on) lists the commands in prose. `profiles/kirocrew.json` carries 61 `gates[]` entries. `scripts/local-gate.py` runs the tests related to a diff. No script runs the gates, writes a record that they ran, or is asked by `push_guard.py`. |
| Proposal | New `scripts/pre_push_gate.py` and `scripts/gate_map.json` (35 gates plus 10 `ci_only` entries with a reason). The runner picks gates from the changed paths, runs all of them, prints every failure at once, and writes a proof for the exact tree. It prints a `Gate proof:` body line. Skips carry a code: `tool-missing:<tool>`, `no-network`, `unsupported-os`, `red-on-base:<sha>`, `other:<30+ characters>`. Exit codes 50 (a gate failed), 51 (a gate could not run and no skip covers it), 52 (`--verify` finds no proof). **`push_guard.py` gets no gate-proof flag and no `require` mode.** There is no hook that refuses a push, and no CI job that fails when the line is absent. `diff_signals.py --check-body` exit **25** is returned only when a `Gate proof:` line is present and contradicts the recorded proof; an absent line never fails. A test, `test_gate_map_covers_ci.py`, pins the map to CI. |
| Why advisory | See section 6. In short: a local check cannot reproduce CI on every machine (missing tools, other operating systems, no network), so a mandatory one would refuse correct pushes; it cannot reach people who run plain `git push`; and a refusal stalls an unattended loop. |
| Evidence | 956 of 7,876 pushes (12.1%) carry a red local-command job. Only 192 (2.4%) are avoidable by the definition in section 3.6, 166 of those (86%) on cheap checks. First observed push red on a local-command job: 17.4% of pull requests (a lower bound). Failing jobs are short (median 1.7 min, p90 5.4). |
| Cost | A runner script and a map; effort medium. Runtime per gate unknown until `--time` runs. |
| Risk and false-positive example | The tool can become a habit that people read as a requirement. The `Gate proof:` line states skips, so a reader sees them; nothing blocks. A `semgrep` gate that cannot run for lack of `uv` is recorded `tool-missing:uv` and the push goes ahead. |
| Rollout | Ships as a tool with a `--test` self-test. No shadow or enforce stage exists for it. |
| Rollback | Delete the tool. Nothing depends on it. |
| Owner (proposed) | Skill owners. |
| Success metric | Adoption, report only: agent pushes with a proof or a coded skip, 0% today. Waste measures M1 to M3 (section 4.8) show the effect. Design F's targets for M1 to M3 assumed `require` and do not apply; set new ones after eight weeks of data. |

### 4.8 D8: measure whether it worked

**Decision asked.** Add the five measures below to the weekly
`fix_loop_metrics.py` report, so every other decision can be judged against a
baseline.

| Metric | Definition | Baseline (study window) | Direction |
|---|---|---|---|
| M1 | Avoidable pushes per 100 merged pull requests | 9.7 (6.9 if only author-owned loops) | Down. Target set after eight weeks. |
| M2 | Avoidable job-minutes per merged pull request | 29.1 (19.5 author-owned) | Down |
| M3 | Pull requests whose first push is red on a local-command job | 17.4% (lower bound) | Down |
| M4 | Agent pushes with a proof or a coded skip | 0% | Up (D7 is optional; this is adoption, not compliance) |
| M5 | Skip rate, and the share coded `other:` | none | Report only |

| Facet | Content |
|---|---|
| Today | `.github/scripts/fix_loop_metrics.py` already computes `deferrals` (open `deferred-finding` issues) and `harvest` (fix pull requests with `## Pattern harvest`) for `.github/workflows/fix-loop-analysis.yml`. It has no CI-waste group, no override-record counters and no size counters. |
| Proposal | A `ci_waste` group (the logic of the study's `waste.py` over the week's merged pull requests), plus counters for D1: `overrides_total`, `overrides_by_class`, `overrides_self_authored`, `overrides_deferred_without_issue`, `bypass_merges`; for D3 and D4: section and line adoption; for D5: `repeat_only_rounds`, `third_plus_fix_rounds`, `sweep_none_share`; for D6: flagged and approved counts. |
| Cost | `GET commits/<sha>/check-runs` per head is about 2 REST calls per push, about 10,000 a week, in nightly slices on its own token. Whether that budget exists is **unknown** (`pr-readiness.yml` shares an hourly pool). Sampling is not enough: avoidable pushes per pull request have mean 0.097 and sd 0.386, so 200 pull requests give plus or minus 55%. If the organisation already has Actions usage reports, use those and drop the census. Dollar cost per runner-minute is **unknown**: some jobs run on AWS CodeBuild (`pr-scope.yml` line 31), so the billing owner would have to supply it. |
| Risk | A measure can turn into a target and drift. The weekly report prints raw counts beside every rate. |
| Rollout | Release 0, in report-only form. |
| Rollback | Remove the group. |
| Owner (proposed) | Owners of `fix-loop-analysis.yml`. |
| Success metric | The report exists and its first run reproduces the baselines above within sampling error. |

## 5. Alternatives considered

| Alternative | What it would do | Why not (or why not alone) |
|---|---|---|
| **Do nothing** | Keep today's rules. | Costs stay: 3.31 loops per pull request, 46.9% of loops in 7.6% of pull requests, 24.3% of bug-fix pull requests repairing a set-aside risk, and about 91 runner-hours a day on avoidable pushes. The study has no trend that says they fall by themselves: loops per PR run from 3.2 to 2.32 across the ten batch rows, but the rows differ in surviving evidence (`SYNTHESIS` section 1). |
| **Keep the concerns tier** | D1 option (a): three tiers plus a `Bug-likely` tag. | A coherent choice with a smaller false-block exposure (section 4.1.5). It leaves a vocabulary in which one word means "read this and decide", and its gate depends on a tag whose accuracy nobody has measured. It stays the fallback if the replay shows an unacceptable false-block rate for D1. Superseded by the D1 amendment in section 2.2: there is no replay, so this condition cannot trigger; direct enforcement on all lanes, safety is a revert by pull request. |
| **Prompt-only calibration of the lanes** | Add the 18 sampled blocker-class bugs as regression prompts; change no gate (design D section 5.2, item 1). | Cheap and worth doing inside D1's replay, but it fixes the 15-point gap between today's rubric and today's behaviour only. 74% of sampled bugs sit outside the rubric. The replay is superseded by the D1 amendment in section 2.2: there is no replay. |
| **Every deferral needs an issue and a pinning test** (design A, Rule 1) | Fire on all `accepted-and-deferred` replies. | 135 of the 282 bugs (47.9%) already had an issue and shipped. The pin would touch about 109 deferrals a week (design D section 11), most of them low risk, and a strict `xfail` pin collides with the GPT lane's "a ratchet may only tighten" rule. D1 asks for an issue on every `deferred` override and a pin only for a sensitive-class deferral (open question 6). |
| **Mechanical sibling probe** (design A, Rule 2, stage 1) | Flag a removed line that still appears elsewhere. | Fires on 7.2% of fix commits and pointed at the later fix in 5.8% of cases. Kept as a non-blocking hint only, after D3. |
| **Fold lint into Fast Gate** | D2 by another route. | Costlier (section 4.2). |
| **Pre-push gate in `require` mode** | Refuse a push without a proof. | Rejected by the repository owner; section 6. |
| **Stop at the third patch and ask a human** (design C, exit 31) | An 8-hour wait for a maintainer. | Rejected by the repository owner; section 6. |
| **Cancel heavy jobs when lint fails, instead of waiting** | Lower latency than D2. | Needs `actions: write` in a PR-triggered workflow. The repo confines that grant to one push-only job (`cancel-superseded-run`, `ci.yml` comment at lines 574-586). |
| **Size limit by lines** | Flag at 1,000 lines. | Wrong signal: 94 of 106 fast, quiet pull requests of 5,000 or more lines are "split" refactors driven by the 2,000-line file cap. |

## 6. What we are not proposing

| Not proposed | Why |
|---|---|
| **Mandatory local-check enforcement.** No `require` mode in `push_guard.py`, no pre-push hook that refuses, no CI job that fails when a `Gate proof:` line is missing. | A mandatory local check would misfire. A local run cannot reproduce CI on every machine (a missing `semgrep`, `uv` or `node_modules`, another operating system, no network). Design B itself needed a recorded-skip escape for each such case. It cannot reach a person who runs plain `git push`. A refusal stalls the unattended loop for a cause the author cannot fix on that machine. CI is already the authority and runs the same checks. What the data supports is an optional tool and a visible record (D7), plus lint-first in CI where it cannot misfire (D2). |
| **A human stop at the third patch.** | It breaks the automation flow. The fix is fewer patches, so D5 changes the approach automatically and never waits for a person. The four existing pause reasons in `SKILL.md` (line 353 on) stay, with their rule that recurrence is never one. Design C's exit 31 and its 8-hour wait are dropped. |
| **A central deferral registry.** | The issue machinery exists: the `deferred-finding` label, an assignee and a due date, and a weekly audit (`deferred-findings-audit.yml`). The problem is follow-through, not lookup: 119 of 267 open `deferred-finding` issues (44.6%) were overdue on 2026-10-19 (design A section 0; not re-queried). A registry would be a second place to drift. D1 binds each `deferred` override to one issue instead. |
| **A new required status check.** | `PR Readiness` stays the only required status (design D section 6.3). `PR Size` and the lint gate feed it through the existing lane list. |
| **Rule 4, main red.** | Stays a backlog item. 573 loops (8.7%) are `main_branch_red`, only 0.2% of them preventable by an author; the cost is not the author's to remove. |
| **The other `SYNTHESIS` rule candidates** (5 text that restates code, 7 hostile inputs, 9 ordinary failure and bound, 11 test state, 12 Windows behaviour, 13 copy and screenshots). | Candidates for `AGENTS.md` text, not part of this decision. |
| **A merge queue change for rebase loops.** | Not proposed; D6 and `regen_shared.py` address the measured concentration first. |

## 7. Rollout plan, order and exit codes

### 7.1 Releases

Each release is a set of pull requests, each independently shippable and
abandonable. A decision that is rejected drops its rows and nothing else.

| Release | Contents | Behaviour change | Gate to the next release |
|---|---|---|---|
| **0** | `SKILL.md` byte-budget move: relocate the retrospective and Phase 4 text into `references/` so the file has real headroom (14 bytes today). D8 metrics in report-only form. D1 replay (offline). D2 `lint-gate` in shadow (no job depends on it). | None | Baselines reproduced within sampling error; replay pilot (50 positives, 150 negatives) done; two weeks of `lint-gate` notices. The D1 replay in this row is superseded by the D1 amendment in section 2.2: there is no replay. |
| **1** | D3 template block and `sibling_list_check.py` in warn mode, local exit 23. D4 `Proof:` text and `proof_line_check.py` in warn mode, local exit 24, `prove.py` update. D7 `pre_push_gate.py` and `gate_map.json`, local exit 25. D5 phase 0: `rounds_view` counts fix outcomes and prints `SEAM REPEAT` (exit 30 unchanged) and the `SKILL.md` edits. D1 shadow: lanes print the new verdict beside the old one; the override handler accepts both grammars. | Warnings and printed lines only | 14 days of warn data; shadow agreement matrix per lane. The D1 shadow in this row is superseded by the D1 amendment in section 2.2: there is no shadow. |
| **2** | D2 enforce: `lint-gate` added to the heavy jobs' `needs`. D3 and D4 turn red for pull requests opened after `ENFORCE_FROM`. D5 `sweep_check.py` in warn mode. D6 `PR Size` check, `/pr-size approve` handler and `regen_shared.py` in shadow (`SIZE_ENFORCED: "false"`, labels only). | Lint-first is live; two body checks go red | 14 days of D5 warn data; D6 shadow list reviewed |
| **3** | D5 `SWEEP_ENFORCED` true. D6 `PR Size` red at 80 or more files. D1 enforce, one lane per date: GPT and Opus first, then Security Scope, First Principles, Design, UX last. | The three remaining enforcement changes | Each D1 lane flips only when its replay and shadow numbers meet the acceptance of section 4.1.7. The D1 part of this row is superseded by the D1 amendment in section 2.2: D1 enforces directly on all lanes from the first merge, not one lane per date and not gated on replay or shadow numbers. |

### 7.2 Why this order

1. Measure before changing: D8 and the D1 replay change no behaviour and give
   every later step a baseline. The D1 replay in this reason is superseded by the D1
   amendment in section 2.2: there is no replay.
2. D5 and D4 add bytes to `SKILL.md`, which has 14 spare, so release 0 frees
   room first.
3. Checks start as warnings for 14 days and turn red only for pull requests
   opened after a stated date, so no open pull request turns red for a section
   it never had.
4. D1 is the largest change in blast radius. It starts as text printed beside
   the old verdict, and each lane flips on its own evidence. UX goes last
   because its category (`ux_copy_or_layout`, 298 loops) is the most
   taste-dependent. Superseded for D1 by the D1 amendment in section 2.2: direct
   enforcement on all lanes, safety is a revert by pull request.
5. D2's shadow job costs a runner slot and nothing else, so the 88-push
   baseline can be compared with live data before the `needs` edges land.

### 7.3 Exit codes, assigned once

Existing codes of `diff_signals.py --check-body` (`diff_signals.py` lines 40-42):
0 passes, 20 an area unaccounted for, 21 `What changed` over the word limit, 22
template sections missing, 2 environment error. The prepare-pr scripts already
use 0, 1 to 3 as phase markers, 10, 20, 21, 22, 30, 40, 41, 64 and 2
(`SKILL.md` script table, lines 265-276). This RFC adds exactly these:

| Code | Script | Meaning | Decision | Mode |
|---|---|---|---|---|
| **23** | `diff_signals.py --check-body` | `## Sibling sweep` missing or malformed on a `fix`/`revert` body | D3 | Printed in warn; returned from the date the CI step turns red |
| **24** | `diff_signals.py --check-body` | `Proof:` line missing from `## Tests`, or a `PROVEN` line that disagrees with `<git-dir>/prepare-pr-prove.json` | D4 | same |
| **25** | `diff_signals.py --check-body` | A `Gate proof:` line is present and contradicts the recorded proof. An absent line never returns 25 | D7 | Advisory: returned only for a contradicting line |
| 50, 51, 52 | `pre_push_gate.py` | A gate failed; a gate could not run and no skip covers it; `--verify` found no proof | D7 | Tool exit codes. No other script reads them |
| 30 | `pr_findings.py --rounds` | Unchanged name ("retrospective due"); fires on a span fixed in two earlier heads, and no longer on re-answers | D5 | Reshaped |
| 40 | `push_guard.py` | Existing "refused" code; `sweep_check.py` runs inside it | D5 | Warn, then refuse |
| 11 | `prove.py` | New: the changed tests are frontend tests, which `prove.py` cannot run (today it returns 10, "nothing to prove") | D4 | Informational |

`diff_signals.py` evaluates in order: `rc or tpl or sibling or proof or
gate_proof`, so the first non-zero code wins and the codes are never combined.
In CI the same checks exit 1 with an `::error::` line, as the Pattern harvest
step does. Design B used 23 for a different check, and design F moved it to 25;
this is the single assignment.

## 8. Open questions for the team

| # | Question | Default if the team has no view |
|---|---|---|
| 1 | **D1: option (a) (three tiers plus tag) or option (b) (PASS or BLOCK)?** | (b), after the replay. Superseded by the D1 amendment in section 2.2: (b), with no replay; direct enforcement on all lanes, safety is a revert by pull request. |
| 2 | **D1: where does the impact floor sit?** Does a cosmetic but observable wrong outcome (a wrapped label, a wording slip) block, or become a note? | A note, unless the outcome makes something unreadable or unusable |
| 3 | **D1: do the premise lanes (Design, First Principles, UX) keep their own BLOCK triggers, with Trigger and Outcome written as "the situation" and "the cost carried", or does D1 use a code-lane shape for all?** | Keep their triggers, require both fields |
| 4 | **D1: the independent-writer rule.** Is 24 hours the right wait before an owner may record `owner-decision`? Can the workflow token read CODEOWNERS team membership (`GET orgs/<org>/teams/<team>/memberships/<login>`)? Unknown today; one call from a workflow would settle it. Who is "owner" when the only available writer is the PR author? | 24 hours; `owner-decision` links a comment and the weekly audit lists it |
| 5 | **D1: is a Design BLOCK a sensitive class?** Design D listed it; the repository owner's list names security, data loss, crash and guard removal only. | Not sensitive |
| 6 | **D1: may the GPT ratchet rule ("a ratchet may only tighten") exempt `xfail(strict=True, reason="GH #N ...")` pins that name an open `deferred-finding` issue?** A pin is needed for a sensitive-class deferral. Design D saw one override already caused by this clash. | Yes, for pins that name an open issue |
| 7 | **D1: keep the adjudication downgrade for non-sensitive BLOCKs?** Two of the nine bugs that shipped past a BLOCK went through it. | Keep, with a mandatory `Downgraded:` line |
| 8 | **D2: full lint-first, or `backend-test` only?** Is a median 8.1 min (about 15% of CI's median wall clock) acceptable for at most 0.7% of job-minutes? | Backend shards only, as the first step |
| 9 | **D3: warn 14 days or red at once, as Pattern harvest was? Should `revert` titles skip the section?** | Warn 14 days; no skip |
| 10 | **D4: accept `not provable -- frontend test` for 20.6% of bug fixes until a vitest runner exists?** | Accept for a month |
| 11 | **D6: is `write` the right owner bar, or only `maintain` and `admin`? May a `write` author approve their own pull request? Is a +25% growth rule right? Is reversing the statement in `pr-scope.yml` acceptable for this narrow tail? Should the age flag mention the CODEOWNERS team?** | `write` and not the author; +25%; yes; label only |
| 12 | **D5: refuse or warn when the shadow period ends?** What is the right cap for the lane-settled wait (45 minutes covers the p90 completion of 42.4 min)? Is a fallback to "fixed in two heads, classes unknown" too eager when a file carries many unrelated findings? | Refuse for the sweep; cap 45 minutes |
| 13 | **`SKILL.md` ceiling:** keep the 56,000-byte limit and move text out, or raise the limit? | Move text out |
| 14 | **D8: may the weekly census use about 10,000 REST calls, on its own token?** Does the organisation already have Actions usage reports that make the census unnecessary? What is the dollar cost per runner-minute (CodeBuild and hosted)? Which accounts are agents (it sets the scope of M1 to M3)? | Own token; use usage reports if they exist |
| 15 | **Process:** does the team want a tracking issue before this RFC merges? The Issue Gate is paused (`issue-gate.yml` lines 6-12), so none is required today. | No issue |

## Appendix A. Where each number comes from

| Claim | Source |
|---|---|
| 6,560 loops, 1,980 PRs, 676 with none, 150 with 10 or more | `aggregate.json` `totals`, `loops_per_pr`, `runaway_prs`; `SYNTHESIS` section 1 |
| Cause classes and shares | `aggregate.json` `loops_by_normalized_category` |
| Who owns the loop | `aggregate.json` `main_moving_vs_author_owned`; `SYNTHESIS` section 7 |
| `preventable` counts | `aggregate.json` `preventable` |
| Bug classes and origins | `aggregate.json` `bugs_by_normalized_failure_class`, `bug_origins`, `bug_failure_class_detail.deferred_risk` (282 bugs, 93 batches, 277 with an introducing PR) |
| Release paths, override and deferral authorship, rubric sample | design D sections 1, 3, 4.3 (path labels are the designer's reading of the saved comments) |
| Avoidable pushes, job-minutes, lint durations, M1 to M5 | design F section 1 (7,876 pushes from 8,954 saved check-run files) |
| Lint-red subset (88 pushes, 25,571 heavy job-minutes) | computed for this RFC from design F's saved data (`code-quality/rfc/analysis/lint_first_savings.py`) |
| Sibling and Pattern harvest numbers | design F section 2; design A section 0 |
| `## Tests` and `prove.py` numbers | design F section 3; design C part II |
| Repeat-fix rounds, lane mix, lane timing | design E sections 3 and 6 |
| Size thresholds and override mechanics | design F section 4; design C part III |
| Repository file and line claims | `origin/main` commit `2cf3d0d69`, read for this RFC |

## Appendix B. Lint-first, the two groups of avoidable pushes

Computed from design F's saved check-runs for the 192 avoidable pushes
(10.5-day window, job-minutes are summed job durations).

| Group (by the red jobs of the push) | Pushes | Pull requests | Job-minutes, all jobs | Job-minutes, heavy test and build jobs |
|---|---|---|---|---|
| Backend or Frontend Lint among the red jobs | 88 | 76 | 34,525 | 25,571 |
| Other red jobs only (PR Hygiene, Bundle Size, Static Ratchet, ...) | 104 | 89 | 23,096 | 13,120 |
| Total | 192 | | 57,621 | 38,691 |

The totals differ from design F's 57,613 by rounding of per-job durations.

## Appendix C. Override classes (D1)

Grammar: `/ai-review override <lane> <sha> <class>[ #N]: <reason, 20 to 500 characters>`.
The old grammar stays valid for six weeks, with a warning.

| Class | Use | Extra requirement checked by the handler |
|---|---|---|
| `false-positive` | The finding is wrong | Reason of at least 20 characters |
| `not-applicable` | The rule does not cover this change | Same |
| `pre-existing` | Not caused by this diff | `#N` or a commit sha that shows it on `main` |
| `evidence-supplied` | A UX evidence gap is closed | A URL in the reason |
| `infra-failure` | The lane failed, not the pull request | A workflow run URL |
| `reissue` | The same ruling for a new head | The source comment id of the earlier record |
| `deferred` | A real risk, fixed later | `#N`, open, labelled `deferred-finding`, assigned, with a `Due: YYYY-MM-DD` line or a milestone date, checked in the same run; otherwise refused. For a sensitive class, also a pin test (open question 6) |
| `owner-decision` | A named person accepted the risk | The actor is a CODEOWNERS member, and the value is a link to that owner's own decision comment (the actor may be its author); the fallback after the time box of 4.1.4. Whether the owner may be the PR author is open question 4 |

Refusals added to the handler: an unknown class or a missing requirement;
the independence rule for the sensitive anchors; an `agent:` reason on those
anchors. Unchanged: writer permission, exact head, the 500-character cap. The
record is kept as a second hidden line in the bot comment (class, issue,
whether the actor is the PR author, anchors). Of 833 override records today,
about 156 (18.7%, by keyword) are re-issues for a new head and about 149
(17.9%) are UX evidence overrides; these are not risk releases and each has a class that needs no
issue.

## Appendix D. Formats for D3 and D4

Sibling sweep (D3), on `fix` and `revert` titles, read outside code fences and
HTML comments:

```markdown
## Sibling sweep

Swept: <optional in release 1: the search you ran>
Sibling: <path>[:<line>] -- changed here | tracked in #<n> | not affected -- <why, 8+ characters>
```

or `Siblings: none -- <why, 8+ characters>`. Checks, none needing a network
call or a model: the heading exists; at least one `Sibling:` or one `Siblings:
none` line; each `Sibling:` line parses; a `changed here` path is in the
changed-file list; every path exists in the head tree or the diff; no duplicate
path and at most 40 lines. Not checked: that the list is complete, that "not
affected" is true, that `#N` exists.

Proof line (D4), inside `## Tests`, on `fix:` titles only:

```markdown
Proof: PROVEN <the new tests that fail without the fix> (head <sha12>)
Proof: not provable -- <reason>
```

Allowed reasons, a closed list so a weekly report can count them: `frontend
test (prove.py runs Python only)`, `test-only change`, `copy-only change`,
`docs or config only`, `prove.py timed out`, `other: <20+ characters>`.

## Appendix E. Sweep file and checks (D5)

`<git-dir>/prepare-pr-sweep.md`, never committed:

```markdown
## span=<12 hex>
class: a git hook can come from a source other than core.hooksPath
search: git grep -nE 'hooksPath|fsmonitor|includeIf|\bhook\.' -- 'src/**/*.py'
sites:
- <path>  fixed
- <path>  covered-by <test id>
- <path>  not-in-class -- <reason>

## span=<12 hex>
class: a recovery path leaves state behind after one transient error
search: none -- ordering across three writers, no common token
branches:
- <case>  covered-by <test id>
- <case>  fixed
```

| `sweep_check.py` rule | Result |
|---|---|
| The pattern matches nothing in the pre-fix tree (vacuous) | Refused |
| A hit no site line accounts for | Refused, with the hit list |
| More than 25 hits | Refused as too broad |
| `covered-by` names a test id that is not in the diff | Refused |
| `not-in-class` with a reason under 20 characters | Refused |
| `search: none` with fewer than two branch rows | Refused |
| A pattern that starts with `-` | Refused, and never passed to git as an option |
| Warn mode | Prints the same findings and exits 0 |

The pattern reaches `git grep` as data, never through a shell.

## Appendix F. Size thresholds (D6)

Same 1,980 pull requests. "Held" is the share of all loops (or rebase loops) in
the flagged pull requests, an upper bound on what a flag could save. "Quiet" is
the share of flagged pull requests that merged within 12 h with at most one
loop (a false alarm).

| Rule | PRs | % of PRs | Rebase loops held | All loops held | Quiet |
|---|---|---|---|---|---|
| lines >= 1,000 / 2,000 / 5,000 | 381 / 241 / 171 | 19.2 / 12.2 / 8.6% | 49.3 / 30.0 / 20.8% | 43.2 / 22.3 / 11.9% | 37.8 / 53.9 / 62.0% |
| files >= 25 / 50 / 80 | 239 / 45 / 17 | 12.1 / 2.3 / 0.9% | 39.2 / 15.3 / 5.0% | 33.9 / 10.7 / 4.1% | 15.9 / 20.0 / 17.6% |
| open >= 72 / 120 / 168 h | 148 / 71 / 49 | 7.5 / 3.6 / 2.5% | 37.7 / 27.9 / 24.0% | 30.9 / 19.7 / 16.9% | 0% |
| **files >= 80 or open >= 168 h** | **65** | **3.3%** | **26.9%** | **19.7%** | 4.6% |
| files >= 50 or open >= 168 h | 91 | 4.6% | 31.9% | 24.1% | 9.9% |

| Level | Rule | Effect |
|---|---|---|
| Hint | 50 or more files (45 PRs, 2.3%) | `diff_signals.py` prints `! SIZE`; lines are shown, never used |
| Flag | 80 or more files, or 168 h open (65 PRs, 3.3%) | Label and one comment |
| Stop | 80 or more files (17 PRs, 0.9%) | `PR Size` red until `/pr-size approve` |
| Age | 168 h open (49 PRs, 2.5%) | Never red; the comment says "merge, split or close" |

## Appendix G. Baselines by decision

| Decision | Measure | Baseline | Source |
|---|---|---|---|
| D1 | `deferred_risk` bug-fix PRs | 282 of 1,162 (24.3%) | `bugs_by_normalized_failure_class` |
| D1 | Set-aside risks whose lane said BLOCK / advisory | 9 (3.2%) / 201 (71.3%) | design D section 1 |
| D1 | Overrides by the PR author | 818 of 833 (98.2%) | design D section 3.3 |
| D1 | Overrides with a class | 0 of 833 | design D section 9 |
| D1 | Path-b risks with no reply | 49 of 282 (17.4%) | design D section 9 |
| D1 | Merges past a non-green `PR Readiness` found | 3 | design D section 9 |
| D2 | Heavy job-minutes in lint-red avoidable pushes | 25,571 per 10.5 days | Appendix B |
| D3 | `partial_fix` loops per fix PR; `partial_fix` bugs | 0.47; 234 | design F section 2.6 |
| D4 | `canonical_path_tests` plus `missing_test` loops | 325 (5.0%) | `aggregate.json` |
| D5 | Repeat-only fix rounds; third-or-later fix rounds | 142 (12.2%); 72 | design E section 3.2 |
| D6 | PRs flagged; rebase loops held | 65 (3.3%); 26.9% | Appendix F |
| D7, D8 | M1 to M5 | section 4.8 | design F section 1.4 |

## Appendix H. Files by release

| Release | Files (existing unless marked new) |
|---|---|
| 0 | `.../kirocrew-prepare-pr/SKILL.md`, new `references/seam-protocol.md`; `.github/scripts/fix_loop_metrics.py`; `.github/workflows/ci.yml` (`lint-gate`, shadow) |
| 1 | `.github/PULL_REQUEST_TEMPLATE.md`; `.github/workflows/code-review.yml`; new `.github/scripts/sibling_list_check.py`, `proof_line_check.py`; `.../scripts/diff_signals.py`, `prove.py`, `pr_findings.py`; new `scripts/pre_push_gate.py`, `scripts/gate_map.json`; lane prompts and workflows listed in 4.1.6 (shadow line only); `AGENTS.md` |
| 2 | `.github/workflows/ci.yml` (`needs` edits); new `.github/workflows/pr-size.yml`, `pr-size-override.yml`; `.github/workflows/pr-readiness.yml`; new `.../scripts/sweep_check.py`, `regen_shared.py`; `.../profiles/kirocrew.json` (`regen[]`) |
| 3 | `push_guard.py`; the lane gates and parsers of 4.1.6; `ai-review-human-override.yml`; new `.github/scripts/release_record.py`; `docs/ci/ci-and-reviews.md`; tests named in each decision |
