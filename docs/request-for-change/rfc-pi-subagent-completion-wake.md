---
title: Pi subagent completion wake for ACP chat
status: draft
author: <contributor>
created: 2026-11-04
last-audited: b963f9883
implementation-prs: []
tracking-issues: []
---

# RFC: Resume a Pi-backed chat after an async subagent completes

> **Proposal.** This RFC records an observed failure mode and the host integration
> needed to address it. It is not evidence that the proposed bridge is implemented
> or that the historic Pi child failed.

## Summary

When an assistant starts an asynchronous `pi-subagents` child, the Pi extension
owns its lifecycle and its completion wake. KiroCrew's ACP host does not currently
bridge that Pi process-local completion into a KiroCrew-owned continuation. Add a
negotiated, authenticated, session-scoped notification path from the Pi ACP process
to its owning KiroCrew chat. On an eligible terminal event, KiroCrew may issue one
continuation to the same still-owned Pi session so the assistant can consume the
completion and finish its response without a human status prompt.

The initial implementation is gated on a contract spike proving that `pi-acp` can
provide trustworthy owner identity and deliver the event without exposing a secret
to the model, shell, or child processes. If that cannot be proven, do not implement
a loopback listener or infer completion from ACP tool text; stop and report the
blocker.

## Motivation and evidence

The user reported that the parent conversation stopped before finishing while
asynchronous reviewers remained active. Conversation history shows Pi `subagent`
tool launches and a quiet parent interval; it does not prove child failure or
whether a completion notification was generated, lost, or ignored.

At KiroCrew commit `b963f9883`, the ACP client owns the child process and session
lifecycle (`src/kiro_crew/acp/client_lifecycle.py`, `src/kiro_crew/acp/client_start.py`)
and ACP turn dispatch (`src/kiro_crew/acp/client_turn.py`). KiroCrew completion
injection elsewhere applies to KiroCrew-owned subagents; static search found no
Pi-subagents completion bridge in `src/` or `test/`. `pi-subagents` documents that
the embedding host owns whether completion wakes can be delivered: the extension
uses `pi.sendMessage(..., { triggerTurn })` while busy, and `pi.sendUserMessage`
when the parent is idle; session shutdown disposes its notifier. The installed
`pi-acp` boundary is a separate process and does not forward the extension's
process-local EventBus to KiroCrew. These facts establish a missing integration
seam, not the exact cause of the observed delay.

## Goals

1. Surface an owner-reported terminal completion to the exact KiroCrew chat and
   Pi session that launched the child.
2. Allow a single safe continuation when the parent turn is idle and the session
   remains live, owned, authorized, and not user-stopped.
3. Make notifications bounded, deduplicated, observable, and recoverable after a
   short transport interruption without polling task state indefinitely.
4. Fail closed when identity, lifecycle, policy, or delivery outcome is ambiguous.
5. Prove the no-human-prompt path with a deterministic integration regression test.

## Non-goals

- Import generic Pi background tasks or implement a task dashboard; that is the
  separate read-only visibility proposal.
- Replace KiroCrew-owned subagent completion delivery.
- Guess completion from assistant prose, ACP tool-call output, child process exit,
  filesystem scans, or lack of activity.
- Retry a model turn whose delivery or execution outcome is ambiguous.
- Add controls to stop, steer, rerun, or collect Pi-owned children.
- Change the global Pi host, user configuration, or live gateway as part of this
  feature.

## Design

### Ownership and transport

The Pi-side integration must report through a host-owned, versioned transport. The
host binds each registration to an opaque KiroCrew session key, ACP session ID,
Pi activation epoch, and process identity. Those values are assigned by the host;
body-provided identifiers are never authority. A per-activation capability is
short-lived and unavailable to model-visible tools, shell commands, logs, child
processes, and persisted user configuration. The receiver authenticates the peer
and validates the binding on every message. Loopback address or a claimed session
ID alone is not authentication.

Phase 0 must identify a supported `pi-acp` extension/host seam and demonstrate
credential isolation and peer authentication on every supported OS. Prefer a
negotiated upstream `pi-acp` event protocol or a host-owned authenticated IPC
channel. If neither is available, stop; do not ship an unauthenticated HTTP
listener, parent-process polling, or a credential passed in ordinary environment
variables or model-readable files.

The message is a bounded terminal receipt, not arbitrary extension data: protocol
version, opaque owner binding, activation, opaque run identity, monotonic or
unique event identity, reported terminal state, and bounded safe summary. Accept
only statuses explicitly declared terminal by the negotiated contract. Keep child
result text out of logs and transport diagnostics; the parent obtains its own
completion context through the owner-supported Pi continuation path.

### Continuation state machine

