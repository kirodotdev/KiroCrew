# Security Event Log (SEL) Module

## Overview

Append-only, tamper-evident audit trail for tool invocations, MCP calls, and dashboard API mutations: every security-relevant decision is written as one signed event. Retention is bounded by age (365 days) and by size (the newest 7 closed 32 MiB segments); whichever is tighter wins (see Retention).

See also the SEL section in [`security.md`](security.md) for the threat-model view of these events.

Storage: the live append-only log is `~/.kiro/crew/security_events.jsonl`.
At the size ceiling it rotates into `~/.kiro/crew/security_events.d/`; each
closed segment is an independent HMAC-SHA256 chain and the new live log starts
with a `sel_rotation` boundary event.

Member executions use the ordinary audit destination. Memory ownership does not
create separate diagnostic chains or OS-isolated log directories. The shared MCP
wrapper records attribution and outcomes for query and session-body tools without
their payloads, including rejected calls. Chat-history search keeps query lengths
and result counts in its handler audit, never the query text in the wrapper audit.
Other audit events, signing, verification,
credential redaction and explicit logging failures retain their existing behavior.

## Module layout

`src/kiro_crew/sel.py` is the import path every caller uses. It owns the record and the
write pipeline: `SecurityEvent` and every place that builds one (the `log_*` helpers and
`_rotation_event`), the `SecurityEventLog` singleton with its writer thread and queue,
`flush`, `_flush_batch` (redaction, then the chain lock, the rotation window, the append
and the forward) with the write-time redaction it applies, which thread may rotate
(`_may_rotate`), `_infer_source` / `audit_sources`, the startup warm and the hard-exit
drains, and the trust-root accessors `sel_hmac_key_path` and `_sel_hmac_key_bytes`. The
class's mechanisms live in owner modules, one responsibility each, and new work for a
responsibility goes to its owner:

| Module | Owns |
|---|---|
| `src/kiro_crew/sel_trust_root.py` | the HMAC key that signs the chain (where it lives, creation, validation, legacy migration) and the per-record MAC (`_compute_hash`, `_record_signature_matches`) |
| `src/kiro_crew/sel_reader.py` | reading the log back: `verify_integrity`, `recent`, segment admission, the backward line reader, and the read-side segment pins (`_open_segment`, `_open_segment_dir`) |
| `src/kiro_crew/sel_chain_lock.py` | the cross-process chain lock: the per-process hold registry, the loop-side single-shot acquire, the sidecar and its pinned directory (`_pin_lock_dir`) |
| `src/kiro_crew/sel_append.py` | chaining a batch onto the live log: the descriptor-checked append, re-anchoring after another process rotates or appends, `SelChainContention`, and chain-tip recovery (`_read_last_hash`) |
| `src/kiro_crew/sel_rotation.py` | keeping the log bounded: size rotation into `security_events.d`, segment naming and enumeration, count retention, and the age `prune` |

`src/kiro_crew/sel_request_provenance.py` is not one of these owners: it holds the
request-bound `forwarded` binding that the deny-audit boundary writes and
`log_api_access` reads (see `metadata.forwarded` below), and the class binds nothing
from it.

Each owner function whose first parameter is `self` is a `SecurityEventLog` method: the
class binds it by name (`_chain_lock = _sel_chain_lock._chain_lock`) under one comment
header per owner, so every member stays an attribute of the one class, and a patch of a
method on the class or on an instance reaches every caller. A bound member's
`__qualname__` and `__module__` name its owner (nothing reads them; a traceback and
`%(funcName)s` show the same function name), and its `self: SecurityEventLog` annotation
is there for type checkers only. Every class an owner defines keeps `kiro_crew.sel` as its
`__module__`, so a traceback still names `kiro_crew.sel.SelChainContention`. Owners log
under the `kiro_crew.sel` logger, import each other lowest layer first (the order of
`_OWNERS`), and never import `sel.py` at run time. `sel.py` keeps binding every name it
bound, serves the module-level names that moved (`_SERVED_BY_OWNER`), and a write through
it reaches every owner that binds the same object (`kiro_crew._facade_mirror`).
`test/test_sel_bindings.py` pins the member inventory, the binding and the reach.

