---
title: Solo-Spawn Gate — a one-task sub-agent has to say why
status: superseded
author: iamwhatever
created: 2026-09-18
last-audited: 2026-09-24
audited-at: 9b098d79fb
doc-pr: 11848
implementation-prs: [11710]
tracking-issues: []
supersedes: []
superseded-by: [../system-specs/modules/subagent.md]
---
# RFC: Solo-Spawn Gate — a one-task sub-agent has to say why

- Status: superseded. Decision of 2026-09-24: the gate is removed and
  delegation policy becomes prompt guidance only, owned by
  [`../system-specs/modules/subagent.md`](../system-specs/modules/subagent.md).
  This document lands first so the removal is judged against a decision on the
  base branch; [#13458](https://github.com/kirodotdev/KiroCrew/pull/13458)
  carries the code. Grounds: local `spawn.solo` audit from 2026-09-22 to
  2026-09-24 showed 33 of 34 one-task calls allowed — 16 on `specialist`, 14 on
  a named model/agent/crew, 2 on other reasons, 1 refused. The gate checks the
  shape of a reason, not its truth, so the model always holds a passing token,
  and the five-reason list in the prompt and schema reads as a menu. After the
  removal `solo_reason` / `solo_details` stay accepted and ignored so existing
  skills keep working.
- Author: iamwhatever
- Created: 2026-09-18
- Related: `test/test_spawn_single_task_gate.py` (the earlier, advisory form of
  this rule), `src/kiro_crew/config/prompt.md` § Subagent Orchestration,
  [rfc-resumable-subagent-sessions.md](rfc-resumable-subagent-sessions.md)
  (`keep=` and continuable conversations, which this gate does not change)

## Summary

`spawn_run(task=...)` with exactly one task — and `spawn_sub_agents` with one
entry — stops being a straight dispatch and becomes a handshake. A one-task call
that names no `solo_reason` and no `model` / `agent` / `crew` other than the
caller's own is **refused before anything is spawned**, and the result asks the
caller whether it can do the task itself. The caller either does the work in
its own session, calls again with a reason from a **closed vocabulary**, or
names a model / agent / crew that genuinely differs from its own. Every outcome
is audited as `spawn.solo`.

This changes what a first-class tool does by default. That is why it is written
down here rather than decided inside the pull request.

## Problem

One sub-agent for one task is a round-trip with no parallelism gain: the parent
waits (or ends its turn and waits for the completion event) for work it could
have done in the same turn, and pays a second model run, a second context
assembly and a second place for the task to go wrong.

The rule "do a single task yourself; spawn only for 2+ independent tasks, bulk
data, or a different agent/model" has existed for a while as **advice**:

- `prompt.md` § Subagent Orchestration says "delegate for hard problems, not just
  because a task has several steps";
- the `spawn_run` / `spawn_sub_agents` tool descriptions open with a `GATE:`
  paragraph saying the same, pinned by `test/test_spawn_single_task_gate.py`,
  which records in a comment that the single task "is not read as forbidden".

Advice lost. The base kiro-cli prompt tells the model to "delegate to a
sub-agent" for investigation and to preserve context, and in a Kiro Crew session
the only tools named sub-agent are the two spawn tools. So a single
investigation, a single fix, a single review each spawned one sub-agent, on
every crew host, and the owner re-taught the rule by hand more than once. A rule
that lives only in prose does not hold against a stronger prose.

## Decision

1. **The gate is mechanical.** A one-task `spawn_run` / one-entry
   `spawn_sub_agents` that names no `solo_reason` and no model / agent / crew is
   refused with a question, and nothing is spawned.
2. **The reason is a closed vocabulary, not a flag and not free text.**
   `bulk_data` (the step would flood the caller's context with bulk output and
   only the distilled result is needed) and `fresh_context` (the result would
   be wrong if the run saw this session's context — a blind review, a
   clean-slate repro). The list deliberately omits "preserve my context" and
   "it is a separate investigation", so a reflex retry has to be a *false*
   claim, not a vague one. The vocabulary changes only by a recorded decision,
   never by a token added inside a fix.
3. **A genuinely different model / agent / crew is a reason in itself.** The
   parent cannot become another model, another tool set, or another member's
   memory silo, so asking "can you do it yourself?" is already answered. The
   gateway checks the claim against the parent session's own agent (its
   resolved template, not a member alias), its member selection, and — for a
   dashboard slot — its pinned model; naming the parent's own value is refused
   with `solo_spawn_unjustified`. `keep: true` alone is not a reason, and the
   `"auto"` model sentinel is not a named model.
4. **Every outcome is audited** as a `spawn.solo` SEL record: denied, allowed
   on a reason (with the reason), allowed on a difference (with what
   differed). The reason is the caller's own claim by design; the audit is
   what makes a habit of lone spawns visible after the fact.
