---
title: ACP backend failover — an opt-in chain a sub-agent spawn falls through when its backend cannot start
status: draft
author: severity1
created: 2026-11-02
last-audited: 2026-11-02
audited-at: f2c0d926cf
doc-pr: 27581
implementation-prs: [26123]
tracking-issues: [13892]
supersedes: []
superseded-by: []
---

# RFC: ACP Backend Failover

> **Status:** `draft`. Nothing is on main. Phase 1 is implemented in the open
> pull request [#26123](https://github.com/kirodotdev/KiroCrew/pull/26123);
> maintainer bolichen97 asked for this document to land first, as its own
> change, and recorded two decisions on that pull request
> ([comment](https://github.com/kirodotdev/KiroCrew/pull/26123#issuecomment-6555008023),
> [review](https://github.com/kirodotdev/KiroCrew/pull/26123#pullrequestreview-5725541271)),
> which §8 carries. The baseline below was verified at `f2c0d926cf`. It builds
> on Phase 1 of [rfc-subagent-backend-selection](rfc-subagent-backend-selection.md)
> (`spawn_run` `backend`, refused rather than degraded at `/api/spawn`) and is a
> sibling of the per-chat and per-crew selection surfaces tracked by
> [#13892](https://github.com/kirodotdev/KiroCrew/issues/13892).

## Summary

A sub-agent's backend is chosen explicitly today: the global
`agent.acp_backend`, a crewmate's `agent.member_acp_backend`, or a per-call
`spawn_run(backend=…)`. When that backend cannot start, the spawn is refused.
This RFC adds `agent.acp_failover`, an opt-in, off-by-default block naming an
ordered chain of selectable backends. A backend is swapped only when **both**
hold: the operator turned the block on, **and** a configured failover trigger
fired. Every swap is reported to the caller and kept on the run record.

It is not a task router. Nothing reads a task to guess a better harness; the
only input is a hard signal that the chosen backend said no.

## Motivation

### Current state (verified at `f2c0d926cf`)

`_requested_backend` in `src/kiro_crew/dashboard/messaging_api/spawn.py`
probes every selectable backend and refuses a spawn whose backend is not
available, with the code `backend_not_installed` or
`backend_restart_required`. The refusal lists only the
backends that can start, so the calling agent can retry on one of them.
`src/kiro_crew/acp/transport_errors.py` already classifies a spent allowance
(`PROVIDER_ERROR_USAGE_LIMIT`) and an expired login as terminal, non-retryable
verdicts.

### Problems

1. **A backend that cannot start costs a round trip and a decision.** The agent
   gets a refusal, has to read it, and has to pick again. An operator who has
   already decided "if codex is missing, use claude" has nowhere to say so.
2. **A spent allowance kills the run.** Observed live: a
   `spawn_run(backend="codex")` authenticated and was then rejected by the
   vendor with `usageLimitExceeded`. Nothing tries another harness. This case
   is a runtime refusal, not a spawn-time one, and is Phase 2's job (§6).

## Goals

- An operator declares an ordered chain, tried until one member can start.
- Off by default. Absent the block, behaviour is byte-identical to today.
- The chain names only `selectable_backend_values()` members. It is a selection
  layer, never authorization, and adds no governance surface.
- A swap requires the operator's opt-in AND a configured trigger.
- Every swap is visible: the caller is told which backend ran, and the run
  record keeps the backend it was moved from.
- The single construction gate is preserved: failover only chooses the value
  the existing admission path already validates.

## Non-goals

- A task router or intent classifier.
- A per-chat backend picker (#15978) or per-crew backend field
  (#11324/#16200). A crew's pin is simply the chain's starting point.
- Failing over on a transient fault (`throttle`, `http_5xx`, `connection`);
  those stay the same-backend retry ladder's job.
- Moving a continuation. A conversation lives on the harness it first ran on.
- Cross-backend model translation. `model: "auto"` already resolves against
  whichever harness ends up running.

## Design

### 1. Config surface

```jsonc
{ "agent": {
  "acp_failover": {
    "enabled": true,
    "chain": ["codex", "claude", "kiro"],
    "triggers": ["backend_not_installed", "backend_restart_required"]
  }
}}
```

- `chain` uses the stored backend spelling (`"kiro"` for Kiro), translated
  through `backend_from_stored` before comparison and back with
  `backend_to_stored` when written. Duplicate ids are rejected.
- `triggers` defaults to every trigger the current phase consumes. A transient
  kind, or a trigger no shipped phase consumes, is a config error.

### 2. Loading never takes the gateway down

The selectable set is narrowed at runtime (an enterprise ceiling via
`apply_selectable_denials`, a host that cannot serve a backend). A chain member
valid when written can become non-selectable later. At load, the chain's
**shape** is validated (types, spelling, duplicates). A member that is no longer
selectable is dropped with a warning, the same way `_read_backend_choice`
treats a backend pick. A hard refusal for a non-selectable id applies only on
the write path (`config set` / PATCH), where the operator can correct it.

### 3. Triggers

| Trigger | Signal | Phase |
|---|---|---|
| `backend_not_installed` | `/api/spawn` probe refusal | 1 |
| `backend_restart_required` | `/api/spawn` probe refusal | 1 |
| `usage_limit` | `PROVIDER_ERROR_USAGE_LIMIT` / `AcpError.usage_limit` | 2 |
| `auth` | `PROVIDER_ERROR_AUTH` / `AcpError.auth_required` | 2 |

Only verdicts the taxonomy already marks non-retryable are eligible. The set
accepted at config load equals the set some shipped caller passes as a verdict:
Phase 1 accepts only the first two rows, and Phase 2 widens it in the same
change that adds the consumer.

### 4. Where it runs

`src/kiro_crew/subagent_parts/backend_failover.py` holds a pure resolver,
`next_failover_backend(*, current, chain, triggers, verdict) -> str | None`,
with no gateway imports. `_requested_backend` calls it when the chosen backend
is unavailable with a configured trigger, and walks forward to the first chain
member that probes available. The walk advances by index, never revisits a
member, and is bounded by the chain's length. An exhausted chain returns the
existing typed refusal, naming the chain. Failover never lands on Kiro unless
`kiro` is a named member.

The gateway's already-loaded config is read; a refused spawn does not reload
config from disk.

### 5. Reporting and lineage

- The `/api/spawn` receipt, and so `spawn_run`'s result, carries `backend` (the
  stored spelling of the backend the run starts on). When a hop happened it also
  carries `failover_from`, the backend the caller asked for.
- `SubagentInfo` keeps `backend` as the backend that actually ran (this is what
  `spawn_continue` already reads in `subagent_manager/continuation.py`) and adds
  `failover_from`, which `spawn_status` and `spawn_list` show.
- A continuation, retry or reset successor never re-runs the chain. It stays on
  the backend the conversation ran on, or is refused.

### 6. Phase 2: runtime failover

A freshly spawned sub-agent whose first request fails with a terminal
`usage_limit` or `auth` verdict is torn down and re-spawned on the next chain
member, bounded by the chain's length, only before any output or tool call
(the same replay-safety reasoning as `AcpProcessDied.ambiguous_delivery`). The
preferred first step is narrower: strengthen the spawn-time probe so a spent
allowance it can see becomes a Phase 1 trigger, and only re-spawn for the
residual a probe cannot observe.

## Migration plan

- **Phase 1: spawn-time failover** ([#26123](https://github.com/kirodotdev/KiroCrew/pull/26123)).
  Exit criteria: with the block absent, `/api/spawn` behaves as today;
  `["codex", "claude"]` with codex not installed starts on claude and the
  receipt says `failover_from: "codex"`; an exhausted chain is refused with the
  chain named; a duplicate id or a transient or Phase 2 trigger fails at config
  load; a chain member later denied by a ceiling still loads, and at spawn time
  is never probed or tried; a
  continuation after a hop stays on the backend that ran; config containing the
  block round-trips through `to_dict` and JSON.
- **Phase 2: runtime failover.** Blocked on Phase 1 landing. Exit criteria: a
  first request capped by `usage_limit` re-spawns on the next member and
  completes; a run that produced output is never re-spawned; a transient
  first-request failure takes the retry ladder.

## Backward compatibility

Off by default, so nothing changes until an operator writes the block. The
receipt's new fields are additive.

## Security considerations

- No new reach: the chain is capped by the governance-narrowed selectable set,
  re-checked at every spawn. An agent cannot widen it.
- **A hop moves the prompt and its context to another vendor and another bill.**
  That is the same exposure as an explicit pick of that backend, but the
  operator chose it in advance rather than per call, so every hop is reported
  on the receipt and kept on the run record.

## Alternatives considered

- **A task router inferring the harness from task text.** Rejected: brittle,
  and crew or agent affinity is owned by explicit pins.
- **Exempting an explicit `spawn_run(backend=…)` from failover.** Rejected as
  the default: an agent-issued pick is the main way a backend is chosen today,
  and the motivating case arrived that way. The maintainer accepted reporting
  the hop as the alternative (§8).
- **Failing over on transient faults.** Rejected: races the retry ladder and
  moves spend for a fault that clears on its own.
- **A silent fallback to Kiro on an exhausted chain.** Rejected: it hides the
  failure. Name `kiro` in the chain instead.

## Open questions

1. Should a caller be able to opt out per call (`failover: false`)? Not proposed
   unless a reviewer asks.
2. Should the dashboard show a hop on the run, beyond `spawn_status`? Left for a
   follow-up; it is a `website/` change.

## 8. Maintainer decisions

Recorded from bolichen97 on [#26123](https://github.com/kirodotdev/KiroCrew/pull/26123),
2026-11-01:

1. A backend the caller named explicitly in `spawn_run` is either never swapped,
   or the response tells the caller which backend ran. **This design takes the
   second** (§5).
2. In Phase 1, `usage_limit` and `auth` are either rejected as triggers at
   config load, or documented as not yet active. **This design rejects them**
   (§3).
3. This document lands under `docs/request-for-change/` first, as its own
   change.