## Event Schema

Each entry records:

| Field | Description |
|-------|-------------|
| `event_id` | Unique 16-char hex identifier |
| `timestamp` | ISO 8601 UTC |
| `event_type` | Stable event-family string chosen by the emitter. Common values include `tool_invocation`, `api_access`, `config_bounds_clamped`, `governance_decision`, `governance_degraded`, `output_anomaly`, `deny_event`, `push_allowed`, and `sel_rotation`; consumers must not treat this list as exhaustive. |
| `caller_identity` | Session key (e.g. `dashboard:abc`, `cron:xyz`, `subagent:123`). API-access events from mixed-internal endpoints that validate `X-Internal-Caller` (the chat folder writes) carry the internal caller's declared **component name** here — e.g. `kirocrew-dashboard`, or `unknown-internal` for an authenticated internal caller that declared no recognized name (a defined, warned state, not log corruption); `source` stays in the interface vocabulary (`mcp`) for those events. Middleware `api_access` rows use the label from `security_middleware.audit_actor`: `dashboard_user` for the full dashboard, `mcp_tool` for the headless API server (or an identity a deny site derived, such as an app label), with a `_via_proxy` suffix when the request carries a forwarding header. The suffix is kept for existing readers; read provenance from `metadata.forwarded` instead |
| `agent` | Agent name (`kirocrew`, custom agent name) |
| `source` | Emitting surface label. Session-key-derived values come from `_infer_source()` and the authoritative `audit_sources()` tuple (dashboard, cron, background, heartbeat, CLI, supported messaging channels, and so on). Specialized emitters also use fixed subsystem labels such as `mcp`, `acp`, `token_auth`, `refresh_tokens`, and `app`; the dataclass does not enforce one closed enum. |
| `operation` | Tool name or `METHOD /api/path`; for `output_anomaly`, the repair that ran (`options_footer_glued_text`: text the model wrote on the line of its own `[OPTIONS:]` footer was moved to its own line and labelled as the assistant's) |
| `tool_kind` | Tool category (`execute_bash`, `fs_write`, `mcp_core`, `mcp_cron`, etc.) |
| `outcome` | Emitter-defined result vocabulary (for example `invoked`, `approved`, `rejected`, `denied`, `completed`, `failed`, `clamped`, `degraded`, `one_shot_completed`, or `labelled`). Callers that expose this field externally must document the values they emit; the core dataclass does not enforce a closed enum. |
| `resources` | Affected resources summary (redacted, then truncated to 500 chars — see `metadata`) |
| `downstream_service` | MCP server name if applicable (`kirocrew-core`, `kirocrew-cron`, `internal-mcp`) |
| `request_id` | ACP permission request ID |
| `error` | Error message if failed/denied |
| `prev_hash` | HMAC of previous entry (chain link) |
| `entry_hash` | HMAC-SHA256 of this entry |
| `metadata` | Additional context (approval reason, step index, etc.). Free-form string values are **redacted at write time**: the writer applies `security.redact` (credential + exfiltration-URL passes) to string values at any nesting depth before the entry is hashed and persisted, so caller-supplied text (a search query, a document title) never lands a secret on disk. Keys and non-string values pass through; the caller's dict is never mutated (the writer redacts a copy). The same write-time pass covers the free-form top-level strings `operation` / `resources` / `error` (an exception message can quote a command body or URL); identity-shaped fields (`caller_identity`, `agent`, `source`, `downstream_service`, `request_id`) are constrained vocabularies and stay verbatim. `outcome` is NOT in the writer's set for the same reason, but `log_api_access` scrubs it at the helper: it reads as a vocabulary and is one for in-tree callers, while an installed app reaches that helper through `ctx.audit`, so the value can be caller text. The pass is the identity function on every in-tree spelling, so no existing row changes. Where a `log_*` helper CLIPS a field to 500 chars it redacts first and clips second: clipping first can cut a credential in half, and the surviving prefix matches no full-token grammar, so the writer's pass could not recover it. The HMAC chain signs the redacted bytes. **`metadata.forwarded`** (bool, `api_access` only): whether the audited HTTP request carried a forwarding header (`origin.is_proxied_request`). Recorded on every `api_access` row written while a request is being served. The gateway's auth and audit layers pass `log_api_access(forwarded=...)` explicitly (both servers' `sel_audit_middleware`, `_audit_denied`, and `token_auth`'s request-bound rows, including its returned 401/403 refusals). A handler's own row passes nothing and takes the verdict the deny-audit boundary, the outermost audit layer of both chains, binds for the request in `sel_request_provenance`; only that boundary writes the binding, so no handler, app or request body can choose the value, and an explicit `forwarded=` still wins. The binding lives as long as the request: work awaited inside it (`asyncio.to_thread`, an awaited task) records it, while a background task that outlives its request finds the binding closed. An absent key means provenance was not recorded (a row written outside any request, from a plain `threading.Thread`, or by a task after its request ended), never "made directly" |

The `config_bounds_clamped` event (`outcome=clamped`, `source=background`, `operation=config.load`, `caller_identity=config_loader`) is emitted by `config/loader_builders.py`'s `_log_config_clamp_event` whenever `_clamp_security_bounds()` changes an out-of-range field from the authoritative `_SECURITY_BOUNDED_FIELDS` table or one of its sentinel-aware follow-up checks. It records `metadata` `{file_value, clamped_to, min, max}`. Best-effort: a SEL failure never makes config loading raise.

The history extraction session (`_consolidate`) is classified as `background`,
like `_bg`; other unrecognized session keys retain the legacy `slack` fallback.

## Integrity

- HMAC-SHA256 chain: each entry signs over the previous entry's hash
- HMAC key: `~/.kiro/crew/trust/sel_hmac.key` (32 random bytes, `chmod 600`, inside an owner-only `trust/` dir, `chmod 700`). The key deliberately lives OUTSIDE the log's directory: an actor who can rewrite the log dir must not also be able to read the key and re-sign a clean-looking chain that `verify_integrity()` accepts. A legacy key at `~/.kiro/crew/sel_hmac.key` is migrated in atomically (`os.replace`) on the next `SecurityEventLog` init — same key bytes, so every existing chain still verifies without re-signing; if BOTH paths exist, the LEGACY key wins and overwrites the destination: `trust/` was not deny-listed before the migration release, so a file already at the destination on a legacy install is untrustworthy (agent-plantable), while the legacy leaf was deny-list-protected all along — with ONE shape excluded from that rule, which fails init closed instead of resolving either way: a legacy file too short to load as a key, sitting beside a destination that holds a usable one, is AMBIGUOUS and neither half may be acted on. Promoting the short legacy file would destroy the only copy of the key every existing record was signed with and then fail the length check on every later boot, so the loss is unrecoverable rather than merely wrong (a mixed-binary window can leave such a 0-byte file behind); ADOPTING the destination is no safer, because length is not trust — `trust/`, the legacy leaf, `security_events.jsonl` and `security_events.d` are ALL sandbox read-write leaves, so the audited agent can truncate the legacy key below the minimum, plant a long-enough destination whose bytes it knows, and forge whatever evidence a proof gate would consult, which would make every later SEL and session-identity MAC forgeable with no recovery path. With no operator-protected source to authenticate against, SEL does nothing and refuses: nothing is adopted, nothing is overwritten, both files survive for the operator, and the error names both paths and the remedy (restore from backup, or remove both to start a fresh chain). The refusal does not depend on what the log contains, so no branch of the decision can be unlocked by controlling the log. A short legacy file with no usable destination still reaches the length check exactly as it would without this carve-out; a linked `trust` dir or key file is likewise removed (link only, never its target) before use. `sel_hmac_key_path()` reports the RESOLVED path in use, so `session_pid_sig` follows automatically. A read-only config dir that blocks creating `trust/` keeps signing with the legacy file (chain continuity beats relocation). Rollback caveat: downgrading the binary after migration mints a fresh key at the legacy path; on re-upgrade that legacy key wins and replaces the trust-dir key, so chain entries signed BEFORE the downgrade will no longer verify — restore a consistent key from backup around a downgrade if chain continuity matters.
- **Key + log are on the sensitive-path floor:** the `trust` directory (whole-dir entry), the legacy `sel_hmac.key` leaf, `security_events.jsonl` and `security_events.d` are in `security._SENSITIVE_HOME_DIRS` ([security](security.md) owns the full enumeration), so the audited agent's `fs_read`/file-edit tools (gated by `is_sensitive_path()`) cannot read the key to forge the chain or rewrite the log. The gateway's own writer/reader (`sel.py` and its owner modules, `dashboard/session_health.py`) opens the files directly and bypasses that gate. Residual: the key still lives in the agent's home namespace — a deeper out-of-process signer is future hardening.
- **Read paths pin the segment directory:** the read-side readers (`recent`, `verify_integrity`) open `security_events.d` itself through `_open_segment_dir` before enumerating; a directory that refuses to pin — planted link, non-directory, or vanished — contributes NO segments to any read (fail closed; a missing dir was already "no segments"), instead of being walked by name. Enumeration stays the bounded `_SEGMENT_SCAN_CAP` walk on every platform, but where the pin carries a descriptor it goes through `os.scandir(pin.fd)` — a path swap can neither redirect nor empty the scan (immune to the swap-mid-read-then-restore shape) — and only the identity-pin platform revalidates the directory's identity after the walk, failing closed on a mismatch. Where directory descriptors exist, every per-file open (`_open_segment` with `dir_fd`) also resolves RELATIVE to the pinned descriptor, so a swap after enumeration still cannot redirect a read; Windows has no directory descriptors, so its pin revalidates the directory's `lstat` `(st_dev, st_ino)` identity before each child open instead, with the residual between revalidations bounded by the rotation-time repair (`_ensure_segment_dir` unlinks a linked segment dir at rotation/prune). The per-file funnel (`O_NOFOLLOW`/`O_NONBLOCK`, descriptor `fstat` regular-file check, name↔descriptor identity) is unchanged, and the LIVE log is never pinned: its writer follows an operator's symlink, so its readers must too. Because the swap is itself tampering, `verify_integrity(detailed=True)` reports a THIRD outcome — `history_verifiable=False` with a `reason` — when the directory refused to pin (planted link, not a directory, an actual directory the OS refused to open, or one that vanished between the pin's `lstat` and its open — every pin failure except ABSENCE confirmed at first sight) or was replaced mid-verification, and the CLI (`kirocrew security verify`) and `GET /api/sel/verify` surface it (`Audit history UNVERIFIABLE` / `integrity: "unverifiable"`) instead of reporting intact over the live log alone; the CLI derives its live-log clause from the same pass's counts, so a tampered live log is reported as such rather than "intact". A directory that simply does not exist yet (fresh install) stays verifiable.
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

