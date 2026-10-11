---
title: Sub-agent backend selection — an orchestrator picks the harness a child runs on
status: draft
author: billygerhard
created: 2026-10-09
last-audited: 2026-10-09
audited-at: 33b67aced2
doc-pr: 18486
implementation-prs: [18150]
tracking-issues: [13892]
supersedes: []
superseded-by: []
---

# RFC: Sub-Agent Backend Selection

> **Status:** `draft`. Acceptance is requested from a maintainer; the status
> flips to `accepted` when one records it here. Nothing is on main. Verified at
> `33b67aced2`: `SPAWN_RUN_SCHEMA` and `SPAWN_RUN_TASK_ITEM_SCHEMA` in
> `src/kiro_crew/validation.py` accept `model` and `agent`
> but no backend, and `select_provider_backend` in `src/kiro_crew/members.py`
> has exactly two arms: a member DM thread runs on `agent.member_acp_backend`,
> and every other session (every sub-agent included) runs on the configured
> default. The implementation is
> [#18150](https://github.com/kirodotdev/KiroCrew/pull/18150). This document
> lands first because it adds a delegation capability (which runtime and which
> process a child gets), and the First Principles lane reads that decision from
> the base branch. It is PR 2 of the plan in
> [#13892](https://github.com/kirodotdev/KiroCrew/issues/13892).

## Summary

`spawn_run` gains an optional `backend` field, for the whole batch or per task.
A sub-agent that names one runs on that harness (for example `claude` or
`codex`) instead of the gateway default. Only backends governance already allows
can be named, and a backend that cannot start on this machine is refused before
the child is created, never quietly swapped for Kiro. A spawn that names no
backend behaves exactly as today.

## Motivation

Kiro Crew can already run a chat on several ACP harnesses, but only one per
gateway: `agent.acp_backend` decides for every session except a member DM
thread. An orchestrator that wants one task done by a different harness (a
second opinion from another model family, a task one harness's tools handle
better, a reproduction on the backend a user reported against) has no way to
ask for it. The workarounds are worse than the feature:

- **Switch the gateway default.** This moves every chat, not one child, and
  needs a restart.
- **Route through a crewmate.** `agent.member_acp_backend` applies to member DM
  threads only (`is_member_session_key` in `members.py`), so it cannot reach a
  sub-agent, and it is one setting for every member.
- **Shell out to another CLI.** This bypasses the sandbox, the approval surface,
  the transcript and the completion event that a sub-agent gets for free.

## Goals

- An orchestrator can run one sub-agent on any backend that governance allows
  and this machine has installed.
- A pick that cannot be honoured **at spawn time** is refused with a typed
  reason the agent can act on, instead of becoming Kiro. Phase 1 makes this
  guarantee at admission only: a backend that becomes unavailable after
  admission (Design §3, and a continuation or retry, Phase 2) still reaches
  the factory gate, which runs Kiro. Q2 (answered "refuse") has Phase 2 close that gap.
- Selection keeps the single construction gate that harness parity requires; no
  second gate is added.
- The pick sticks to the conversation: a continuation, a retry and a
  reset/compaction successor run on the same backend.
- No spawn that names no backend changes behaviour.

## Non-goals

- **A per-chat backend picker.** That is PR 1 of #13892
  ([#15978](https://github.com/kirodotdev/KiroCrew/pull/15978)), and it needs its
  own decision.
- **New backends.** Which harnesses exist is
  [#14861](https://github.com/kirodotdev/KiroCrew/pull/14861)'s question. This
  RFC selects among the registry as it is.
- **Changing governance.** The selectable set, its floor (`kiro`) and its denial
  rules are unchanged.
- **Cross-backend model routing.** `model` stays a per-backend id; this RFC does
  not translate model names between harnesses.

## Design

### 1. Surface

`backend` is an optional string on `spawn_run` (batch-wide) and on each
`tasks[]` item (per task, overriding the batch value). It is spelled the way a
governance rule spells a backend (`kiro`, `claude`, `codex`, ...), so every
backend has a non-empty name. It is held to `^[a-z0-9][a-z0-9_-]*$` at the
schema, so it cannot carry anything into a log line or an error message.

### 2. Admission: refuse, do not degrade

`/api/spawn` resolves the name before any run is created. A backend is
**available** when it is in `selectable_backend_values()`
(`agent_sdk/backends.py`) and `probe_backend` (`agent_sdk/backend_install.py`)
does not report it missing or installed-after-start. Otherwise the request
fails with HTTP 400 and one of three codes, each listing the backends that can
start:

| Code | When | Extra field |
|---|---|---|
| `unknown_backend` | not a backend, or not selectable under governance | `backends` |
| `backend_restart_required` | installed after the gateway started | `backends` |
| `backend_not_installed` | allowed here, not installed | `backends`, `install_command` |

The factory's gate would coerce any of these to Kiro. That is the right
behaviour for a persisted setting, and the wrong one for an explicit request: an
orchestrator that sent a task to another harness for a second opinion and got
Kiro back has been misled. A probe that cannot decide does not block.

### 3. Construction: the same single gate

The resolved id reaches the provider factory as a `backend_override` factory
kwarg, and `select_provider_backend` gains one arm, checked first: an explicit
override goes through `resolve_selected_backend` (`agent_sdk/backends.py`),
the same gate the persisted field crosses. The factory body stays one selection
call (harness parity H3/H13). A request that admission accepted but governance
narrowed in the meantime therefore still lands on Kiro rather than on a denied
backend, so governance cannot be bypassed through this path. In Phase 1 that
swap is not reported: the run record keeps the requested backend. This is the
window between admission and construction, a `probe_backend` call and a cold
start apart, and only a governance change inside it can open it. Phase 2 closes it (Q2). The override sits
above the member-DM arm only for sub-agent sessions; a crewmate's own DM thread
is unaffected.

### 4. Isolation

A run with an override never takes the shared runtime or a warm-pool process
(`pool_decision = "bypass_backend_override"` in `session_allocation.py`). A pool
process is already bound to the default harness, so a child sharing it would
silently run on the parent's backend.

### 5. Lineage

The backend is recorded on the run (`SubagentInfo.backend`, `state.json`
`"backend"`) and on the live session (`_Session.backend_override`, carried by
`allocation_identity`). `spawn_continue`, the failed-run retry and a
reset/compaction successor read it back, so a conversation keeps its harness for
its whole life.

### 6. Model and effort

`model` and `reasoning_effort` are judged against the requested backend, not the
default. The effort note names the channel that actually delivers effort on that
harness (a Kiro slash command or an ACP config option), and says plainly when
the harness has no effort channel instead of reporting it applied.

## Migration plan

**Phase 1 — the capability.**
[#18150](https://github.com/kirodotdev/KiroCrew/pull/18150). Exit criteria:
- a spawn with no `backend` creates the same session it creates on main;
- `spawn_run(backend="claude")` completes on Claude, and `spawn_continue`
  resumes the same Claude session;
- each of the three refusal codes is returned for its case, and no run folder
  is created;
- a denied backend that reaches the factory lands on Kiro.

The PR's live evidence covers the first three on a preview gateway.

**Phase 2 — no silent swap after admission (follow-up PR; Q2 answered "refuse").** In Phase 1 a
continuation or retry whose inherited backend has since become unavailable, and
an admitted spawn that governance narrows before construction, both degrade to
Kiro at the factory gate (Design §3) without saying so. Phase 2 makes
`spawn_continue` and the retry path return the same typed codes as
admission, and the backend the factory actually ran is recorded on
`SubagentInfo.backend` and in the completion event, so a swap that the race
still produces shows wherever the result is reported. Exit criteria: a
continuation on a backend governance has since denied returns `unknown_backend`
and starts nothing, and a spawn whose backend is denied between admission and
construction completes with the run record and completion event naming `kiro`.

## Backward compatibility

The field is optional and absent by default. With no `backend`, the schema,
admission, factory arm order, pool use and persisted run record are all
unchanged, and an older `state.json` without `"backend"` reads as "no override".

## Security considerations

- **No new reach.** Only backends the owner's governance already allows can be
  named. Each child runs under its harness's existing sandbox routing and
  credential mask; this RFC changes neither.
- **The agent chooses.** A prompt-injected orchestrator could pick a different
  allowed harness than the owner expected. What it can reach is bounded by
  governance and the sandbox, as above, but the choice can still move cost to
  another vendor's account. Q1 asks whether that warrants an owner opt-in.
- **Input.** The name is pattern-checked at the schema, and every refusal echoes
  it through `repr`.
- **Audit.** The run's `state.json` records the backend that was requested. In
  Phase 1 that is the harness that ran in every case except a swap after
  admission (Design §3), where the record keeps the requested name while Kiro
  ran. The record names the harness that actually ran only from Phase 2, which
  records the factory's choice.

## Alternatives considered

- **Degrade to Kiro on a bad pick** (the factory's existing behaviour).
  Rejected: an explicit request that silently changes meaning is the failure
  this field exists to remove.
- **A backend on the agent template.** Rejected: it ties a template to one
  harness, while the choice belongs to the task. It also changes every existing
  spawn of that template.
- **A per-backend `delegatable` owner flag**, proposed in #13892. Not taken in
  Phase 1: governance's selectable set already expresses "this backend may
  run here". Kept open as Q1.
- **Spawn through a crewmate whose DM is pinned to the backend.** Rejected: it
  needs one crewmate per harness and routes ordinary delegation through the
  member surface.

## Open questions

Both questions were answered by a maintainer on
[#18150](https://github.com/kirodotdev/KiroCrew/pull/18150); the answers are
recorded under each one.

1. **Owner opt-in for delegation.** Is governance's selectable set enough, or
   should naming a non-default backend from `spawn_run` need its own owner
   switch (the `delegatable` flag)? The cost is the reason to ask: a child on
   another harness bills that vendor's account, which belongs to the owner, not
   to the orchestrator that chose it. Governance says a backend may run here,
   not that an agent may spend on it unprompted.
   **Answer:** the selectable set is enough. Selection is limited to the
   harnesses governance already allows, with no separate switch.
2. **A backend that becomes unavailable after admission.** This covers a
   continuation or retry whose inherited backend is no longer available, and a
   spawn whose backend governance denies between admission and construction.
   Should they be refused with a typed code and have any remaining swap
   recorded (Phase 2), or keep degrading to Kiro at the factory gate unreported,
   as Phase 1 does?
   **Answer:** refuse. Phase 2 ships as its own follow-up PR after #18150;
   until then `spawn_run`'s schema promises the refusal only at spawn time.
