# Security Event Log (SEL) Module

## Overview

Immutable, tamper-evident audit trail for all tool invocations, MCP calls, and dashboard API mutations. Implements transactional event logging per Amazon Security Event Logging Standard.

See also the SEL section in [`security.md`](security.md) for the threat-model view of these events.

Storage: `~/.kiro/crew/security_events.jsonl` (append-only JSONL with HMAC-SHA256 chain).

## Event Schema

Each entry records:

| Field | Description |
|-------|-------------|
| `event_id` | Unique 16-char hex identifier |
| `timestamp` | ISO 8601 UTC |
| `event_type` | `tool_invocation`, `api_access`, `config_bounds_clamped`, `governance_decision`, `governance_degraded` |
| `caller_identity` | Session key (e.g. `dashboard:abc`, `cron:xyz`, `subagent:123`). API-access events from mixed-internal endpoints that validate `X-Internal-Caller` (the chat folder writes) carry the internal caller's declared **component name** here — e.g. `kirocrew-dashboard`, or `unknown-internal` for an authenticated internal caller that declared no recognized name (a defined, warned state, not log corruption); `source` stays in the interface vocabulary (`mcp`) for those events |
| `agent` | Agent name (`kirocrew`, custom agent name) |
| `source` | Interface: `slack`, `dashboard`, `cli`, `cron`, `subagent`, `taskrunner`, `mcp`, `background`, `acp` (ACP-transport events, e.g. `tool_interrupted`), `token_auth` / `refresh_tokens` (dashboard auth), `host` (the `_host` sentinel — an in-process host action like app activation / workspace admission), `unknown` (empty/unrecognized session key, which must NOT be mis-tagged `slack`). This is a closed interface vocabulary — component attribution does not extend it; see `caller` below |
| `operation` | Tool name or `METHOD /api/path` |
| `tool_kind` | Tool category (`execute_bash`, `fs_write`, `mcp_core`, `mcp_cron`, etc.) |
| `outcome` | `invoked`, `auto_approved`, `auto_approve_declined` (a name-based auto-approve was withheld by the name-grant check and the request took the surface's normal path — see `name_grant.log_decline`), `approved`, `rejected`, `denied`, `completed`, `failed`, `clamped`, `degraded` (a governance chokepoint failed OPEN), `one_shot_completed` (a one-shot cron consumed by its own completion — an automated removal, not an operator delete) |
| `resources` | Affected resources summary (redacted, then truncated to 500 chars — see `metadata`) |
| `downstream_service` | MCP server name if applicable (`kirocrew-core`, `kirocrew-cron`, `internal-mcp`) |
| `request_id` | ACP permission request ID |
| `error` | Error message if failed/denied |
| `prev_hash` | HMAC of previous entry (chain link) |
| `entry_hash` | HMAC-SHA256 of this entry |
| `metadata` | Additional context (approval reason, step index, etc.). Free-form string values are **redacted at write time**: the writer applies `security.redact` (credential + exfiltration-URL passes) to string values at any nesting depth before the entry is hashed and persisted, so caller-supplied text (a search query, a document title) never lands a secret on disk. Keys and non-string values pass through; the caller's dict is never mutated (the writer redacts a copy). The same write-time pass covers the free-form top-level strings `operation` / `resources` / `error` (an exception message can quote a command body or URL); identity-shaped fields (`caller_identity`, `agent`, `source`, `downstream_service`, `request_id`) are constrained vocabularies and stay verbatim. Where a `log_*` helper CLIPS a field to 500 chars it redacts first and clips second: clipping first can cut a credential in half, and the surviving prefix matches no full-token grammar, so the writer's pass could not recover it. The HMAC chain signs the redacted bytes |

The `config_bounds_clamped` event (`outcome=clamped`, `source=background`, `operation=config.load`, `caller_identity=config_loader`) is emitted by `config/loader.py`'s `_log_config_clamp_event` when an out-of-range security-bounded knob (`agent.subagent_auto_max` / `agent.max_subagents` / `agent.subagent_max_turns` / `session.pool_size`) is clamped to its API-enforced ceiling at load time, recording `metadata` `{file_value, clamped_to, min, max}`. Best-effort: a SEL failure never makes config loading raise.

## Integrity

- HMAC-SHA256 chain: each entry signs over the previous entry's hash
- HMAC key: `~/.kiro/crew/trust/sel_hmac.key` (32 random bytes, `chmod 600`, inside an owner-only `trust/` dir, `chmod 700`). The key deliberately lives OUTSIDE the log's directory: an actor who can rewrite the log dir must not also be able to read the key and re-sign a clean-looking chain that `verify_integrity()` accepts. A legacy key at `~/.kiro/crew/sel_hmac.key` is migrated in atomically (`os.replace`) on the next `SecurityEventLog` init — same key bytes, so every existing chain still verifies without re-signing; if BOTH paths exist, the LEGACY key wins and overwrites the destination: `trust/` was not deny-listed before the migration release, so a file already at the destination on a legacy install is untrustworthy (agent-plantable), while the legacy leaf was deny-list-protected all along; a linked `trust` dir or key file is likewise removed (link only, never its target) before use. `sel_hmac_key_path()` reports the RESOLVED path in use, so `session_pid_sig` follows automatically. A read-only config dir that blocks creating `trust/` keeps signing with the legacy file (chain continuity beats relocation). Rollback caveat: downgrading the binary after migration mints a fresh key at the legacy path; on re-upgrade that legacy key wins and replaces the trust-dir key, so chain entries signed BEFORE the downgrade will no longer verify — restore a consistent key from backup around a downgrade if chain continuity matters.
- **Key + log are on the sensitive-path floor (`cdf82704`):** the `trust` directory (whole-dir entry), the legacy `sel_hmac.key` leaf, and `security_events.jsonl` are in `security._SENSITIVE_HOME_DIRS`, so the audited agent's `fs_read`/file-edit tools (gated by `is_sensitive_path()`) cannot read the key to forge the chain or rewrite the log. The gateway's own writer/reader (`sel.py`, `dashboard/session_health.py`) opens the files directly and bypasses that gate. Residual: the key still lives in the agent's home namespace — a deeper out-of-process signer is future hardening.
- **Read paths pin the segment directory (#4999):** the read-side readers (`recent`, `verify_integrity`) open `security_events.d` itself through `_open_segment_dir` before enumerating; a directory that refuses to pin — planted link, non-directory, or vanished — contributes NO segments to any read (fail closed; a missing dir was already "no segments"), instead of being walked by name. Enumeration stays the bounded `_SEGMENT_SCAN_CAP` walk on every platform, but where the pin carries a descriptor it goes through `os.scandir(pin.fd)` — a path swap can neither redirect nor empty the scan (immune to the swap-mid-read-then-restore shape) — and only the identity-pin platform revalidates the directory's identity after the walk, failing closed on a mismatch. Where directory descriptors exist, every per-file open (`_open_segment` with `dir_fd`) also resolves RELATIVE to the pinned descriptor, so a swap after enumeration still cannot redirect a read; Windows has no directory descriptors, so its pin revalidates the directory's `lstat` `(st_dev, st_ino)` identity before each child open instead, with the residual between revalidations bounded by the rotation-time repair (`_ensure_segment_dir` unlinks a linked segment dir at rotation/prune). The per-file funnel (`O_NOFOLLOW`/`O_NONBLOCK`, descriptor `fstat` regular-file check, name↔descriptor identity) is unchanged, and the LIVE log is never pinned: its writer follows an operator's symlink, so its readers must too. Because the swap is itself tampering, `verify_integrity(detailed=True)` reports a THIRD outcome — `history_verifiable=False` with a `reason` — when the directory refused to pin (planted link, not a directory, an actual directory the OS refused to open, or one that vanished between the pin's `lstat` and its open — every pin failure except ABSENCE confirmed at first sight) or was replaced mid-verification, and the CLI (`kirocrew security verify`) and `GET /api/sel/verify` surface it (`Audit history UNVERIFIABLE` / `integrity: "unverifiable"`) instead of reporting intact over the live log alone; the CLI derives its live-log clause from the same pass's counts, so a tampered live log is reported as such rather than "intact". A directory that simply does not exist yet (fresh install) stays verifiable.
- Verification: `verify_integrity()` walks the chain and reports tampered entries
- Append-only: no in-place edits; pruning rewrites with chain rebuild
- **Second protocol anchored on this key — domain-separated:** `session_pid_sig.py`
  authenticates the `session_pid_<pid>.txt` -> session-key mapping consumed by
  strict MCP identity resolvers. It does **not** sign with the raw
  `sel_hmac.key`; it derives a purpose-specific subkey
  (`HMAC(sel_hmac.key, "kirocrew.session_pid.sig.v1")`) so the sidecar MAC and
  the SEL audit chain never share a signing key — a MAC minted under one
  protocol is valueless to the other (no cross-protocol confusion/replay). The
  key file remains a single on-disk trust root; only `SecurityEventLog` ever
  *creates* it. **Recorded acceptance — widened compromise impact:** anchoring
  session identity here means compromise of `sel_hmac.key` no longer only
  permits forging the audit chain — it also permits minting valid
  session-identity sidecars and driving state-mutating MCP tools against
  another session (cross-session state mutation). The likelihood of compromise
  is unchanged (same sensitive-path floor); the *impact* grew, and any future
  hardening of this key (the out-of-process signer above, issue #302) must
  treat `session_pid_sig` as a dependent of equal weight. See
  `docs/system-specs/modules/session.md` for the sidecar contract.

## Async Writer

`log()` is off the hot path: callers enqueue the event on an unbounded
`queue.Queue` (never blocking) and a single daemon writer thread drains it,
computing the HMAC chain in enqueue order and batching up to `_QUEUE_DRAIN_BATCH`
events into one `open()`+write. The writer starts lazily on first `log()` and
registers an `atexit` flush.

- **Durability**: eventually-durable, not synchronously-durable — a crash/kill
  can lose at most the events still queued. Acceptable for an audit log; the
  hot path (e.g. per-message skill triggering) no longer pays fsync/lock latency.
- **Read-after-write**: `flush()` runs before every read path (`recent`,
  `verify_integrity`, `prune`) and on exit. It waits on a pending-event counter
  (a `threading.Condition`, race-free vs a bare queue-empty check), bounded by
  `_FLUSH_TIMEOUT_SECS` so a wedged writer can't hang a read.
- **Fallback**: if the writer can't be started, `log()` writes synchronously so
  an event is never silently dropped.
- **`sync=True`**: `SecurityEventLog(base_dir=..., sync=True)` writes each event
  inline (no thread) — used by tests that read the raw JSONL immediately after
  logging.

## Retention

Default 365 days. Pruned daily by heartbeat service (`_PRUNE_TICKS`).

## Integration Points

| Surface | What's Logged | Module |
|---------|---------------|--------|
| Slack handler | `tool_call` (invoked/denied), `permission_request` (all outcomes) | `slack/handler.py` |
| Dashboard chat | `tool_call` (invoked), `permission_request` (all outcomes) | `dashboard/chat.py` |
| TaskRunner | Permission requests during decomposition and step execution | `taskrunner.py` |
| Subagent | Permission requests during subagent execution | `subagent.py` |
| Background tasks | Permission requests via `_resolve_permission()` | `llm_helpers.py` |
| MCP core tools | `spawn_run`, `learn_add`, `task_run` calls and outcomes | `mcp_core.py` |
| MCP cron tools | `cron_add`, `cron_remove`, etc. calls and outcomes | `mcp_cron.py` |
| Session directives | Structured monitor create/update/stop application outcomes; every refusal records `denied` rather than `success` | `dashboard/session_directive_apply.py` |
| Dashboard API | All POST/PUT/DELETE operations via middleware, plus allowed and denied project-skill trust, app-slot, saved-workflow, and strict session-monitor read authorization decisions | `dashboard/server.py`, `dashboard/handlers/prompts.py`, `dashboard/handlers/workflows.py`, `dashboard/handlers/autonudge.py` |
| ACP worker-pool audit | Per-`tool_call` `auto_approved` `tool_invocation` (`source=subagent`), bounded by `_SEL_AUDIT_TIMEOUT_SECONDS` (5.0s) and offloaded off the event loop so a wedged SEL backend never gates dispatch. Two emitters: the knowledge LLMPool via `AcpClient._maybe_audit_tool_call` (gated on the `audit_source` ctor param, offloaded to `subprocess_executor()`); and **code-review-sage's ReviewPool**, which migrated to the shared `AcpRuntime` (no `audit_source`) and re-emits the same per-tool record itself | `acp/client.py`, `apps/builtins/code_review_sage/sage_lib/review_pool.py` |
| Structured monitor mutation audit | Critical `monitor_update` / `monitor_stop` invocation records are audit-before-mutation. Both singleton resolution and the synchronous write run in a worker thread, so SEL initialization or disk latency cannot block the gateway event loop | `autonudge_authz.py` |
| Token auth | `internal_auth`, `app_scope_check`, `dashboard_sessions_revoked`, `refresh_token_initial_mint`, `nonce_evicted` (`source=token_auth`) | `dashboard/token_auth.py` |
| Refresh tokens | `refresh_token_use`, `refresh_token_logout`, `access_cookie_revoked` (`source=refresh_tokens`) | `dashboard/handlers/auth_refresh.py` |
| ACP transport | `tool_interrupted` per-turn cancellation audit (`source=acp`) | `acp/client.py` |

## APIs

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/sel/events?limit=N` | Recent security events (max 1000) |
| GET | `/api/sel/verify` | HMAC chain integrity check |

## CLI

```
kirocrew security events [-n 20]   # Show recent events
kirocrew security verify            # Verify HMAC chain integrity
```

## Thread Safety

Singleton pattern. Two locks guard the chain, and they are always taken in this
order: the cross-process **chain lock** first, then the in-process
`threading.Lock`.

`threading.Lock` guards the chain state (`_last_hash`) and the file append inside
one interpreter, held only by the writer thread (and the synchronous fallback /
`prune`), never by enqueuing callers. Enqueue is lock-free via the thread-safe
`queue.Queue`.

A `threading.Lock` cannot order the gateway against the MCP server stdio
processes, which are separate processes sharing one log file — each holds its own
singleton with its own cached chain tip, so two of them chaining off the same
`prev_hash` fork the HMAC chain permanently. Cross-process ordering therefore
comes from an advisory lock on a sidecar file, `trust/security_events.lock`. The
sidecar lives in the trust subdirectory (owner-only, inside the sensitive-path
floor) so the audited agent cannot unlink or hold it out from under the writers;
a linked or hard-linked sidecar is refused rather than followed. When the trust
directory could not be created at init and the HMAC key fell back to its legacy
location beside the log, the lock is taken on the legacy key file itself — the
one sibling of the log the deny list has protected all along — rather than
failing every append on a mkdir that cannot succeed.

The chain lock is taken **before** `threading.Lock`. The reverse order would let
a cross-process wait stall the event loop indirectly: a writer thread holding the
thread lock while it waits leaves a loop-side critical audit blocking on that
lock, which has no bound of its own.

On the event-loop thread neither potentially-slow step may wait:

- **Acquire** — a SINGLE nonblocking `try_acquire_lock` attempt, then a
  fail-closed refusal. No retry and no sleep: a poll spin would sleep the
  gateway's event loop, stalling every session it serves, so contention is
  refused here and absorbed off-loop (the background writer and `prune` in an
  executor take the blocking lock).
- **Chain-tip read** — capped at a single tail chunk. A healthy log yields the
  tip from one read; only an already-corrupt multi-kilobyte tail would walk
  further, and exhausting the cap raises rather than returning a genesis tip.

Both refusals surface as `OSError`, which the append path turns into a rollback
plus warning, or into a propagated error for a `critical=True` audit — the
audit-or-deny contract. Off the loop, both steps are unbounded and recover fully.