The singleton itself is warmed at gateway startup: both async server start
paths await `warm_sel_singleton()` (an `asyncio.to_thread(sel)`, best-effort)
before building the middleware chain, so when the warm succeeds
`_init_locked` — blocking file I/O — never runs on the event loop as a
handler's first touch, and non-critical call sites need no per-site thread
hop (#8608). A failed warm logs a warning and leaves that init retry to the
first later touch, on its caller's thread. `critical=True` writes are
synchronous by design and their call sites still offload themselves when
reached from the loop.

Private-member authorization denials keep their typed 403 responses even when
SEL initialization or event submission fails. Their shared denial audit resolves
the singleton and submits the event in a worker thread, including after an
unsuccessful startup warm. Audit failure is diagnostic only: it cannot grant
access or allow the protected handler to read or mutate a resource. Critical
grant audits retain their audit-or-deny contract.

- **Durability**: eventually-durable, not synchronously-durable — a crash/kill
  can lose at most the events still queued. Acceptable for an audit log; the
  hot path (e.g. per-message skill triggering) no longer pays fsync/lock latency.
- **Read-after-write**: `flush()` runs before every read path (`recent`,
  `verify_integrity`, `prune`) and on exit. It waits on a pending-event counter
  (a `threading.Condition`, race-free vs a bare queue-empty check), bounded by
  `_FLUSH_TIMEOUT_SECS` so a wedged writer can't hang a read.
- **Hard exits**: the gateway's normal-shutdown exit and the restart-exec
  failure exit (`platform_compat.exit_after_failed_restart_exec`) drain an
  existing SEL singleton with `flush_audit_queue_before_hard_exit`, which uses
  the teardown executor and an outer deadline, before the gateway.log tail.
  Restart retains its inline off-loop drain. These best-effort drains avoid
  initializing a trust root during exit. The second-signal force exit does not
  drain SEL yet (#19042).
- **Fallback**: if the writer can't be started, `log()` writes synchronously so
  an event is never silently dropped.
- **Write failures**: when an append or the chain lock raises `OSError`, the
  writer retries the batch up to `_WRITE_RETRIES` times with a capped backoff.
  It does not retry an errno that cannot heal, or an append that may have left
  bytes on disk (the live log changed, or a stat could not tell). A batch it
  gives up on adds to `dropped_events`, a per-process counter that
  `GET /api/sel/verify` reports, and logs one ERROR per streak of failures.
- **`sync=True`**: `SecurityEventLog(base_dir=..., sync=True)` writes each event
  inline (no thread) — used by tests that read the raw JSONL immediately after
  logging.

## Retention

Two bounds, and the tighter wins:

- **Age:** 365 days (`_RETENTION_DAYS`), pruned daily by the heartbeat service (`_PRUNE_TICKS`).
- **Size:** the live log rotates at 32 MiB (`_SEGMENT_MAX_BYTES`) and only the newest 7 closed segments are kept (`_SEGMENT_KEEP`), so on a busy host evidence can age out before 365 days.

## Integration Points

| Surface | What's Logged | Module |
|---------|---------------|--------|
| Slack handler | `tool_invocation` rows (`operation` = tool name; `outcome` invoked / denied / approved / rejected / …) for tool calls and every permission decision | `slack/handler.py` |
| Dashboard chat | `tool_invocation` rows (`operation` = tool name; `outcome` invoked and every permission decision) | `dashboard/chat_runner.py` |
| Transport permission floor | `tool_invocation` refusals from `permission_floor.audit_refusal` (`source=transport_floor`, `outcome=rejected_transport_floor`) | `permission_floor.py` |
| ACP session cancel | `approval_cancel` rows with `outcome=rejected_on_cancel` for approvals pending when the session is cancelled | `acp/client_turn_control.py` |
| Chat persist seams | `output_anomaly` / `options_footer_glued_text` (`outcome=labelled`, `source` from the turn's session key, `metadata` `{count, chars, preview (redacted, 200 chars), directive_shaped}`) when a persisted turn carried text glued to its own `[OPTIONS:]` footer; skipped while SEL is cold so the first open never runs on the event loop | `dashboard/chat_turn/segment_flush.py` (`_log_glued_footer_text`) |
| TaskRunner | `tool_invocation` rows for permission decisions during decomposition and step execution | `task_planner.py`, `tool_permission.py` (`TaskrunnerRows`, through `task_executor.py`'s `sel`) |
| Subagent | Permission requests during subagent execution | `tool_permission.py` (`SubagentRows`, through `subagent.py`'s `sel`) |
| Background tasks | Permission requests via `_resolve_permission()` | `llm_helpers.py` |
| MCP core tools | `spawn_run`, `learn_add`, `task_run` calls and outcomes | `mcp_core.py` |
| MCP cron tools | `cron_add`, `cron_remove`, etc. calls and outcomes | `mcp_cron.py` |
| Session directives | Structured monitor create/update/stop application outcomes; every refusal records `denied` rather than `success` | `dashboard/session_directive_apply.py` |
| Dashboard API | All POST/PUT/DELETE operations via middleware, plus allowed and denied project-skill trust, app-slot, saved-workflow, strict session-monitor read authorization, and in-app update authorization decisions (`update.arm` / `update.approve`; denial audits are best-effort, while a granted approval fails closed when its audit is unwritable) | `dashboard/server_runtime/middleware_chain.py`, `dashboard/server_runtime/security_middleware.py`, `dashboard/handlers/prompts_caller_gates.py`, `dashboard/handlers/prompts_skill_catalog.py`, `dashboard/handlers/workflows.py`, `dashboard/handlers/autonudge.py`, `dashboard/handlers/updates.py` |
| ACP worker-pool audit | Per-`tool_call` `auto_approved` `tool_invocation` (`source=subagent`), bounded by `_SEL_AUDIT_TIMEOUT_SECONDS` (5.0s) and offloaded off the event loop so a wedged SEL backend never gates dispatch. Two emitters: the knowledge LLMPool via `AcpClient._maybe_audit_tool_call` (gated on the `audit_source` ctor param, offloaded to `subprocess_executor()`); and **code-review-sage's ReviewPool**, which runs on the shared `AcpRuntime` (no `audit_source`) and emits the same per-tool record itself, plus its `rejected_*` permission rows (`rejected_hook_deny`, `rejected_transport_floor`) | `acp/client_tool_events.py`, `acp/client_timeouts.py`, `apps/builtins/code_review_sage/sage_lib/review_pool.py` |
| Structured monitor mutation audit | Critical `monitor_update` / `monitor_stop` invocation records are audit-before-mutation. Both singleton resolution and the synchronous write run in a worker thread, so SEL initialization or disk latency cannot block the gateway event loop | `autonudge_authz.py` |
| Structured provider probes | Credential-free invocation and completion/failure events; protected-binary resolution failures (the `gh` resolver included), refused owner credentials, revoked GitLab hosts, and incomplete Bitbucket credentials also record `denied`. On `monitor.glab_probe`, `monitor.az_probe`, `monitor.gh_probe` and `monitor.bitbucket_probe` each `denied` or `failed` row's `error` is one fixed word (`executable_not_found`, `executable_untrusted`, `unsupported_provider`, `authorization_refused`, `host_not_allowlisted`, `credentials_incomplete`, `exit_nonzero`, `timeout`, `output_too_large`, `output_not_utf8`, `provider_error`), never a target, argv, credential or output. Denial audit failure never allows the rejected request | `monitoring/provider_cli.py`, `monitoring/gitlab_merge_request.py`, `monitoring/github_pull_request.py`, `monitoring/bitbucket_pull_request.py` |
| Token auth | `internal_auth`, `app_scope_check`, `dashboard_sessions_revoked`, `refresh_token_initial_mint`, `nonce_evicted` (`source=token_auth`) | `dashboard/token_auth.py`, `dashboard/token_auth_tokens.py`, `dashboard/token_auth_app_scope.py` |
| Refresh tokens | `refresh_token_use`, `refresh_token_logout`, `access_cookie_revoked` (`source=refresh_tokens`) | `dashboard/handlers/auth_refresh.py` |
| ACP transport | `tool_interrupted` per-turn cancellation audit (`source=acp`) | `acp/client_turn.py`, `acp/client_tool_events.py` |
| Installed apps | `api_access` rows an app writes about its own decisions via `ctx.audit` (`source=app`, `caller=app:<name>`, `operation=<name>.<verb>`). Attribution is minted from the app name the context was built with, so there is no `caller=` parameter to pass the wrong value into — cooperative, not unforgeable, since in-process hook code can construct another app's SDK or reach `sel()` directly; `record` swallows a write failure, because auditing must not break the operation it only describes; `outcome` is not narrowed to a vocabulary (rewriting it would record something other than what happened), and the SDK adds no scrubbing of its own — `log_api_access` puts `outcome` through `_redact_and_clip` for every caller, because this seam is what turns that slot from an in-tree constant into caller text and the fix belongs at the boundary all fillers cross | `apps/audit_sdk.py` |

## APIs

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/sel/events?limit=N` | Recent security events (max 1000). **Owner only** — the rows name the resources a security decision was about, and a dashboard session is not by itself the owner, so every other caller gets `403 owner_only` from the shared owner gate and the refusal is itself audited. A session signed before an owner was configured keeps its bootstrap subject and is refused too, but that caller IS the owner, so it gets `401 stale_session_reauth` instead: re-signing in is the remedy, and a token refresh preserves the subject. A read that succeeds is audited as well, so the trail distinguishes an untouched log from one the owner has read. |
| GET | `/api/sel/verify` | HMAC chain integrity check. Any authenticated dashboard session (no owner gate), because it returns counts and a reason only, never event rows or resource names: `total`, `valid`, `integrity` (`ok` / `compromised` / `unverifiable`), `tampered`, `detail` (the reason text, empty when verifiable), and this process's `dropped_events` |

## CLI

```text
kirocrew security events [-n LIMIT] [--since AGE_OR_TIME] [--until AGE_OR_TIME]
kirocrew security verify
```

`events` defaults to 20 rows. `--since` and `--until` accept relative ages such
as `30m`, `2h`, and `7d`, or ISO 8601 dates/timestamps.

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
a linked or hard-linked sidecar is refused rather than followed.

Which file is locked is resolved **per acquire**, and the rule every branch of
that resolution obeys is that only a condition NO actor can flip between two
writers' acquires may select a target — a target that differs across two
concurrent writers is two flocks on two inodes, the fork the lock exists to
prevent. On the healthy path it is the sidecar under `trust`, decided from one
property of that DIRECTORY (whether it can be created, and whether it is a real
directory rather than a link) and never from the sidecar path's own spelling of
where the key lives: the key's location differs BETWEEN PROCESSES on one
directory (an install whose migration fails keeps signing from the legacy
location while a sibling that completed it reads the relocated one), so a lock
path read off the key hands those two writers locks on DIFFERENT inodes, whereas
a directory's permissions are a property of the filesystem that every writer
reads the same answer from. A linked `trust`
is refused as well as an uncreatable one: `mkdir(exist_ok=True)` cannot detect
it, because its `EEXIST` check follows the link and reports the target as a
usable directory, so the sidecar would be created inside storage the audited
agent controls and could be swapped between two writers' acquires. In both
cases the lock falls back to the legacy key file — opened WITHOUT `O_CREAT` so a
0-byte key is never minted — rather than failing every append; where the
fallback does not apply there is no protected file to serialize on and the
failure surfaces, which `_flush_batch` turns into a rollback or a denied
critical audit.

What ADMITS that fallback is that this process RESOLVED its HMAC key at the
legacy location (`_hmac_key_file`, settled once at init), never that a file
merely EXISTS there. The existence test is not safe to gate on: `sel_hmac.key`
is a sandbox read-write leaf just as `trust` is, so the audited agent can drop a
file at the legacy path on an install whose trust dir is perfectly healthy, and
the lock path never reads its bytes — so it could arm the fallback at will, then
trigger one writer's refusal and leave that writer on the legacy key while its
sibling held the sidecar. Keying on where this process actually signs means a
planted file buys nothing, and it is the install-wide fact the fallback was
always argued from: a process signing at the legacy location is one whose trust
dir `_load_or_create_hmac_key` could not create or migrate out of, so the legacy
key is what every writer there signs from. The legacy FILE is itself agent-
writable, so on an install signing there the agent can replace it between two
acquires and reach the same fork; that is deliberately undefended, because an
agent that can rewrite the key signing the chain can forge any MAC on it
directly, which is strictly more than forking it.

That directory screen and the sidecar's own open are separate syscalls, so the
screen alone leaves a window: `trust` is the one component below the log
directory the sandbox grants the audited agent read-write, so it can replace the
directory after the check, and BOTH writers' screens then pass truthfully while
their opens land in different directories — two flocks on two inodes, the forked
chain the lock exists to prevent, and invisible to a by-name screen. The
directory is therefore PINNED by descriptor the moment its screen is still the
truth (`_pin_lock_dir`: `O_DIRECTORY | O_NOFOLLOW | O_NONBLOCK`, a descriptor
`fstat` directory check, and a name↔descriptor `(st_dev, st_ino)` identity check
that makes the flag degradation safe), and the sidecar is opened RELATIVE to
that descriptor — so the directory screened IS the directory the sidecar is
opened inside, and a later swap of the name cannot redirect it. A pin refusal
FAILS THE ACQUIRE and never selects another target, which is the one place the
fallback above does not reach: the two screens before it are filesystem
properties every writer reads alike, whereas the pin raises precisely when it
catches `trust` being REPLACED mid-acquire — per-acquire, agent-triggered, and so
able to be true for one writer and false for the next. Routing it into the
fallback would hand those two writers different inodes on an install whose trust
dir is healthy, so a detected swap has one safe answer: fail, and let
audit-or-deny decide. Windows has no directory descriptors, so there the junction probe remains
the only screen — the same platform split the read-side segment pin carries.

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
