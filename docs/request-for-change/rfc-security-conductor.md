---
title: Security Conductor — proactive vulnerability discovery as a conductor use case
status: draft
author: zejiangg
created: 2026-09-07
last-audited: 2026-09-07
audited-at: e992b7771
doc-pr: 9195
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Security Conductor — proactive vulnerability discovery as a conductor use case

A dedicated `kirocrew-security-conductor` agent and a `security-conductor` builtin skill that run
proactive vulnerability discovery as a supervised worker fleet: one auditor per attack surface, an
independent verifier per finding, and an optional fixer lane behind a human gate. The skill is the
operating procedure of record; this document carries the intent and the decisions. Nothing here is
built.

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
  the `prepare-pr` skill; acceptance is PR checks green.

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

Measured at `e992b7771`.

- The conductor pattern is shipped twice, with one standalone installer each in
  `src/kiro_crew/agent.py` (`_install_conductor_agent`, `_install_pipeline_conductor_agent`), a
  filename constant each in `src/kiro_crew/agent_files.py` (`CONDUCTOR_AGENT_FILENAME`,
  `PIPELINE_CONDUCTOR_AGENT_FILENAME`), and roster hiding through `UNADVERTISED_AGENTS` in
  `src/kiro_crew/subagent.py`, which carries exactly three entries today.
- "Never does the work itself" is already expressed as a spec property, not a prompt request:
  `_install_pipeline_conductor_agent`'s `tools` list omits both `fs_write` and `code`, and
  `execute_bash` is mounted but never auto-approved because `allowedTools` has no argument matching.
- The bundled-script half of the pattern is shipped: `claim_preflight.py`, `fleet_probe.py` and
  `credit_spend.py` under `src/kiro_crew/builtin_skills/pipeline-conductor/scripts/`.
- **SQLite is this repository's established store for durable agent knowledge**, which is the pattern
  the findings ledger mirrors. `src/kiro_crew/memory.py` keeps `memory_index.db` beside the workspace
  config; `src/kiro_crew/vector_memory.py` keeps `memory.db` in WAL mode behind a `schema_version`
  table and a `_MIGRATIONS` ladder; `src/kiro_crew/knowledge/store.py` owns its own database with
  `CREATE TABLE IF NOT EXISTS` DDL and one connection per thread. All three import SQLite through the
  `src/kiro_crew/_sqlite_compat.py` shim rather than the stdlib module directly, and `data_home()` in
  `src/kiro_crew/config/paths.py` is where a new store's path is resolved from.
- The fixer lane has a procedure to reuse:
  `src/kiro_crew/builtin_skills/kirocrew-dev/prepare-pr/SKILL.md`.
- The three pilot surfaces are real code, not hypotheticals: the deny classifier `is_denied` in
  `src/kiro_crew/security.py`, reached through `src/kiro_crew/platform/security_authority.py` from
  the PreToolUse gate in `src/kiro_crew/hooks.py`; webhook ingest in `src/kiro_crew/webhooks.py`;
  dashboard token and session handling in `src/kiro_crew/dashboard/token_auth.py`.
- Nothing this document proposes exists. `security-conductor` and `rules-of-engagement` return zero
  hits in the tree. `scope_check` matches only two unrelated symbols — `app_scope_check` in
  `src/kiro_crew/dashboard/token_auth.py` and `scope_check_exception` in
  `src/kiro_crew/dashboard/websocket_hub.py`.
- The `security-assistance` (ARCC) skill the auditor brief depends on is **not** a builtin in this
  repository; it is an installed skill. M0 must decide whether the auditor brief requires it as an
  environment precondition or the skill is vendored.

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

Plus a first set of `roe_rules` rows, reviewed by a human before any auditor runs.

## Rules of engagement are machine-checked

"Not too aggressive" is enforced by a spec a script evaluates, not by prompt tone. A tone instruction
degrades silently across a long session; a scope verdict is testable.

The fields, held as `roe_rules` rows and exported as `rules-of-engagement.json`:

| Field | Contents |
|---|---|
| `scope` | Allowed repositories and paths. Everything else is out of scope by default. |
| `allowed_techniques` | Code review, dependency audit, local unit-level PoC tests. |
| `forbidden` | No production systems and no external hosts; no reading credential material; no denial-of-service and no broad fuzzing; no disabling safety protections; no attempting to bypass a policy block — a policy refusal IS the boundary. |
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
3. **Token and session handling** — `src/kiro_crew/dashboard/token_auth.py`.

Every finding goes through the verifier before it is reported. Fix PRs only after a human approves
each one.

## Learning from past audits — the findings ledger is SQLite

A round that does not remember the last one repeats its false positives. Findings, verdicts, lessons
and the rules of engagement therefore live in **one SQLite database**, at
`<data_home>/security-conductor/findings.db`, mirroring the pattern the memory and knowledge stores
already use in this tree (see the verified facts above): a versioned schema, `CREATE TABLE IF NOT
EXISTS` DDL, and SQLite imported through `src/kiro_crew/_sqlite_compat.py`. Four tables:

```sql
findings   (id, surface, severity, title, paths, poc, auditor_verdict,
            verifier_verdict, final_verdict, status, created, round_id)
verdicts   (finding_id, role /* auditor | verifier | human */, verdict, reason, ts)
lessons    (id, kind /* true-positive | false-positive | missed | out-of-scope */,
            surface, pattern, guidance, source_finding_id, approved_by, ts, active)
roe_rules  (id, field, value, reason, approved_by, ts, active)
```

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

## Phases

- **M0** — the agent, the skill, and the first set of `roe_rules` rows. The human reviews the rules of
  engagement before any auditor runs; that review is M0's exit criterion, not a formality. Includes
  the ARCC-dependency decision named above.
- **M1** — the scripts and the SQLite ledger: schema plus `scripts/ledger.py`, with behaviour pinned
  by tests — scope verdict precedence, `UNKNOWN` never reported as in-scope, dedupe identity, verifier
  verdict mapping, append-only `verdicts`, and an unapproved lesson never reaching a seed message.
- **M2** — the pilot round on this repository, including the retrospective lane. Exit criteria: every
  round-1 finding carries a verifier verdict, the false-positive rate is recorded, and the
  retrospective has proposed lessons a human has ruled on.
- **M3** — the fixer lane. Blocked on M2's recorded false-positive rate: a lane that dispatches fix
  PRs from an unmeasured finding stream is worse than no lane.

## Open decisions

1. **How lessons are scored and pruned** to stay under the seed byte budget. Recency, surface match
   and hit rate are all plausible orderings, and a lesson that never changes an outcome should
   eventually stop being injected.
2. **Whether lessons are per-repository or shared across targets.** A false-positive pattern in this
   codebase's deny classifier may generalize to any deny classifier, or may not generalize at all.
3. **Whether the database is committed to the repository or stays in `data_home()` only.** An unfixed
   finding in a public repository is itself a vulnerability; a database only in `data_home()` is
   invisible to review and lost with the host.
4. **Whether the verifier may use a different model than the auditor.** A different model is a
   stronger independence argument against a shared hallucination; it is also a second failure mode
   and a cost.
5. **How an auditor's policy block is reported** — as a finding (the fence stopped a legitimate audit
   step, which is a usability defect) or as an event (the fence worked, which is a non-finding). The
   two answers route to different readers.
6. **Whether the fixer lane is ever automatic for Low or Medium.** M3 assumes a human yes per
   dispatch; whether that gate is ever lifted for low-severity findings is unresolved.
