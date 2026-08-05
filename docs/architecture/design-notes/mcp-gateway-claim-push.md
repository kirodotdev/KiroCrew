# Claim-push: event-driven caller identity for pooled MCP stubs

Status: implemented. Supersedes the recaller poll as the primary
identity-repair path; the poll remains as a fallback.

## Problem

gatewayd stamps a caller identity (`_meta.kirocrew.caller`) on every MCP call
it forwards, so pooled backends know which session is calling. That identity
comes from one place: the `register` frame each stub sends when it connects.
No `session_key` on the register means every call from that connection is
anonymous.

Warm-pool runtimes register before any session owns them, so their key is
empty. The recaller repaired this by polling for
`session_pid_<pid>.txt` — but only for 180 s. Pool runtimes routinely idle
longer than that before being claimed; after the budget expired, the
connection stayed anonymous for life. Observed blast radius of an empty
caller:

- `spawn_run` records `parent_session=""` → subagent completion events fall
  back to notification-only (main agent never wakes),
- the FE "N agents running" indicator cannot attribute the subagent to its
  owning session (and historically ghost-attributed it to whatever session
  was active),
- pooled state-mutating tools (`learn_add`, memory writes) are refused.

A second, documented limitation: the recaller only handles the key's *first*
appearance. Re-claiming a pool runtime for a different session left the
caller stale.

## Design

The claim event — "session S now owns runtime PID P" — is born in the main
gateway process at warm-pool `rekey()` time, with both the session key and
the PID in hand. Claim-push makes the knower push instead of having the
downstream stub poll:

```
Pull (old): gateway rekey() ──write──> session_pid_<pid>.txt <──poll×180s── stub ──recaller──> gatewayd
Push (new): gateway rekey() ─────────────────── claim frame ───────────────────────────────> gatewayd
```

1. **Register carries `ancestor_pids`** (`stub.py`): the stub's parent PID
   chain, nearest first. The chain matters because the PID the gateway
   records for a runtime (`AcpClient._process.pid`) can sit several layers
   above the stub's immediate parent — the live topology is
   sandbox wrapper → kiro-cli → kiro-cli-chat → stub — and a single-level
   index made every claim miss (found in pod QA: claims applied to 0
   connections while the recaller fallback masked the failure).
2. **gatewayd indexes each connection under EVERY ancestor PID**
   (`_CONN_INDEX: pid → {_StubConn}`; `gatewayd.py`), so a claim naming any
   level of the runtime's process tree hits. `_StubConn` is a mutable holder
   for the connection's caller; the forward loop re-reads it per incoming
   frame, so an update takes effect on the very next call.
3. **The gateway pushes a claim on rekey** (`claim.py`, hooked into
   `AcpClient.rekey` and `SessionHandle.rekey`): a one-shot connection to the
   gatewayd socket sends
   `{"type": "claim", "pid": P, "caller": {...}}` and reads one ack frame.
   Fire-and-forget (`schedule_claim`), bounded at 5 s, no-ops cleanly when
   preconditions are missing.
4. **gatewayd applies the claim** (`_apply_claim`): every indexed connection
   under P gets the new caller, each change SEL-audited
   (`mcp-gateway.caller-claim`). Idempotent re-claims (same key) are silent.

## Trust model

- The stub-initiated `recaller` stays **deny-by-default**: it may only move a
  key-less connection to a valid identity — a compromised stub must not pivot
  an existing identity.
- The gateway-initiated `claim` may **replace** an existing identity. Trust
  basis: the unix socket is uid-gated 0700 — the same gate that authenticates
  `register` frames. Allowing replacement is what fixes re-claim staleness:
  the caller always tracks the *current* owning session.
- Malformed claims (non-int pid, pid ≤ 1, empty/missing session key) update
  nothing and are audited as denied.

## Fallback

The recaller poll is retained for claim-frame loss and gatewayd restarts
(a restart empties `_CONN_INDEX`; stubs reconnect and re-register, and a
still-key-less register restarts the poll). Its 180 s deadline is replaced by
unbounded polling with interval backoff (1.5 s → 30 s cap), so it can never
permanently strand a connection while costing a long-idle pool stub one
identity probe per 30 s.

## Interaction with transparent respawn

A backend death re-binds the stub connection to a fresh backend
(`_respawn_backend_for_stub`). The caller lives on the *stub connection*
(`_StubConn`), which survives the rebind — identity is preserved without any
claim-path involvement.

## Files

- `src/kiro_crew/mcp_gateway/claim.py` — frame builder + sender (stdlib-only)
- `src/kiro_crew/mcp_gateway/gatewayd.py` — `_StubConn`, `_CONN_INDEX`,
  `_apply_claim`, claim first-frame dispatch, per-frame caller pickup
- `src/kiro_crew/mcp_gateway/stub.py` — `parent_pid` on register; unbounded
  backoff recaller
- `src/kiro_crew/acp/client.py`, `src/kiro_crew/acp/session_provider.py` —
  `rekey()` claim hooks
- `test/test_mcp_gateway_claim.py` — functional + unit coverage
