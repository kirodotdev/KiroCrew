---
title: Pi task visibility: import owner-reported Pi work into Crew Activity
status: draft
author: kirocrew agent session, directed by livebruce
created: 2026-10-28
last-audited: 2026-10-28
audited-at: 0cf608a1
doc-pr: null
implementation-prs: []
tracking-issues: [25135]
supersedes: []
superseded-by: []
---

# RFC: Pi task visibility: import owner-reported Pi work into Crew Activity

> **Status:** `draft`. Acceptance is requested from a maintainer; the status
> flips to `accepted` when one records it in Open questions below. Nothing is on
> main. Baseline verified at `0cf608a1` (2026-10-28): `pi-acp` (0.0.34) forwards
> normal ACP tool execution frames (`tool_call`, `tool_call_update`), but does
> not bridge extension-local EventBus or subagent RPC channels. `_native_subagent_close_all`
> in `src/kiro_crew/dashboard/chat_turn/native_cards.py` closes open native cards at
> parent chat turn end, which causes running external Pi tasks to be falsely marked
> complete if routed through that existing native roster path. Tracks [#25135](https://github.com/kirodotdev/KiroCrew/issues/25135).

## Summary

When KiroCrew runs Pi as its agent harness, background tasks launched by Pi extensions
(`bg_run`, `bg_delegate`, `Fusion`) return a launch receipt immediately while execution
continues asynchronously. KiroCrew currently sees only the tool call completion.

This RFC proposes a session-scoped observation bridge for KiroCrew to receive
authoritative task lifecycle events from the running Pi process and display them as
externally owned tasks in KiroCrew's Activity panel.

Phase 1 is strictly **read-only** and covers only `pi-background-tasks` (Host API v1).
Task lifecycles remain owned by the Pi task registry: a parent chat turn ending does
not mark background tasks complete. `pi-subagents` and mutating controls (stop, rerun,
steer) are deferred to separate follow-up RFCs.

## Motivation

### Current state

1. **Crew-owned execution works:** Running `spawn_run(backend="pi")` or executing
   through Task Runner with a Pi harness is tracked by KiroCrew because KiroCrew
   initiates and supervises the session lifecycle.
2. **Pi-owned background execution is unobserved:** When a user chats with a Pi agent,
   tools such as `bg_run` return a launch receipt. Under ACP, `pi-acp` translates this
   to a completed `tool_call`. The actual job continues in the background, but KiroCrew
   has no further visibility into progress, output, or completion.
3. **Card lifecycle mismatch:** In KiroCrew, native subagent cards are closed when the
   parent turn ends (`_native_subagent_close_all`). Reusing this mechanism for Pi
   background tasks falsely marks running tasks as completed.

### Available upstream interfaces in Pi

The installed Pi packages already expose stable, versioned host interfaces:
- **`pi-background-tasks` (Host API v1):** In-process EventBus channels
  (`pi-background-tasks:host:{request,response,changed}:v1`) supporting capability
  discovery, subscription, snapshot listing, revisioned change broadcasts, and
  stable task IDs.
- **`pi-subagents` (RPC v1):** Separate in-process RPC (`subagents:rpc:v1:*`) with
  fleet status and async snapshots.

Neither interface is currently exported across process boundaries by `pi-acp`.

## Goals

- Display registered Pi background tasks in KiroCrew Activity for the originating chat session.
- Preserve task state across parent chat turn boundaries, reloads, and reconnects.
- Enforce strict session isolation: a KiroCrew chat sees only tasks from its own linked Pi process.
- Read-only observation in Phase 1: zero execution or control authority granted over the bridge.
- Keep existing Crew-managed tasks and native Kiro/KAS subagent cards untouched.

## Non-goals

- Scanning `~/.pi` directories, process tables, or guessing ownership by `cwd`.
- Supporting arbitrary unregistered Pi extensions in Phase 1.
- Exposing task controls (`stop`, `rerun`, `pause`, `steer`, or result-consumption).
- Bundling `pi-subagents` into Phase 1 (requires a distinct adapter and identifier mapping).
- Building a cross-agent universal BI status board (tracked separately in [#9712](https://github.com/kirodotdev/KiroCrew/issues/9712)).

## Design

### 1. Ingestion architecture

```text
Pi Process
  pi-background-tasks (Host API v1)
         │
         ▼ (in-process EventBus subscription)
  Pi Bridge Extension
         │
         ▼ (local authenticated loopback transport, scoped by session token)
KiroCrew Host Gateway
  External Task Store (session-scoped)
         │
         ▼
  Activity Panel ("Pi task" external card)
```

### 2. Transport and security boundary (Phase 0 Spike)

Because `pi-background-tasks` is in-process, an extension running inside the Pi process
subscribes to `pi-background-tasks:host:changed:v1`.

- **Transport:** Loopback HTTP / IPC endpoint on the KiroCrew host gateway.
- **Authentication:** At spawn, KiroCrew provisions a high-entropy, short-lived secret
  passed securely to the Pi bridge extension.
- **Security requirement:** The secret must never be exposed in model-visible tools,
  shell child processes, or conversation transcripts.
- **Authority:** The gateway derives session ownership from the authentication token,
  never from an unverified caller-supplied `session_key` body field.
- **Phase 0 stop gate:** If Phase 0 cannot prove secure token isolation and caller
  verification on all supported OS platforms, implementation stops and requests a
  protocol-level `pi-acp` extension upstream instead of shipping an unauthenticated
  local listener.

### 3. Task identity and model

A task observation is keyed by:
`(crew_session_key, pi_session_id, activation_id, source, owner_task_id)`

- `source`: `"pi-background-tasks"`
- `owner_task_id`: authoritative string ID assigned by Pi
- `state`: `"running"`, `"succeeded"`, `"failed"`, `"stopped"`, `"stale"`, or `"unknown"`

Imported tasks are stored in an **External Task Store** separate from Crew's internal
`SubagentInfo`. They are rendered in the Activity panel with a distinct "Pi task" tag.

### 4. Lifecycle & state reconciliation

- **Connect / Subscribe:** The extension registers, verifies protocol compatibility,
  subscribes to change events, and requests an initial task snapshot.
- **Incremental updates:** Incoming changed events update rows matching the exact key
  only if the revision number is greater than the current stored revision.
- **Turn end:** When the parent assistant turn finishes, KiroCrew leaves external
  task cards intact.
- **Disconnect / Exit:** If the Pi process exits or the lease expires without a terminal
  event, the task is marked `"stale"` or `"disconnected"`—never `"succeeded"`.
- **Reconnect:** On session reload or reconnect, the bridge fetches a fresh snapshot to
  reconcile state before streaming incremental events.

## Migration and Phases

| Phase | Scope | Exit Criteria |
|---|---|---|
| **0: Contract Spike** | Validate companion extension, credential isolation from model/shell, and OS peer verification | Verified token isolation; no credentials leaked; safe no-op if API is absent |
| **1: Read-Only MVP** | Read-only observation of `pi-background-tasks` in Activity for active chat | External task cards reflect live state across turn end; no duplicate cards on reconnect |
| **2: Reconnect Hardening** | Snapshot reconciliation, lease expiry, and bounded metadata retention | Stale/disconnected states displayed accurately on process crash or disconnect |
| **3: Subagents RFC** | Separate design for `pi-subagents` RPC mapping and distinct run IDs | Separate RFC accepted |
| **4: Task Controls** | Explicit `stop` command routing with terminal confirmation | Exact routing proven; security review signed off |

## Backward Compatibility

Fully additive:
- If the bridge is disabled or Pi background tasks are not installed, KiroCrew and `pi-acp`
  behave exactly as they do today.
- Crew-managed `spawn_run` and Task Runner execution paths are untouched.
- Native Kiro/KAS subagent lifecycles are unchanged.

## Security Considerations

- **Read-Only Surface:** The Phase 1 ingestion route accepts only task state snapshots
  and lifecycle events. It exposes no execution, steering, or cancellation routes.
- **Secret Redaction:** Task titles and progress text are treated as untrusted strings:
  secret-shaped tokens are redacted, and text is rendered without active HTML/Markdown links.
- **Rate & Memory Limits:** Ingestion rate is capped (max updates per second, max active
  tasks per session, payload size ceiling) to prevent denial of service or UI flooding.

## Open Questions

1. **Activity vs Dedicated Page:** Should external Pi tasks appear directly in the
   Activity sidebar, or under a dedicated "Pi Tasks" view? (Proposal: Activity sidebar
   with clear external ownership badge).
2. **Transport upstreaming:** Should KiroCrew maintain this companion extension bridge,
   or collaborate with `pi-acp` maintainers to standardize an extension-event tunneling
   channel in ACP?
3. **Retention Policy:** Should completed external tasks expire with the chat turn
   session or persist in session history? (Proposal: expire with session memory).

---
*Reference Research: `source/research/pi-task-tracking.md` and `source/research/rfc-pi-task-visibility-proposal.md`.*