For each authenticated completion receipt, atomically reconcile by
`(owner binding, activation, run identity, event identity)`. Ignore duplicates and
stale activations. A completion received while the parent turn is busy is queued
for its next safe boundary; it must not interrupt or steer an unrelated in-flight
turn. At an idle boundary, claim one continuation only if the exact ACP session is
still live and owned, no person has stopped/replaced/reset it, policy permits an
unattended continuation, and the receipt is still eligible. Claiming is
idempotent. Issue a single bounded continuation referencing the completion; never
replay the original user prompt or prior tool calls.

If submission is rejected before it begins, retain an explicit pending/refused
state and surface it to the user. If submission outcome is unknown (disconnect,
timeout, lost acknowledgement), do not retry automatically: mark it ambiguous and
require owner reconciliation or a human action. A second terminal event cannot
silently trigger another continuation until the first continuation reaches a
known terminal state. Bound pending receipts per session and expiry; overflow is
visible and fails closed.

Session replacement, user Stop, cancellation, restricted-memory boundary, logout,
policy change, and gateway shutdown revoke or invalidate pending claims according
to their owning lifecycle. A stale callback cannot wake a successor session.
The implementation must use existing session leases and turn ownership guards;
it must not create a parallel session-control path.

### User-visible result

The parent continuation consumes the Pi owner's completion context and writes its
final report through the ordinary chat turn. If the owner cannot provide that
context or the continuation fails, show a clear pending/failed indication in the
owning chat; never report success based solely on a terminal signal. Do not send
cross-session or cross-channel output through this feature.

## Rollout and exit criteria

| Phase | Work | Exit criteria |\
|---|---|---|\
| 0 — contract spike | Identify host-supported notification seam, authenticate exact process/session/activation, prove secret isolation and terminal-event semantics | A deterministic host-to-KiroCrew probe succeeds on supported platforms; model and shell cannot read the capability; unsupported hosts fail closed. If any condition fails, stop and revise the transport design. |\
| 1 — receipt path | Validate and reconcile bounded, owner-reported terminal receipts without issuing a model turn | Tests prove binding, stale epoch rejection, duplicate suppression, bounded inputs, disconnect handling, restricted-session isolation, and no tool-text inference. |\
| 2 — safe continuation | Add one-shot continuation at a safe idle boundary using existing session ownership and policy controls | Deterministic test proves a child completes after the assistant turn ends, KiroCrew wakes the same parent once, parent consumes the completion and reports final output without a user prompt. Stop, replacement, timeout/unknown dispatch, duplicate, and race tests prove no replay or duplicate side effects. |\
| 3 — rollout | Opt-in behind a feature gate; structured diagnostics without task content; disable path | Feature-off and unsupported-host behavior matches current ACP; isolated preview passes; no live gateway cutover without separate authorization. |\

Each phase can be abandoned independently. Phase 2 is blocked until Phase 0
proves secure host integration. Implementation updates this RFC's status and
audited commit in the same change.

## Backward compatibility

The bridge is optional and negotiated. Hosts that do not support it retain current
Pi ACP behavior; no completion is inferred. KiroCrew-owned subagent lifecycle and
existing ACP sessions remain unchanged when disabled. No database migration is
required for the first in-memory receipt implementation; any durable receipt store
requires a follow-up design and retention review.

## Security considerations

The bridge crosses a process boundary and can cause an unattended model turn.
Host authentication, exact session/activation binding, existing policy checks,
user-stop precedence, replay protection, bounded payload/rate, no secret logging,
and fail-closed handling of uncertain submissions are mandatory. A completion
receipt is untrusted input, not authorization to execute a tool or to access a
session. Summary text is not inserted as trusted instructions; it must be consumed
only through the Pi owner's validated completion mechanism. Session-scoped
capabilities must not survive process replacement.

## Alternatives considered

1. **Use KiroCrew's existing subagent injection:** it only owns Crew-spawned
   children, not jobs launched and managed by the Pi extension.
2. **Parse ACP tool output or assistant text:** launch receipts are not terminal
   events and text is neither stable nor authoritative.
3. **Poll Pi process/session files:** the EventBus is process-local; scans cannot
   establish child ownership, terminal state, or safe continuation authority.
4. **Keep the parent alive with indefinite polling:** wastes resources and does
   not fix a reclaimed host session or establish event identity.
5. **Automatically resend the prior user prompt:** can repeat completed side
   effects and is unsafe when dispatch acknowledgement is lost.

## Open questions

1. Which upstream/host-supported `pi-acp` transport can authenticate a Pi
   activation without exposing a credential to the model or its shell/children?
2. Can `pi-subagents` provide a stable, owner-scoped terminal receipt and a
   documented way for the host to let the parent consume the completion after the
   original ACP turn has ended?
3. Which unattended-turn policy should gate an automatic final synthesis, and
   what visible state should represent a blocked continuation?

Phase 0 must resolve questions 1 and 2 before code can ship. No unsafe fallback is
approved.
