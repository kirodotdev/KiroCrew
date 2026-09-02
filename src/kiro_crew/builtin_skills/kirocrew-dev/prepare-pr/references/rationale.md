# prepare-pr — why the rules are shaped this way

`SKILL.md` carries only what the loop executes. This file carries the evidence and
the design history behind those rules. Read it when you need to justify a deviation,
audit a rule, or decide whether a rule still applies — not on every skill load.

`references/gate-floor.md` is the companion file for the gate list specifically.

## Why the loop is one bounded cycle, never a patch-in-place

Resolving findings locally first (Phase 2 mirrors the server reviewers before any
push) cuts CI cost and wall-clock, and keeps the findings and reasoning in-session
for better fixes. Local-green does **not** guarantee server-green — same model,
different harness — so Phase 3's server poll stays the backstop rather than the
primary gate.

A failed server check re-enters Phase 1 rather than patching in place because base
movement between rounds is the common case on a repo carrying 175+ open PRs. Patching
in place produces a fix validated against a base that no longer exists.

## Why 10 iterations is a backstop and 3 is the real limit

The escalation triggers fire at ~3 rounds. That makes 10 unreachable in any healthy
run: a loop that reaches 10 has already missed a trigger. It is retained purely as a
runaway guard, and `SKILL.md` says so, because a cap presented as a target invites
using it.

**Same-span recurrence needed its own trigger** because the count-based rule cannot
see it. When each round closes exactly the finding it was given and receives one new
blocker in the *same* `file:function` span, the failing-check count stays pinned at
1 — it never rises and never falls, so "3 iterations with no drop" never fires. This
is the most expensive round pattern measured on this repo, and in every instance the
correct structural fix had already been written down mid-flight and then deferred.

## Why the two Phase 0 gates come before opening

Measured across the 20 slowest PRs on this repo, rounds spent before the decision
gate and the file-overlap gate were the largest single waste class. The worst case
reached full green and was then parked by a one-line product hold — every round of
that work was discarded.

The file-overlap gate's `--limit 500` is load-bearing because `gh pr list` returns
30 rows by default. With 175+ open PRs the default silently checks about a sixth of
them and the gate reads as passing.

## Why the fetch in Phase 1 must succeed

Operating on a stale `origin/<base>` ref was the root cause of the 2026-07-31
clobber, where a force-push replayed 114 duplicate commits. The fetch therefore fails
closed rather than proceeding on the cached ref.

## Why force-with-lease must be SHA-pinned

The implicit form (`--force-with-lease` with no SHA) silently accepts a
just-fetched remote-tracking ref, so a maintainer commit pushed between iterations is
overwritten without the lease firing. The pin must record the remote tip *before*
any fetch/rebase/squash that could advance the ref.

The pre-squash ancestor check (`git merge-base --is-ancestor origin/<branch> HEAD`)
is not re-run after the squash because it can never pass on a rewritten branch — the
old remote tip is not an ancestor of the new squashed commit. Re-running it there
would fail every iteration for a structural reason unrelated to safety.

## Why the profile is read from the base ref

The profile declares which reviewers run and which contract each mirrors. A branch
that could edit its own profile could drop the lane that reviews it. Reading
`.prepare-pr.toml`, the Kiro Crew markers, and the workflow globs from the base ref
closes that. A ref resolving to nothing is a hard error rather than a silent fall
back to the checkout, because the fall-back case is exactly the attack.

## Why the gate list is data, not prose

A gate an LLM has to notice in a paragraph is followed exactly as unreliably as the
gates this loop kept missing. `test/test_prepare_pr_profiles.py` pins the floor to
`ci.yml`: every script, npm script and tool `ci.yml` runs must appear in `gates[]` or
be named exempt with a reason, and every gate must name a target that exists. CI
gaining a blocking scan therefore fails that test instead of surfacing as a review
round on a later PR.

Narrowing **within** a surface needs a real import graph, not a text scan, so
`run_scoped_tests.py` deliberately does not attempt it. It only replaces the *other*
surface's full suite with the cross-surface set `ci.yml` runs for a single-surface
diff — measured at 350 backend files or 146 frontend specs, against 62k collected
backend tests and ~1.4k frontend specs that are not worth re-running serially on ten
inner-loop iterations for a signal CI produces on the merge ref anyway.

