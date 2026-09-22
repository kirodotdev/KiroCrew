# Claim-push: event-driven caller identity for pooled MCP stubs

The claim push is the primary identity-repair path; the recaller poll remains as a
fallback, so both designs below are live behaviour.

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

1. **Register carries `ancestor_pids` and, for gateway-injected entries,
   `stub_session_token`** (`stub.py`). The ancestor chain is nearest first. It
   matters because the PID recorded for a runtime can sit several layers above
   the stub's immediate parent. The token names one ACP session on a runtime
   that may host several; without it a shared-session sub-agent is
   indistinguishable from its parent by process ancestry alone.
2. **gatewayd indexes each connection under every usable ancestor PID**
   (`_CONN_INDEX: pid → {_StubConn}`; `gatewayd.py`). It merges the stub's
   register-time chain with the host chain derived from the kernel-attested peer
   PID, so PID namespaces cannot make every claim miss. `_StubConn` stores the
   caller, per-PID start tokens, and the optional session token; the forward
   loop re-reads the caller per frame.
3. **The gateway pushes a claim on rekey** (`claim.py`, hooked into
   `AcpClient.rekey` and `SessionHandle.rekey`): a one-shot connection sends
   `{"type": "claim", "pid": P, "pid_start_id": T, "stub_session_token": U,
   "caller": {...}}` and reads one ack. `pid_start_id` is the runtime's process
   start token (`platform_compat.get_process_start_id`; `None` where
   unavailable). The session token is omitted for legacy/tokenless runtimes.
   Fire-and-forget (`schedule_claim`), bounded at 5 s, no-ops cleanly when
   preconditions are missing.
4. **gatewayd applies the claim** (`_apply_claim`). It records the token binding
   even when no stub has registered yet. Among connections indexed under P, a
   token-bearing claim retargets only a connection with the same token or no
   token; a connection positively naming another session is left alone. A
   tokenless claim retains the legacy PID-wide behavior. Definite process-start
   token mismatches are skipped and audited. Each caller change is SEL-audited
   (`mcp-gateway.caller-claim`), and idempotent re-claims are silent.

## Trust model

- The stub-initiated `recaller` stays **deny-by-default**: it may only move a
  key-less connection to a valid identity — a compromised stub must not pivot
  an existing identity.
- The gateway-initiated `claim` may **replace** an existing identity, but a
  session token narrows replacement to that session's connections (plus legacy
  tokenless connections). Trust is established by the local transport's
  positive same-principal check: Linux `SO_PEERCRED`, macOS `LOCAL_PEERCRED`,
  or the Windows named-pipe owning-process SID. POSIX mode bits and the Windows
  owner-only DACL are defense in depth.
- Malformed claims (non-int pid, pid ≤ 1, empty/missing session key) update
  nothing and are audited as denied.
- A claim naming a **recycled PID** never lands on the pre-recycle
  connection: the per-connection start-token check above is the guard. This
  is a correctness/attribution boundary, not a principal boundary.

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
- `src/kiro_crew/mcp_gateway/stub.py` — `ancestor_pids` on register; unbounded
  backoff recaller
- `src/kiro_crew/acp/client.py`, `src/kiro_crew/acp/session_provider.py` —
  `rekey()` claim hooks
- `test/test_mcp_gateway_claim.py` — functional + unit coverage
