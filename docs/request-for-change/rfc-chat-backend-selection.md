---
title: Per-chat backend selection — a dashboard chat picks the harness it runs on
status: draft
author: billygerhard
created: 2026-10-12
last-audited: 2026-10-12
audited-at: c4c51aac52
doc-pr: 19438
implementation-prs: [19599]
tracking-issues: [13892]
supersedes: []
superseded-by: []
---

# RFC: Per-Chat Backend Selection

> **Status:** `draft`. Acceptance is requested from a maintainer; the status
> flips to `accepted` when one records it here. Nothing is on main. The
> baseline below was verified at `c4c51aac52`: `select_provider_backend` in
> `src/kiro_crew/members.py` decides a session's backend from an explicit
> `backend_override` (today set only by `spawn_run(backend=...)`), then the
> member-DM route, then the configured default; a dashboard chat slot stores
> a `model` but no backend; and `/api/chat/slots/{slot}/model` exists in
> `src/kiro_crew/dashboard/routes/chat.py` with no backend sibling. This is
> PR 1 of the plan in
> [#13892](https://github.com/kirodotdev/KiroCrew/issues/13892), and it reuses
> the plumbing
> [rfc-subagent-backend-selection](rfc-subagent-backend-selection.md) added.

## Summary

A dashboard chat slot may carry an optional backend pick (for example `claude`
or `codex`). A chat with a pick runs on that harness; a chat without one runs on
the gateway default exactly as today. Only backends governance already allows
can be picked, and a pick that cannot start is refused when it is set rather
than quietly swapped for Kiro (Security considerations covers the one later
case set-time checks cannot see). Switching starts a fresh agent session on the new
backend. The same change ships an HTTP endpoint and an MCP tool, so an agent can
set it through the same gate a person does.

## Motivation

The gateway runs every chat on `agent.acp_backend`, except member DM threads
(`agent.member_acp_backend`) and, since
[#18150](https://github.com/kirodotdev/KiroCrew/pull/18150), sub-agents that name
a backend. A person who wants one conversation on another harness (to compare
answers, or because one harness handles a task better) has to change the
gateway default, which moves every chat and needs a restart.

## Goals

- One chat can run on any backend governance allows and this machine has
  installed, without changing any other chat.
- A pick that cannot be honoured is refused at set time with a typed reason.
- The pick survives a gateway restart, a reset and a compaction.
- An agent can set it through the same API and gates as the UI.
- A chat with no pick behaves exactly as on main.

## Non-goals

- **Custom or additional backends** (for example several accounts of one
  harness). That is [#14861](https://github.com/kirodotdev/KiroCrew/pull/14861)'s
  territory; this RFC selects among the registry as it is.
- **Automatic rotation** between backends.
- **Translating model ids** between harnesses. `model` stays a per-backend id.

## Design

### 1. The pick and what it allows

A chat slot gains an optional backend field. It stores a governance **policy
id** (`kiro`, `claude`, `codex`, ...), never a raw backend id, so Kiro is picked
as `kiro`. The empty string is the clear value and is not a backend: with it the
session is created with no `backend_override` at all (`None`), and the existing
arms of `select_provider_backend` decide. A non-empty pick is translated to its
backend id (`kiro` becomes Kiro's `""`) and passed as `backend_override`, so a
chat can be pinned to Kiro even when the gateway default is another backend.
The override arm, which already wins over the member-DM route and the default,
selects it through `resolve_selected_backend`
(`src/kiro_crew/agent_sdk/backends.py`), the same governance gate a sub-agent's
backend crosses.

The allowed set is the one sub-agent spawn already enforces:
`selectable_backend_values()` filtered by the install probe. A pick that is
unknown, not selectable under governance, not installed, or installed only
after the gateway started is refused with the codes `/api/spawn` returns
(`unknown_backend`, `backend_not_installed`, `backend_restart_required`; see
`_requested_backend` in `src/kiro_crew/dashboard/messaging_api/spawn.py`), each
listing the backends that can start. Nothing is stored on a refusal.

### 2. Switching

A backend is fixed when a session starts, so the swap happens between turns.
Setting a different backend saves the pick and queues a reset, the way an
agent's model pick is queued; the chat's next turn resets its session onto the
new backend before it starts. The transcript stays visible in
the chat. The conversation **is** carried over: the new session's cold start
replays the chat's transcript, as it does after a model switch or any other
fresh start. The replay is text the gateway builds, not a harness's native
session state, so it works the same on every backend. A common reason to switch
is a spent quota mid-task, and the new backend needs the conversation to carry
on. A switch sends the history to the new backend's provider. In Phase 1 the
switch comes from an agent through `session_set_backend`, which reaches only
sessions that agent created (`authorize_target` with the creator fence applied
to every caller), and the tool's
description, its result and the in-chat notice all say the transcript is
replayed, so the person sees it happened. Phase 2 gives the owner the switch
directly (see Cost below).

A model id belongs to one backend, so a switch also clears the slot's stored
`model` (and its Auto routing flag) to the new backend's default, the same
rule a provider switch applies to every slot (`server_runtime/config_watch.py`).

A switch never tears down a running session itself. A turn in flight finishes
on the old backend, and the reset waits for attached sub-agents, as every
queued reset does. The next turn start also compares a picked chat's live
session with its pick, so a session that came up on another backend (a hard
Stop respawns on the gateway default, as on main) is reset there too. Setting
the backend the slot already has is a no-op.

### 3. Persistence and lineage

The pick is persisted with the slot's other metadata, so it survives a gateway
restart. When the session is created, the pick reaches the factory through
`get_or_create(..., backend_override=)`, which records it as
`session.backend_override`. `allocation_identity` in
`src/kiro_crew/session_lifecycle_parts/parked_queue.py` already carries that field to a reset or
compaction successor, so those keep the chat's backend with no new code.

### 4. Agent surface

- `POST /api/session-control/set-backend`, beside `set-model`, takes
  `{"target": "<slot>", "backend": "<name>"}` or `"backend": ""` to clear.
- MCP tool `session_set_backend` in `src/kiro_crew/mcp_dashboard_session_tools.py`, beside
  `session_set_model`, posts to that route.

The route runs `authorize_target`, the gate every session-control verb uses,
before the switch and again after each lock await. It holds every caller to the
creator fence, so an agent re-picks only sessions it created; moving any other
chat to another provider is left to the Phase 2 dropdown, where a person
clicks. A crewmate DM or worker session is refused, the worker judged from its
durable execution record. The switch itself is one function,
`switch_slot_backend` in `src/kiro_crew/dashboard/chat_handlers.py`, which the
Phase 2 dropdown will call through a dashboard route of its own.

## Migration plan

**Phase 1 — the capability.** One PR: the slot field, the endpoint, the MCP tool
and tests. No UI. Exit criteria:
- a chat with no pick creates the same session it creates on main;
- a chat with `claude` picked runs its next turn on Claude, and keeps Claude
  across a gateway restart, a reset and a compaction;
- each refusal code is returned for its case and nothing is stored;
- a switch during a turn returns 409;
- a switch from a chat with a non-default model clears that model;
- the tool and the endpoint refuse the same callers.

**Phase 2 — the composer.** A backend dropdown in the chat composer that calls
the endpoint and shows the backend the running session actually uses, with
screenshots.

This supersedes [#15978](https://github.com/kirodotdev/KiroCrew/pull/15978) as
PR 1 of #13892's plan; that PR is parked and grew far past this scope.

## Backward compatibility

The field is optional and empty by default. A slot persisted before this change
reads as "no pick", and with no pick the session is created with exactly the
kwargs it has on main. No existing route or tool changes.

## Security considerations

- **No new reach.** Only backends governance already allows can be picked, and
  each runs under its harness's existing sandbox routing and credential mask.
- **The gate is not duplicated.** The tool reuses the endpoint's validation and
  `deny_app_slot_access`, and construction still crosses
  `resolve_selected_backend`, so a backend that governance denies *after* the
  pick was stored is never started. That session runs on Kiro, the one case
  where a stored pick does not hold, and set-time refusal cannot cover it. The
  chat must not hide it: the composer (Phase 2) shows the backend the running
  session actually uses (`AcpRuntime.acp_backend`), not the stored pick, and
  the gateway logs the swap. Refusing that turn instead, as sub-agent
  continuations will, is left to the Phase 2 follow-up of
  [rfc-subagent-backend-selection](rfc-subagent-backend-selection.md), which
  this design depends on so the two paths stay alike.
- **Cost.** A pick can move a chat's usage to another vendor's account. That is
  the owner's choice for their own chat; an app or another member cannot make
  it for a slot it does not own.

## Alternatives considered

- **Change the gateway default.** Rejected: moves every chat and needs a
  restart.
- **Start the new backend with no memory of the conversation.** Rejected: it
  defeats the common reason to switch (a spent quota mid-task), and holding
  history back would need a cutoff in every reader of the transcript that feeds
  a prompt (replay, Side Chat, recent-session context), not one switch.
- **Degrade an unavailable pick to Kiro.** Rejected for the same reason as for
  sub-agents: an explicit request that silently changes meaning misleads.
- **The #15978 design.** Not taken: it kept a second store for the pick and grew
  to cover many edge cases; this RFC rides the existing `backend_override`
  plumbing instead.

## Open questions

1. **Member DM threads.** Should a crewmate's DM thread also take a per-chat
   pick (overriding `agent.member_acp_backend` for that thread), or should the
   pick apply to ordinary dashboard slots only? This RFC proposes dashboard
   slots only.
2. **A pick governance revokes later.** Phase 1 lets that chat's next turn run
   on Kiro, with the swap in the gateway log only, until the composer shows the
   running backend (Phase 2). Is that acceptable, or should Phase 1 refuse the
   turn with the spawn codes instead (`unknown_backend`)? This RFC proposes the
   log-only fallback for Phase 1, because a revoke is rare and the refusal is
   the shared work of the sub-agent follow-up.