## Why the extraction fallback must announce itself

`local_review.py` exists to stop the hand-written charters from drifting away from
the CI workflows they mirror. A silent fallback to those charters reintroduces
exactly the drift the script prevents, so exit 40 requires a visible WARNING and a
fix to the extractor rather than a quiet degradation.

The charter budgets are hand-copied from CI and that copy is what drifted before —
the skill claimed ≤2 BLOCKING long after CI moved to 5.
`test_charter_budgets_match_the_ci_workflows` now pins the wording to
`.github/review-prompts/opus-validate.md` and `gpt-review-core.md`. The GPT lane
carries no numeric cap on purpose: a numeric budget encouraged staging discoveries
across review rounds.

## Why proportionality is a separate question from legitimacy

Legitimacy is necessary but not sufficient. A finding correct in the abstract can
still demand a change out of proportion to the PR's purpose — speculative hardening
against inputs that cannot occur, robustness for a single caller, gold-plating an
internal tool as if it were a public API. Appeasing those is the mirror image of
appeasing a false positive and costs the same.

Both keep-the-code outcomes record as `rebutted` because the *action* is identical —
the code does not change and a reply goes on the thread. Only the argument differs.
Earlier drafts made push-back a separate disposition, which produced dispositions
that were indistinguishable in effect and a taxonomy the agent had to reason about
instead of applying.

## Why `needs-a-decision` is not `accepted-and-deferred`

`accepted-and-deferred` files an issue. An issue whose body asks the maintainer to
choose between options is not a task: no contributor can act on it until the choice
is made, so it occupies the tracker indefinitely while the question inside it goes
unread. `needs-a-decision` puts the question where it will be answered and files
nothing. `test/test_deferred_disposition_ratchet.py` enforces that every surface
offering the first also offers the second.

## Why every concern must be answered, including advisory ones

`Design Review 🟡 CONCERNS` and `UX Review 🟡 CONCERNS` post their concern *and* pass
the check. The readiness rollup will therefore never force an answer, and nothing
else in the loop nags. To a human maintainer a silently ignored concern reads as
"the author never looked at it", regardless of how green the checks are.

Design Review owns the long-term-reversibility lens. It is advisory and reds only on
a genuine `BLOCK`, but an irreversible choice it flags needs a written justification
a reviewer can read — which is why it is fix-or-justify rather than a raw Medium.

## Why the disposition comment is scoped twice

The reviewer's adjudication ledger keeps the marker, the `> ` rationale lines, and
the `- **...**` title bullets, and scopes a ruling's coverage by its recorded
rationale. So `target=` naming a single lane keeps a Design concern from riding along
on the GPT comment, and one-rationale-per-finding keeps a reused reason from silently
claiming findings it was never checked against. A rationale reused across several
findings is the blanket line from "Common mistakes" with a marker on top of it.

## Why the PR body must come from the template file

The maintainer's auto-approval bot greps for the template's exact heading strings.
`## Problem` instead of `## Problem / Motivation`, or `## Fix` instead of
`## What changed`, blocks workflow approval indefinitely. There is no bundled copy
because the repo's file is the single source of truth and the skill always runs
inside a checkout.

## Why screenshots live in `temp-screenshots/`

`docs/` and `src/kiro_crew/**` ship in the wheel, the sdist, and the desktop DMG, so
review images placed there ride into a shipped artifact. `temp-screenshots/` is
outside every packaged path and is pruned periodically — long enough for the PR to be
reviewed.

SHA-pinned URLs are required because branch-pinned URLs break when the branch is
deleted on merge, and external image hosts leak content and are camo-blocked for
private repos. The pinned blob stays reachable through the historical commit even
after cleanup removes the file from `main`'s tip.

## Why the closing-keyword check reads the API back

The host resolves the link at PR-open/edit time and exposes it as a field, so
`closingIssuesReferences` is ground truth and the prose you just wrote is not.
Merging with no closing keyword is the leak with the longest tail: nothing reconciles
it afterwards, so the work ships, the issue stays open, and the next person to read
that issue plans against stale information.