5. **Fail open on any unknown parent fact.** The gate is an efficiency guard,
   not a security boundary: a value the gateway cannot compare never refuses a
   spawn the tool side let through, and the audit records `(parent unknown)`.
6. **Programmatic callers are not gated.** Batches of 2+ tasks never pass
   through the gate, and the SDK or an app posting to `/api/spawn` directly
   never sends the `solo` marker, so the gate's reach is exactly "a model's
   spawn decision", never an app's.

### Not a goal

- Verifying that a claimed reason is *true*. No pre-merge check can verify a
  model's future behaviour; the gate forces the decision to be made and
  recorded, it does not adjudicate it.
- Gating batches, the SDK, apps, cron or workflow engines.
- Changing `keep=`, `spawn_continue`, context groups or the approval flow.

## Design

Two halves, two hosts, because each host knows one thing the other cannot:

| Half | Runs in | Knows | Refuses |
|---|---|---|---|
| Count gate (`solo_spawn_refusal`) | the MCP tool process | how many tasks the call carries (the gateway sees one POST per task) | one task, no reason, nothing named |
| Roster check (`solo_spawn_difference`) | the gateway (`/api/spawn`) | the parent session's own agent, member selection and slot model | one task, no reason, and every named value is the parent's own |

The tool marks a one-task POST with `solo=true` (plus `solo_reason` when
given); a POST without the marker is never gated. The refusal text names the
`solo_reason` parameter and points at its schema description; it does not spell
the passing words, which live in the schema alone. The `spawn_run` result
carries the reason when one was given; when the call passed on a difference,
the result points at the audit rather than guessing which value differed — only
the process that computed the ground prints it.

Before → after, for a caller whose parent is a plain `kirocrew` template session:

| Call | Before | After |
|---|---|---|
| `tasks=[a, b]` | spawns | spawns |
| one `task`, nothing else | spawns | refused with the question |
| one `task` + `solo_reason` | — | spawns; audited with the reason |
| one `task` + a different `model` / `agent` / `crew` | spawns | spawns; audited with what differed |
| one `task` + the parent's own agent / model / crew | spawns | refused (`solo_spawn_unjustified`) |
| one `task` + only `keep=true` | spawns | refused |
| SDK / app POST to `/api/spawn` | spawns | spawns |

Shipped instructions that produced a bare one-task call are brought under the
gate in the same PR: the Dev Fleet `pod-e2e` skill (one QA sub-agent:
`bulk_data`), the ops-mission-control dispatch SOP (the unattended
investigator: `fresh_context`), the frontend-design-workflow blind usability
review (`fresh_context`), and image-authoring (fan out only for 2+ images).

## Alternatives considered

- **Keep it advisory, but louder.** Rejected: that is the state this RFC
  replaces; the record in `test_spawn_single_task_gate.py` shows the advice was
  already as prominent as a description can make it.
- **A `confirm=true` flag on the second call.** Rejected: a yes/no toggle is
  rubber-stamped by the retry; it produces a wasted round-trip and no signal.
- **A free-text `because`.** Rejected for enforcement: unverifiable and
  unbounded. Kept as the first candidate for the *audit line only* if the
  reason-rate data later shows `bulk_data` becoming reflexive.
- **Tool-side only, no roster check.** Rejected: the tool cannot see the
  parent's own agent / model / crew, so naming your own model would be a free
  pass. The roster half is kept at its current size — agent and crew grounds
  on every surface, the model ground on dashboard slots, fail open elsewhere —
  and is not to grow more grounds.
- **A third reason for a user-requested long-running background task.** Not
  decided here; see Open questions.

## Open questions

1. **Background execution.** A user who asks to run one long job "while we keep
   talking" fits neither `bulk_data` nor `fresh_context`; today the honest
   model runs it inline and blocks the chat slot. Options: add a
   `long_running` reason (widening the vocabulary), or state in the spec that
   this case runs inline by design. Default until decided: not added.
2. **Where the audit is read.** The `spawn.solo` records are the backstop the
   design leans on. Default until decided: they live in the SEL log and are
   reviewed by the owner on demand; a periodic count per reason is a possible
   follow-up, not part of #11710.

## Success criteria

- A one-task spawn that names nothing is refused on every surface (unit and
  E2E coverage in #11710).
- Post-merge, the `spawn.solo` audit shows denials converting to inline work
  rather than uniform `bulk_data` retries. If it shows the opposite, the
  remedy goes through the vocabulary or the audit line by a recorded decision,
  not through more roster grounds.

## Rollout

Ships whole in [#11710](https://github.com/kirodotdev/KiroCrew/pull/11710):
`src/kiro_crew/solo_spawn.py`, the gate blocks in `mcp_tools/spawn.py` and
`dashboard/handlers/messaging.py`, the `solo_reason` field in `validation.py`,
the spec, docs, prompt and skill updates, and 53 tests in
`test/test_spawn_solo_gate.py`. There is no flag: the gate is on for every
MCP-calling session once merged, and off for nothing else.
