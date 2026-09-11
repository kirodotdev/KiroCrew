---
title: Security Conductor — proactive vulnerability discovery as a conductor use case
status: partial
author: zejiangg
created: 2026-09-07
last-audited: 2026-09-10
audited-at: f6d38741c
doc-pr: 9195
implementation-prs: [9270, 9271, 9273, 9362, 9332, 9495, 9499, 9500, 9813]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Security Conductor — proactive vulnerability discovery as a conductor use case

A dedicated `kirocrew-security-conductor` agent and a `security-conductor` builtin skill that run
proactive vulnerability discovery as a supervised worker fleet: one auditor per attack surface, an
independent verifier per finding, and an optional fixer lane behind a human gate. The skill is the
operating procedure of record; this document carries the intent and the decisions.

**Status.** M0 and M1 are on main. The agent is registered — `kirocrew-security-conductor` in
`UNADVERTISED_AGENTS` (`src/kiro_crew/subagent.py`) with its installer
`_install_security_conductor_agent` in `src/kiro_crew/agent.py`
([#9273](https://github.com/kirodotdev/KiroCrew/pull/9273)) — and the skill plus the first rules of
engagement are on main as `src/kiro_crew/builtin_skills/security-conductor/SKILL.md` and
`rules-of-engagement.json` ([#9271](https://github.com/kirodotdev/KiroCrew/pull/9271)), whose merge
is the human review of the rules of engagement that M0 required as its exit criterion. M1 is
complete: the SQLite findings ledger and its CLI landed as
`src/kiro_crew/builtin_skills/security-conductor/scripts/ledger.py`
([#9270](https://github.com/kirodotdev/KiroCrew/pull/9270), with its test guard corrected in
[#9362](https://github.com/kirodotdev/KiroCrew/pull/9362)); the three evaluator scripts
`scope_check.py`, `finding_entry.py` and `verify_finding.py` merged
([#9332](https://github.com/kirodotdev/KiroCrew/pull/9332)); the `golden_paths` table, its committed
export `golden-paths.json` and `scripts/verify_fix.py` merged
([#9500](https://github.com/kirodotdev/KiroCrew/pull/9500)); and `scripts/deny_diff.py` with its
three-platform CI gate merged ([#9499](https://github.com/kirodotdev/KiroCrew/pull/9499)). What was
left after those four was wiring rather than machinery, and it is what this document's own text
already required: the skill has to cite `verify_fix.py` as the fixer lane's acceptance gate, the
`forbidden` rules of engagement have to carry the cross-platform row, and the denial differential has
to read the security-conductor's own corpus instead of a second fixture that could disagree with it.
That wiring landed as [#9813](https://github.com/kirodotdev/KiroCrew/pull/9813). M2 and M3 are unstarted, so no pilot round has run and no fixer has been
dispatched.

## What a security conductor is

The third instance of the conductor pattern, after `kirocrew-conductor` (free-form goals) and
`kirocrew-pipeline-conductor` (one repository pipeline). Same shape — **agent + agent skills**: the
skill carries the procedure, bundled zero-token scripts carry the bookkeeping, the agent carries the
judgment. The conductor decomposes an audit into surfaces, dispatches one child session per surface,
verifies each finding through a script rather than by reading a transcript, adjudicates severity, and
reports upward. It never touches a target itself.

Three child roles:

- **Auditor** — one per attack surface. Static review plus a unit-test-level proof of concept in a
  disposable local checkout. Emits one structured finding file per candidate. Runs the installed
  `security-assistance` (ARCC) skill before starting, so governance search happens before any probe.
- **Verifier** — one per finding, independently re-runs the PoC. It exists specifically to reject
  false positives. Hallucinated vulnerabilities are the dominant noise source in agentic security
  review, so every finding gets a second, independent rejection pass before a human sees it.
- **Fixer** — optional, only for a verified High or Critical, dispatched only after a human yes. Runs
  the `prepare-pr` skill; acceptance is PR checks green **and** `scripts/verify_fix.py` exit 0, which
  is the golden-path half described below. Checks green alone is not acceptance.

## Why

Kiro Crew's security posture today is reactive: a deny classifier, a governance ceiling, and human
review of what lands. Nothing looks for the next hole. The work of looking is fan-out over
independent surfaces with a high false-positive rate and a hard blast-radius constraint — which is
the conductor shape exactly: parallel workers, script-computed verification, one adjudicator, human
gates at the two points where a wrong call is expensive.

Two properties make this a conductor rather than a cron scanner:

1. **The verification pass is the product.** A scanner emits findings; nobody trusts them. A
   conductor's second, independently-dispatched verifier is what turns a claim into evidence, and
   deciding what to re-verify and when to escalate to a human is judgment.
2. **Aggression must be bounded, and the bound must be checked.** A prompt asking an agent to be
   careful is not a control. The bound belongs in data the conductor evaluates with a script.

## Verified facts this plan rests on

The pattern facts were measured at `e992b7771`; the shipped-state facts were re-measured at
`acc99f217`.

- The conductor pattern is now shipped three times, with one standalone installer each in
  `src/kiro_crew/agent.py` (`_install_conductor_agent`, `_install_pipeline_conductor_agent`,
  `_install_security_conductor_agent`), a filename constant each in `src/kiro_crew/agent_files.py`,
  and roster hiding through `UNADVERTISED_AGENTS` in `src/kiro_crew/subagent.py`, which carries four
  entries at `acc99f217` — `kirocrew`, `kirocrew-conductor`, `kirocrew-pipeline-conductor` and
  `kirocrew-security-conductor`.
- "Never does the work itself" is already expressed as a spec property, not a prompt request:
  `_install_pipeline_conductor_agent`'s `tools` list omits both `fs_write` and `code`, and
  `execute_bash` is mounted but never auto-approved because `allowedTools` has no argument matching.
  The security-conductor installer keeps the same shape.
- The bundled-script half of the pattern is shipped for the pipeline conductor
  (`claim_preflight.py`, `fleet_probe.py`, `credit_spend.py` under
  `src/kiro_crew/builtin_skills/pipeline-conductor/scripts/`) and now for this one as well: all five
  of `ledger.py`, `scope_check.py`, `finding_entry.py`, `verify_finding.py` and `verify_fix.py` are
  on main under `src/kiro_crew/builtin_skills/security-conductor/scripts/`, beside the committed
  `golden-paths.json` corpus.
- **SQLite is this repository's established store for durable agent knowledge**, which is the pattern
  the findings ledger mirrors. `src/kiro_crew/memory.py` keeps `memory_index.db` beside the workspace
  config; `src/kiro_crew/vector_memory.py` keeps `memory.db` in WAL mode behind a `schema_version`
  table and a `_MIGRATIONS` ladder; `src/kiro_crew/knowledge/store.py` owns its own database with
  `CREATE TABLE IF NOT EXISTS` DDL and one connection per thread. All three import SQLite through the
  `src/kiro_crew/_sqlite_compat.py` shim rather than the stdlib module directly, and `data_home()` in
  `src/kiro_crew/config/paths.py` is where a new store's path is resolved from. `ledger.py` follows
  that shape: a `schema_version` table plus `CREATE TABLE IF NOT EXISTS` DDL for the four data
  tables.
- The fixer lane has a procedure to reuse:
  `src/kiro_crew/builtin_skills/kirocrew-dev/prepare-pr/SKILL.md`.
- The three pilot surfaces are real code, not hypotheticals: the deny classifier `is_denied` in
  `src/kiro_crew/security.py`, reached through `src/kiro_crew/platform/security_authority.py` from
  the PreToolUse gate in `src/kiro_crew/hooks.py`; webhook ingest in `src/kiro_crew/webhooks.py`;
  dashboard session and bearer handling in `src/kiro_crew/dashboard/token_auth.py`.
- What this document proposes and main does **not** have at `f6d38741c`: the M2 pilot round, the
  retrospective lane that rules on `policy_block` events, the harness that executes `flow` and
  `cron` golden paths, and the M3 fixer lane. Every M1 script and both gates are on main.
- The `security-assistance` (ARCC) skill the auditor brief depends on is **not** a builtin in this
  repository; it is an installed skill. The shipped skill resolves this by treating an absent script
  or skill as `UNKNOWN` rather than as permission, so the dependency is an environment precondition
  the seed states, not a vendored copy.

## The agent

`kirocrew-security-conductor` is cloned from `kirocrew-conductor` and keeps every security invariant
that installer argues for:

- **No file-writing tool** — neither `fs_write` nor `code`. The conductor cannot edit a target, write
  a PoC, or patch a finding. That is a property of the spec, so it holds on unattended cycles.
- **Every auto-approval is a named verb.** Session creates and reads, the patrol loop's own
  lifecycle, and owner reporting are granted. Anything that mutates a peer session or starts new work
  (`session_send`, `session_stop`, `spawn_run`, `execute_bash`) stays mounted but gated. A security
  conductor ingests hostile-by-assumption content — its own auditors' findings — on unattended
  cycles.
- **Unattended operation is a session-level trust grant** by the operator, never a spec-level bypass.

Registered alongside `kirocrew-pipeline-conductor` in `UNADVERTISED_AGENTS`
(`src/kiro_crew/subagent.py`), with its own filename constant in `src/kiro_crew/agent_files.py` and
its own installer in `src/kiro_crew/agent.py`, mirroring the existing installer tests.

## The trust boundary

Every containment claim below is bounded by one fact: the verifier runs as the operator, on the
operator's machine, against a checkout the operator chose. There is no privilege drop between the
two. A checkout that can execute code already has everything the verifier has, so a defense against
a hostile checkout defends a boundary that does not exist. This RFC therefore draws the boundary in
one place and states plainly what falls outside it.

**Trusted, for host integrity: the target checkout.** Its code runs as the operator, and this
design does not defend the operator's machine from code the operator chose to check out — a
symlink out of the tree, a detached process, a report the checkout's own code rewrote. Those are
the operator attacking themselves, and no privilege drop exists to stop it.

**Defended regardless: verdict integrity.** Trusting the checkout with the host is not trusting
its exit status or its stdout with the verdict. A test suite that exits 1 because collection failed,
a `conftest.py` that prints `1 failed`, a command that dies on a missing import — none of these is a
reproduction, and the verifier reads its verdict from a structured report keyed to the requested
test, or from a deliberate exit status, precisely so that ordinary breakage cannot confirm a
finding. That discipline is about false positives, not about adversaries, and it stays.

**Untrusted: the auditor's finding.** The proof of concept is model-authored text, and the
conductor ingests it on unattended cycles. Screening it against the `forbidden` rules of engagement
before execution is the mechanism that makes "not too aggressive" checkable, and it stays. It is a
named-shape screen, and this RFC says plainly what that does and does not bound: it catches the
sloppy PoC -- an auditor that reaches for `curl`, names a credential path, or writes outside the
worktree -- and it does not catch an adversarially crafted one, because a `cmd::` proof is an
arbitrary program and `python -c` with `urllib` names none of the screened words. What bounds that
case is not this script. It is the two layers the RFC already places around it: the auditor is a
Kiro Crew session and its PoC is written under the session's own tool gate and, where the host
provides one, its sandbox; and the verifier runs as the operator's session, inheriting the same
containment the operator granted that session. A `cmd::` proof therefore executes with exactly the
privileges the operator gave the crew, no more. For the dogfood pilot -- the operator auditing the
operator's own repository -- that is the accepted bound. An operator who wants a stricter one has
two levers without any change here: disallow `cmd::` in the rules of engagement so only `pytest::`
proofs file, or run the crew under the host sandbox. A future RFC may add attended confirmation for
first-run `cmd::` proofs; this one does not.

**Containment is a disposable worktree plus a deadline, not an OS sandbox.** The verifier refuses a
`--worktree` that is not a git checkout, refuses the checkout it is itself running from, bounds the
proof's wall time, and reaps the process it started. It does not attempt kernel-level isolation.
Kiro Crew's own namespace sandbox is Linux-only and package-internal, and these scripts are
standard-library files with no package import, so reaching for it would trade a cross-platform
verifier for a Linux one. Where those two conflict, cross-platform wins.

**Out of scope, explicitly.** A verifier pointed at a checkout the operator did not author —
auditing a third party's repository, or a branch from an untrusted pull request — is not a supported
use. Nothing in this design contains such a tree, and a finding that assumes one is out of scope
rather than unfixed.

## The harness

Deliverables, following the four-piece shape the pipeline conductor ships:

1. **`skills/security-conductor/SKILL.md`** — the operating procedure: what qualifies as a work item
   (one attack surface, independently auditable, with a named PoC shape), the auditor seed template
   including the mandatory ARCC step, the verifier flow, severity adjudication, and stop conditions.
2. **`scripts/scope_check.py`** — is this path, repo or technique in scope? Reads the active
   `roe_rules` rows from the ledger. Exit codes are the interface, and an unresolvable answer is
   `UNKNOWN`, never permission. The conductor decides scope with this script, not by judgment.
3. **`scripts/finding_entry.py`** — dedupe and format. One finding per real defect, so a surface
   re-audited later does not re-file what is already recorded.
4. **`scripts/verify_finding.py`** — re-run one finding's PoC in a disposable worktree under a
   deadline (see the trust boundary above) and emit the verdict. The conductor reads the verdict;
   it never reads a verifier's prose and decides for itself.
5. **`scripts/ledger.py`** — the ledger CLI: init schema, add finding, record verdict, propose a
   lesson, approve a lesson, export the rules-of-engagement JSON, list. This is also the human's
   editing surface (see the learning section).
6. **`scripts/verify_fix.py`** — the fixer lane's acceptance gate: the finding's PoC is refused
   **and** every active golden path still passes. Exit codes carry which half failed.
7. **`scripts/deny_diff.py`** — the denial differential: classify every active golden-path shell row
   at the base commit and at the head commit, and report what the head newly refuses and which rows
   it deactivated.

Plus a first set of `roe_rules` rows, reviewed by a human before any auditor runs, and a first set of
`golden_paths` rows, reviewed on the same terms.

## Rules of engagement are machine-checked

"Not too aggressive" is enforced by a spec a script evaluates, not by prompt tone. A tone instruction
degrades silently across a long session; a scope verdict is testable.

The fields, held as `roe_rules` rows and exported as `rules-of-engagement.json`:

| Field | Contents |
|---|---|
| `scope` | Allowed repositories and paths. Everything else is out of scope by default. |
| `allowed_techniques` | Code review, dependency audit, local unit-level PoC tests. |
| `forbidden` | No production systems and no external hosts; no reading credential material; no denial-of-service and no broad fuzzing; no disabling safety protections; no attempting to bypass a policy block — a policy refusal IS the boundary; and no fix that introduces a code path usable on only one platform. |
| `severity_scale` | The adjudication vocabulary. |
| `report_schema` | The finding shape below, so a malformed finding fails at write time. |
| `human_approval` | Two gates: any active testing beyond static review plus a local unit-level PoC needs a human yes, and any fixer dispatch needs a human yes. |

The `forbidden` clause about policy blocks is the one that matters most in practice. An auditor whose
job is finding fence weaknesses will meet the fence, and the correct response is to stop and report,
never to rephrase around it.

**The JSON file is an export, not the source of truth.** The active `roe_rules` rows are, and
`scope_check.py` reads them directly. A scope or `forbidden` change is a row with a reason and an
approver, so every widening of what an auditor may do is attributable, and reverting one is flipping
`active` rather than editing a file nobody diffed.

## Finding schema and `finding-status/v1`

Each finding is one record: `id`, `surface`, `severity`, `title`, `affected paths`, `PoC` (a command
or a test), `verifier verdict` (`confirmed` / `rejected` / `needs-human`), `status`. Findings live in
the ledger described below.

Alongside them, a conductor-owned state record — `finding-status/v1`, analogous to
`conductor-status/v1` in [rfc-pipeline-conductor.md](rfc-pipeline-conductor.md). The session ledger
records the surfaces under audit. It does not record the conductor's own obligations, and those are
the ones that go missing: a finding awaiting a verifier dispatch, a verified High awaiting a human
answer on the fixer gate, an auditor's policy-block report awaiting a ruling. Each is reviewed every
cycle independently of what any probe fired, and an entry clears when the obligation is discharged,
not when it is decided.

## Pilot: dogfood Kiro Crew

Round 1 is this repository, two to three auditors, concurrency two to three:

1. **Safety-policy fence bypass** — the `execute_bash` deny classifier: `is_denied` in
   `src/kiro_crew/security.py` and its gate path through
   `src/kiro_crew/platform/security_authority.py` and `src/kiro_crew/hooks.py`.
2. **MCP gateway and webhook ingest input validation** — `src/kiro_crew/webhooks.py`.
3. **Session and bearer handling** — `src/kiro_crew/dashboard/token_auth.py`.

Every finding goes through the verifier before it is reported. Fix PRs only after a human approves
each one.

## Learning from past audits — the findings ledger is SQLite

A round that does not remember the last one repeats its false positives. Findings, verdicts, lessons,
the rules of engagement and the golden paths therefore live in **one SQLite database**, at
`<data_home>/security-conductor/findings.db`, mirroring the pattern the memory and knowledge stores
already use in this tree (see the verified facts above): a versioned schema, `CREATE TABLE IF NOT
EXISTS` DDL, and SQLite imported through `src/kiro_crew/_sqlite_compat.py`. Five tables:

```sql
findings     (id, surface, severity, title, paths, poc, auditor_verdict,
              verifier_verdict, final_verdict, status, created, round_id)
verdicts     (finding_id, role /* auditor | verifier | human */, verdict, reason, ts)
lessons      (id, kind /* true-positive | false-positive | missed | out-of-scope */,
              surface, pattern, guidance, source_finding_id, approved_by, ts, active)
roe_rules    (id, field, value, reason, approved_by, ts, active)
golden_paths (id, kind CHECK IN (shell, flow, cron), surface, command_or_flow,
              platform CHECK IN (any, posix, windows), reason,
              source_finding_id NULL, approved_by, ts, active DEFAULT 1)
```

The first four ship in `scripts/ledger.py` at `acc99f217`; `golden_paths` is the table the next
section adds.

`verdicts` is append-only, so `findings.final_verdict` is a fold and the disagreement between auditor
and verifier stays readable rather than being overwritten by the winner.

**The learning loop.** After each round the conductor dispatches one **retrospective** child session
that compares auditor verdicts to verifier and human verdicts and proposes `lessons` rows: why a
false positive looked real, what pattern the true positives shared, what the auditor missed. A
proposed lesson is `active=0` until a human approves it. Approved lessons are injected into the next
round's auditor and verifier seed messages under a byte budget, top-N by surface — bounded on purpose,
because an unbounded lesson list becomes the seed and crowds out the brief. Every lesson carries its
`source_finding_id`, so a piece of guidance can always be traced back to the finding that earned it.

**This is the human intervention point.** A human edits, approves or rejects rows directly — SQL, or
`scripts/ledger.py` — and the change takes effect on the next round with no code change and no
redeploy. The same mechanism carries rule changes: a scope or `forbidden` edit is a `roe_rules` row
with a reason and an approver, `scope_check.py` reads the active rows, and a bad rule is reverted by
flipping `active` rather than by a commit.

## Fixes must keep legitimate use alive — golden paths and the denial differential

A security fix in this system is almost always a rule tightened: a deny pattern widened, a scope
narrowed, a guard moved earlier. The only question verification asked was "is the PoC now refused?"
— and a change that refuses everything answers that question yes. Nobody asked whether the chat still
starts, whether the cron that fired yesterday still fires, whether `prepare-pr` can still push. A fix
that passes the first question and fails the second is an outage the audit itself caused, and it is
worse than the finding, because the finding was hypothetical and the outage is not.

Three failure modes, in the order they are likely:

- **The tool is made unusable.** The newly refused shape was a false positive: a read-only query, a
  build command, a test invocation. The rule is right about the hazard and wrong about the traffic.
- **Existing automation breaks silently.** A cron, a monitor loop, or a conductor's own bundled
  script stops working. Nothing fails loudly, because a refusal looks like a normal denial and an
  unattended job has no reader.
- **The platform is split.** A fix written and tested on Linux refuses the macOS or Windows path, or
  introduces a code path only one platform can reach. Nothing catches this unless the check itself
  runs on the matrix.

### The golden-path corpus

A fifth ledger table, `golden_paths`, holds the legitimate operations that must always pass. Its DDL
is in the ledger section above; the columns that carry the judgment are `kind` (`shell`, `flow` or
`cron`), `command_or_flow` (the exact operation), `platform` (`any`, `posix` or `windows`, so a
platform-branched row is not asserted where it cannot run), `reason` (why this is legitimate),
`source_finding_id` (set when the row was earned by a fix that broke it) and `approved_by`.

The first set of rows covers the operations this system cannot lose:

- Representative read-only `gh` and `git` queries — issue and PR reads, `git log`, `git show`,
  `git merge-base`.
- The venv `pytest` invocation the repository's own gates use.
- Chat start plus one completed turn.
- An existing cron firing on schedule.
- `monitor_start` arming a loop and `autonudge_stop` ending it.
- The `prepare-pr` commit-then-push sequence.
- Each conductor's bundled-script calls — `claim_preflight.py`, `fleet_probe.py`, `credit_spend.py`,
  `scope_check.py`, `finding_entry.py`, `verify_finding.py`, `ledger.py`.

A row is data, so adding one is attributable and removing one is flipping `active` — the same property
that makes `roe_rules` reviewable.

**The gating corpus is a committed export.** The ledger lives at
`<data_home>/security-conductor/findings.db`, which is per-host and invisible to a CI runner, so a
gate reading it directly would be unenforceable in exactly the place both gates below are declared
blocking. The active rows are therefore exported to a committed `golden-paths.json`, reusing the
relationship `rules-of-engagement.json` already has to `roe_rules`.

The export is **authoritative for gating**: both gates read the file and nothing else, and a row that
is not in the committed export does not gate. The ledger rows remain the editing surface, the reason
and the approver record — but they are not consulted at gate time, so two hosts' ledgers cannot
silently disagree with the corpus that actually decides. That is the one place this differs from
`roe_rules`, where `scope_check.py` reads the rows directly: a scope question is asked on the host
that holds the rows, and a gate question is asked in CI, which does not.

This is independent of open decision 3 below, which is about the findings — an unfixed finding in a
public repository is a disclosure, a list of legitimate commands is not.

**What each `kind` costs to run, and which ones gate.** A `shell` row is *classified*, not
executed: the deny classifier is a function of the command string, so the entire shell corpus is
checked in milliseconds with no session, no provider, no network and no side effect. That is what
makes running it on the full platform matrix cheap, and it is all `deny_diff.py` ever does. A `flow`
row ("chat starts and completes one turn") and a `cron` row ("an existing job fires on schedule") are
a different animal: each needs a live session, a provider and real elapsed time, and none of that is
affordable three times per fix on a CI matrix.

So the gate is scoped by `kind`. **M1 gates `shell` rows only.** `deny_diff.py` runs in CI across the
matrix; `verify_fix.py` runs where the fixer runs — in the fixer's own worktree, before it marks the
PR ready — and checks the PoC plus the shell corpus. `flow` and `cron` rows are recorded in the
corpus from M1, so the knowledge of what must keep working is written down and attributable, but they
are marked as not gating until M2 builds their harness. M2 is the honest home for it: the pilot round
already stands up live sessions and already has a cron firing, so executing those rows there costs
almost nothing extra, and until it exists a `flow` row is a claim about a check nobody can run.

This is also what keeps fail-closed defensible. Failing closed is right for a row that was supposed
to run and could not, and wrong for a row nothing was ever able to run — the second case makes exit
30 the *normal* result, and once the gate is normally red the cheapest way to green is deactivating
precisely the outage-shaped rows, which is the move the paragraph below exists to forbid. A gate that
creates pressure toward its own bypass is not a control. Scoping by `kind` keeps fail-closed strict
where it means something.

**Deactivation is gated exactly like activation.** A proposed row is `active=0` until a human
approves it; flipping an approved row back to `active=0` needs the same human approval and carries
the same reason and approver. Without the symmetry the gate is defeated by shrinking its own input:
a fixer that can write the ledger deactivates the rows its fix would break, and both scripts below
then pass on a corpus that no longer contains the broken paths. That would leave the agent able to
loosen the ceiling it is meant to be held to, which is the one property the rules-of-engagement
design exists to deny it.

### `scripts/verify_fix.py` — both halves or nothing

Given a finding id and a worktree, `verify_fix.py` asserts two things and reports which one failed:

- The finding's PoC is refused or otherwise fixed, via `verify_finding.py`.
- Every gating `golden_paths` row in the committed export whose `platform` matches this host
  still passes — `shell` rows in M1, plus `flow` and `cron` rows once M2 harnesses them.

| Exit | Meaning |
|---|---|
| `0` | Both hold. The fix is acceptable. |
| `10` | The PoC still reproduces. The fix does not fix. |
| `30` | A golden path broke. The output lists the broken rows. |

It fails closed: an unresolvable golden path is a broken one, not a passing one, because a check that
cannot run is indistinguishable from a check that runs and finds nothing.

**This replaces the fixer lane's acceptance criterion.** "PR checks green" becomes "PR checks green
AND `verify_fix.py` exit 0". Checks green proves the repository still builds; it does not prove the
product still works, because no existing test asserts that a legitimate command is *not* refused.

### The denial differential

Any PR touching `src/kiro_crew/security/**`, `deny_guidance.py`,
`src/kiro_crew/platform/security_authority.py` or a `rules-of-engagement.json` must run
`scripts/deny_diff.py`. It classifies every active `golden_paths` row of kind `shell` with the deny
classifier twice — once at the base commit, once at the head — and reports the rows the head newly
refuses.

- **Zero newly refused rows** — advisory. The differential is recorded and the PR proceeds.
- **One or more** — blocking. It needs an explicit human yes naming which rows are acceptable
  casualties and why.

The differential reports rows the head **deactivated** as well, and blocks on them on the same
terms. A row dropped from the corpus and a row the head refuses are the same event seen from two
sides: both end with a legitimate operation no longer protected, and the deactivation is the cheaper
one to reach, so the gate that ignored it would be the easier one to walk around.

This gate is deterministic, so on this one question it **outranks model review**. A reviewer reading
a widened regex is guessing at what the regex now matches; the classifier run against a corpus is
not guessing. Where the two disagree about whether a legitimate command is now refused, the
differential is correct.

### Platform

The golden-path gate runs on the full Linux / macOS / Windows matrix, because a fix that only refuses
on one platform is exactly the case a single-platform run cannot see.

A `posix-only-approved`-style label is scoped to **a single platform-branched line**, never to a
whole PR. A PR-wide exemption converts a targeted exception into a blanket one and the blanket
survives long after the line is gone.

A new `roe_rules` `forbidden` row states the rule the label is an exception to: **a fix must not
introduce a code path usable on only one platform.**

### Feeding the corpus — a policy block is an event, not a finding

This resolves the open decision about how an auditor's policy block is reported.

A policy block hit by an auditor or a verifier is recorded as a **`policy_block` event**, not as a
finding. The two readings that were in tension — "the fence worked" and "the fence stopped a
legitimate step" — are not answered by the block itself, and a finding is the wrong container for an
unanswered question: it enters the severity queue, competes with real defects, and inflates the
false-positive rate that M3 is gated on.

The retrospective is what rules on the event. For a block it confirms was a false positive, it does
two things: propose a `false-positive` `lessons` row, and propose a `golden_paths` row for the
operation that was wrongly refused, `active=0` until a human approves it. So the corpus grows from
the blocks the system actually hit, rather than from a list somebody imagined up front — and the
lesson and the golden path are proposed together, because a lesson tells a future auditor not to
re-file it while the golden path stops a future fix from re-breaking it.

The mechanism is not hypothetical. Designing this section hit the self-protection classifier several
times on read-only `gh` queries, in a session whose entire output is a documentation diff. Each of
those is a `policy_block` event with a confirmed false-positive reading and a golden-path row
waiting to be written.

## Phases

- **M0** — the agent, the skill, and the first set of `roe_rules` rows. The human reviews the rules of
  engagement before any auditor runs; that review is M0's exit criterion, not a formality. Includes
  the governance-search skill dependency decision named above. **Shipped**
  ([#9271](https://github.com/kirodotdev/KiroCrew/pull/9271),
  [#9273](https://github.com/kirodotdev/KiroCrew/pull/9273)).
- **M1** — the scripts and the SQLite ledger: schema plus `scripts/ledger.py`, the three evaluator
  scripts, the `golden_paths` table with its first reviewed rows and its committed export,
  `scripts/verify_fix.py` and `scripts/deny_diff.py` gating `shell` rows, with behaviour pinned by
  tests — scope verdict precedence, `UNKNOWN` never
  reported as in-scope, dedupe identity, verifier verdict mapping, append-only `verdicts`, an
  unapproved lesson never reaching a seed message, `verify_fix` failing closed on an unresolvable
  golden path, and `deny_diff` reporting a newly refused row on a deliberately over-wide test
  pattern. **Shipped**: the ledger ([#9270](https://github.com/kirodotdev/KiroCrew/pull/9270)), the
  evaluator scripts ([#9332](https://github.com/kirodotdev/KiroCrew/pull/9332)), the golden-path
  corpus and `verify_fix.py` ([#9500](https://github.com/kirodotdev/KiroCrew/pull/9500)), the
  denial differential ([#9499](https://github.com/kirodotdev/KiroCrew/pull/9499)), and the wiring that
  makes the fixer lane's acceptance criterion and the gate's corpus one thing ([#9813](https://github.com/kirodotdev/KiroCrew/pull/9813)).
- **M2** — the pilot round on this repository, including the retrospective lane, plus the harness that
  executes `flow` and `cron` golden paths and promotes them from recorded to gating. Exit criteria:
  every round-1 finding carries a verifier verdict, the false-positive rate is recorded, the
  retrospective has proposed lessons a human has ruled on, and at least one `flow` row and one `cron`
  row run green through `verify_fix.py`.
- **M3** — the fixer lane. Blocked on M2's recorded false-positive rate: a lane that dispatches fix
  PRs from an unmeasured finding stream is worse than no lane. A fix is accepted when **PR checks are
  green AND `scripts/verify_fix.py` exits 0** — never on checks alone.

## Open decisions

1. **How lessons are scored and pruned** to stay under the seed byte budget. Recency, surface match
   and hit rate are all plausible orderings, and a lesson that never changes an outcome should
   eventually stop being injected.
2. **Whether lessons are per-repository or shared across targets.** A false-positive pattern in this
   codebase's deny classifier may generalize to any deny classifier, or may not generalize at all.
   The same question applies to `golden_paths`: a shell row is about this product's commands, while a
   `flow` row like "chat starts and completes one turn" is about any target running this product.
3. **Whether the database is committed to the repository or stays in `data_home()` only.** An unfixed
   finding in a public repository is itself a vulnerability; a database only in `data_home()` is
   invisible to review and lost with the host.
4. **Whether the verifier may use a different model than the auditor.** A different model is a
   stronger independence argument against a shared hallucination; it is also a second failure mode
   and a cost.
5. **Whether the fixer lane is ever automatic for Low or Medium.** M3 assumes a human yes per
   dispatch; whether that gate is ever lifted for low-severity findings is unresolved.