`pr_status.py` owns the parsing so the agent does not have to model Markdown:

- A trailer is read as a complete visible line of `<keyword> <reference>` pairs; a leading bullet, a trailing `.`, and a trailing HTML comment are tolerated.
- Text inside a fenced block, an inline code span, or an HTML comment is masked before classification, and a trailer indented four or more columns is never read as a declaration — four columns is Markdown's own code boundary, and a tab reaches it. That single bound is what makes a copied example safe regardless of what precedes it.
- The bias is deliberately toward refusing: withholding credit from an oddly-indented trailer at worst prints an advisory notice, while crediting an example silently suppresses one.
- Reconciliation runs in the inverse direction too, matching on repository **and** number. A bare `#<n>` resolves to this PR's own repository, so a stale `Fixes other/repo#7` no longer vouches for a resolved closure of this repository's `#7`. Where either side's repository is genuinely unknown the match stays wide, so the notice fires only on a real disagreement.

## Why the check rollup is fetched separately

`statusCheckRollup` needs Checks read access, which a fine-grained PAT structurally
cannot grant, and GitHub resolves each `gh ... --json` request atomically. Both
scripts therefore fetch the rollup in its own `gh pr view` call. On failure they
print `NOTICE: CI check status UNAVAILABLE ...` and continue with an empty rollup
rather than aborting. The rollup read re-fetches `headRefOid` and is discarded
(`NOTICE: CI check status DISCARDED ...`) when a concurrent push moved the head
between the two reads, so one head's metadata is never paired with another head's
checks.

Both states are deliberately distinct from a genuine "no checks yet":
`pr_status.py` still fails closed at exit 20 but with a `CI status unreadable ...`
reason naming the environment cause, while `no CI checks reported` is reserved for a
healthy read that truly returned zero checks. A loop comparing `progress_key.status`
can then tell an environment gap from a code blocker.

## Why `${VAR:-default}` cannot appear in a path position

An agent safety filter resolves `$HOME` but cannot statically evaluate a `:-`
default, so it refuses the whole call as an *"unresolved shell variable in path
position"* and ends the turn before any script runs. The unresolved value taints
every path derived from it, so splitting the assignment across lines does not help.

## Why the driver is `monitor_start` and not a cron

A turn is capped at 2 hours and a CI round here costs 20–40 minutes, so an in-turn
poll loop reliably hits the cap around iteration 3–4 — losing the loop, though not
the work. `monitor_start` gives each round a fresh turn and survives a tab close or
gateway restart.

Cron and heartbeat cannot drive the fix loop, and both report success while doing
nothing. A cron has no owning slot, so its tool calls hit a deny-by-default approval
path and time out after 180s without a global auto-approve grant — and a denied tool
inside a completed turn still records `last_status: ok`. A real PR watcher logged 101
runs over 25 hours with 23 approval blocks, zero pushes, and a healthy-looking
registry. Heartbeat runs under a strict name allowlist (`HEARTBEAT_SAFE_TOOLS`) with
no shell and no `git push`, so it cannot amend a commit at all.

`pr_watch` is exempted only for a pure-watch stretch because it reads no comment
bodies. A round is complete when every check finished **and** every bot posted, and
`pr_watch` cannot see the second half of that condition.

## Why arming cannot be confirmed from the reply

Arming happens when the turn's *result* is processed, so a successful
`monitor_start` can only ever come back as *requested* — the tool says so itself. A
synchronous refusal is visible before the turn ends and is real. The residual case —
the applier refusing after the turn ends — is unobservable from inside the turn by
construction and shows up as a cycle that never arrives.

Treating a bare *requested* as an arming failure fires the `wait` fallback on every
arm and reinstates the very timeout the `monitor_start` branch exists to remove,
which is why `SKILL.md` states the two branches as one rule with an explicit default.

`max_cycles` counts cycles, not rounds. One 20–40 minute round costs several
5-minute cycles, so the default expires after roughly the first two or three rounds
and deactivates the loop silently, well short of the 10-iteration backstop.
