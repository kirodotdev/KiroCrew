# Self-Learning, Cron & Dashboard Modules

## Overview

Phase 5 adds self-learning from corrections, scheduled cron jobs, and a web dashboard.

The Kiro credits readout opens a privacy-first account panel backed by the
existing `/api/sessions/usage` authority. Account identity is exposed only when
the billing ARN and `whoami` ARN are coupled; the UI never derives identity from
the readiness latch and introduces no parallel account endpoint. Email is blurred
by default and the local visibility choice persists. Every bounded promotional
breakdown returned by the free billing API is retained, displayed individually,
and pooled into the compact readout. The paid `kiro-cli /usage` turn remains
fallback-only, and its two known bonus formats produce the same list shape.

## Self-Learning (`learn.py`)

Detects user corrections (e.g. "use X instead of Y", "remember that X", "never use X") and stores them in `~/.kiro/crew/lessons.jsonl`. Categories: `tool`, `preference`, `knowledge`. Injected into LLM context as `[Learned corrections:]` block (max 50). Detection runs after each ACP response.

### `learn_add` Session Authorization

The `learn_add` MCP tool (backed by `POST /api/lessons`) is subject to session-scope checks in `dashboard/handlers/cron.py:api_lessons_create`:

1. `X-Session-Key` header is required; missing → HTTP 400 `missing X-Session-Key`.
2. `dashboard:ui` (browser UI's static key) is always allowed.
3. Otherwise the slot name (portion after the `:` prefix, or the whole key) must satisfy at least one of:
   - Present in `state._slots` (live in-memory slot), **OR**
   - Key is in `state._restricted_keys`, **OR**
   - Key is in a **messaging-channel namespace** — recognised by `messaging.link.is_channel_session_key()` (any of `slack:`/`discord:`/`telegram:`/`webex:`/`wecom:`/… per `CHANNEL_SESSION_NAMESPACES`), or it is a bare Slack `thread_ts` matching `validation.SLACK_THREAD_TS_RE` (`^\d{10,}\.\d{6,}$`), **OR**
   - The corresponding JSONL file exists under `~/.kiro/crew/sessions/{slot_name}.jsonl`, `~/.kiro/crew/sessions/dashboard_{slot_name}.jsonl`, `~/.kiro/crew/sessions/cron_{slot_name}.jsonl`, or `~/.kiro/crew/sessions/dashboard_cron-{slot_name}.jsonl` — resolved by `_session_has_persisted_history()` in `handlers/_shared.py`. The two `cron` forms exist because `history._safe_key` folds `:` to `_`: a cron session keyed off `cron:{id}` persists as `cron_{id}.jsonl`, and its linked dashboard slot keyed off `dashboard:cron-{id}` persists as `dashboard_cron-{id}.jsonl`, so an idle-evicted cron session's `learn_add` is recognised rather than rejected as forged.

   If none match → HTTP 400 `unknown session`.

A Slack thread keys its session off the **bare** `thread_ts` (e.g. `1781215864.487849`), set in `slack/handler.py` and frozen into the MCP subprocess's `KIROCREW_SESSION_KEY` env var; the `slack:<chan>:<ts>` form is only a `send_message` delivery target, never the session key. Recognising the bare-`thread_ts` shape is required because the session JSONL is written *after* the LLM turn completes, so the first `learn_add` in a fresh Slack thread would otherwise race the flush and fail with `unknown session` until the transcript lands on disk (then succeed minutes later). Dashboard keys are always prefixed (`dashboard:*`, `chat-N-*`), never a bare `digits.digits`, so the regex cannot widen authorization for dashboard or forged keys.

The same first-turn flush race applies to **every** messaging channel, not just Slack: a Telegram/Discord/Webex/WeCom session key is namespaced `{channel}:{conversation_id}` (e.g. `telegram:kirocrew:forum:-100…:18:gen3`) and, post-#232, the transport publishes `session_pid` so the gateway resolves it into `X-Session-Key`. Recognising the whole channel-namespace family via `is_channel_session_key` (not just `slack:`) is therefore the load-bearing acceptance for channel sessions. The `_session_has_persisted_history` fallback alone cannot rescue them: `slot_name = sk.split(":", 1)[-1]` keeps the inner colons (`kirocrew:forum:-100…:18:gen3`) and drops the channel prefix, while the on-disk file is `dashboard_<safe_key>.jsonl` with `:` folded to `_` — so no probed name ever matches. Before this generalization, only `slack:` was accepted, so `learn_add` (and the other `POST /api/lessons` writers) failed with `unknown session` from every non-Slack channel even though the session was fully identified (regression #1268). The `dashboard:`/`cron:`/`hook:`/`subagent:`/`channel:` namespaces are deliberately **not** in `CHANNEL_SESSION_NAMESPACES`, so they are unaffected and still resolve through the slot / persisted-JSONL paths.

The JSONL-existence check exists because MCP subprocesses retain their original `KIROCREW_SESSION_KEY` env var for the life of the process, but the gateway's idle-sweep loop evicts in-memory slots after ~60 minutes of inactivity (see `session.py`). Without this fallback, reopened dashboard tabs would deterministically fail `learn_add` once the slot is swept, even though the user is actively engaged.

Note that JSONL presence is *not* a negative signal for ephemeral sessions: incognito and temporary dashboard slots **do** write a session JSONL (`_save_slot_to_history` has no `memory_mode` gate — see `history.md`), because tab recovery and gateway-restart restore need the transcript. What "ephemeral" governs is *memory* — consolidation and lessons (`is_restricted`) and memory-context injection (`blocks_reads`) — not the session file. So the JSONL check widens `learn_add` session-scope acceptance to ephemeral slots too; the write itself is still refused downstream by the separate write-scope check. That refusal cannot rely on in-memory state alone: archiving a tab removes the slot from `state._slots` **and** discards its `state._restricted_keys` entry, so for an archived ephemeral session both in-memory signals are gone while the transcript remains. `api_lessons_create` therefore resolves existence *and* mode in one off-loop probe, `_probe_persisted_session()` (via `asyncio.to_thread`, so the blocking read never runs on the event loop — AUTOSDE `no-blocking-call-on-event-loop`), reading `memory_mode` back out of the metadata line of the very file the session-scope check accepted. One path resolution feeds both answers, so the two decisions can never describe different files. The mode reader returns a **third state, `None` = unknown** (no file, or no parseable metadata object as the *first* line) and the handler denies on it: a metadata line is written at file creation before any message is appended, so an unreadable header is not evidence that writes are permitted. A valid header merely *lacking* `memory_mode` reads as `persistent`, which is what a legacy pre-field session is. `_is_restricted_session` itself stays free of disk I/O — it has ~49 call sites and is a sync helper reached from async handlers — but for `slack:` keys it does call `_hydrate_conv_flags()` first (an in-memory `SessionMap` lookup), because `_thread_incognito`/`_thread_temporary` are process-local and populated only by inbound Slack messages: a cron, webhook, monitor or subagent turn would otherwise reach the gate with empty maps after a restart despite a durable `!incognito` on disk.

The `learn_add` tool wrapper in `mcp_core.py` maps the backend `unknown session` error into a user-actionable message that accurately reflects the post-check semantics — the error is returned only when the key matches none of the accepted forms above (live slot, restricted key, Slack namespace, or persisted JSONL), so the message tells the LLM to re-state the lesson in a new thread rather than promising an automatic retry that would cross a fresh LLM context boundary. This mapping depends on the transport helpers (`_post`/`_get`/`_delete`) decoding the structured `{"error": ...}` JSON body out of `urllib.error.HTTPError` (via `_http_error_body()`) rather than surfacing the opaque `"HTTP Error 400: Bad Request"` string — without that, the `unknown session` match never fires and the LLM sees a generic transport error. Because an HTTP response body is content originating outside KiroCrew, `_http_error_body()` redacts the decoded message with `redact_exfiltration_urls()` + `redact_credentials()` at that trust boundary before returning it, so every tool branch that echoes a transport `error` value inherits the redaction (per the `security-controls` rule: never trust output from outside KiroCrew on an external surface).

Every `learn_add` session-scope permission decision — both allows and the deny — emits a SEL audit event via `_sel().log_api_access()` with a distinct `resources` tag, so the full authorization flow is traceable in the audit log:

| Condition | Outcome | `resources` tag |
|---|---|---|
| `sk == "dashboard:ui"` | allowed | `dashboard_ui` |
| `slot_name in state._slots` | allowed | `live_slot` |
| `sk in state._restricted_keys` | allowed | `restricted_key` |
| `is_channel_session_key(sk)` (slack/telegram/discord/webex/wecom/…) or `SLACK_THREAD_TS_RE` match (bare `thread_ts`) | allowed | `channel_namespace` |
| JSONL exists under `~/.kiro/crew/sessions/` | allowed | `jsonl_fallback_recovery` |
| None of the above | denied | `unknown_session` |

The downstream `_is_restricted_session` check can still reject the call with HTTP 403 after the session-scope *allow* decision (e.g. for an incognito/temporary slot), emitting a separate `restricted_session_block` deny event; that is a distinct write-scope authorization, not a session-scope one.

## Cron Service (`cron.py`)

Scheduled job execution with three schedule types: `every` (interval, min 60s), `at` (one-shot timestamp), `cron` (5-field expression). Supports natural language parsing via `parse_wakeup()`.

- Persistence: `~/.kiro/crew/crons.json` with atomic writes and cross-process file locking
- Lock contract: mutators (`add_job`/`update_job`/`remove_job`/`enable_job`) acquire the store lock via a bounded non-blocking spin (`_file_lock`); the loop-resident scheduling paths (timer tick, `run_job`) never take the store lock or `_sync()` on the event loop — they **offload** the locked `_sync()`+drain+snapshot to a worker thread via `asyncio.to_thread` (`_tick_scan_locked` for `_on_timer`, `_synced_snapshot` for `run_job`) and run only the mutation-free due-scan / `_executing` claim on the loop; the reaper sweep snapshots the atomically-swapped in-memory job list **cache-only** (no lock, no `_sync`). The hot read paths (`list_jobs`/`get_job`) run on the event loop from many callers (per-connection status push, Slack, apps SDK, MCP) and are **cache-only**: they perform **no filesystem I/O at all** — no lock-file open, no `read_bytes()`, no digest hash, no `_sync`/`_load` — and simply return `list(self._jobs)` (never torn; CPython swaps the list reference atomically). This closes the `no-blocking-call-on-event-loop` hazard: a large `crons.json` can no longer freeze the loop with a synchronous read+hash on every status push **or every timer tick**. Cross-process freshness for these reads is maintained **off-loop**: the in-memory snapshot is `_sync()`-refreshed under the store lock by the timer tick (`_on_timer`, every ≤`_TIMER_POLL_SECS`, in its worker-thread transaction) and by every mutator, so an external write is reflected within one poll interval. A freshly-constructed `CronService` performs one synchronous `_load()` at construction so loop-less callers (MCP/CLI/apps-SDK processes, tests) read on-disk state immediately. That inline load is a whole-file `read_bytes()`+blake2b hash, so an **event-loop** context must NOT use the plain constructor: the gateway builds its service *inside* its running async startup coroutine, where a synchronous construction-time load would block the sole loop (chat/WS/timers/heartbeat). Loop contexts therefore construct via the async factory **`await CronService.create(...)`**, which passes `_defer_initial_load=True` (the constructor does no store I/O) and runs the initial `_load()` in a worker thread via `asyncio.to_thread`; `start()` likewise offloads its `_load()` to a worker thread. `_running` is `False` during both, so neither arms a timer off-loop. Callers that need a **guaranteed cross-process-fresh** read on the loop use the async variants `list_jobs_async()`/`get_job_async()`, which offload the locked `_sync()`+read/hash/parse+snapshot to a worker thread via `asyncio.to_thread` (degrading to the in-memory snapshot under lock contention rather than raising, and draining any deferred timer arm on the loop after the offload). The user-facing `GET /api/crons` handler uses `list_jobs_async()`. **Exhaustive on-loop-`_sync` audit** (enumerated in the `_sync` docstring and enforced by `TestReadPathsLocked` + `TestConstructionLoadOffLoop`): every `_sync()` caller, raw `read_bytes()` site, and initialization/`start()` load is either in a worker thread (`asyncio.to_thread`) or a loop-less CLI/MCP/apps-SDK process — none runs on the gateway event loop. **Write-path (`_save`) invariant**: every `_save()` call site and every structural `self._jobs` mutation holds `_file_lock` and is reached from the loop ONLY via `asyncio.to_thread` — no bare on-loop `_save()` remains. This includes the reaper timeout (`_force_reap`) and user-cancel (`cancel`) terminal paths, which previously mutated the in-memory job and called an unlocked `_save()` directly on the loop: a lost-update race that re-serialized a stale job list and could silently drop a job a concurrent `add_job_async`/`update_job_async` worker had just persisted. Both now offload the locked helper `_merge_terminal_state_locked` (mirroring `_merge_job_result`), which `_sync()`s first so the concurrent write is reloaded and the terminal fields are applied to the disk copy inside one lock transaction. The full writer→lock→loop-entry table lives in the `_save` docstring and is enforced by `TestTerminalStateMergeLocked` (lost-update + off-loop-ticking regressions).
- **Loop-safety guard (machine-enforced)**: the "no store lock on the event loop" invariant above is no longer only docstring-enforced. `_file_lock` calls `_guard_off_event_loop()` on entry: if a running asyncio loop is detected on the current thread it raises `CronLoopSafetyError` under strict mode (`KIROCREW_STRICT_LOOP_SAFETY=1`) or emits one throttled warning otherwise. This catches a future writer that calls a synchronous mutator on the loop (which would re-freeze it under contention) rather than letting it compile, pass tests, and regress silently. It is gated (default = warn) so an unforeseen legitimate on-loop caller is never hard-broken in production and the many existing tests that seed jobs via the sync mutators from an `async def` body keep passing; the regression suite (`test_cron_arbiter_items.py::TestLoopSafetyGuard`, run in CI) flips strict on to prove the guard fires and that the sanctioned async / offloaded-sync paths do **not** trip it. Operators can export the env var to escalate the warning to a hard failure fleet-wide (mirrors `KIROCREW_STRICT_ON_LOOP_PERSIST`).
- **App SDK (`CronSDK`) sync/async contract**: the app-facing mutation API exposed via `ctx.cron` — `add_job` / `remove_job` / `update_job` / `remove_all` — is **synchronous**, preserving the published App Kit contract (a prior revision flipped it to `async def` with no shim, which silently turned a third-party app's `ctx.cron.add_job(...)` into an un-awaited coroutine that never ran). Each has a separately-named `*_async` sibling for loop-native callers. The sync methods **never run on the loop**: `_run_sync_mutator` runs the blocking `CronService` mutator inline only in a genuinely loop-less context, and when a loop IS running (an app calling the sync SDK from an on-loop `on_startup`/route hook) it **refuses**, raising `CronSyncOnLoopError` naming the `*_async` sibling. Offloading the mutator to a worker thread was implemented first and rejected: it moves the lock acquisition off the loop thread (satisfying the guard) but the calling frame must still block on the worker's result, so the loop stays parked for the whole bounded lock window and every other gateway task — chat, WS pushes, timers, heartbeat — stalls with it. Relocating the lock does not unpark the loop. Running inline is not available either (the `_file_lock` guard rejects a store-lock acquisition on a thread with a live loop), so no correct synchronous on-loop behavior exists and the SDK fails fast at its boundary, before any mutation, rather than trading one app's convenience for an instance-wide stall. This is a **breaking change** for on-loop sync callers (CHANGELOG documents the one-line `await *_async` migration); no in-tree caller is affected. In-tree loop-resident callers (the `register_app_crons_with_service` / `deregister_app_crons_from_service` bridges and `on_app_disable`) use the `*_async` variants. `remove_all`/`remove_all_async` remove every owned job in ONE atomic `CronService.remove_jobs_by_owner`/`remove_jobs_by_owner_sync` transaction that SELECTS the owned set inside the lock — after the in-lock `_sync()` reload, against the authoritative on-disk store, not from a cache-only `list_jobs()` id snapshot taken before the lock. This closes a cross-process orphan window: because this PR made `list_jobs()` cache-only, an owned-id set computed before the lock would miss a job the app created in another process since the last cache refresh; the locked removal would then delete only the stale ids, report success, and uninstall would proceed to delete the app — leaving an ENABLED orphan cron with no owning app. Selecting inside the lock removes it too (all-or-nothing — a contended store never orphans a still-ENABLED subset) and **propagates `CronStoreBusy`** so a failed disable/uninstall cleanup is reported, not masked as a successful `0`. Enforced by `test_cron_arbiter_items.py` (including a regression proving a job present on disk but absent from the cache is still removed). **Cleanup-failure policy is deliberately asymmetric between the two lifecycle paths that call it.** `POST /api/apps/{name}/uninstall` treats cleanup as a **hard precondition**: `_deregister_crons_with_retry` retries the atomic removal `_CRON_CLEANUP_ATTEMPTS` times with a short backoff (transient contention must not fail a user's uninstall; each attempt is all-or-nothing so a retry can never partially remove), and if the store is still busy the handler **aborts with a retryable `409`** before `deregister_app()` — the app stays installed and its jobs stay consistent. It cannot proceed-and-log, because uninstall is irreversible: `deregister_app()` drops the per-app cron manifest and Step 4 deletes the app directory, so still-ENABLED owned jobs would become permanent orphans that keep executing their command/script/agent payload with nothing left that knows they belonged to a removed app. "Durably disable the jobs instead" is not an available fallback — disabling is itself a store mutation needing the very lock that is contended. `on_app_disable`, by contrast, **reports and continues** (`result["cron_cleanup"] = "failed: cron store busy — jobs may still be enabled"`): disable is reversible, the app and its cron manifest survive, so the cleanup remains retryable and hard-failing a disable would be worse than surfacing it. Enforced by `test_app_routes.py::test_uninstall_aborts_409_when_cron_cleanup_busy` (abort + app still installed + retry count) and `::test_uninstall_retries_then_succeeds_on_transient_cron_busy`.
- Mutator failure contract (`CronStoreBusy`): the bounded spin raises `CronStoreBusy` (a `TimeoutError` subclass) when the store lock stays contended past `_FILE_LOCK_TIMEOUT_SECS`. This is a deliberate **public** contract at every mutator boundary, translated per surface: the dashboard REST handlers return **409** `{"error": "cron store busy, retry"}`; the MCP tools (`cron_add`/`cron_update`/`cron_remove`/…) return `"Error: cron store busy, please retry"`; the Slack command handlers reply with a busy message. All of these hand the retry decision back to a real caller (a human clicking, or an MCP/CLI client), so a contended store is a *retryable* error, never a dropped mutation. **Best-effort bookkeeping** (`ack_job`/`unack_job` context trims) is the sole exception — it logs and continues, because the visible action already succeeded and the trim is cosmetic. The **one fire-and-forget removal** — the gateway removing a completed `delete_after_run`/Done one-shot after delivery — has no caller to retry, so it does NOT drop: `_deliver_script_result` calls `CronService.defer_removal(job_id)`, which disables the job in memory immediately (so it cannot re-fire) and queues the id in `_pending_removals`; the next timer tick drains it via `_drain_pending_removals_locked()` inside its worker-thread transaction (`_tick_scan_locked`, holding the store lock, before the due-scan), guaranteeing the finished one-shot is always eventually removed and never re-executed or re-delivered.
- Mtime-based sync: auto-reloads when file modified externally (by the CLI or an out-of-band hand edit — agent-mediated writes are refused by the sensitive-path floor, see [security.md](security.md); the store is a keystone leaf, `crons.json`, since #4812)
- Load resilience (per-entry isolation): `_load()` deserializes each `jobs[]` record independently via the module-level `_job_from_record` builder — a malformed or legacy record (missing required key, or not shaped like a job object) is warned about and skipped, and every well-formed job survives. A skipped record is NOT left in place: the next `_save()` rewrites `jobs[]` from the in-memory registry, so the bad entry is dropped from disk on the first write that follows (the load warning says so — the WARNING is the operator's only window to recover the record). The whole-store reset (empty registry + fingerprint reset) is reserved for a genuinely unparseable file (`json.JSONDecodeError`), where there is nothing to salvage. `count_enabled_from_disk` applies the SAME `_job_from_record` skip decision before counting, so the two readers of the store cannot drift (a record the scheduler refuses to load is never counted, and a non-dict entry cannot crash the WS status pusher). Before this, one bad record aborted the whole deserialization comprehension and the `KeyError` handler silently replaced the entire registry with an empty list — and because `_load()` also runs from `_sync()` on every external file change, a hand-edit or partial schema migration could wipe the live schedule at runtime, not just at boot (#4664). Enforced by `test_cron_load_malformed_entry.py`.
- Timer restore: `_load()` re-arms the timer loop for active (enabled) jobs when the service is already running, ensuring jobs resume after gateway restart
- Timer cap: `_arm_timer()` caps delay at `_TIMER_POLL_SECS` (30s) so the timer always wakes to `_sync()` and detect external file changes — fixes one-shot reminders set via Slack silently failing
- Timer loop: non-blocking execution via independent `asyncio.create_task()` per job (via `_run_job_isolated()`), with strong refs in `_running_tasks` to prevent GC. One hung ACP session no longer freezes the entire cron system. Per-job timeout increased from 5 min to 30 min (`_JOB_TIMEOUT_SECS`); zombie detection (`is_responsive()` after 10 min inactivity) provides additional defense-in-depth
- Semaphore safety: `_acquired` flag pattern in gateway.py, handler.py, chat.py, task_executor.py prevents over-release when `get_or_create()` throws
- ACP zombie detection: `_last_activity` timestamp on AcpClient, `is_responsive()` returns False after 10 minutes of inactivity
- Job execution: resets LLM session, streams response, posts to the job's own surface plus the dashboard (unless `silent`). Delivery is routed by ORIGIN: `_deliver_cron_to_channel` resolves the session that CREATED the job (`_cron_origin_key`, off `job.session_key`) and, when that session belongs to a non-Slack channel, delivers there and the Slack owner-DM leg stands down, so one run notifies one operator once. Every leg gates on that send's own return value rather than on a prediction, so a governance refusal or a wire failure falls through to Slack instead of dropping the run, and the SEL `downstream_service` names `slack` only when the Slack post landed. An explicit `job.channel` is a destination the user pinned and takes precedence over both. A Slack-origin, dashboard-origin or origin-less job keeps the Slack leg, which is every job an install carries today. The same routing covers the result, the run-failure alert and the crash alert. Delivery via `_deliver_cron_response`: attempts configured `channel`/`thread_ts` first, falls back to owner-DM if channel delivery fails; applies boundary redaction to all output before posting. It also renders any `[OPTIONS: ...]` tags as interactive Slack buttons — `extract_options()` strips them from the text and `build_options_blocks()` posts them under a try/except guard so a Block Kit failure never blocks the text delivery (`gateway.py`)
- Session scoping: identity comes from the per-call caller block gatewayd injects (`_meta.kirocrew.caller`, consumed via `_resolve_session_key_strict`), with `KIROCREW_SESSION_KEY` and `KIROCREW_HOST_PID`+HMAC as the non-gateway-launch fallbacks; the lenient `/proc` pid walk is deliberately NOT an authorization source. `kirocrew-cron` advertises `kirocrew.caller-identity` so gatewayd injects that block — without the advertisement it is pooled AND identity-blind (nothing declines to pool an unadvertised backend), which is why every session-scoped path used to read an empty key and fail open. One scope function, `_owned_by`, gates BOTH reads and writes: a caller reaches only the jobs whose `session_key` matches its own. Consequences: an unidentifiable caller (gatewayd forwards `caller=None` when a stub registers with no key and peer resolution fails, so it may be sharing a pooled backend with identified sessions) reaches nothing and every write refuses; `cron_add` refuses rather than storing an ownerless row; a job with no recorded owner — written by `kirocrew cron add`, the onboarding importer, and pre-fix pooled `cron_add` — is outside every session's scope in both directions, so it does not appear in `cron_list` from chat and the CLI remains its management surface; `cron_remove_all` removes only the calling session's jobs. There is no admin bypass on this server and no ambient environment value grants cross-session scope: identity comes only from the sources `_authz_session_key` accepts. The CLI's authority does not come from this server at all — `kirocrew cron ...` reaches `CronService` directly and never routes through it, which is why the refusal text names that route. `cron_add` also takes its default delivery channel from the caller block's `channelId`, since `KIROCREW_CHANNEL_ID` has the same one-session-per-process defect as the key
- Silent mode: `CronJob.silent = True` suppresses auto-delivery of results; the agent decides when to notify the user via the `send_message` MCP tool. Silent also gates **failure** broadcasts: the failure-alert sites — the dup-failure dashboard bell, the fresh-failure dashboard bell, and the fresh-failure Slack DM (`gateway.py`) — are wrapped in `not job.silent`, so a silent cron's failure is *recorded but not broadcast*. The dedup-state advance (`last_failure_hash`/`last_failure_at`), the consecutive-failure auto-pause, the SEL `cron_failure_alert` audit, and the re-raise still fire regardless of `silent`
- Per-agent cron: jobs store optional `agent_id`; gateway passes `agent=job.agent_id` to `get_or_create()` so each job runs with its configured agent
- Handler intercepts cron commands before ACP (no LLM round-trip needed)
- CLI: `kirocrew cron {list|add|update|remove|pause|resume|trigger}`. `add` and `update` accept `--every`, `--cron`, `--channel`, `--agent`, `--approval-mode`. CLI flags mirror the corresponding `cron_add`/`cron_update` MCP tool parameters.
- Update: `CronService.update_job(job_id, **kwargs)` for partial updates (name, message, schedule, agent, channel, approval_mode, silent) with file locking and cron expression validation
- Message cap: the cron `message` has its own `MAX_CRON_MESSAGE` (50 000) cap — a cron message is a task prompt, so it does not borrow the shared `MAX_MEDIUM_STRING` (5 000). Enforced on the MCP `cron_add`/`cron_update` schemas, `POST /api/crons` and `PATCH /api/crons/{id}` (PATCH routes `message` through `validate_string_field`, 400 `code: "invalid_message"` on non-string/oversize — it was previously unvalidated on that surface), and at the `CronService` persistence chokepoint (`_build_job`/`_update_job_locked` raise `ValueError`), so the CLI and apps-SDK paths cannot admit a value the validated surfaces would reject
- Chokepoint string-field gate: `_build_job`/`_update_job_locked` type/length-validate **every** caller-supplied string field through one table (`_CRON_STRING_FIELD_CAPS` — name, message, channel, thread_ts, agent_id, created_by, folder_id, session_key, model, command, script, timezone), with caps matching the REST/MCP boundary schemas. Non-string truthy values and over-cap strings raise `ValueError` at the persistence owner regardless of caller; `None`/`""` keep their "not set"/no-op semantics while any other falsy non-string is rejected. An anti-drift test (`test/test_cron_string_field_validation.py`) asserts every persisted str field on `CronJob` is either in the table or in a documented runtime-only exclusion set
- Async stop: `stop()` is now async, cancels and awaits `_running_tasks` before returning

### Result delivery order, and the delivery-agnostic dedup anchor

A finished `message` cron fans out to three surfaces, in this order:

1. the **dashboard slot + bell** — `inject_cron_result_to_dashboard` (gated on `persistent_session and not hide_in_chat`) then `notify`;
2. the **originating channel**, when `job.session_key` names a non-Slack conversation. `_deliver_channel_reply` runs the governed cross-surface ladder (origin link → non-Slack mirror link → a direct session's stored peer id; see [slack-gateway](slack-gateway.md)) and reports whether the send landed. Without this rung a cron created from Telegram reached only the bell and Slack, so a Telegram-only operator's scheduled jobs ran and reported nowhere they would ever see;
3. **Slack**, only when rung 2 did NOT deliver, so a channel-born cron never double-posts.

`job.last_posted_hash` / `consecutive_dupes` / `last_posted_at` are written by one
helper, `_record_cron_delivery`, from rungs 2 and 3 — never from rung 1. That
split is the contract:

- **Any confirmed delivery advances the anchor.** The anchor is what the
  duplicate-suppression read compares against, so a surface that delivers
  without advancing it can never suppress. While only the Slack branch wrote it,
  a channel-delivered cron kept `last_posted_hash == ""` forever: identical
  output re-posted on every tick, and the "same result N times in a row"
  reminder could never fire there at all.
- **Suppression is therefore delivery-agnostic too.** An identical result now
  suppresses the channel post as well as the Slack one, re-posting after
  `_SUCCESS_REMINDER_SECS` (24h) with the "same result N times in a row"
  caption. For a channel-delivered cron this is a behaviour change from
  "always post" to "suppress", and the intended one — identical output is
  equally noisy in a chat.
- **The dashboard-notification-only path does NOT advance it.** The bell is
  passive and the operator may never open it, so counting it as delivered would
  suppress a result nobody has seen.

The Slack branch additionally records where its post landed
(`sessions.set_thread` / `set_channel` on the `cron:{id}` key) so a later
subagent completion can be threaded onto it. The channel leg deliberately has no
equivalent: its conversation was resolved FROM the creating session's own durable
origin/mirror link rather than learned at send time, and `_channel_reply_link`
refuses a `cron:` key outright (no channel namespace) while its stored-value rung
expects a direct peer's user id rather than a conversation — so the write would
be inert. Routing a cron's subagent completions back to the creating channel
needs a `cron:{id}` → creating-key edge, which does not exist yet.

### Failure alerts take the same ladder

A failure reports **where the results report**. Both failure surfaces run the
channel-first ladder above and keep Slack as the fallback:

- `_alert_cron_failure` — the script/command arms, which signal failure by mutating
  the job and returning normally, plus fire-time policy denials;
- the `message` arm's own `except` branch, which alerts and then re-raises.

An alert reaching only the dashboard bell while the results reach a chat is worse
than a uniform gap: the success path is what taught the operator to watch the
channel, so a job crashing every minute there reads as an idle one — the exact
state the alert exists to prevent.

- **`job.silent` gates every surface identically.** `_alert_cron_failure` returns
  before any of them; the `message` arm gates the channel leg on `not job.silent`
  exactly as it gates the Slack DM. A silent job still counts toward auto-pause.
- **The channel gets an unescaped twin, not the Slack string.** mrkdwn escaping and
  fence-neutralization are Slack-sink concerns; a ``` fence or a `&lt;` would reach
  another channel's reader literally. Both legs compose from the same
  already-redacted pieces, and `_deliver_channel_reply` adds the egress redaction
  pass.
- **The failure dedup anchor is delivery-agnostic too.** A channel-delivered alert
  never sets `slack_failed`, so `last_failure_hash` / `last_failure_at` advance and
  an identical failure re-alerts once per `_FAILURE_REMINDER_SECS` rather than on
  every fire. The SEL `downstream_service` names the channel namespace that
  actually took it, so the audit trail does not read "slack" for a Telegram
  delivery.

### Per-Job Timezone

Each job stores an optional `timezone` field (IANA name, e.g. `America/Los_Angeles`). Schedule evaluation, next-run computation, and display all use the job's timezone rather than the server timezone. The `/api/crons` response includes both the per-job `timezone` and a top-level `server_tz` field so frontends can render correctly.

### Skip Dates

Jobs can define `skip_dates` — a list of dates (YYYY-MM-DD) on which the job should not fire. The executor already skipped these at runtime; `compute_next_run_ts` now also advances past skip_dates when computing the display/preview "next run" time. The advance is bounded by a **wall-clock horizon** (~2 years, `_MAX_SKIP_DATE_HORIZON_SECS`) rather than a fixed iteration count, so the bound does not couple to schedule granularity — a daily and a `*/5` cron both simply look ~2 years ahead for the next non-skipped fire (an earlier fixed iteration cap sized for one granularity silently returned `None` — the job never firing again — for finer-grained schedules with long skip ranges). A large absolute iteration ceiling (`_MAX_SKIP_DATE_LOOKAHEAD`) remains only as an anti-infinite-loop safety net for a pathological all-skipped sub-minute config. The `/api/crons` response includes `skip_dates` per job.

### Strict Schedule (`strict_schedule`)

Jobs receive random jitter by default (0-5min hourly, 0-59min daily) to spread load. Set `strict_schedule: true` on a job to disable jitter entirely — the job fires at the exact cron/interval time.

### Create-path persistence-owner validation (`timezone` / `skip_dates`)

`CronService.add_job()`/`update_job()` in `cron.py` are the **persistence
owner** for cron creation/mutation. They validate `timezone` and `skip_dates`
**before** the job's single `_save()`, and fold both fields into that first
persist — so no create path (MCP `cron_add`, apps `cron_sdk`, dashboard, CLI)
can strand a half-populated or invalid job on disk for these two
calendar-validity-sensitive fields:

- **Timezone** is checked with `cron.is_valid_timezone()` — a single cached
  `ZoneInfo(tz)` constructor lookup, NOT `available_timezones()` (which
  recursively walks the tzdata tree and opens many files, blocking the async
  dashboard PATCH event loop). An invalid IANA name raises `ValueError` and
  **nothing is persisted**.
- **Skip dates** are checked with `cron.is_valid_skip_date()`, which requires
  the parsed value to round-trip **exactly** back to `%Y-%m-%d`. This rejects
  non-padded inputs like `"2026-1-1"` that `strptime` accepts but that render
  as `"2026-01-01"` at fire-time comparison and would therefore silently never
  match. An invalid entry raises `ValueError` and nothing is persisted.

Because validation precedes the single locked persist, a `cron_add` with an
invalid `timezone`/`skip_dates` returns an error **and leaves no job on disk**,
so a retried create cannot duplicate a previously-stranded job (the earlier bug:
validation ran *after* the immediate `_save()`, so the invalid job persisted and
a retry duplicated it). `mcp_cron.cron_add` keeps only a thin pre-check to
return a *redacted* user-facing error message and passes the values through to
`add_job` for the authoritative check.

**Scope (field-partial invariant).** This owner-level guarantee currently covers
`timezone` and `skip_dates`. The other first-save fields (`agent_id`, `model`,
`session_key`, `strict_schedule`, `hide_in_chat`, `silent`) are still applied by
the dashboard and MCP create handlers as a post-hoc fold + a second `_save()`
after `add_job` returns. Totalizing the invariant over all first-save fields
(so every create is a single fully-formed locked persist) is tracked in
**issue #391** and delivered by the concurrent `fix/cron-locking` rework
(PR #331), which consolidates every first-save field into one
`_build_job`/`_persist_add_locked` transaction.

### App-Manifest Cron `enabled` Flag (register-paused contract)

App manifests (`app.json`) may declare crons with `"enabled": false` — a cron
the app ships disabled-by-design (e.g. a nightly job that needs user
configuration before it is useful). The contract, end to end:

- **Parse**: `CronEntry.enabled` (`apps/manifest.py`, default `true`). The value
  must be a JSON boolean — a non-boolean (e.g. the string `"false"`, truthy
  under coercion) is rejected by `AppManifest.validate()` with a clear error;
  coercion remains only on the self-written `app-crons.json` read path.
  Serialization is sparse — `to_dict()` emits the key only when `false`.
- **Persist**: `_register_crons()` carries `enabled` through the app's
  `app-crons.json`. A legacy `app-crons.json` without the key registers
  **active** (default `true` — behavior unchanged for existing apps).
- **Register**: `register_app_crons_with_service()` (`apps/bridges.py`)
  threads `enabled` to `CronSDK.add_job_async(enabled=...)` (`apps/cron_sdk.py`),
  which forwards it to `CronService.add_job_async(enabled=...)`. A disabled cron is
  **created paused** — `enabled=False` + `user_paused=True`, mirroring
  `CronService.enable_job(False)` — **atomically in the job's first persist**
  (never an enabled-then-paused two-save window that a crash or a concurrent
  reader of `crons.json` could capture as enabled). It is visible in the
  Schedule view and resumable via `cron_resume`/dashboard, never silently
  active and never invisible.
- **Startup idempotency**: gateway-startup re-registration skips existing
  jobs by name using `list_jobs(include_disabled=True)`, so a paused job IS
  found and skipped — a user-resumed cron survives gateway restart
  un-clobbered.
- **Lifecycle caveat**: `on_app_disable` removes the app's jobs; re-enabling
  the app re-registers from the manifest, so a cron the user had resumed
  returns to **paused** (manifest is the sole source of truth on
  re-register). Documented in `docs/app-kit/manifest-reference.md`.

### Hide in Chat (`hide_in_chat`)

By default a persistent-session agent cron auto-creates a linked dashboard chat slot (`cron-{job_id}`) on first delivery (see Auto-inject), so its runs appear in the active session list. Set `hide_in_chat: true` to suppress that slot creation — the run's result still reaches Slack/dashboard notifications, and the run stays visible in the History tab via the **cron execution-history store** (`CronHistoryStore`, written by the executor whenever the store is usable — see **History is best-effort** below — and surfaced at `GET /api/crons/{id}/history`), but no entry clutters the Chats sidebar. Useful for fire-and-forget jobs (daily digests, log cleanups, polling). Default `false` (preserves prior behavior; absent field reads as `false`). Orthogonal to `silent`: `silent` suppresses the push notification, `hide_in_chat` suppresses the chat slot. The flag is a no-op for `script`/`command` crons, which never create a slot. The executor gates all three `inject_cron_result_to_dashboard` call sites on `not job.hide_in_chat`; the dashboard notification's CTA falls into the pre-existing no-slot branch ("View last result", which lazily rebuilds a slot from history on click) instead of "Continue session". Note: the `cron:{job_id}` *dashboard conversation_log* is written ONLY by `inject_cron_result_to_dashboard`, so it is intentionally empty for a hidden cron — it exists solely to give a dashboard follow-up turn context, which a no-slot cron never has. Hidden-cron result persistence is the execution-history store, not `cron:{job_id}`.

### Cron Folders (`folder_id`)

Cron jobs carry an optional `folder_id` field (string, default `""`) that groups them into user-defined folders in the dashboard UI. Folder definitions are persisted in `~/.kiro/crew/cron_folders.json` as a JSON list of `[{id, name, order}]` objects, managed by `DashboardState.load_cron_folders()`/`save_cron_folders()` with the same atomic-write pattern as chat folders.

**REST endpoints** (registered at `/api/cron-folders` to avoid ambiguity with `/api/crons/{job_id}`):

- `GET /api/cron-folders` — list all folders.
- `POST /api/cron-folders` — create a new folder (`{name}` → `{id, name, order}`).
- `PATCH /api/cron-folders/{folder_id}` — rename a folder (`{name}`).
- `DELETE /api/cron-folders/{folder_id}` — delete a folder and clear `folder_id` on any assigned cron jobs (jobs are never deleted).

**Job field wiring**: `folder_id` follows the same pattern as `hide_in_chat` — accepted on `POST /api/crons` (create) and `PATCH /api/crons/{id}` (update), included in the `GET /api/crons` response, persisted in `crons.json` via `_save()`/`_load()`, and threaded through `_build_job()`/`add_job()`/`add_job_async()`/`_update_job_locked()`.

### Code-Based Script Execution (`cron_script.py`)

Deterministic cron jobs that bypass the LLM entirely:

- **Script mode**: `script` field specifies a Python callable path (`~/.kiro/crew/crons/file.py:function`). The function receives a `ScriptContext` with `ctx.call_tool()` (MCP tool access), `ctx.notify()` (deliver message), `ctx.message` (arguments from the `message` field). Control flow via exceptions: `raise Skip()` to silently retry next tick, `raise Done()` to complete and remove the job, `raise Report("msg")` to deliver a message and keep running.
- **Script-mode MCP identity**: a script cron runs as the principal `cron:<job id>` — the same key `ScriptContext` presents to the gateway over HTTP (`X-Session-Key` / `caller_session`) and the key an agent cron's session runs under, so ownership and audit see one principal per job whichever surface the job uses. It is delivered on two hops: `run_script_sandboxed` exports `KIROCREW_SESSION_KEY=cron:<job id>` into the script child, and `McpToolClient` hard-assigns the same key on the env of every MCP server `ctx.call_tool()` spawns — after both the inherited-env and per-server `env` overlays, so a script that rewrites its own `os.environ` cannot present a different principal (best-effort friction on the same footing as the rest of the env-based identity contract, not a containment boundary). This is load-bearing rather than cosmetic: a direct MCP spawn is not routed through gatewayd, so it gets no caller block, and nobody publishes a signed `session_pid_*` sidecar for the launcher pid — leaving a script cron with **none** of the three sources `_resolve_session_key_strict()` accepts, which fails closed for writes only. Every state-mutating tool then returns its refusal as an ordinary result string while read-only calls keep working, so the job reports `ok` and writes nothing. Two consequences to keep in mind: `cloud.aws.assert_human_action` keys on the same env var, so destructive cloud verbs from a script cron are refused exactly as they are from an agent session; and job ownership is unchanged, so a script cron reaches only the jobs recorded under its own key (`cron_trigger` against another job stays refused, as it is for agent crons). The general rule this is one instance of: **a spawn path that hands a child MCP access must hand it an identity too**, because the strict resolver fails closed for writes alone and the omission is therefore silent.
- **Command mode**: `command` field specifies a shell command to run (mutually exclusive with `script`). Stdout captured as result.
- **Timeout**: configurable per job (default 30s for scripts, 300s for commands).
- **Safety**: scripts must live under `~/.kiro/crew/crons/`. `is_sensitive_path()` blocks credential file access. SEL audit on every invocation. Auto-pause after 5 consecutive failures (`_AUTO_PAUSE_THRESHOLD`, single-sourced in `CronJob.record_failure`/`record_success`). The auto-pause is **persistent**: an execution-owned `auto_paused` flag (distinct from `user_paused`) is written by `_save`, propagated by `_merge_job_result`, and folded into the effective `enabled` derivation in `_load` — so a failing job stays paused across a daemon restart; `enable_job(True)` or a later success clears it (SEL-audited transitions). Concurrent execution guard prevents double-fire. The script **body** is scanned as well — at authoring time and again at every fire, via `_vet_script_file` on the freshly re-resolved path — for credential-path references, protected secret environment variables, exfiltration URLs, and the shared sensitive-path matcher. Two shell-grammar heuristics are deliberately excluded there, because a body is Python source rather than a command line: `is_denied` (tool-name semantics such as `*git*push*`) and the raw-text separator-run collapse (in source a backslash run is an escape, so collapsing it manufactures paths the body never contained). The collapse is **replaced, not dropped** — `security.is_sensitive_source_body()` owns both halves, applying the same fence checks to each decoded string or bytes literal and exonerating one only when it sits provably in the pattern operand of a pattern-consuming call that is the outermost expression it reaches, and the body has neither rebound `re` or one of its attributes nor read a pattern back off an `re` object, so a run that survives into a real path (`open(r"…\\kiro-cli\\c.json")`, which Win32 collapses at open time) is still refused; docstrings are scanned, because Python retains them as `__doc__` where a body can read them back; see [security.md](security.md). A body that does not parse, or one too deep to traverse, keeps the raw-text collapse instead. The passes that need a command line are likewise pointed at the command strings the body CONTAINS rather than at the document: two of them walk shell structure under a fail-closed budget and every line of a Python file counts as a pipeline stage — so before that a body of a few hundred lines was refused for its LENGTH, on every fire, carrying no shell at all — and the third, the env-credential rules, are ordered-existence patterns that over a document match pieces lying arbitrarily far apart (`_SOURCE_COMMAND_SUBJECT_CAP` bounds the subject count; an unparseable body keeps the whole-document scan). A fire-time denial keeps the job and does not feed the auto-pause counter, so a body the scan misjudges is denied on every tick until it is edited.
- **Kind tag**: `cron_list` labels each job as `script`, `command`, or `agent` based on which mode is configured.

#### Operator-Granted Vault Secrets (`secret_env` / `secret_env_pin`)

SCRIPT crons (and only script crons — command jobs are refused at every
layer, because a pin over command text cannot cover the helper files the
command invokes) can receive secrets from the encrypted `SecretVault`
(`kiro_crew.secrets`) without a plaintext token ever living in `.env`, the cron
store, or the script. A grant is a per-job map `secret_env: {ENV_NAME:
vault-secret-name}` plus a code pin, persisted on the job and resolved only at
fire time:

- **Grant surface is operator-only, but requests are agent-first.** The MCP
  tool `cron_secret_request` (job ownership enforced) lets the agent record a
  PENDING request — mapping + a pin of the code at request time, written to the
  separate `secret_env_pending*` fields, never the active pair — after which
  the operator approves or denies. `PUT /api/crons/{id}/secrets` accepts
  `{"approve_pending": true}` (routes through `_promote_pending_grant`, which
  re-verifies the pending pin against the job's CURRENT code and refuses 409
  `code_changed` on drift, so an approval never blesses code that changed
  after the request), `{"deny_pending": true}`, or a revoke (empty map) — a
  non-empty `secret_env` map is refused 400 `direct_grant_removed`, because
  the request->approve flow is the only mint path (see the pin bullet
  below). **The machine/human
  boundary is enforced IN the handlers**, because `/api/crons` is a prefix
  entry in the mixed internal paths: the grant route refuses a proven
  `X-Internal-Secret` caller (`request["internal_auth"]`, 403 `operator_only`)
  — machines request, humans grant — while the agent-side request path is
  the `cron_secret_request` MCP tool, which records the pending request
  through the cron store directly (no dashboard endpoint is exposed for
  it). **Granting is additionally owner-only**: a
  dashboard token minted for an allowed Slack user (`!dashboard`) clears the
  machine check but is not the vault's owner, so the grant route also requires
  the shared owner gate (`require_owner_dashboard_request`, 403 `owner_only`
  otherwise). **Both denials are SEL-audited** (`api_access` / `denied`,
  operation `cron.secret_grant`) — a machine or non-owner probing the grant
  endpoint is exactly the signal the audit log exists to record. `GET
  /api/crons` serializes the `secret_env*` metadata fields (names only even
  for the owner) exclusively into owner-view responses, with every key and
  value passed through the credential/exfil-URL redaction like the sibling
  fields — the store is agent-writable and the read path loads these dicts
  verbatim, so a mapping planted directly in `crons.json` cannot carry
  credential- or URL-shaped content to the dashboard. For the same reason
  the grant endpoint checks that both stored grant fields are
  string-to-string mappings before it snapshots them, refusing a planted
  list or string with a structured 400 `malformed_grant_state` instead of
  letting the copy raise into a 500. The grant endpoint's
  own echoes take the same scrub: the `secret_env` in its success body and
  the names listed in its `unknown_secret` error are agent-authored (they
  came in via the request), and the vault-name slug grammar admits a bare
  access-key id, so neither is serialized raw. The persistence layer
  (`_update_job_locked`)
  re-validates every grant AND every pending request: env-name grammar
  (`[A-Z][A-Z0-9_]*`), the protected-name deny set (`_CRON_ENV_DENY`, `PATH`,
  loader-hijack prefixes `LD_`/`DYLD_`/`PYTHON`, product-internal
  `KIROCREW*`/`_KIROCREW*`), a 16-entry cap, and script/command jobs only — an
  `agent` job is refused because its session would hand the plaintext to the
  model, defeating the vault's agent fence.
- **Approval lives EXCLUSIVELY on the owner-gated Schedule page.** There is
  deliberately no in-chat approval card for a pending grant: an approval
  record registered with the generic approval broker is reachable from
  resolution surfaces (chat state-approval fallbacks, the approvals
  endpoint) whose identity floor is "authenticated dashboard caller", not
  "owner", and its `tool_input` summary carries vault secret NAMES — so a
  card kept widening the owner-only boundary. The MCP tool records the
  pending request and tells the agent to point the user at
  Schedule > job > Secrets, where the pending banner renders and
  approve/deny are owner-gated server-side. An in-chat surface can return
  later behind a dedicated owner-only delivery channel.
- **The pin binds the grant to the code the operator approved.**
  `compute_secret_env_pin` digests, in one payload: the HMAC **domain**
  (`pending` for an agent request awaiting approval, `active` for an
  operator-minted grant — a pending pin copied verbatim into the active
  store fields never verifies), the **job id** (no cross-job replay), the
  **delivery fingerprint** (a canonical blob of every agent-mutable field
  deciding where or whether the run's output is delivered — `session_key`,
  `silent`, `channel`, `thread_ts`; binding them one by one invites the
  next omission, so the pin binds the blob and rewiring ANY of them under
  a still-valid pin fails the run closed and asks for re-approval), the
  **canonical grant mapping** (re-pointing which secrets flow under an
  existing pin breaks it), and the job's code — the script spec + the job
  `message` (the script's agent-updatable *arguments*, so re-aiming an
  approved script requires re-approval) + current body bytes. The two
  domains differ in trust model: a **pending** pin is an UNKEYED sha256 —
  it authorizes nothing (only approval mints the pin the runners honour)
  and it must be computable by the MCP server, whose sandbox hides the
  `.vault` dir; its job is integrity, since approval recomputes it against
  the current code and refuses on drift. An **active** pin is HMAC-SHA256
  under a purpose-scoped derivation of the existing vault key
  (`SecretVault.derive_subkey("cron-grant-pin")`) and additionally binds
  the job's **grant epoch**, held in the agent-fenced
  `.vault/.grant_epochs.json`: every owner grant WRITE (grant, replace,
  promote, revoke) bumps the epoch before minting, and **every job-removal
  path bumps it before the store swap** (single delete, batch delete,
  by-owner teardown, one-shot delete-after-run, deferred removals) — a
  deleted job's record is agent-readable history in an agent-writable
  store, so without the bump a re-created job carrying the saved record
  would still verify. The removal bump keys on the id's **committed epoch
  entry**, not on the grant fields the record shows at delete time: the
  agent can clear the fields before deleting, but an id with an entry once
  had an active pin minted under it and stays replayable until bumped (an
  id with neither never had one — only approval commits entries — so the
  epoch file stays bounded across one-shot churn). A formerly valid
  mapping+pin the agent copied out of
  the store and writes back after a revoke is therefore always minted under
  a dead epoch and fails closed. Epoch reads-modify-
  writes are serialized under a CROSS-PROCESS guard (an in-process thread
  lock plus an exclusive flock on a dedicated lockfile — removal paths also
  run from the CLI, so the writer set is no longer one gateway process)
  with unique-temp atomic
  replaces that fsync the file and directory (a bump rolled back by a crash
  would revive every pin under the old epoch), so overlapping
  grant/revoke requests cannot collapse a revoke's bump. The approval's
  epoch commit is **compare-and-swap** on the value its mint peeked: a
  concurrent bump landing between the store swap and the commit refuses
  the commit, bumps once more (killing the just-swapped pin), and returns
  409 `grant_conflict` — never re-committing the value the concurrent bump
  minted. A **failed bump aborts the job delete** (owner paths propagate
  the error; background ticks requeue and retry), so a delete can
  never leave a still-live grant record behind. When the held delete is a
  completed `delete_after_run` one-shot, the run path also **parks it**
  (persisted `user_paused`) and queues it for the deferred drain: the job
  has already run and the delete is what normally stops it, so a held
  delete without a persisted pause would re-fire it on every tick until
  the epoch state healed. **Corrupt epoch
  state fails closed**: a file that exists but cannot be parsed refuses
  every verification, mint, and bump (never silently restarting the
  counter at 0, which would revive low-epoch pins) — only a genuinely
  missing file reads as epoch 0. Neither key nor
  epochs are reachable by the agent's tools or sandboxed cron children, so
  a forged store entry cannot mint a pin the runner accepts, even on hosts
  whose OS sandbox backend degrades to "none"; a vault store whose key is
  missing fails every grant path closed. **The request->approve flow is the
  ONLY mint path**: direct grants were removed because nothing binds what
  the owner's page displayed to what a save would pin — the agent could
  rewrite the script between render and click and the pin would bless the
  rewrite. A request pins the code at request time; approval refuses on
  drift; pins are always computed server-side and any client value is
  ignored. The endpoint keeps revoke and pending decisions only.
  Scripts under `crons/` stay agent-writeable by
  design, so at fire time `run_script_sandboxed` reads the body ONCE,
  re-verifies the pin (constant-time compare), and hands those verified
  bytes to the child **over stdin** (base64 in a JSON line) — the launcher
  executes exactly what was verified, never re-opening any pathname a
  same-UID writer could swap after verification, and a granted script's
  `sys.path[0]` is an **empty private temp dir** AND both the live `crons/`
  dir and the **script's own parent directory** are **hidden at the sandbox
  layer** (`extra_hidden_dirs`) for granted runs — the verified bytes travel
  over stdin so the child never needs the script file —
  so it cannot `import` — or re-add to `sys.path` and then import — an
  unpinned sibling module the agent rewrites after approval (the import
  fails instead of running with the secrets; ungranted scripts keep sibling
  imports). The granted **launcher file itself** is created inside that
  private dir, never the shared temp dir, and its prelude strips that
  directory from `sys.path` by value before the first stdlib import: `-I`
  implies `safe_path` only on Python 3.11+, so on the 3.10 floor the
  interpreter still prepends the launcher's own directory, and a
  `json.py` an agent parked in the shared temp dir would otherwise be
  imported before the secrets payload is read. A pin mismatch fails the run
  closed with a re-approve message;
  the grantable body is capped at the dashboard's script-preview ceiling
  (`_MAX_GRANTED_SCRIPT_BYTES` = `_SCRIPT_SOURCE_MAX_BYTES`), because the
  approval signs the WHOLE body while the owner reviews the preview — a
  larger grantable script would let an agent hide executable tail bytes
  behind the preview truncation and have the operator approve code they
  never saw (a pinned regression test keeps the two caps aligned).
  Command jobs cannot carry grants at all: a pin over the command TEXT
  cannot cover the bytes of a helper file the command invokes (`bash
  helper.sh` runs whatever the agent last wrote there under a still-valid
  pin), so every surface refuses them and the command runner fails closed on
  any grant a hand-edited store smuggles in.
- **Secrets never ride in an `execve` environment.** `/proc/<pid>/environ`
  is the startup-env snapshot, readable by any same-UID process for the
  child's whole lifetime, so the runner never puts grant values in the
  child's spawn env. The script launcher receives them over stdin and applies
  them with `os.environ.update` AFTER its own exec (the kernel snapshot never
  contains them), behind the protected-key filter (`_filter_grant_env`), so a
  grant can never name a product-internal `_KIROCREW*` key. The launcher
  also seeds `_GRANTED_ENV_KEYS` with the granted names, and
  `_clean_cron_env` — the base env for every descendant subprocess the
  granted child spawns, notably `ctx.call_tool`'s MCP server — strips them:
  the grant authorizes the approved script body, never the server binaries
  it calls.
- **Granted values are scrubbed from child diagnostics.** Pattern-based
  redaction only recognises known credential shapes, and a vault value has
  no required shape — so the runner, which resolved the exact values to
  build the stdin payload, replaces every occurrence of them in the child's
  stderr tail, the launcher's status JSON (which carries `str(e)` from the
  script's own exception and would otherwise bypass the stderr path), and
  the bad-stdout diagnostic with `[redacted-grant-value]` before the result
  reaches gateway logs or the persisted `last_error`. Longest values first,
  so containment between values cannot leave a recognisable fragment. The
  launcher JSON's `status` field is exempt: it is a closed vocabulary the
  launcher itself emits (`ok`/`skip`/`done`/`report`/`error`) and the caller
  only compares it against those literals, so it can neither carry a secret
  nor be rendered — scrubbing it would let a vault value that happens to
  equal a token rewrite a successful run into an unrecognised (failed) one.
- **A grant decision is audit-or-deny.** Approve, deny and revoke each write
  their SEL intent record (awaited, off-loop) BEFORE the store mutates, as a
  **critical** write — synchronous and re-raising, because the default SEL
  path enqueues for a background writer and swallows a filesystem failure,
  which would make the gate a no-op. An unwritable audit store refuses the
  decision with a structured 503
  (`audit_unavailable`) and nothing mutated, so a grant can never exist
  without a durable permission record. The terminal success event stays
  best-effort — an applied grant is already covered by the intent record.
- **The agent surface cannot enumerate vault names.** `cron_secret_request`
  performs NO existence check against the vault — a request naming an
  unstored secret is recorded as pending exactly like a stored one, so
  guessing names reveals nothing. Existence is validated on the owner-only
  approval surfaces (grant endpoint and promotion), where the operator sees
  the vault and the request side by side.
- **A decision acts on exactly the request the human saw.** The agent can
  replace a pending request at any moment, so approve and deny both restate
  the displayed mapping (`expected_secret_env`, plus the request timestamp on
  approve). On approve both fields are **required** and compared
  unconditionally — an optional check is one the agent can arrange to skip,
  so an approval that omits either is refused with 400 `invalid_secret_env`
  before anything is promoted — and the final swap runs as a
  **compare-and-swap inside the cron store lock** (`expect_secret_env_pending*`
  → `CronPendingMismatch` → HTTP 409 `stale_request`): a request replaced
  after render is neither promoted nor silently discarded.
- **A decision acts on exactly the CODE the human saw, too.** The request
  binding above cannot carry this on its own: an agent can rewrite the script
  and re-issue the request, and a Schedule page whose request banner
  refreshed while its source view did not would restate the NEW request's
  mapping and timestamp — passing both request checks, with the pin verifying
  against the new file — while the operator is still reading the OLD code.
  The pin proves the code matches the request, never that the approver read
  it. So the pending banner loads the script INSIDE itself, keyed to the
  request's timestamp (a re-issued request re-fetches; a job-id-keyed view
  would keep the stale body under the fresh approve button), keeps **Approve
  disabled until that exact source has rendered** (loading, unreadable, or
  truncated all leave it disabled), and the approve call echoes the digest the
  source endpoint returned (`expected_source_sha256`, the raw-bytes `sha256`
  of GET `/api/crons/{id}/script`). The field is REQUIRED — an approval
  without it is 400 `invalid_secret_env`, because it attests to nothing about
  the code — and the promotion compares it against the ONE body snapshot both
  pins derive from: a mismatch is 409 `stale_source` (checked after
  `code_changed`, so a rewrite without a re-issue still reports the more
  actionable code-drift reason). Approving therefore requires having been
  shown the body, which is the premise the whole flow rests on made
  enforceable rather than assumed. A digest can only attest to what was
  SHOWN, so the source endpoint also reports `reviewable` — true only when
  the rendered text equals the raw UTF-8 decode byte-for-byte (nothing
  masked by credential/exfil-URL redaction, no undecodable bytes replaced,
  not truncated). The banner keeps Approve disabled and says why when it is
  false, and the promotion RE-DERIVES the verdict from its own body snapshot
  (never the client's flag), refusing with 409 `source_not_reviewable`:
  otherwise an agent could hide executable content behind exactly the span
  the redactor masks and the operator would approve a raw digest over code
  the display never let them read.
- The promoting write also CONSUMES the pending
  request in the same atomic swap — leaving it behind would let a concurrent
  deny (or a second approval) pass its own compare-and-swap against the same
  snapshot and misreport success against the just-activated grant. When the
  epoch commit then fails (503 `epoch_commit_failed`), a compensating write
  restores the PRIOR active grant (its pin is still valid — the failed
  commit never advanced the epoch) AND the consumed request (original
  mapping, pin, and timestamp) so
  the owner re-approves once epoch storage heals; the restore is CAS-guarded
  on an empty pending slot, so a newer request the agent posted into the gap
  is never overwritten — the prior grant is then restored in its own write.
  The restore is ALSO CAS-guarded on the active fields still holding the
  just-promoted (dead) grant: a concurrent revoke that cleared them in the
  gap owns the state, and the compensation yields instead of resurrecting a
  grant the operator withdrew.
- **Resolution is in-memory only and fail-closed.** The runner resolves
  vault names via `SecretVault.get_many` immediately before spawn and
  delivers the values over the child's stdin (see the delivery bullet
  above); the product-internal keys (`_KIROCREW_SECRET_FILE`,
  `_KIROCREW_DIAL_PORT`) live only in the spawn env and are shielded from
  grants by the protected-key filter. A missing vault entry aborts the run;
  error messages name the env-var KEY only (the CWE-117 no-echo discipline
  of `mcp_gateway/secret_uri.py`). `GET /api/crons` exposes grant NAMES for
  the UI; plaintext never leaves the vault. Grants and revokes are
  SEL-audited (`cron.secret_grant` / `cron.secret_revoke`, names only).

#### Auto-pause applies to `agent` (message) crons too

Auto-pause is not script/command-only. An `agent` cron's turn signals failure
only by raising, so a turn that returns prose carries no verdict of its own even
when the security gate refused every tool it attempted — the gate outcome has to
be tallied explicitly. A success resets `consecutive_failures` and clears
`auto_paused`, so recording one for such a run would keep the guard out of reach
for a job that cannot work.

Only a block that indicts the attempt itself counts toward that budget. A
governance denial does not, in either form it takes: the ceiling refusing a tool
outright, or a built-in rule that the operator disabled and a governance pin put
back into the effective deny set. Both are policy state, and because clearing an
auto-pause never restores `enabled`, counting them would strand a job that a
later loosening of policy could not revive.

Clearing an auto-pause re-enables the job, because `enabled` is derived rather
than independent: a reload reconstructs it as `not user_paused and not
auto_paused`. A job left disabled with `auto_paused` already cleared is stopped
in memory but enabled on disk, so it would stay stopped until the next restart
silently resumed it. A pause the user set is untouched — `user_paused` is never
mutated by execution, and lifting it is the user's action.

`stream_and_collect`'s `on_tool_gate` callback reports each tool permission
decision as `(tool_title, approved, security_blocked)`. Both cron agent paths
(single-agent and the sequential `agent_sequence` loop) tally those decisions
per run and route the outcome through one shared verdict: when tools were
attempted and every one was **security-blocked** with none approved, the run
sets `last_status = "error"` with a redacted reason and calls `record_failure()`,
so the same 5-failure `_AUTO_PAUSE_THRESHOLD` applies. Any other outcome records
a success.

**History is best-effort.** `CronHistoryStore` never lets a history failure reach
the scheduler. Its directory is resolved once by `prepare()`: usable means history
is fully on, unusable means the store constructs with `enabled` False, where every
read returns empty and every write is dropped. Usability is decided by the syscalls
the store's own paths issue — an `os.stat` of the directory plus the lock-file
`os.open` — because a refused `mkdir` does not imply an unusable directory
(`Path.mkdir(exist_ok=True)` consults `Path.is_dir()`, and pathlib re-raises
`EPERM` out of that stat rather than reporting False, so the flag cannot absorb a
directory that is denied for reading as well as writing). Deciding it is blocking
I/O, so a loop-bound caller MUST defer it: `CronService.__init__` passes
`_defer_prepare=_defer_initial_load` and `CronService.create()` runs `prepare()`
through `asyncio.to_thread`, the same hop it already uses for `_load()`; a deferred
store reads `enabled` False until that completes, so a read racing the prepare
degrades rather than touching a directory of unknown state. At runtime `_degrade`
disables the store only on a DENIAL (`EPERM`/`EACCES`/`EROFS`), which is a standing
condition; every other `OSError` (a full disk, fd exhaustion) costs one record and
leaves history on. Callers do not branch on any of this: the store answers
every read with an empty result, so a denied install and a job that has never run
are deliberately indistinguishable over the API today, and the `logger.warning`
above is the operator's signal. Surfacing the state belongs in the change that
renders it.

Only an unconditional security block counts. A governance `TOOL_DENY` and an
unattended-approval timeout also arrive unapproved, but they describe the policy
state or an absent approver rather than a defect in the job — the same reason the
fire-time governance denials deliberately skip `record_failure()`. Counting them
would durably auto-pause a healthy job, since `record_success()` clears
`auto_paused` but never restores `enabled`.

### Enabled Predicate & Off-Thread Count (`_record_is_enabled` / `count_enabled_from_disk`)

The effective-enabled predicate of a serialized job — enabled iff neither
`user_paused` nor `auto_paused`, with the legacy `!enabled` fallback for stores
written before those fields existed — has exactly ONE owner, the module
function `_record_is_enabled(job_dict)`. Both `_load` (the scheduler
deserialization path) and `count_enabled_from_disk` route through it, so a
future pause-state change cannot land in only one reader and drift.

`count_enabled_from_disk()` is a **read-only** parse of `crons.json`: it counts
enabled jobs via `_record_is_enabled` and never mutates loop-owned state
(`self._jobs`, `self._last_mtime`) or the asyncio timer. The reduction itself
lives one level down, in the module function `enabled_count_from_disk(path)` — a
sibling of `unhealthy_jobs_from_disk` that returns `(count, loadable)` and is the
single owner of the "loadable record AND enabled" loop. Two callers want
different halves of that pair: this method keeps the count and degrades an
unloadable store to `0`, because its caller is a status pusher that must always
have a number to render; the telemetry probe
(`metrics/inventory_gauges.read_active_crons`) needs `loadable` so it can report a
present-but-unreadable store as a fault rather than as a plausible count. Before
the split each side carried its own spelling of the loop, capped only by the
shared predicates.

It exists specifically
for the dashboard WS status pusher, which needs an enabled-job count off the
event loop. The pusher MUST NOT run `list_jobs` off-thread: `list_jobs` calls
`_sync()` → `_load()` → `_arm_timer()`, and `_arm_timer` both calls
`asyncio.create_task` (raising `RuntimeError` with no running loop in a worker
thread) AND cancels the existing timer first — so off-thread `list_jobs` would
silently stop ALL scheduled cron jobs until restart. A slightly stale count is
acceptable (the caller caches it), and `_save`'s atomic tmp→rename write
guarantees a concurrent reader sees a whole file, never a partial one.

### Trigger Command

On-demand job execution via CLI (`kirocrew cron trigger <job_id>`) and MCP tool (`cron_trigger`). Delegates to `POST /api/crons/{id}/run`. The endpoint returns job name and uses `create_task` for non-blocking execution. If a run for the job is already in flight (tracked in `_running_tasks` or `is_running(job_id)`), the endpoint returns **409** `{"error": "job is already running"}` instead of starting a second overlapping run — the guard is an atomic check-and-set with no `await` between the check and the `_running_tasks` assignment, so overwriting (and thereby orphaning) the in-flight task handle is impossible. Unknown job → 404. This 409 propagates to the dashboard "Run now" button, the CLI `trigger`, and the `cron_trigger` MCP tool, which surface it as an "already running" outcome rather than a duplicate launch. Both CLI and MCP paths include SEL audit logging.

### Compact `cron_list` MCP Response

Default response is a compact one-line-per-job summary (id, name, status, schedule, next-run, kind, agent, channel, last-status, error/result preview, message preview). Stays under ~30KB for 50+ job registries. Options:
- `verbose: true` — legacy multi-line format (byte-identical to pre-change output)
- `ids: ["<job_id>", ...]` — drill into specific jobs with full bodies (takes precedence over `verbose`, max 200 items)

Security: sanitize-then-truncate ordering enforced for all user-controlled fields (message <=80 chars, last_error <=200 chars, last_result <=120 chars) so credentials straddling truncation boundaries cannot leak as partial fragments.

### Schedule Page Templates (Featured Row + Gallery)

Schedule templates live in `SCHEDULE_PRESETS` (`website/src/utils/schedulePresets.tsx`). Each `SchedulePreset` carries an id, icon (lucide), title, description, human-readable cadence, a `category` (`hygiene | quality | security | ops | comms | knowledge` — section order and labels exported as `PRESET_CATEGORIES`), an optional `featured` flag, and a `CronPrefill` payload (name, message, and schedule mode + values). Preset prompts follow two authoring rules: a **silence-on-no-signal** clause ("if none, end silently") so polling-style jobs don't spam notifications — paired with `CronPrefill.silent = true`, without which the saved job still auto-delivers "_No response._" every quiet run, so those prompts deliver positive findings via `send_message`, and **stateless trigger anchors** (a fixed time window like "failed within the last 30 minutes", a state predicate like "has no reply from this agent yet", or search-before-acting dedup) rather than "since the last run", because cross-run session memory is best-effort and prompt text is copied into the user's saved job at save time. Guardrail sentences in prompts (never push to the default branch, never merge, never echo secret values) are advisory instructions to the agent, not an enforced security boundary.

Templates surface in two places on the Schedule page (`/schedule`, `SchedulePage.tsx`):
- **Empty state** (no jobs): a bottom-pinned "Start from a pre-made schedule" row of the `featured` presets only (`SCHEDULE_PRESETS.filter(p => p.featured)`), plus a **Browse all templates** link that opens the gallery.
- **Any state**: a **Templates** button next to Add Job in the Jobs header opens `ScheduleTemplateGallery` (`website/src/components/ScheduleTemplateGallery.tsx`) — a modal listing every preset grouped into `PRESET_CATEGORIES` sections (empty categories are skipped).

**Write-capability indicator**: presets whose job performs write actions on the user's repos or issue trackers (pushes branches, opens PRs, comments, labels, closes issues) are tagged `writes: true`. These render a "Writes to your repos" badge (`warn-subtle`/`warn-fg` tokens) on their gallery cards, and picking one shows an advisory notice (`role="note"`) above the seeded create form stating that the prompt's guardrail sentences are agent instructions, not enforced policy. **Invariant: a `writes: true` preset is never `featured`** — the empty-state row is the first surface a new user sees, so it carries only read/report presets; write-capable ones are one click further in, behind the gallery and the badge. A test pins this (`never features a write-capable preset`). Pairing write-capable presets with enforced deny-rule counterparts, after which featuring them could be reconsidered, is tracked as a follow-up.

Clicking any card (row or gallery) opens the **existing** create panel with those fields pre-filled — it does **not** create a job directly. This is driven by an optional `prefill?: CronPrefill` prop on `JobForm`: in **create mode** (no `job`) the prefill seeds the initial form state (name, message, `schedMode`, interval, `weekDays`, `weekTime`, `cronExpr`); in **edit mode** (a `job` is present) `prefill` is ignored. The create panel is keyed on the prefill name plus a **selection nonce** (bumped on every pick) so re-selecting the same preset remounts a fresh form instead of retaining prior edits. The user reviews and saves through the normal `createCron` path, so a preset-seeded job is an ordinary cron job with no special lineage. `CronPrefill.weekDays` uses JobForm's grid convention (Mon=1 … Sun=7); a test pins the weekly preset to a Monday (`dow=1`) cron so a future change to that mapping fails loudly. Cards are rendered as accessible `Clickable` elements, hug their content, and equalize to the tallest via CSS-grid stretch. In the empty state only, the scroll container uses an 8px bottom pad (`pb-2`) instead of the default `pb-8` so the card row's bottom lines up with the left-nav panel's `m-2` bottom edge; the list/table view retains the standard `pb-8`. Tests derive all expectations (counts, section labels) from the imported preset data, never hardcoded totals.

### Imported schedules

The foreign-agent importer accepts only schedules that can be represented by
the native cron model, including finite positive interval and one-shot values.
Every imported job is created disabled (`enabled=False`, user-paused) in its
initial `add_job` persistence, with its validated timezone included in that
same call, regardless of the source's enabled state. An import can therefore
never start unattended work; the user must explicitly review and resume it
through the normal cron surface. A record-level timezone is validated and
preserved for string cron schedules as well as object schedules; an object
schedule's own timezone takes precedence when both are present. A present
non-string or unknown timezone rejects the schedule rather than silently
falling back to host-local time.

Schedule deduplication is semantic rather than based on a foreign job id: an
equivalent existing/imported schedule is reported as deduplicated and is not
created again, regardless of `created_by`. Re-applying a source is therefore
idempotent even when its native ids are unstable. A record is rejected whole
rather than narrowed when it contains command/script/environment execution,
tool filters, cwd, skills, chaining, delivery/channel, repeat/count,
provider/model/agent/session, approval, or sandbox semantics. Unsupported
schedule forms, unknown fields in the record/payload/schedule containers, and
all scheduler runtime state (last/next run, failures, leases, process/session
state) are reported but not copied. Interval values must map exactly to an
integer number of native seconds and remain at least 60 seconds; fractional or
sub-minute values are rejected rather than rounded. A credential detected in a
schedule name or prompt rejects the entire schedule rather than importing a
redacted command.

Hermes `cron/jobs.json` is admitted before generic projection only when it has
name, prompt, and one native schedule: cron `expr`, interval `minutes`, or once
`run_at`. Cron and naive one-shot wall clocks require a resolvable timezone.
Empty/null skill, script, context, toolset, workdir, provider/model/base URL and
snapshot fields plus `no_agent=false` are inert defaults; nonempty variants are
active semantics and reject the record. Local delivery, empty origin, and
canonical repeat state (`times=null, completed=0` for recurring jobs;
`times=1, completed=0` for one-shots) are the only accepted forms. Display,
pause, and last/next-run fields are ignored only after this semantic gate.

OpenClaw's canonical `openclaw.sqlite` schedule store is safety-checked and
diagnosed as unsupported rather than partially interpreted. Likewise, a
current Codex `sqlite/codex-dev.db` with the `automations` table is opened only
after the shared SQLite/sidecar check; RRULE automations are diagnosed as
unsupported schedule semantics and are never approximated into cron.

## Foreign-Agent First-Run Import

**Authoritative contract:** `docs/system-specs/modules/onboarding-import.md`.
That spec owns the scope (including the explicit non-goals), the destination
mapping into the memory hierarchy, the dry-run contract, the conflict
strategies, and the per-source layout assumptions. This section covers only how
the flow is surfaced through the dashboard and where it sits in onboarding.

The dashboard can scan and selectively merge local setup from five shipped
foreign agents — **Codex, Claude Code, Gemini CLI / Antigravity, OpenClaw, and
Hermes** — plus any an edition registers through the `ImportSourceProvider`
seam. Quick is not a source and must not appear as an import option.

The only selectable categories are:

| Category | Import contract |
|----------|-----------------|
| Instructions | User-authored rules + persona *directives* → `lessons.jsonl` (capped at 50 so import cannot evict the user's own lessons) |
| Memories | Durable memories/preferences through native writers and limits |
| Workspaces | Valid, existing, non-sensitive local directories only |
| MCP servers | Secret-free stdio/HTTP definitions; managed servers protected |
| Skills | User-authored only; source-namespaced and symlink-safe |
| Denied commands | Deny rules only — an allow-list is never imported |
| Schedules | Compatible native schedules; always imported disabled; semantic dedup |
| Settings | Explicit non-security allowlist only; existing KiroCrew values win |

**Sessions are not a category.** Conversation transcripts are deliberately not
imported — see `onboarding-import.md` → "Not migrated" for the rationale, and
"Session-import removal" for what that removal covers.

The dashboard MUST present a dry-run review before any apply, and MUST offer the
conflict strategy (`skip` default, `rename`, `overwrite`) for the categories that
support it. Item state is exactly the four writer statuses mapped to three
outcomes (`accepted` / `deduplicated` / `rejected`) plus non-attempted
`skipped` entries; the UI must not invent a fifth state, and aggregate counts
must be derived from `item_outcomes` rather than reported independently.

### Supported foreign format snapshot

The compatibility snapshot below is current as of 2026-07-26. Import is
path-and-shape based; there is no third-party format-version negotiation. A
future release remains compatible only while it preserves these recognized
paths and record/database shapes. Unknown paths are ignored, while malformed,
ambiguous, unsafe, or over-limit recognized records are skipped with
diagnostics rather than guessed.

| Source | Root resolution | Recognized layouts |
|--------|-----------------|--------------------|
| Codex | `CODEX_HOME`, else `~/.codex` | `config.toml` (workspaces/MCP/settings); `AGENTS.md` (instructions); `memories/*.md` (memories); `skills/**/SKILL.md` except `.system` (skills). Session trees, `sqlite/codex-dev.db` automations, and unstable memory stores are not imported. |
| Claude Code | `CLAUDE_CONFIG_DIR`, then `CLAUDE_HOME`, else `~/.claude`; `~/.claude.json` is also recognized | Root and workspace settings/MCP JSON files (workspaces/MCP/settings, and `permissions.deny` Bash rules → denied commands); root and `<workspace>/.claude/skills` packages (skills); root/project `memory` or `memories` Markdown (memories); `CLAUDE.md` and `rules/*.md` (instructions). Session trees (`projects/**/*.jsonl`), tasks, and schedules are not imported. |
| a registered lineage source | whatever the descriptor declares | `config.json` and `mcp.json` (workspaces/MCP/settings); `recent_projects.json`, `workspace_dir`, and `project_dir` (workspaces); resolved workspace `skills` and Markdown memory trees (skills/memories); supported `memory.db` semantic/episodic tables (memories); `crons.json` and `cron/jobs.json` (schedules). Read by `_scan_lineage_install`, which knows this product's own layout rather than a foreign format. |
| OpenClaw | `OPENCLAW_STATE_DIR`; else `OPENCLAW_HOME/.openclaw[-profile]`; else `~/.openclaw[-profile]`, with existing `~/.clawdbot` fallback for the default profile | Explicit/default JSON5 config (workspaces/MCP/settings); `SOUL.md` directive body, `MEMORY.md`, and `USER.md` (instructions/memories); explicit, configured, profile, state, and per-agent workspace roots containing `skills`, `memory`, or `MEMORY.md` (skills/memories); `cron/jobs.json` (schedules). Native session/schedule SQLite stores are not imported. |
| Hermes Agent | `HERMES_HOME`, then `HERMES_AGENT_HOME`, then `HERMES_CONFIG_DIR`; else existing `%LOCALAPPDATA%/hermes`; else `~/.hermes`. Scan the root plus at most 50 non-link directories from a bounded `profiles/*` enumeration; report overflow without consuming the rest of a large directory. | Exact `memories/MEMORY.md` and `memories/USER.md` (memories), plus `SOUL.md` (instructions); `config.yaml` or `config.yml` (MCP/settings); active local `skills/**/SKILL.md` packages after managed/cache and `*-imports/` re-import exclusions (skills); `cron/jobs.json` (schedules). `memory_store.db` is diagnostic-only. |

The import is merge-only, idempotent, and provenance-tracked. It never
overwrites an existing KiroCrew item **under the default `skip` strategy**;
conflicts preserve the KiroCrew version. `rename` imports alongside the existing
item under a derived name, and `overwrite` replaces it only after writing a
restore copy under `imports/replaced/<timestamp>/` — both require an explicit
user choice, and neither is reachable by default. A durable ledger records
imported source-item identity so a retry or later manual import reports
already-imported/deduplicated items rather than copying them again. Scan and
apply treat every source tree as read-only and leave it byte-for-byte untouched.

Traversal is bounded by the importer file and directory-entry ceilings. A
skill package is rejected when the ceiling prevents observing its complete
tree, rather than being imported as a partial package. Schedule records must
contain exactly one representable trigger family (cron, interval, or one-shot);
mixed triggers are skipped as ambiguous.

Unsupported and secret-bearing items are included only in skipped/reporting
counts and reasons; they are not copied. The excluded set includes conversation
transcripts, hooks, native agent definitions, a foreign persona's *identity*
role (its directive text is imported as memory instead — see
`onboarding-import.md`), foreign system prompts, allow-lists, credentials and
secret-bearing MCP fields, security/governance configuration, and
scheduler/provider/runtime state.

### Authenticated API contract

All three import endpoints require normal dashboard authentication and the
existing mutation protections; none belongs to the unauthenticated boot
allowlist:

| Method | Path | Contract |
|--------|------|----------|
| `GET` | `/api/onboarding/import/scan` | Return detected supported sources, selectable category counts, skipped/unsupported reporting, and the merge-only marker; do not expose source content, private paths, or secret values |
| `POST` | `/api/onboarding/import/apply` | Accept selected source ids and category ids, re-scan/validate at apply time, merge eligible items, and return imported/deduplicated/skipped/error summaries |
| `PUT` | `/api/onboarding/import/state` | Persist completion or skip of the independent import-onboarding gate |

Apply never treats client-provided counts or paths as authority. Source and
category ids outside the fixed catalogs are rejected rather than interpreted.

### UI ordering and replay

For a new workspace, the full-screen import gate runs before the existing
theme/feature onboarding. That onboarding is **six steps**: theme → about-you →
Schedule → Apps → Sessions → **privacy** (the final step; the same telemetry
disclosure and opt-out toggle as Settings → Privacy, rendered in the onboarding
shell — passive, never a consent gate; see
`docs/system-specs/modules/metrics.md` → "In-product opt-out"). Because privacy
is last, the three tour popovers all advance with "Next" and only the privacy
step offers "Done". Completing the merge or explicitly skipping it sets
`dashboard.import_onboarded`; the existing `dashboard.onboarded` flow then
continues independently. When `import_onboarded` is missing, it migrates from
`dashboard.onboarded`, so existing onboarded users retain legacy status and do not
receive a surprise first-run gate.

An older browser profile may have only the legacy `mc-onboarded` completion
marker. When no newer `mc-import-onboarded` marker exists, the SPA migrates that
completion to both workspace flags before it applies false boot defaults. Once
the newer marker exists, the workspace flags remain authoritative.

After onboarding, Settings → Import can launch the same fresh scan/review/apply
flow again. Replay remains merge-only and idempotent.

## Dashboard (`dashboard/`)

Modular aiohttp package at `127.0.0.1:5476` (configurable). Split into:
- `folder_repository.py` — chat-folder load, serialized read-modify-write, rollback,
  full-value write confirmation, and breadcrumb traversal. `DashboardState` keeps
  its existing folder methods as compatibility facades; request handlers continue
  to use `mutate_folders()` / `read_folders()` and never depend on the repository.
- `slot_buffers.py` — live wire-frame delivery and deferred-context buffering,
  including consumer ownership, bounded release, restore, and corruption logging.
- `slot_queue_repository.py` — queued-turn mutation plus the subagent delivery
  ledger, including stable delivery identities, replay recovery, and bounded
  orphan bookkeeping.
- `slot_projection.py` — read-only source-link indexing/cache and the exact public
  slot-summary projection consumed by REST and WebSocket clients.
- `slot_registry.py` — live-slot, session, and Slack-link lookup; construction
  reservations; slot-key allocation; and counter reseeding against restored slots.
- `interaction_coordinator.py` — approval and question registration, redaction,
  timeout, audit, cancellation, resolution, and question-card lifecycle.
- `notification_coordinator.py` — notification validation/redaction, channel
  settings, in-memory log and unread state, WebSocket fan-out, and ordered durable
  storage.
- `dashboard_persistence.py` — dirty-slot flushing, open-slot snapshots, and
  context snapshots while preserving the facade's locks, generations, rollback,
  cancellation, and close ordering.
- `websocket_hub.py` — WebSocket client/subscriber registries, scope checks,
  serialization, fan-out, browser-event redaction, and bounded close behavior.

`_ChatSlot` and `DashboardState` are the stable compatibility facades and the
canonical owners of their mutable containers. This is the intended end state,
not a migration waypoint. The composed components retain no aliases to replaceable
containers and read the current facade state on every operation, because replay,
rollback, cleanup, tests, and integrations replace or inspect those fields and
methods directly. Moving ownership behind a component would require a separately
specified compatibility change.

- `state.py` — `_ChatSlot` and `DashboardState` data classes; in-memory message buffer (5000 per slot); `_ChatSlot.append` stamps every non-wire row with `meta.mid`, a per-row **delivery identity** (random, not a counter — a counter rebased after a restore could reissue an id a restored row already holds, and a colliding id makes a client DROP a real message). It is the only thing that lets a client tell one row's second delivery from a different row that looks identical: `ts` cannot (a coarse OS clock stamps same-tick appends identically) and content cannot (two identical messages are legitimate). Preserved when supplied, so it survives the JSONL round trip; the appended row is returned, so a dual-writer (the cron/workflow/crew injectors) reads the minted id off the return and stamps its durable `ConversationLog` copy with the SAME `meta.mid`; skipped for `chunk`/`done`/`streaming`, which are never broadcast as a `chat_message` nor persisted and would pay a uuid4 per streamed token; WS client tracking (`_ws_clients`, `_ws_log_subscribers`); `_broadcast()` sends to both SSE queues and WS clients (dual path — both doors read the SAME note and serialise it through ONE helper, `chat_message_frame(note, *, include_metadata)`, so a field added to the frame cannot reach one transport and miss the other; `cls`/`meta` are carried when the note has them and omitted entirely when it does not, never as an empty key. `include_metadata` names a property of the TRANSPORT, not a preference: the WS arm passes `True` because it filters per socket downstream (`_send_ws_all` → `_ws_client_allowed`, deny-by-default event scope), while `api_stream` passes it only for a dashboard-user token because the SSE fan-out has NO per-app filtering — `meta` carries `tool_input` / a live `oauth_url` / `approval_id`, so an unconditional pass there would expose it to any app token granted that route whatever its `slots:*` scope, the same class as GPT #6789. Enrichment belongs on the door that filters); `broadcast_ws()` for WS-only events (chat_chunk, chat_done, refine); `close_all_ws()` for clean shutdown; `_slack_linked` bool on `_ChatSlot` (set from `SessionMap.get_slack_link()` on slot init); `linked_session_key` str on `_ChatSlot` (when set, `_run_chat` uses this as session key instead of deriving from slot name — enables cron slots to share the cron's persistent session); `_artifact` str on `_ChatSlot` (companion chat: the artifact slug this slot is bound to; parsed at slot create against the slug grammar, exposed as `artifact` in `to_dict()`/WS `slots`, persisted in history meta — see `modules/artifacts.md` § Companion Chat); `push_artifact_update(slug, version, deleted=False)` broadcasts the typed `artifact_update` WS envelope from the artifact mutation funnel; **approval queue**: `_pending_approvals` dict + `_approval_futures` (asyncio.Future per request), `request_approval(..., is_background=False)` creates future + broadcasts WS `approval` event, `resolve_approval()` resolves future (state-level first, then a bare id-match slot scan — safe only for callers that legitimately own the id: native gateway / Slack click / session-scoped handler). `resolve_state_approval()` is the state-level-ONLY variant with no slot scan and thus no cross-slot authority: the slot-approve handler locates the owning slot under a **session-identity** guard (`linked_session_key`/`_history_key_for`) and its no-owner fallback uses `resolve_state_approval` so an ACP `request_id` collision across unrelated slots can't approve a different slot's pending tool. Timeout auto-denies. Interactive sources wait `_APPROVAL_TIMEOUT` (7200s / 2h, pauses for resume); **unattended background sources** (cron/heartbeat/taskrunner — passed `is_background=True` by the gateway) deny-fast after `_BACKGROUND_APPROVAL_TIMEOUT_SECS` (180s / 3 min) since no human is present to respond. **Slot titles**: untitled slots serialize as `NEW_SESSION_TITLE` (`"New Session…"`) via the `_ChatSlot.display_title` property — applied at the serialization boundary so brand-new empty sessions and the pre-LLM-title window all read the same, while slots with a real (non-key) title are unaffected; `push_slot_title(key, title, *, full=True)` broadcasts a title update; callers pass `full=False` to emit only the lightweight `slot_title` event for high-frequency streaming title partials (word-by-word reveal — two characters at a time for a script written without word spaces, where the whole title is one token), then finalize with one default `full=True` call (which also fires a `push_slots_update()`). **Status snapshot**: `status_snapshot()` also carries `branch` and `commit` (from `_build_info`) so clients can detect an actual code update. **Sidebar preview**: `_ChatSlot.to_dict()` skips `assistant`-role turns tagged `meta.kind=="compaction"` (auto-compact notices, `/compact` banners) when picking the `last_message` preview and its OPTIONS, mirroring the frontend's `deriveFollowUpOptions` skip so the sidebar shows the last *real* message
- `chat.py` — multi-slot chat with per-tab kiro-cli sessions (`dashboard:{slot.key}`), background LLM streaming (survives browser disconnect), session lifecycle management (active ↔ history), chunk cleanup, tool approval flow. Each tab gets its own kiro-cli process for true multi-agent parallelism — tabs can run tools simultaneously. Sessions idle-expire; on restart, the live tab set is restored from `~/.kiro/crew/open_slots.json` (snapshotted on every flush + shutdown by `DashboardState._persist_open_slots()`, replayed on startup by `restore_open_slots()` before the legacy mtime-based `restore_recent_sessions()` so long-running tabs survive regardless of message age; after both restore paths `DashboardState.reseed_slot_counter()` advances `_slot_counter` past the highest restored `chat-<N>-<ts>` index so a newly minted tab can't reuse a low index that collides with a restored tab and scrambles the tab↔session binding), and full tab history is re-injected. Cross-tab context (recent messages from other dashboard sessions, capped at 5k chars) is injected at session start for continuity. `?ws=1` mode returns JSON immediately and pushes chunks via WS. `_prepare_messages()` collapses `chunk` entries into `streaming` role for API responses during active streaming. Timestamp preservation on resume (original `ts` from JSONL) and save (single-pass JSONL write preserving `ts` and `created_at`). **Agent persistence**: `slot.agent` saved to JSONL metadata on close, restored on resume — custom agent sessions survive close/reopen. Pushes `refresh("history")` after chat completion. **Bidirectional Slack sync**: mirrors user messages to linked Slack threads when `slack_client` is available. Stop resets the per-tab session; delete kills the per-tab session via `sessions.remove()` to free resources. **Slash commands**: `_SLASH_COMMANDS` frozenset skips context injection (sent verbatim to kiro-cli). `_BLOCKED_SLASH_COMMANDS` (`/quit`, `/exit`, `/q`, `/chat`, `/paste`, `/reply`, `/editor`, `/tangent`) are rejected before session acquisition — returns warning message without touching kiro-cli — and are excluded from the `GET /api/slash-commands` suggestion payload (both provider paths), so the autocomplete never advertises a command the dashboard rejects. `/compact` is additionally gated as a LOCAL command — above the Slack OPTIONS-expiry boundary and before session acquisition — on the backend's `ACP_BACKENDS_COMPACT` membership (the `manual_compact_unsupported_backend` LLMProvider capability property, peeked off the live session when one exists, else read from the `agent.acp_backend` config the factory would build one with): a backend outside the set (KAS) gets an immediate informational "the <backend> backend manages compaction automatically" assistant message — mirroring the `cc_managed` relationship, not an error — instead of a dispatched prompt whose compaction-status wait would strand for `COMPACT_WAIT_TIMEOUT_SECS` (#7800); because the gate answers before the turn machinery, no session is created and no pending OPTIONS control is struck through. **ACP extension events**: `_run_chat` handles `compaction_status` (shows ✅/❌ completion/failure), `clear_status` (clears slot messages + broadcasts `slot_clear`), `agent_switched` (updates `slot.agent` + resets session + broadcasts `slot_agent_switch`).
- `ws.py` — WebSocket endpoint at `/api/ws`. Single multiplexed connection for all real-time events. Pushes dashboard status every 5s, current slots on connect, log ring buffer replay on subscribe. The lesson/cron counts in the status snapshot refresh only every 30s (`_WS_COUNTS_CACHE_TTL`) through ONE gateway-wide cache (`_refresh_status_counts`, single-flight — one store touch per TTL no matter how many sockets are open) and are computed OFF the event loop by `_load_status_counts` (`asyncio.to_thread` for both `DashboardState._count_lessons` — the JSONL half plus a vector-store SQL `COUNT(*)`, the same total `/api/status`/SSE report, fixing the JSONL-only false zero of #7204 — and `CronService.count_enabled_from_disk`), so slow/large/NFS home-dir latency cannot stall the loop and starve every other WebSocket/coroutine. Each count is guarded independently: a component that has never refreshed successfully is published as `null` (= unknown, rendered as a loading skeleton), never an authoritative 0; failures retry on the next ~5s tick until 6 consecutive, then back off to the TTL cadence with one rate-limited operator-visible warning. It deliberately uses `count_enabled_from_disk` (a pure read) rather than `list_jobs`, whose off-thread `_arm_timer` would raise `RuntimeError` and silently cancel all cron timers (see cron spec § Enabled Predicate & Off-Thread Count). Server→Client: `{"type": "dashboard|slots|slot_title|notification|refresh|chat_message|chat_chunk|chat_done|log|refine|sessions_restarting|slot_clear|slot_agent_switch", "data": {...}}`. Client→Server: `{"type": "subscribe_logs|unsubscribe_logs|slot_focused"}`. `slot_focused` (`{"type": "slot_focused", "slot": "<slot-key>"|null}`) is the resume-prefetch intent signal: focusing a slot whose session is persisted but not live schedules a speculative `session/load` (`schedule_eager_spawn(allow_resume=True)` → `get_or_create(speculative=True, speculative_resume=True)`), overlapping the multi-second transcript replay with the user reading that history; `slot: null` means blur (tab hidden). The handler tracks the prefetch task per connection and cancels it on every focus change, blur, and disconnect — rapid tab flipping settles into at most one pending prefetch per socket, and only the task this path armed is ever cancelled, never one from the slot-create/project-set signals. A prefetched session no real turn claims is torn down by a TTL (`_RESUME_PREFETCH_TTL_SECS`, 600s) via `SessionManager.remove_if_unclaimed`, releasing kiro-cli's native per-session lock instead of waiting out the idle sweep. `sessions_restarting` event pushed by `_reset_all_sessions()` with `{"status": "restarting"|"ready"}` so the frontend knows when sessions are being recycled. Each provider shutdown is bounded by `_SHUTDOWN_TIMEOUT_SECS` (5s) via `asyncio.wait_for`; on timeout, `_sync_kill_provider` force-kills the process tree to prevent leaks (see `docs/architecture/resource-protection.md`). `slot_clear` pushed on `/clear` (frontend clears messages for active slot). `slot_agent_switch` pushed on `/agent` switch (frontend re-fetches slots for updated agent label). Uses `asyncio.ensure_future(ws.send_str())` because aiohttp 3.13's `send_str()` is a coroutine. **Security**: `_check_ws_origin()` validates the `Origin` header before accepting the upgrade — rejects missing or cross-origin requests (allowed: `127.0.0.1`, `localhost`, `kirocrew.localhost`). Max 5 concurrent WS connections (`_MAX_WS_CLIENTS`).
- `handlers.py` — status, system (live CPU/memory/network), memory CRUD, cron CRUD, lesson CRUD, skills, agent config (save + auto-restart sessions), logs SSE with persistent ring buffer (1000 entries, replays on connect) + queue-based handler (also pushes to WS log subscribers via `ensure_future`), log level control, session delete, refine status push via `broadcast_ws` with throttled chunks (~4/sec). `start_time` included in SSE/WS dashboard status payload. MCP management: probe cache (`_bg_mcp_probe()` at startup, 10-min TTL, merges enabled/disabledTools from global mcp.json), server/tool toggle writes to `~/.kiro/settings/mcp.json` + syncs to kirocrew.json, bulk toggle-all, `_sync_mcp_to_agent()` helper.
- `handlers/kiro_prerequisite.py` + `kiro_prerequisite.py` — authenticated
  first-run readiness surface. `GET /api/kiro-prerequisite` discovers viable
  `kiro-cli` candidates and checks `whoami`; exact owners receive structured
  platform/install/auth state — all of it DETECTED, never performed, since
  Kiro Crew neither installs the CLI nor signs in — while authenticated non-owner
  dashboard users receive only redacted `ready` and
  `initial_setup_complete` results plus `setup_allowed=false`, so readiness
  cannot lock them out and host details do not leak. Successful authentication
  persists an owner-only setup-complete marker; existing installations are
  inferred only from that marker or non-empty persisted session/history
  content. Empty directories and zero-byte files created during gateway startup
  do not bypass first-run setup. App tokens remain denied. The two
  owner-only POST route (`repair-specs`) rewrites Kiro Crew's own agent specs and
  returns `200`; it is the only write on this surface.
  **Probing is boot-and-explicit-action only.** The readiness probe (two
  `kiro-cli` spawns) runs ONCE per gateway, in `warm_up()` shortly after start,
  and thereafter only on an explicit user action: the gate's Refresh / Check
  again button (`GET /api/kiro-prerequisite?refresh=1`, owner-only) or an
  install/login operation. There is deliberately **no timer re-probe and no
  probe on the send path** — `session_ready()` is a pure read of the latched
  status, and the polled status endpoint serves that same latched value, so the
  SPA's 30s poll costs no subprocess. The React gate still polls every second
  during an operation, every three seconds for a waiting non-owner, and every
  30 seconds otherwise; those polls are now free reads that render whatever the
  latch says.
  **A probe that TIMED OUT is a third condition, not a missing binary.**
  `probe_timed_out` on the snapshot separates "the spawn never answered" from
  both "no binary on disk" and "the sandbox refused the spawn". It is its own
  field rather than a reuse of `sandbox_unavailable` because a timeout raises no
  typed sandbox failure, so every `sandbox_*` field is legitimately empty, and a
  consumer that maps `sandbox_unavailable` to a userns remedy must not be handed a
  slow filesystem to fix with an AppArmor profile. A timeout with a runnable
  candidate reports `installed=True` (the binary was stat'd; verification is what
  failed) and `authenticated=False` as UNKNOWN, since `whoami` runs through the
  same probe path and is never reached on such a host.
  **Known interim gap:** `KiroPrerequisiteGate.tsx` keys only on
  `sandbox_unavailable` / `installed` / `authenticated`, so `probe_timed_out` has
  no UI consumer yet. On a FIRST-RUN host (`initial_setup_complete=false`) a
  timed-out probe therefore falls past the `sandbox_unavailable` intercept into
  the generic setup shell, which highlights signing in — a remedy that cannot
  speed up a slow probe. That is not worse than the state it replaced (the same
  host previously read `installed=false` and was told to install a CLI it already
  has), but it is still wrong, and the field is the carrier for the intercept that
  closes it. An established install is unaffected: `initial_setup_complete=true`
  returns before either branch.
  **A mid-session logout is discovered by the ACP attempt, not by a probe.**
  Because the latch can be arbitrarily stale, turn-starting paths do **not**
  gate on it: `reject_if_kiro_not_ready()` is advisory and always admits (its
  call sites were removed from chat / regenerate / edit-resend / rewind / side /
  optimizer / `POST /v1/chat/completions`). Blocking a send on a stale latch was
  the stuck case — a user who signed in from a terminal stayed locked out until
  something re-probed. Instead `chat_runner` handles `AcpAuthRequired`
  explicitly (ahead of the generic `AcpError` branch, since it is a subclass):
  it never re-queues (respawning hits the same wall), appends the actionable
  "not logged in — run `kiro-cli login`" error card, mirrors that message to a
  linked Slack thread (preserving the delivery the old pre-turn gate performed),
  and calls `KiroPrerequisiteService.mark_signed_out()`. That latch narrows
  `authenticated`/`ready` to false without spawning anything and never touches
  `initial_setup_complete` (so a returning user is never demoted to first-run
  setup). It only ever narrows readiness, and defers while an install/login
  operation owns the status. Nothing in the dashboard renders off that latch —
  the error card is the ONLY sign-out signal the user sees (see "The dashboard
  does not guide the user to sign in" below).
  Queued messages and post-fan-out synthesis therefore no longer park on
  readiness: the readiness-waiter tasks are gone, the successor turn simply
  runs, and a signed-out CLI surfaces as an error card from that turn.
  **One exception preserves the no-loss rule:** when the finishing turn itself
  caught `AcpAuthRequired`, the queue handoff is SKIPPED (`_auth_required`).
  Every queued prompt would hit the same wall, so popping them one by one would
  drain the whole queue into identical failures and leave nothing to resume after
  the user signs in. The queue is held intact — cards stay visible and
  individually cancellable — and resumes on the user's next send. This is the
  no-loss guarantee the deleted readiness waiters used to provide, without a
  waiter that a latched-stale value could strand.
  Side turns get the same treatment at their own boundary: `_run_side_turn`
  catches `AcpAuthRequired` ahead of its generic handler and broadcasts the
  actionable `kiro-cli login` text instead of "(side conversation failed — see
  server logs)", since the side panel has no other channel to tell the user what
  to do. It latches the service signed-out too.
  **Two classes still fail closed** via the blocking guard
  `reject_if_kiro_unverified()`, because neither can use the ACP attempt as its
  authority: the **poll-driven `kiro-cli` spawn sites** (`/api/models`,
  `/api/sessions/usage`) have no turn to carry the failure and `kiro-cli`
  auto-opens an interactive browser login when run unauthenticated (and
  `kiro-cli chat` hangs), so an unverified spawn on a timer opens a window and
  leaks a process every poll; and the **destructive reruns** (regenerate,
  edit-resend, rewind) have already rewritten durable history by the time a turn
  could fail; and `POST /v1/chat/completions` has no transcript, so an error card
  would surface as a successful empty completion. A missing or invalid service
  fails closed in all three.
  **These callers authorize on a FRESH probe, not the latch**
  (`kiro_verified_ready` → `KiroPrerequisiteService.verified_ready`, re-probing
  when the latch is older than `_VERIFY_MAX_AGE_SECS` = 30s). The latch is
  written at boot and narrowed only when a chat turn observes an auth failure, so
  an external logout with no chat turn in between would leave it `ready=True`
  indefinitely — and a stale `ready=True` here authorizes exactly the irreversible
  act the gate exists to prevent (a history rewrite, or a browser-opening spawn).
  "Probe at boot only" is right for the send path, which risks nothing; it is
  wrong for authorization. The re-probe is bounded: only these paths call it,
  never the message hot path, and `_PROBE_CACHE_SECS` collapses a burst onto one
  probe. A probe that cannot run denies rather than admits. Both the full dashboard and the
  headless Slack/API server attach the service to application and dashboard
  state and close it during runner cleanup; explicit offline test gateways use
  the service's `assume_ready` mode.
- `handlers/members.py` — the Crew Members page's two routes. `GET /api/members` returns one roster row per GLOBAL crew (name, slug via `members.slug_for_name`, the crew-record fields, `bound`/`slot_key` from the member dir's `dm.json` read off-loop, and an O(1) `running` flag); richer live detail rides the already-subscribed WS `slots` frames, so the endpoint only fills the cold-start gap. `POST /api/members/{slug}/thread` is the idempotent get-or-create of a member's pinned DM thread and the ONLY birthplace of member slots: it derives the stable slot key `member-<slug>` (`members.member_slot_key`), creates the slot with `mode="member"` and the crew as its agent, and persists the binding via `members.write_dm_binding` (atomic, records the exact crew name so a lossy slug collision stays disambiguated). `mode="member"` is what keeps these threads out of the ordinary Sessions list (the frontend surface filter never admits it) and it round-trips through history restore, so the pin survives restarts. The pin itself is enforced at every agent-writer: the chat send path and the slot-agent switch endpoint refuse with 409 `member_thread_agent_pinned`, the OpenAI-compat per-request write refuses in OpenAI error shape, and a mid-turn `EVENT_AGENT_SWITCHED` on a member slot is vetoed (slot agent kept, session marked for reset so the next turn cold-starts on the pinned crew). App tokens are denied on both routes. Errors carry machine-readable `code` fields (`invalid_member_slug`, `member_not_found`, `member_slot_conflict`, `member_binding_write_failed`).
- `handlers/source_providers.py` — validates GitHub PR and GitLab MR URLs, delegates authentication to `gh`/`glab`, normalizes metadata/files/comments/reviews/checks, recursively redacts provider strings, enforces subprocess and aggregate-payload limits, and maintains separate bounded caches for full source payloads and lightweight sidebar check state. Both caches age an entry by the lifecycle it describes: an open PR/MR by the short TTL (`_CACHE_TTL_SECS` for the payload, `_CHECK_TTL_SECS` for the chip), a **merged** one by `_TERMINAL_TTL_SECS` (six hours) and a **closed** one by `_CLOSED_TTL_SECS` (one hour — it can be reopened and keeps accruing discussion), because re-reading a finished pull request on the open-PR cadence for as long as its chip stays in a sidebar was one provider subprocess per finished PR per minute, forever. The payload TTL is decided from the payload itself through the same `_project_state` the chip projection uses (`_full_payload_ttl`), so the two caches cannot disagree about whether a URL is finished; the explicit refresh button and mutation invalidation still bypass both, and the turn-boundary force (`request_check_refresh_now`) still re-reads a closed chip (an agent can reopen one) but never a merged one (`_chip_refresh_due`). Those lifecycle clocks govern the chip cache and the full payloads that have no cheaper read (GitLab, registered plugins). A github.com full payload is instead REVALIDATED past the open TTL whatever its lifecycle: `_revalidate_pull_request` first issues small conditional REST GETs through `gh api -i -H If-None-Match` — `issues/{n}` (its ETag follows the pull request's `updated_at`: title/body/labels/lifecycle including a reopen, a push, reviews, comments) and, for an OPEN pull request only, `commits/{head_sha}/check-runs` plus `commits/{head_sha}/status` (CI hangs off the commit and never moves `updated_at`; check runs and legacy commit statuses are separate resources and the rollup renders both) — and only when any probe answers something other than `304` does the fanout run; all-304 re-stamps the cached entry instead. So post-merge comments and a reopen reach the panel within one open TTL for one rate-limit-free request. It is strictly 304-only: the first probe of a URL has no validator, answers 200, and only LEARNS the ETags (`_REVALIDATORS`, bounded, the two commit-level validators scoped to the head sha so a push cannot reuse the old commit's), a failed probe is "unknown", and nothing is ever judged unchanged by comparing bodies. Validators returned by an all-304 are committed at once; those returned by a 200 describe a payload the cache does not hold yet and are committed only after the full read that follows has succeeded, so a failed fanout can never pair the old payload with new validators (which would make every later probe re-stamp it as current). Re-stamping is also capped: each validator set remembers when its payload was last read in full (`read_at`), and past `_REVALIDATED_MAX_AGE_SECS` (the merged TTL, 6 h) one full read runs without probing and the validators are dropped so the next cycle learns a fresh set — the probes rest on GitHub moving the issue ETag for every rendered field, which the API does not promise, so a coverage gap degrades to bounded staleness instead of unbounded. `pulls/{n}` is deliberately not the probe (its ETag churns on the embedded repository counters), GitHub's GraphQL API — what `gh pr view` speaks — has no conditional requests, an authenticated 304 costs nothing on the primary rate limit, and `gh` exits 1 on a 304 so `_parse_conditional_get` reads the status line rather than the exit code. The merge pair moves no validator and stays with the chip protocol; an explicit refresh, a mutation invalidation, GitLab and registered plugins never probe. Full source payloads carry a normalized merge-state pair shared by both providers: `mergeable` is `mergeable|conflicting|unknown` (`''` when the provider omitted it) and `mergeStateStatus` uses GitHub's lowercased merge-state vocabulary extended with a GitLab-specific value (`clean|dirty|behind|blocked|unstable|draft|need_rebase|unknown|''`). GitHub maps `mergeable`/`mergeStateStatus` from `gh pr view` directly; GitLab derives `mergeable` from `detailed_merge_status` (falling back to legacy `merge_status`) and maps `detailed_merge_status` onto the shared vocabulary (`conflict`→`dirty`, `need_rebase`→`need_rebase` (kept distinct: on fast-forward-only projects a merge commit cannot unblock the MR, so it must not be conflated with `behind`), approval/CI/discussion/policy/security gates (including `status_checks_must_pass`, `policies_denied`, `security_policy_violations`, `merge_request_blocked`)→`blocked`, `ci_still_running`→`unstable`, unrecognized non-empty values→`unknown`). Both providers compute mergeability **lazily**: the first read of a pull request they have not evaluated recently answers "not known yet" (GitHub `UNKNOWN`, GitLab `checking`/`unchecked`) *and* is what starts the computation, so a single read reports a conflicting pull request as having no merge blocker at all. Each full fetch therefore re-reads the merge fields alone — `gh pr view --json mergeable,mergeStateStatus` / the GitLab merge-request endpoint, which is the only place `detailed_merge_status` is exposed — at most `_MERGE_STATE_REREADS` (2) times spaced `_MERGE_STATE_REREAD_DELAY_SECS` (0.8s) apart, dispatched inside the existing secondary fanout so the wait overlaps calls the request was already making. A value that is empty rather than unknown is never re-read (the provider omitted the field; re-reading cannot settle it), and an unsettled, failed, or malformed re-read degrades to `unknown` rather than raising — an unknown merge state costs one banner, never the panel. Settledness is judged on the **pair**, not on `mergeable` alone: GitLab settles `need_rebase` and its branch-protection gates in the detail field while `mergeable` stays `unknown`, so keying on `mergeable` would re-read a state the provider had already answered and then discard it. Provider CLIs run through `sandboxed_spawn_argv(..., mode="standard")` using absolute paths only: an explicit absolute `KIROCREW_GH_BIN` / `KIROCREW_GLAB_BIN` override, else the fixed well-known install dirs, else the ambient `PATH` (`provider_executable_candidates()`). The trust policy itself (candidate dirs, validation, strict mode) is single-sourced in `kiro_crew.github_runner` and re-exported here, so this panel, Issue Radar, and Code Review Sage can never drift apart. The default policy is *if the CLI works in the user's terminal, it works here*: the gateway user's OWN install is accepted — Homebrew/Linuxbrew/asdf symlink layouts included — and only provenance the user did not choose is refused, namely a binary (or ancestor) owned by another unprivileged account, anything world-writable (a world-writable *directory* is tolerated only when sticky — `/tmp`-style 1777, where only the owner may replace an entry, so the ownership check still decides), and anything inside the agent-writable project checkout or workspace root (`github_runner.agent_writable_roots()`, the one substitution vector the model controls; the same rule codex applies to its own sandbox helper). A gateway running as **root** is refused outright in BOTH modes, because every process it spawns — the agent's own shell included — would be root too, which makes the ownership and agent-tree checks vacuous. Requiring a root-owned copy instead made every stock `brew install gh` fail and pushed users into a `sudo cp` ritual for a CLI they had already installed and authenticated, so provenance was traded for containment: the provider child still gets only a minimal provider-scoped env, and every spawn is SEL-audited. `KIROCREW_PROVIDER_BIN_STRICT=1` restores the historical hardened rule for shared or multi-tenant hosts — canonical, symlink-free, root-owned and non-writable through every ancestor, `PATH` never consulted — and its setup error then names the privileged `/usr/local/libexec/kirocrew/` (or `/usr/libexec/kirocrew/`) copy to provision plus the override to point at it. The child receives a fixed system `PATH`, never the gateway/workspace `PATH`; `resource_limit_preexec()`, a minimal provider-specific environment, and host pinning remain enforced, and unrelated gateway/AWS/Slack credentials are not inherited. `github.com` and `gitlab.com` are always accepted; a self-managed GitLab instance is accepted only when the URL's exact `host[:port]` is a member of the operator's `dashboard.gitlab_hosts` allowlist (deny-by-default, config-only, never browser-supplied, no suffix or wildcard matching, `www.` not stripped, and a portless entry does not authorize an arbitrary port; an explicit `:443` is treated as absent on both sides so it matches the browser URL API, which drops the default HTTPS port). The allowlist is served from a process-cached snapshot refreshed at most once per 30s by `ensure_gitlab_hosts_loaded()`, which every async entry point awaits before validating a URL: the config read runs in a worker thread, never on the event loop, and the synchronous accessor that URL parsing and slot serialization use only reads the cached snapshot (empty before the first refresh, which fails closed). Refreshes are serialized behind a lock with a post-acquire freshness recheck, so a loader holding the pre-revocation config cannot install its snapshot after a newer one and re-admit a just-removed host for another interval. Each content change bumps `gitlab_hosts_generation()`, which `_ChatSlot._pr_source_links()` folds into its per-slot cache key alongside the message revision -- the synchronous scan can run before the first load, and without the generation that cold-snapshot rejection would stay memoized until the next message mutation, leaving a self-managed chip missing (or a revoked one present). A `glab` call to a self-managed host drops `GITLAB_TOKEN` from the child environment: that variable carries no host binding, so forwarding it would hand a gitlab.com credential to the self-managed server; such hosts authenticate from their per-host entry in glab's own config, still reachable via `GLAB_CONFIG_DIR`. gitlab.com keeps the ambient token. `host` is a REQUIRED argument for every `glab` invocation rather than a defaulted one: an omitted host raises instead of silently resolving to gitlab.com, so no future call site (including a mutation endpoint) can read or write an allowlisted self-managed MR on the public instance at the same project/IID. Because slot source-link extraction is synchronous and cannot load the snapshot itself, the owner WebSocket awaits `ensure_gitlab_hosts_loaded()` before its first `serialize_slots()` and again once per refresh round, pushing a slots update whenever the generation changes -- otherwise a newly authorized (or revoked) self-managed chip would wait for an unrelated message mutation. `GET /api/chat/slots` performs the same warm-up so a cold direct fetch is not missing those links either. A full payload's `url`/`number` identity always comes from the validated `SourceRef`, never from the provider's echoed `web_url`/`iid`/`url`: the browser submits that url back for refresh and thread resolution, so a hostile or compromised instance echoing a different merge request could otherwise steer an owner-authenticated call at an unrelated one. Each `glab` invocation pins `GITLAB_HOST` to the host `parse_source_url` authorized for that URL and re-checks it against the allowlist at spawn time, so a configured self-managed default in `glab` config cannot redirect bare API paths and a caller that skipped URL validation is denied rather than reaching an unauthorized instance. The dashboard-config GET exposes `gitlab_hosts` read-only (absent from the PUT allowlist) purely so the client knows which pasted links to surface as source tabs. Provider stdout is section-bounded at 1 MiB for metadata/checks, 2 MiB for discussions, and 4 MiB for diffs/changes; normalized full payloads are capped at 8 MiB. A global four-command semaphore covers full-source, direct-check, resolve, and sidebar work. Unique direct full/check tasks share a 16-task ceiling and a conservative 128 MiB retained-byte budget: full tasks reserve 64 MiB and checks tasks reserve 8 MiB until the underlying task terminates. Same-URL callers coalesce before admission, detached stale full fetches retain their leases, and a stale pre-mutation fetch plus its required fresh successor can coexist at the exact aggregate ceiling. Successful thread resolution advances that URL's cache generation and detaches older shared fetches so pre-mutation results cannot refill the cache or satisfy the post-resolution refresh. Secondary metadata endpoints degrade independently so core source details remain available, but every failed files, commits, discussion/thread, pipeline, job, or GitHub check-rollup request is named in `partialSections` before its data falls back to an empty section. GitHub's `statusCheckRollup` is never bundled into another `gh pr view` field set: `gh` resolves a `--json` field set atomically, so a fine-grained token without Checks read access would lose the fields it WAS authorized for — the full payload, the sidebar chip, and the checks poll all read the rollup through one isolated query (`_github_rollup_read`, which also carries `headRefOid`), and a rollup read that fails or that straddled a push (its head sha differing from the core read's) marks `checks` partial with an empty list instead of failing the read or rendering another commit's checks; provider page limits and overflow evidence use the same deduplicated markers. Native Windows is not refused by a platform check of its own: it has no OS-level provider sandbox backend, so it reaches the same no-backend policy a backend-less Linux host does — `sandboxed_spawn_argv` fail-closes and the read is refused unless the operator set `agent.sandbox_allow_unsandboxed_exec`, whose refusal text names that opt-in. With the opt-in set, provider commands (reads and the PR/MR mutations alike) do spawn on Windows, under the same non-sandbox bounds every host keeps: the `{gh, glab}` allowlist, the validated absolute executable, the minimal provider env with a fixed system `PATH`, host pinning, the output caps, and the SEL audit. Every provider execution emits credential-free SEL `invoked` plus `completed`/`failed` lifecycle events; policy and provenance rejections emit `denied`. The critical `invoked` append is shielded and awaited on a worker thread before spawn, preserving audit-or-deny ordering without blocking the gateway event loop; cancellation waits for that worker, pairs a landed `invoked` event with `failed/request_cancelled`, and never spawns, while other terminal events are best effort. Events contain only the logical provider and coarse reason, never argv, URL, repository, output, environment, credentials, thread id, or exception text. Sidebar refreshes run outside slot serialization with inflight deduplication, a 16-task pending ceiling, one-TTL overflow backoff, and a 512-entry status cache. Scheduling requires an exact dashboard-owner request. Cached check state is otherwise only repopulated at WebSocket-connect and slots-GET time, so each owner WebSocket connection additionally runs a background refresh driver that re-schedules refreshes for its currently-rendered sidebar chip URLs (`DashboardState.source_link_urls()` — the first `_SERIALIZED_SOURCE_LINKS_PER_SLOT` links of every slot) once per cache TTL (`CHECK_STATUS_TTL_SECS`, sleeping exactly one TTL so each round finds the previous round's entries just expired — one provider fetch per URL per TTL, coalesced across tabs by the inflight dedup; a merged or closed chip is skipped by the same `_chip_refresh_due` gate until its terminal TTL lapses); without it a PR merged or a CI run completed after page load would keep its stale connect-time chip until a full reload. The driver is owner-gated (never created for non-owner connections), cancelled with the connection, and advances a per-round starting offset by the pending admission cap (`CHECK_STATUS_PENDING_MAX`) each round so that when the number of stale chips exceeds the cap the admitted window rotates across every chip within `ceil(len/cap)` rounds instead of the same slot-order prefix winning every TTL and starving newer slots' chips indefinitely. Each round's work is individually guarded so a transient failure is logged and the loop continues rather than the driver dying silently and reverting to frozen chips. Generic slot serialization and broadcasts omit cached `state`/`ci`; owner HTTP and WebSocket snapshots opt in, and changed statuses trigger a debounced generic update followed by an owner-WebSocket-only overlay. `_ChatSlot` retains only the first 64 unique durable source links and stops scanning at the cap; `to_dict()` exposes up to three sidebar chips as `{url, provider, number}` to all authenticated callers and adds the whole cached chip-status entry — `{state?, ci?, mergeable?, mergeStateStatus?}` — only at an owner-authorized serialization boundary. `state` is `open|draft|merged|closed` and `ci` is `running|passed|failed` when known; the merge pair uses the shared merge-state vocabulary above and is present only once settled. Both providers derive `state` with terminal states outranking draft (a GitLab MR keeps `draft` set after being closed as a draft, so draft is reported only while the MR is `opened`), and a provider state outside the known set (e.g. GitLab `locked`) yields no `state` rather than a mislabeled `open`. `ci` rolls a GitLab pipeline status up the same way GitHub's check conclusions roll up: any failure fails, anything still in flight runs, and a terminal non-failure -- including a wholly `skipped` pipeline -- passes, so a skipped pipeline settles instead of spinning. Pipeline-level `manual` is the one deliberate split from the job-level bucket: a pipeline in `manual` is blocked awaiting a required manual job, so it reports `running` rather than green, while a single `manual` job among finished ones still buckets as skipped -- unless it carries `allow_failure: false`, which makes it a required gate and therefore pending. GitLab's chip status reads `head_pipeline` from the merge-request payload and falls back to the MR pipelines list only when that field is absent. GitHub's chip refresh pairs its core-field read (`state,isDraft,mergeable,mergeStateStatus,headRefOid`) with the same isolated rollup read, concurrently; only the core read is load-bearing — a rollup that fails or describes a different head degrades to an internal ci-unavailable marker that `_refresh_check_status` strips before caching, keeping a previously known `ci` glyph rather than erasing it (the same keep-known posture the full-payload write-through applies when `checks` is partial), while a successful rollup with zero checks still clears a stale glyph. Because the chip entry is spread whole, a refresh in which only the merge pair settles also counts as a status change and triggers the same debounced generic update plus owner overlay. This module also owns the TRANSCRIPT-SEARCH seam for provider ids: `SourceProviderPlugin` carries an optional `search_ref(token) -> (canonical, alts) | None` hook (getattr-discovered, like `path_markers()`), `source_search_ref()` fans out across the registered plugins and asks each registered plugin until one answers and hands that answer through UNVALIDATED, without reading `alts` itself (the shape checks belong to the single normalizer, whose guard also wraps this collector because it IS the resolver core calls, while a raising hook is caught per provider here); the FIRST plugin to ANSWER wins, for every token shape, and the spellings it contributes are bounded once in core by `_MAX_SEARCH_REF_SPELLINGS` rather than by a collector-side ceiling that would drift from it. There is deliberately no cross-plugin merge: one would exist to serve two registrants holding a real item at the same number, and this repo registers no provider at all, so it would be surface no code path can reach — additive if a second registrant ever appears. `register_source_provider()` additionally publishes that collector downward into `history_search.register_search_ref_resolver` — dashboard → core, at registration rather than from a route handler, so the non-HTTP callers of `parse_search_query` answer identically. Casefolding, dedup and the per-provider spelling cap belong to `history_search._provider_search_ref`, the single normalizer; `reset_source_providers_for_tests()` unpublishes the resolver alongside the plugin registry.
- `server.py` — app factory, route registration, startup, SPA fallback middleware for React Router, `/api/ws` WebSocket route, token auth middleware, loopback-only binding (`127.0.0.1`). Fires background MCP probe at startup via `asyncio.create_task()`. Honors `agent.yolo=true` config at startup via `_apply_startup_yolo()` — attempts SEL audit first and only activates dashboard YOLO (6h TTL) if the audit succeeds (fail-closed).

### Security

- **Network binding**: dashboard binds to `127.0.0.1` only (loopback), never `0.0.0.0`. Prevents unauthenticated remote access from network-adjacent attackers.
- **Token authentication**: `dashboard/token_auth.py` validates a signed HMAC session token on every request. Pure stdlib — no external deps. Returns `401` if invalid. The enterprise SSO status surface (`sso_status.py`) is an inert stub in the OSS build — no cookie validation is performed.
- **WebSocket origin validation**: `ws.py:_check_ws_origin()` validates the `Origin` header on every WebSocket upgrade request before accepting the connection. Rejects missing Origin (non-browser clients) and cross-origin requests. Only allows `http://127.0.0.1:{port}`, `http://localhost:{port}`, and `http://kirocrew.localhost:5476`. Prevents cross-origin WebSocket hijacking where a malicious page could connect to `ws://127.0.0.1:5476/api/ws` and passively exfiltrate conversation data.
- **Token authentication**: `dashboard/token_auth.py` validates the signed HMAC session token on every request including WebSocket upgrades. Pure stdlib — no external deps. Returns `401` if invalid. The enterprise SSO status surface (`sso_status.py`) is an inert stub in the OSS build — no cookie validation is performed.
- **CSRF protection**: `server.py` CSRF middleware validates `Origin` header on all non-safe HTTP methods (POST, PUT, DELETE). Same allowed origins as WebSocket.
- **Source-provider mutation ownership**: GitHub/GitLab source reads may use a signed machine-local bootstrap subject (`local-app` / `local-startup`) before an owner is configured, but every provider mutation remains owner-only. When one of those signed local dashboard sessions reaches a mutation with no configured owner, the gateway still returns `403` but labels the already-denied response with `code: "owner_not_configured"`; the Changes panel, review threads, and Code Review Sage replace the server's English fallback with localized guidance and link to Settings → Channels → Slack. App tokens, unauthenticated requests, and non-local subjects retain the generic forbidden body so the response does not disclose the install's owner state.
- **Prerequisite mutations**: agent-spec repair is the ONLY one left — there is
  no install route and no login route (see the gate section), so the only other
  endpoint is the status read. Repair accepts no body-controlled command, URL,
  argument, or path, and requires an explicit empty app claim, so an app token
  stays denied even if its manifest declares the route prefix.
  Invoked actions are critical-audited before spawn and terminal outcomes are
  best-effort audited.
- Static assets (`/assets/`, `/static/`) bypass auth check.

### Session Lifecycle

Each chat tab gets its own kiro-cli session keyed by `dashboard:{slot_key}` with a corresponding JSONL file (`~/.kiro/crew/history/dashboard:{key}.jsonl`). Sessions move between active and history:

1. **New**: user clicks + → creates slot with unique key → `get_or_create("dashboard:{key}")` assigns a kiro-cli session (cold start)
2. **Chat**: messages accumulate in-memory (`slot.messages`, max 5000); kiro-cli session is per-tab so tabs run tools in parallel
3. **Close**: user clicks ✕ → slot saved to JSONL (overwrites, preserves `created_at`) → per-tab session killed via `sessions.remove()` → removed from active → appears in history
4. **Resume**: user clicks history item → JSONL loaded into slot (same key) → new kiro-cli session created with history re-injected (if already active, returns existing — no duplicate)
5. **Close again**: saved back to SAME JSONL file → same history entry (no duplicates)
6. **Gateway shutdown**: all active slots saved to JSONL

**Guarded metadata writes:** slot metadata endpoints mutate the live slot before
their history write. While a write is pinned to an authorized transcript key, the
periodic unpinned flush skips that slot; this prevents a rebind from making the
provisional value durable in a different transcript. A refused pinned write rolls
back its endpoint-owned value before a later flush may resume.
7. **Idle expiry**: per-tab sessions expire after `session.timeout_secs` (default 60 min) like any other session; on next message a fresh session is created with history re-injected

Cross-tab context: **removed** (budget redistributed to other caps). Previously injected recent messages from other dashboard tabs; this block was eliminated and its 6,000-char budget absorbed into the raised memory/lessons caps above.

**Context budget** (`context.py`): total cap 165,000 chars (~55k tokens). Priority order: critical rules → memory (preferences 4,250, projects 6,400, history 26,600) → skills (on-demand, few always-on) → lessons (37,250) → conversation history (8k budget, 8,000 chars/message cap, most-recent-first fill) → provenance. Individual messages exceeding 8,000 chars are truncated with `…[truncated]`. If total exceeds 165,000, hard-truncated at nearest newline.

**Per-turn timeout** (`constants.py:CHAT_TURN_TIMEOUT`): every `_run_chat` invocation is wrapped with `asyncio.wait_for(timeout=CHAT_TURN_TIMEOUT)` regardless of dispatch site. This applies uniformly to: primary user-typed turn (`chat_handlers.py`), queue-drain (`chat_runner.py` finally block), cron injection (`handlers/messaging.py`), Slack/dashboard nudge (`slack/gateway.py` autonudge path), subagent injection (`slack/gateway.py` two paths), and the post-fan-out synthesis turn (`chat_runner.py` drain/idle branch — fires one consolidated synthesis after the last sub-agent of a fan-out completes). The structured dashboard-monitor path runs `_run_chat` inside an authorization coroutine passed to `spawn_guarded_turn`; authorization is rechecked after the background permit and the helper still owns the same ceiling. The cap (7200s, 2 hours) is sized to match the inner ACP `_DEFAULT_PROMPT_TIMEOUT` so the dashboard layer does not bound below the transport. The `_STALE_TURN_TIMEOUT` (90s, in `acp/client.py`) is the real wedged-session guard — it fires when streaming has gone silent. `CHAT_TURN_TIMEOUT` is the upper safety ceiling for genuinely runaway work, not a "this turn took too long" guard.

**Custom agent context**: When a dashboard slot uses a non-kirocrew agent, `build_message()` and `build_session_context()` skip only skills and workspace identity (custom agents load their own via kiro-cli). All other context is injected for all agents: critical rules (diff rendering, OPTIONS buttons), memory (preferences, projects, history, semantic, episodic), lessons, hooks, and OPTIONS reminder. This ensures custom agents, cron jobs, and task runners all benefit from the user's learned preferences and project context.

Streaming chunks are cleaned up after each response (only final assistant message kept). Transient roles (`chunk`, `done`, `queued`, `permission`) are excluded from history saves.

**File-change snapshots** (`chat_runner.py::_snapshot_write_target`): captures before/after content for write-tool invocations, attached as `file_changes` meta on the last assistant message by `_flush_file_changes`. The 'before' content is sourced in priority order: (1) **strReplace only** — full-file reconstruction via `_reconstruct_str_replace_before` (declines outright for `replaceAll` edits, where oldStr uniqueness is not enforced and reversal would over-revert pre-existing `newStr` occurrences): read the file (regular files only — `S_ISREG` at the stat gate, so `/dev/zero`/FIFOs never read — ≤ `_MAX_RECONSTRUCT_BYTES`, re-checked post-read) through `hooks.safe_read_file` (symlink-safe: resolved-target re-check + `O_NOFOLLOW`) and reverse-apply the substitution from the tool params. Both `_snapshot_write_target` call sites in `_run_chat` are offloaded via `asyncio.to_thread` so a slow/hung filesystem cannot stall the event loop. Classification: `newStr` absent → post-write excluded, pre-write proven iff `oldStr` unique; `newStr` present but not unique (overlap-safe `find()==rfind()`) → post-write can neither be excluded nor reversed → decline; `newStr` unique → the single reversal candidate decides (tool-consistent AND pre-write plausible → undecidable seam, decline; consistent only → reverse; inconsistent + pre-write plausible → pre-write proven). This exists because kiro-cli's diff content block `oldText` for strReplace is only the replaced **fragment** — using it verbatim diffed a fragment-before against the full-file after and counted the entire file as additions (the #920 race fix's full-file assumption holds for create, not strReplace). Reconstruction declines (falls through) on missing params, empty-`newStr` deletions (position unrecoverable), unreadable files, or neither needle matching. (2) ACP diff content block `oldText` carried on the tool-call event (`AcpEvent.diff_old_text`, threaded from `acp/_dispatch.py::_build_tool_call_event`) — authoritative and race-free for create (`''` for a new file, full previous content for an overwrite). (3) Disk read fallback — used only when no diff content block exists (e.g. the blocking `session/request_permission` path where the write has NOT yet executed). No-op entries (before == after) are kept and surfaced — the dashboard renders an explicit "no changes" caption for them (a backend drop would compare post-truncation/post-redaction content and silently discard real changes past the snapshot limit or inside redacted spans). Security invariants apply equally to content-block-sourced and reconstructed text: `validate_file_path` refuses sensitive paths before any source is consulted, `_truncate_snapshot` caps content length (after reconstruction, so the needle can't be cut mid-file), and credential/exfil-URL redaction in `_flush_file_changes` covers all sources.

**Agent Config**: PUT saves to `~/.kiro/agents/kirocrew.json` and auto-restarts all kiro-cli sessions so changes take effect immediately.

### Tool-Approval Resolution Persistence

A pending tool approval has **two** pieces of state that must stay in lockstep: the in-memory `asyncio.Future` in `slot._approval_futures[request_id]`, which the chat runner awaits, and the `permission` message in `slot.messages`, whose `cls` JSON is what the UI renders the approval bar from. The message is the durable half — it is the only piece that survives a history reload.

**Invariant: every path that resolves or discards an approval future MUST also mark the message.** A future resolved without marking leaves a card that renders as pending while nothing is listening: the UI keeps showing live Allow once / Trust / Reject buttons, `POST /api/chat/slots/{slot}/approve` answers `404 no pending approval` for all of them, and a reload resurrects the card. The resolution paths and their markers:

| Path | Marker |
|---|---|
| HTTP `POST /api/chat/slots/{slot}/approve` | `api_chat_slot_approve` marks `"trust"` / `"trust_reads"` verbatim (the UI renders those distinctly), everything else as the coerced `"approved"` / `"approved_trust_reads"` / `"rejected"` |
| Slack approval click | `DashboardState.resolve_approval` marks `"approved"`/`"rejected"` |
| Bulk trust/yolo mode switch | `api_chat_mode` marks with the mode name (`"trust"` / `"yolo"`) |
| Stop / interrupt | `_reject_pending_approvals` marks `"rejected"` |
| Chat runner's own exits — 2h `wait_for` timeout, Slack delivery-failure auto-reject, task cancellation | the `finally` backstop in `_run_chat` marks with `only_if_pending=True` |
| Turn-start sweep (repairs orphans from prior turns) | `_sweep_stale_permissions` marks `"stale"` |

**`resolved` field values** (`cls.resolved`): `"approved"`, `"approved_trust_reads"`, `"rejected"`, `"trust"`, `"trust_reads"`, `"yolo"`, `"stale"`. Presence of the key — not its value — is what makes a permission message non-pending; `selectSlotPendingApproval` in the SPA and the `only_if_pending` guard both key off presence alone. The frontend additionally writes `"stale"` locally when a decision 404s, which clears the orphaned card in that tab; it is a display-layer dismissal, not a backend write, so orphans persisted before the marking paths existed converge only via the next turn's sweep.

**`only_if_pending` guard**: `_mark_permission_resolved(..., only_if_pending=True)` returns `False` and writes nothing when `resolved` is already present. The runner's backstop uses it so it cannot flatten a richer decision a primary resolver already recorded — `"trust"` renders as *"Trusted — auto-approving future calls"* and would otherwise be downgraded to a bare `"approved"`. It also suppresses a duplicate `approval_resolved` WS broadcast on the paths where the primary resolver already sent one.

**`slot._dirty` obligation**: `_mark_permission_resolved` mutates `slot.messages` in place and returns `True` when it wrote. The periodic flush (`_flush_dirty_slots`, every 5s) **skips slots whose `_dirty` is `False`**, so a caller that marks without flagging the slot can lose the write on restart and resurrect the card. Every call site holding the owning slot sets `slot._dirty = True` on a `True` return. This invariant is enforced by convention at each call site, not by an owning helper — a new future-resolution path must honor both halves.

**Runner backstop totality**: `outcome` is pre-seeded to `"rejected"` before the approval `await`, because the `finally` runs on *every* exit including `CancelledError` (slot deletion and cleanup endpoints cancel `slot.task`). Assigning it only inside `try`/`except` would raise `UnboundLocalError` from the `finally`, replacing the cancellation with a spurious exception and skipping both the message marking and the Slack prompt cleanup.

### Queue turn boundary finalize

A successor turn dispatched WITHOUT a `chat_done` -- the tail-drain starting a
queued turn (`_start_next_queued_turn`), and the synthesis dispatch in
`_run_pending_synthesis` -- broadcasts `chat_segment` for the slot once the
successor is certain to dispatch, before the successor's row and first chunk.
The end-of-turn flush suppresses its own `chat_segment` (deferring the
client-side streaming->assistant finalize to `_finish_queue_cycle`'s
`chat_done`, which never comes on these paths), and the flush's
`chat_message{role:assistant}` frame is conditional (suppressed while an HTTP
SSE reader drains the slot, absent when the final segment is empty, droppable
by the client's mid-keyed redelivery guard) -- without the unconditional
boundary frame the successor's chunks append into the still-open `streaming`
row and a line-final `[OPTIONS: ...]` marker degrades to prose. Non-fire
condition: a boundary where no successor dispatches (empty queue, dropped
entry, synthesis not eligible) emits nothing here -- `_finish_queue_cycle`'s
`chat_done` stays that path's sole finalizer, so no path double-finalizes.

### Mid-Turn Steer (dashboard transcript contract)

A steer (`POST /api/chat` with `steer: true` while the slot is running) injects
the message into the in-flight turn via kiro-cli `_session/steer`. kiro-cli does
NOT end the current text segment at the steer, so both sides of the boundary
must be handled explicitly or the transcript disagrees with what the user
watched stream (the "stuck streaming marker" bug: the live streaming message is
stranded ABOVE the steer bubble, the rest of the segment streams into it there,
and the finalized reply jumps BELOW the bubble when the `chat_done` refresh
rebuilds from server history).

**Backend — segment cut at the steer boundary.** `_run_chat` publishes a sync
closure on the slot (`slot._steer_segment_cut`, same lifecycle as
`slot._acp_client`: set at turn start, cleared in the turn's `finally`). The
steer handler calls it right BEFORE `slot.append("user", …, meta={"steer": True, …})`
(the meta also carries the client's `sendId` when the POST supplied one — see
the send-identity paragraph below),
so the accumulated segment text is flushed as its own assistant message and the
persisted order is `[assistant(pre-steer), user(steer), assistant(post-steer)]`
— identical to the live view. The cut persists SILENTLY: `broadcast=False`
(no `chat_segment`), `quiet_persist=True` (no per-message `chat_message` from
the append), and the wire redactor's withheld tail is dropped via
`_wsred.reset()` rather than emitted (no late `chat_chunk`). At the cut
boundary every client has already finalized its streaming message — the
initiating tab froze at optimistic-push time, other tabs freeze on the
`steer_push` echo — so any of those events would materialize a duplicate copy
of the pre-steer text (or a phantom streaming bubble) below the steer bubble.
No text is lost: `assistant_text` accumulates independently of the wire
buffer and is re-redacted at persist. The cut also sets
`_produced_visible_output` (the flushed segment is visible output; without it
a stream→steer→quiet-end turn would trip the empty-response requeue). The cut
is best-effort (a failure logs and never loses the steer) and also prevents the
chunk-entry leak: without it, `_flush_segment`'s trailing-run walk stops at the
mid-run steer user message and the pre-steer `chunk` entries stay in
`slot.messages` for the life of the process.

**Frontend — finalize-on-steer.** Every path that INSERTS a steer bubble first
finalizes the live `streaming` message in place via `finalizeTrailingStreaming`
(streaming → assistant; placeholder-only content is dropped, same rule as the
`_segment` handlers, which share the helper): the optimistic push
(`appendMessage`, `meta.steer`), a pane-scoped optimistic push, and the
`steer_push` echo when no optimistic bubble exists to reconcile (another tab /
scene-interaction steered). The reconcile path deliberately does NOT freeze — by
echo time a new post-steer streaming message may be live below the bubble and
must keep streaming. Pinned by `test_chat_steer.py` (cut ordering, cut-failure
resilience) and the `finalize-on-steer` describe in `chatSlice.test.ts`.

**Send identity — `sendId` through the steer path (#6075).** The optimistic
steer bubble is minted with a client `sendId` (the same one-shot convention the
plain send path uses) and the steer POST carries it as `meta.sendId`. Both
backend paths persist it: the accepted-steer row via
`steer_into_running_turn` — which normalizes the raw client value at entry
(`normalize_send_id`: the URL-safe id alphabet within `SEND_ID_MAX_LEN`, AND
nothing the canonical credential scanner would redact, since the value reaches
slot history and the `steer_push` broadcast without the outbound redaction the
message text goes through; anything failing either gate is treated as absent,
never truncated) and also echoes it on the `steer_push`
broadcast — and the raced new-turn row via the generic client-meta persistence
in `api_chat`. Resolution is id-first everywhere text used to be the key: the
`steer_push` reconcile matches the bubble by id (an id-mismatched bubble is
never consumed; a non-optimistic row already carrying the echo's id means the
`chat_done` refresh installed it first and the echo is a redelivery that
inserts nothing), and `mergePreservedThinking` resolves an optimistic STEER
bubble's accepted-vs-new-turn ambiguity from the covered page's row with that
id (steer flag = acceptance, scan continues; non-steer = new-turn boundary,
recorded so the finished turn's chip drops with coverage). Text identity never
resolves the bubble; an unmatched, duplicate, or absent id keeps the
decline-to-guess default. `sendId` is additive optional meta everywhere it
travels — no schema bump, and a send without one keeps the exact prior row,
payload, and scan behavior. Pinned by the sendId tests in
`test_chat_steer.py`, `chatThinkingSteerBoundary.test.ts`, and
`ChatSliceCoverageSecondPass.test.tsx`.

**Send identity through the REQUEUE path (#6751).** A steer whose turn dies
before kiro-cli confirms it does not persist its own row: the teardown degrades
it into a queue card and the DRAIN writes the row. That path is covered by the
same id, threaded one step further. `steer_into_running_turn` records the
normalized value in `slot._steer_send_ids` (keyed by the message text, like
`slot._steer_delivery_ids`, and removed in LOCKSTEP with it at every site --
the unwind, the terminal persisting tail, the hard-kill discard, and the requeue
itself -- so an entry in one always implies an entry in the other),
`_requeue_unconsumed_steers` moves it onto the queue entry's meta beside
`steer_delivery_id`, and the drain's union over every consumed entry's meta
carries it onto the row it appends. The three `STEER_REQUEUED` returns are
deliberately NOT the write site: two of them cannot be, because one returns
before the teardown has requeued anything and the other after the drain has
already written the row, so the requeue is the only writer common to all three.
The resulting row is a non-steer user row carrying `meta.sendId` -- the same
shape `api_chat`'s new-turn path already persists -- so `mergePreservedThinking`
reads it as a new-turn boundary for that id and the finished turn's chip drops
with coverage instead of stranding at the tail until a reload. No client change.
Known residual: `_drained_meta` accumulates only `steer_delivery_id` (into
`steer_delivery_ids`) and is last-writer-wins for every other key, so a row that
MERGES two requeued steers keeps one `sendId` and the other send keeps the
over-keep default. Pinned by the requeue sendId tests in
`test_steer_requeue.py`.

### Wait countdown and early end (`/api/session-keepalive` as a control channel)

While the MCP `wait` tool sleeps, the transcript shows a live countdown and an
"End wait" button that returns the tool early **without ending the turn**. Both
ride `POST /api/session-keepalive`, which is therefore no longer write-only.

Why that endpoint and not a cancel path. The tool sleeps in a separate MCP
subprocess that runs **no inbound listener** — communication is outbound
`urllib` only. The one channel that can interrupt it, `notifications/cancelled`
on stdin, is unusable for this: it is a session-teardown signal whose response
`_run_tool` suppresses entirely (so kiro-cli would wait out the 600s stall
watchdog), it requires `mcp_gateway.enabled` (default **False**), it cancels
*every* in-flight request for the PID's stubs, and on Windows there is no path
at all because `select.select()` cannot poll stdin. The keepalive round-trip was
already running on a timer, already authenticated (`X-Internal-Secret`), and
already carried session identity (`X-Session-Key`) — and its reply was being
discarded.

Contract:

- **Request** (`wait` only; every other caller still sends `{}` and is
  unaffected): `{wait_id, seconds, remaining, interval}`, or
  `{wait_id, wait_done: true}` on the final ping. `wait_id` is a uuid4 hex
  minted per sleep. The body is optional and advisory — a malformed or
  non-object body degrades to touch-only rather than failing the keepalive,
  because that half of the route is what stops the watchdog killing the ACP
  subprocess mid-wait.
- **Reply**: `{"ok": true}`, plus `{"end_wait": "<wait_id>"}` when the user has
  asked to end *that* sleep. The tool breaks its loop and returns a normal
  string — deliberately **not** `ToolCancelled`, whose response is suppressed.
- **Ping interval** is `mcp_core.WAIT_PING_SECS` (5.0s), not the 60s the
  staleness watchdog alone needs: it is the button's worst-case latency. The
  handler only touches two timestamps, so a 30-minute wait costs ~360 loopback
  POSTs and no meaningful work.
- **`POST /api/chat/slots/{slot}/end-wait`** with `{wait_id}` parks the request
  on the slot for the tool to collect. 400 `wait_id_required`, 404
  `slot_not_found`, 409 `wait_not_in_flight` when the id does not match the
  sleep currently tracked — which is how a click on a stale countdown is
  rejected instead of ending a *later* wait. Consumed exactly once.
- **`_ChatSlot`** gains `_wait_state` (`{wait_id, seconds, deadline_ts}`, emitted
  as `wait_state` in `to_dict()`), plus the server-only `_wait_last_ping` and
  `_wait_contested`. `deadline_ts` is absolute and minted **once** on first
  sight from the tool's own `remaining`: recomputing per ping would jitter the
  countdown by a round-trip, and the tool's monotonic clock shares no epoch.
  Riding the slots payload (rather than a bespoke WS event) is what makes a
  mid-wait reload re-seed the countdown from `GET /api/chat/slots`. Pushed on
  start and end only — never on the 5s heartbeats.

**Authoritative identity is a precondition, not a nicety.** Before publishing
anything the tool calls `_resolve_session_key_strict()`, and when that comes back
empty it sends the original bare `{}` ping and refuses to honour `end_wait` at
all. The countdown and the button simply do not appear. This is the primary
control, and it is what makes the channel safe rather than merely
usually-right: `_resolve_session_key()` — the lenient resolver behind the
`X-Session-Key` header — ends its ladder with a `/proc` ancestor walk, so a
subagent's MCP-core child walks up into its parent slot's process tree and
resolves to the **parent**. Publishing a `wait_id` under that key puts a
subagent's deadline on the parent's pill and lets the parent's button end the
subagent's sleep. Nothing downstream can catch that case, because with a single
`wait_id` pinging there is no collision to observe. `_resolve_session_key_strict()`
is the existing primitive for this class of session-mutating tool
(`monitor_start`, `autonudge_stop`, `set_project`): it drops the walk and accepts
only a gateway-injected caller context, `KIROCREW_SESSION_KEY`, or an
HMAC-verified pid sidecar. Consequence worth stating plainly: with
`mcp_gateway.enabled` at its default of **false** and no sandbox launcher, the
feature is absent. [#2347](https://github.com/kirodotdev/KiroCrew/issues/2347) is
the work that supplies per-session identity and lets this gate go away.

**Ambiguous-identity containment.** Behind that gate, two sleeps can still share
one slot wherever identity resolves to a session that hosts both. The ping doubles
as a heartbeat, which makes that detectable — a second `wait_id` arriving while the
incumbent is still pinging (within `interval * 2.5`) means two sleeps genuinely
share the slot, so **neither** is tracked, any parked request is dropped, and the
slot is marked contested. The countdown disappears and the feature degrades to the
bare spinner it replaced. The latch is **turn-scoped** — released only by the
turn-end block in `chat_runner`, never on a timer. A self-expiring window was
tried first and rejected: both sleeps keep pinging, so on expiry whichever pinged
first re-minted its own `wait_state`, and for up to one ping interval that
deadline was published and painted onto the *other* sleep's pill with a live
button that would have ended the wrong one. The attribution flapped open once per
window instead of staying shut. A `wait_id` change *after* the incumbent
goes quiet is still a legitimate hand-over (missed `wait_done`, killed process).
Two frontend guards cover the one-sided case: the countdown requires the slot to
be `running` and requires its pill to be the transcript's newest tool row of any
kind — within one session the agent is blocked on the wait's result and cannot
issue another call, so a later tool row means the wait is over or belongs to
someone else. All three are **containment, not a cure**; the cure is per-session
identity and is tracked separately in
[#2347](https://github.com/kirodotdev/KiroCrew/issues/2347), which lists what to
delete here once it lands.

Frontend joins the countdown to a pill by tool *title*, via `isWaitToolTitle()`,
an **allowlist** of the shapes the transport produces: `wait`, `<server>___wait`,
`wait (mcp)`, and the two decorations combined. Deliberately stricter than its
backend counterpart `acp/liveness.py::is_wait_tool`, and the asymmetry is
intentional rather than drift. The backend's looser rule only answers "is this
session legitimately blocked in a long tool", where over-matching merely declines
to reap something; here a false positive misattributes a live deadline. Concretely,
a per-token rule accepts an unrelated `wait_for_ci` from another server, so a
subagent's `wait` could publish its deadline onto the parent's `wait_for_ci` pill
and arm the button against the wrong sleep — and the contested latch cannot cover
that case, because only one `wait_id` ever pings and there is no collision to
detect.

Dashboard-only: the Slack renderer tears its stream down for the
duration of a `wait`, so there is no surface there to host either affordance.

### Agent Questions (`ask_question`)

`ask_question` is **non-blocking** as of issue #755: the tool renders a question card in the caller's own chat window and the agent ENDS its turn — it no longer holds the turn open on a server-side wait resolving to an answer map. User-facing documentation lives in `src/kiro_crew/docs/agent-questions.md`; this section is the contract.

The MCP tool resolves NO session identity of its own and calls NO HTTP endpoint. It validates its arguments (`ASK_QUESTION_SCHEMA`) and returns a **session directive** — a human-readable confirmation line plus a machine-readable marker (`session_directive.encode`) carrying the validated `questions` and no session key (the stateless mechanism is specified in `session.md` → "Stateless session-directive tools", `src/kiro_crew/session_directive.py`). The session-aware consumer — `chat_runner._run_chat`'s `EVENT_TOOL_RESULT` handler, which owns the turn's authoritative `slot`/`session_key` — decodes the marker and applies the effect in-process against ITS OWN slot via `dashboard/session_directive_apply.py::_ask_question`.

Application posts the card through `DashboardState.post_question_card(slot_key, questions)`: it runs `_redact_questions` over every question/header/option string, broadcasts a `question_card` **with no `ask_id`** to the slot's owner sockets, and returns the count of clients that received it (zero → the model is told to ask in plain text and end its turn). There is no future, no `_pending_questions`/`_question_futures` entry, and no `question_card_resolved` follow-up — nothing is left pending, so no stop/interrupt/delete or session-reset path has a blocking wait to unwind for questions.

The user's answer arrives as an **ordinary next message** that resumes the session with full context; the frontend submit for an `ask_id`-less card uses the same send-as-a-normal-message path the legacy `AskUserQuestion` tool-call sniff already used (`chatSlice.pendingQuestions` remains keyed by slot). The agent must end its turn and must not re-ask or guess in the meantime.

**Forgery gate + audit** (shared by every directive tool): the consumer honours the directive ONLY when the tool call was recorded — via kiro-cli's out-of-band `_meta` channel — as an MCP call whose canonical `_meta.kiro.toolName` (with `_meta.kiro.mcpServerName` set) is a known directive tool, never the LLM-authored `title`; native sub-agent tool calls are refused (no independently bindable slot); and every application emits a SEL tool-invocation event tagged `source="mcp-directive"`.

**Non-dashboard surfaces**: at validation time the tool still inspects its session shape — a resolved non-`dashboard:` session key is answered with the `[OPTIONS:]`-tag hint instead of a directive (a card needs a chat window), while an empty default-install key falls through to the directive, which the consumer applies (or reports as undeliverable when no client is attached).

Bounds (`validation.py`, single source of truth) still gate the tool's arguments at validation time: ≤ `_ASK_MAX_QUESTIONS` (4) questions, ≤ `_ASK_MAX_OPTIONS` (6) options each, question 500 / header 50 / label 200 / description 500 chars. The answer no longer returns through a bounded answer map — it arrives as an ordinary chat message (above) — so it is subject to normal message handling rather than the former `_ASK_MAX_ANSWER_LEN` truncation.

**Legacy blocking stack (retained in code, no longer used by the tool, slated for removal).** The former second blocking round-trip — `DashboardState.request_question` / `resolve_question` over `_pending_questions` / `_question_futures`, the `ask_id`, the `question_card_resolved` broadcast, the `POST /api/ask-question` + `POST /api/ask-question/{ask_id}/answer` + `GET /api/ask-question/pending` endpoints, the caller-chosen `_QUESTION_TIMEOUT_*` window bounded by the ACP tool-stall watchdog (`acp/client.py::_TOOL_STALL_TIMEOUT`), and the `_unblock_pending_waits` / `cancel_questions_for_slot` release chokepoints — still exists in the codebase but is dead on the `ask_question` path. It is described here only so a reader tracing those symbols knows they are no longer how the tool works.

### Key Endpoints

**AutoNudge maintenance**: `maintenance_service()` gives administrative recovery one authoritative store view and holds a per-data-home transaction lock across its full cleanup; service startup and public `add()` / `update()` / `remove()` transactions take the same lock, so maintenance cannot scan a temporary in-memory absence from a removal that later rolls back or race an external reactivation. The maintenance view owns unserialized cleanup mutations while its transaction is held. A per-loop quiesce signal wakes an update/removal already queued behind that transaction instead of letting a firing timer and maintenance wait on each other's lock; cleanup retains and ultimately removes the durable row. A caller-authorized arm carries a commit-time session predicate into `add()`, evaluated only after it owns this transaction, so an arm validated before cleanup cannot recreate the archived slot afterward. Startup holds the lock across load, repair, timer arming, and singleton publication, while concurrent maintenance waits and then reuses the published live service. An offline view never arms timers or publishes the singleton. `deactivate_and_wait()` persists an inactive restart marker and waits for both the timer captured before the update and any replacement installed while that update waited. Administrative recovery removes the marker only after its dependent worker cleanup succeeds. Persistence is the commit point for every mutation: failed add/update writes restore the prior live loop and timer state, while failed removal restores the in-memory row (and its timer when active), leaving the same durable view visible for an immediate retry.

**Status/System**: `/api/status`, `/api/system` (live metrics, 1s refresh, static fields cached), `/api/stream` (SSE), `/api/ws` (WebSocket — single multiplexed connection replacing SSE + polling for React SPA)
**Project git**: GET `/api/project/git?path=<dir>` (checked-out branch label for the activity panel). `path` is matched against the gateway's own known project directories (every live chat slot's `project` plus the recorded recent-projects list) using pure string normalisation (`expanduser` + `normpath`), with no filesystem access on the untrusted value; an unrecognised directory is refused with 403 plus an SEL audit record, and only the matched server-held string is stat'd. Response is `{"repo": false}`, `{"repo": true, "repoRoot", "branch"}`, or the detached form `{"repo": true, "repoRoot", "detached": true, "head"}`, plus `path` on the envelope. No `git` subprocess is spawned; the branch comes from reading `.git/HEAD` directly (a `git` invocation parses repo-local config, which can carry `[include] path = <file>` and read an arbitrary file, so reading HEAD ourselves removes that class), and a linked worktree's `.git` pointer file (`gitdir: <path>`) is followed to the real HEAD. Both metadata reads go through `hooks.safe_read_prefix` (realpath canonicalisation, sensitive-target rejection, `O_NOFOLLOW`, bounded read). `branch`, `head`, `path`, and `repoRoot` all pass through `redact` before being returned; the SEL audit keeps the real path. Every failure mode degrades to "no branch" rather than erroring, since the only consumer is a decorative label. A detached HEAD gets a fixed 7-char prefix, not git's dynamic abbreviation.
**Project tree**: GET `/api/project/tree?path=<dir>` (workspace file listing for the Files tab's Pierre tree, and for a chat side-panel `folder` tab whose path IS the chat's project root — `FolderPanel` renders the same `PierreWorkspaceTree` there so descending expands in place instead of re-targeting the tab, and falls back to its one-level `browseFiles` listing for every other directory and whenever this endpoint does not answer, because a directory path chip can name any directory on the gateway while this allow-list deliberately does not). File tabs start with their optional project-tree rail hidden so the document receives the full panel width; the toolbar toggle and persisted `mc-files-rail-open` preference keep it available on demand. Same known-project allow-list, sensitive-path refusal, and SEL auditing as `/api/project/git`. Inside a git repository the listing is `git ls-files -z --cached --others --exclude-standard` with `cwd=<dir>` (tracked + untracked, `.gitignore` honored; a project dir that is a repo subdirectory lists only its own subtree); outside one it is a bounded `os.walk` that skips hidden and heavy directories (`_PROJECT_TREE_SKIP_DIRS`). Response `{root, paths: [<project-relative POSIX file paths>], repo, truncated?}` capped at `_PROJECT_TREE_MAX_ENTRIES` (10,000) entries; `root` and every path pass through `redact` before egress.
**Theme / display**: GET `/api/theme/boot` (**unauthenticated** — same boundary as `/api/health`; returns `{mode, color, language, onboarded, import_onboarded}` so the SPA can apply the workspace theme, pick its UI language, and resolve onboarding gates before the token flow completes; no secrets), GET/PUT `/api/config/theme` (read/persist workspace display prefs `{mode?, color?, language?, onboarded?, import_onboarded?}`; `mode` restricted to `""`/`dark`/`light`/`system`; `language` is `""` (auto-detect from the browser) or a BCP-47 tag validated for **shape only** via `_LANGUAGE_TAG_RE`, so adding a shipped language stays a frontend-only data change). All four response sites are built by one helper (`handlers/core.py::_theme_payload`) so a newly-added display pref cannot be surfaced by some and omitted by others — see `config.md` → "Dashboard UI language".
**Foreign-agent import**: authenticated GET `/api/onboarding/import/scan`, POST `/api/onboarding/import/apply`, PUT `/api/onboarding/import/state`; see "Foreign-Agent First-Run Import".
**Memory**: GET/PUT `/api/memory/preferences`, GET/PUT `/api/memory/projects`, GET/PUT `/api/memory/history`, GET/PUT `/api/memory/settings` (consolidation config: `history_idle_hours`, `history_max_days`; writes to config.json, applies immediately to running consolidator)
**Cron**: GET `/api/crons` (includes `last_run_ts`, `has_result`, `has_slot`, `hide_in_chat`, `folder_id`, per-job `timezone`, top-level `server_tz`, `skip_dates`), POST `/api/crons` (create, optional `agent`, `hide_in_chat`, `folder_id` fields), DELETE `/api/crons/{id}` (single delete; SEL-audited `cron.remove` with the job id and `ok`/`not_found` outcome — the audit lands before history cleanup so a history-store failure cannot lose the record of a completed delete, and it is exception-contained so a SEL construction failure can never report a completed delete as failed; the single-delete MCP tool `cron_remove` and CLI `kirocrew cron remove` emit the same `cron.remove` event, spelling the success outcome `allowed` to match their own `cron.create`/`cron.update` neighbours), DELETE `/api/crons` (batch delete; body `{"ids": [...]}`, ids de-duplicated, capped at 500, per-id failure isolation, returns `{ok, deleted, failed}` with `ok` true iff anything was deleted; history purged per removed job; single `crons` refresh push; SEL-audited), PATCH `/api/crons/{id}` (partial update: name, message, cron, every, agent (or agent_id), channel, approval_mode, silent, strict_schedule, hide_in_chat, folder_id, timezone), POST `/api/crons/{id}/enable`, POST `/api/crons/{id}/run` (immediate execution, returns `{name}`, non-blocking via `create_task`), POST `/api/crons/{id}/to-chat` (creates linked chat slot `cron-{id}` with `linked_session_key="cron:{id}"`, hydrates from session history, reuses existing slot), GET `/api/crons/{id}/script` (read-only source of a script cron's callable: `{source, file, function, truncated, reviewable, sha256}` — `reviewable` says the rendered `source` equals the raw body verbatim (no redaction masking, lossless decode, not truncated), `sha256` is over the raw bytes read, pre-redaction, and is what a grant approval echoes back as `expected_source_sha256`; the path is derived server-side from the job's stored `script` field — never from the client — re-validated by `resolve_script_path` and read through `safe_read_file_bytes_nolink` pinned to `<config_dir>/crons/`, capped at 256 KiB with truncation flagged; source is credential/exfil-URL redacted before leaving the backend; 404 for unknown job or a job with no `script` (`code: job_not_found` / `no_script`), 404 `script_not_found` for a missing file, 422 `script_path_refused`/`script_read_refused` for containment refusals, 501 `unsupported_platform` on Windows where the nolink chokepoint is unavailable; the blocking resolve+read runs off-loop in `discovery_executor`)
**JSON body shape**: every route above that reads a JSON body requires a top-level
object, and answers a scalar, array, or `null` the same way it already answers a body
it cannot parse at all. `PATCH /api/crons/{id}` and `DELETE /api/lessons` refuse with
400 `code: "invalid_json"`; `POST /api/crons/{id}/enable` and `POST /api/crons/{id}/ack`
fall back to their defaults, as they do for an unreadable body. Neither reads a field
off a non-object, which has no `.get` and no string key lookup and would surface as a
500.

**Cron Folders**: GET `/api/cron-folders` (list), POST `/api/cron-folders` (create `{name}` → `{id, name, order}`), PATCH `/api/cron-folders/{folder_id}` (rename `{name}`), DELETE `/api/cron-folders/{folder_id}` (delete folder, clears `folder_id` on assigned jobs)
**Messaging**: POST `/api/send-message` (send to Slack DM + dashboard notification; body: `{text, title?, blocks?}`; when `blocks` provided, sends Block Kit message via `post_blocks()` with `text` as fallback; used by `send_message` MCP tool in `kirocrew-core`)
**AutoNudge** (`autonudge.py`, `autonudge_authz.py`, `dashboard/handlers/autonudge.py`): GET `/api/autonudge` (list loops), GET `/api/autonudge/slot/{slot_key}` (loop for a slot), POST `/api/autonudge` (start/replace a loop), PATCH `/api/autonudge/{loop_id}` (update), DELETE `/api/autonudge/{loop_id}` (stop). Loop-by-id resolution — the DELETE handler's pre-remove audit capture and the `PATCH`-path channel-banner refusal — shares ONE accessor, `svc.get_by_id(loop_id)` (returns the live loop or `None`), alongside `get_by_slot`/`list_all`, rather than two inline id-scans. Loop creation goes through the single chokepoint `authorize_and_add_nudge(svc, state, slot_key, message, …, source)` — a transport-agnostic security module at `autonudge_authz.py` (NOT the HTTP handler file; `state` is a narrow structural Protocol, and `dashboard/handlers/autonudge.py` is a thin HTTP mapping that re-exports it) — shared by the REST handler AND the workflow `ctx.nudge` bridge, so both enforce identical authorization before `svc.add`: dashboard `slot_key in state._slots`; Slack session routable; **Discord deny-by-default** (transport up, DM session only, user in `allowed_user_ids`, and `slot_key` == the user's *current* session key — blocks spoofing another `discord:` session); 8000-char message limit; sensitive `stop_sentinel_path` refused. **Load-time sentinel repair (`repair_sentinel_path`)**: the kill-switch path is resolved once at arm time and persisted verbatim, so the loop store can outlive the one-time `~/.kiro/crew`→`~/.kiro/crew` data-home migration and `start()` would re-arm a loop whose sentinel points at a directory that no longer exists — `_timer` only tests `Path(stop_sentinel_path).exists()`, so that loop's kill switch is **dead** (only `max_cycles` or an explicit remove can stop it). `_load()` therefore re-homes any legacy-rooted path onto the resolved current home and re-applies the arm-time sensitivity refusal (a persisted path that is sensitive *now* is dropped to `""` rather than stat'ed on a timer); a repair sets `_store_dirty` so `start()` flushes it once via an executor-offloaded write instead of re-deriving it every boot. The check deliberately does NOT require the path to live under the data home — an absolute `workspaces.<name>.dir` is a legitimate configuration, and filtering on containment would clear working kill switches. **`PATCH /api/autonudge/{loop_id}` applies the SAME message redaction as the arm chokepoint** (`redact_exfiltration_urls` + `redact_credentials`) — the field it mutates is the one that gets persisted and re-injected/posted on every fire, so redacting only on arm would make update a trivial bypass — and it SEL-audits `denied` outcomes (non-string/oversized message, non-integer `idle_secs`/`max_cycles`, non-boolean `active` — `bool("false")` is `True`, so a string would turn a pause into a resume — unknown loop), not just `success`. Both the redaction and the audit live in the transport-agnostic `authorize_and_update_nudge` chokepoint beside the arm one, not in the HTTP handler, so no future non-HTTP caller can bypass them; it too is **audit-or-deny** (critical `invoked` event before the mutation, 503 if unwritable). **Update/fire write serialization** — one protocol, two entry points: every writer must snapshot the store *while holding* `_lock` and then await an executor-offloaded `_write_state` (fsync must never run on the loop). `_update_locked` and `_add_locked` do that inline because they already hold the lock for their mutation; the post-fire bookkeeping, which holds no lock, calls the `_persist_locked` helper to acquire it first. A writer that snapshotted and *then* released could otherwise land a stale payload over newer `cycle_count`/`active` state and resurrect it after a restart, so `update()` is shielded like `add()` to keep the lock held across its own offloaded write. **One timer-cancellation policy (`_cancel_timer`)**: two conditions make a cancel wrong rather than redundant, and both live in that one method so no caller has to remember either — the currently running timer task (a self-re-arm from inside `_timer`) is about to return on its own, and a task whose event loop has already CLOSED must be dropped rather than cancelled. `Task.cancel` schedules through `loop.call_soon`, so cancelling a task on a closed loop raises `RuntimeError: Event loop is closed`, which escaped `remove`/`remove_sync` and made the dashboard handler above it answer **500**. The service is a process-global singleton (`_INSTANCE`, published by `start()`), so its `_timers` outlive the loop that created them whenever one loop replaces another: the gateway's own shutdown, and — the way this surfaced — a test driving a handler after an earlier test's loop closed. The condition is asked positively (`get_loop().is_closed()`) rather than by catching the `RuntimeError`, because a closed loop is the one state where cancelling is a no-op by definition and catching would also swallow a genuine scheduling fault. `stop()` routes through it for the same reason rather than looping over bare `t.cancel()` — shutdown is the likeliest moment for a timer's loop to be closing already. The closed-loop question is asked BEFORE the current-task one because it needs no running loop of its own: `stop()` is reached from synchronous callers (gateway shutdown, test teardown) where `asyncio.current_task()` raises `RuntimeError: no running event loop` rather than answering None, which `_current_task_or_none` turns into the None the caller means. The test-side floor for the singleton is `_restore_autonudge_singleton` (see [testing-conventions](../common/testing-conventions.md)). **Mid-fire updates never cancel the timer** (`_firing` tracks the fire window): channel loops run the unattended turn inline in `_on_fire`, so cancelling would kill the turn — the re-arm is deferred to the running timer, and the undelivered path refuses to re-arm a loop that was deactivated mid-fire so an explicit pause is not silently resumed. The window spans delivery, the bookkeeping persist, and the re-arm decision (`_run_fire_cycle`), and `notify_turn_complete` observes it too: a dashboard turn that completes mid-window records the re-arm in `_rearm_pending` and it is applied when the window closes, because arming immediately would cancel the firing task while it is writing the delivered cycle. **`monitor_update` MCP tool**: revises the loop bound to the calling session in place (message / idle_secs / max_cycles) so a stale instruction can be corrected without tearing the loop down and losing its `cycle_count`. It resolves the loop id from the caller's own binding key rather than accepting a caller-supplied id, so a cross-session rewrite is unrepresentable. Since issue #755 it is STATELESS (see `session.md` → "Stateless session-directive tools"): the tool validates its patch and returns a directive, and the session-aware consumer resolves the loop from ITS OWN binding key via `svc.get_by_slot(binding)` (`session_directive_apply.py`), never via `GET /api/autonudge/slot/{key}`. The tool still calls `_resolve_session_key_strict()` (no PID walk), but only as a context guard that rejects un-appliable sessions (cron/hook/subagent) — not to bind the effect — for the same reason `monitor_start`/`autonudge_stop` do. **`monitor_start` cycle cap**: omitting `max_cycles` now yields a bounded default (`_MONITOR_DEFAULT_MAX_CYCLES = 24`) instead of unlimited, because an unbounded loop stops only when the model volunteers `autonudge_stop` and observed loop stores show that is unreliable (real babysit loops ran to 24/24 and 20/20 delivered cycles, stopping only on the cap); explicit `0` still means unlimited. **Wall-clock budget (`max_runtime_secs`)**: a second, opt-in terminal bound (0 = unlimited, ceiling `MAX_RUNTIME_SECS_CEILING` = 604800s/7 days — enforced at BOTH authz chokepoints, not just the MCP schemas, so REST/workflow callers cannot exceed it, and the REST handler refuses non-integral floats rather than truncating them) alongside the cycle cap, because a cycle cap alone cannot bound COST — a loop with slow turns or a long idle gap can run for days within its cycle budget. Measured from the persisted `created_ts` (not arm time), so a gateway restart re-arms the loop but never resets its clock; a store entry with no `created_ts` never trips (no anchor — guessing one could kill a healthy loop on its first post-upgrade cycle). Enforced in `_timer` via the shared `runtime_budget_exceeded` predicate, checked AFTER the cycle cap (both exhausted → the cap wins) and BEFORE the fire so a spent budget never buys one more unattended turn, **and re-checked immediately post-delivery** in `_run_fire_cycle` — a budget that expired during a slow turn deactivates the loop the moment that turn ends instead of arming another full idle cycle. The service never cancels an IN-FLIGHT turn (the mid-fire contracts above forbid it — cancelling kills channel turns), so the declared bound can overshoot by at most one turn, itself capped by the transport's per-turn ceiling (`constants.CHAT_TURN_TIMEOUT`); terminal treatment matches the cap — deactivate (inspectable/restartable, not removed) + `expired` event, and the notifier (`_notify_nudge_expired`) words the notification per bound using the same predicate. **A terminal subject outranks every bound in that wording, and an OWED terminal turn counts as one**: a channel-bound loop does not settle on observation (it learns its watch finished from a delivered turn), so the probe records the owed turn in `monitor.terminal_pending` and leaves the loop active with no `outcome` and no `monitor_terminal` reason — and if that final turn is refused and the retry finds a bound spent, `_timer` deactivates on the bound before the settlement that would promote the debt ever runs. The notifier therefore derives `terminal` from `stopped_reason == monitor_terminal` **or** an outstanding `terminal_pending`, and takes the merged-vs-closed-unmerged distinction from the settled `outcome` falling back to the debt (both use `success`/`blocked`), so a watch whose subject merged is never announced with the signal of one that ran out of cycles. This is WORDING only: the bound that stopped the loop keeps its own `stopped_reason`, so the spent cap stays observable and the `monitor_update` revival affordance keyed on `cycle_cap` is unchanged. Whether an owed terminal turn should instead outrank the cap in `_timer` and be DELIVERED is a separate, open question (issue #8060), not settled here. **Approval-stall stop (`approval_stalled`)**: a third terminal condition, evaluated in `_timer` LAST of the three so a loop also out of cycles or budget still reports the bound it historically would have. It is **reactive by construction** — it fires only on recorded evidence that a cycle's tool approval went unanswered, never on a reading of whether an auto-approve grant is in force. The evidence is written by `notify_approval_stalled(slot_key)`, a sync slot-keyed hook alongside `notify_turn_complete`/`notify_user_input`, called from the approval-timeout branch of ALL THREE paths a nudge cycle can stall in, matching the three fire paths `_run_fire_cycle` dispatches: `chat_runner`'s per-slot wait (dashboard-slot loops), `_interactive_approval`'s raced wait in the Slack gateway (Slack loops, whose turns are approved there rather than through the dashboard runner -- the fire path threads the loop's own `slot_key` in as `nudge_key`, and the guard keeps cron/taskrunner/subagent consumers of that same callback unaffected), and `DiscordApprovalDecider.__call__`'s button wait (Discord loops, keyed on the decider's own `session_key`). Only the timeout branch records; an explicit human rejection is a decision, not evidence that nobody is present. The hook sets the persisted `approval_stalled` flag and returns rather than stopping inline, because stopping there would cancel a possibly-mid-fire timer -- the one thing the fire-window contracts forbid -- and race the turn that produced the evidence. The predictive alternative (test the grant before dispatching) was rejected: a loop whose cycles only touch auto-approved tools needs no grant, so it would be stopped for nothing, and such a loop can never reach an interactive approval wait, which is what makes the reactive test free of that false positive rather than merely tuned against it. Cost is bounded at the one cycle already in flight. `monitor_update` deliberately has **no revival affordance** for this reason (raising a bound does not restore an authorization), but its paused-loop denial names the real remedy — re-enable auto-approve, then `monitor_start` — and every revival clears the flag so a re-granted loop is not stopped by spent evidence on its first wake. The clear is keyed on an actual revival (`not was_active`), not on any `active=True`: a still-active loop also receives one from an ordinary settings save, and treating that as an answer would erase evidence recorded moments earlier and let one more doomed cycle fire. Before this existed, a loop whose grant lapsed kept waking, dispatching, being declined and spending its cap on cycles that were never able to act, with `cycle_count` making a capped-out run indistinguishable from a finished one; merged reporting (an operator notice on grant expiry) explained that after the fact but never stopped it, and being edge-triggered on the expiry event could not cover a loop that started after the grant was already gone. **`stopped_reason`** (persisted on the loop: `""`/`"manual"`/`"autonudge_stop"`/`"cycle_cap"`/`"runtime_budget"`/`"approval_stalled"`) records WHY the last deactivation happened — `_timer`'s terminal bounds tag themselves, any other `update(active=False)` defaults to `"manual"`, and every revival clears it. The `autonudge_stop` session-directive applier persists `"autonudge_stop"` only for `research-*` loops, whose watchdog consumes that source-owned evidence; ordinary dashboard and channel loops retain the historical remove-on-stop behavior instead of leaving a paused record with no consumer. A Research Lab stop may replace an earlier manual pause from an app-disable race, while cycle/runtime-bound writers still cannot overwrite a deactivation that landed first. The caller’s free-form explanation is deliberately not persisted because the watchdog needs only the deterministic source and model-authored text may contain sensitive content. The Research Lab record is restart-durable until consumed. Its watchdog checks the tombstone before trust-expiry handling or reviving loops suspended by an app disable: on the first watchdog poll after any in-flight worker turn exits, it prefers the tombstone over `worker_done.json`, preserves the existing verified-finding-first verdict, requires at least one readable finding before reporting STOPPED, then removes the consumed loop record. `worker_done.json` remains the conservative fallback at the normal idle deadline when no tombstone exists, while mere loop absence remains untrusted because unreachable-session cleanup also removes loops. Revival via `monitor_update` keys on a paused record's source, NOT on elapsed-time inference: wall-clock keeps growing after a manual pause, so "budget looks spent" cannot distinguish a pause from an expiry, and a budget raise must never resume a loop the user paused (the cycle-count heuristic survives only as a legacy fallback for stores written before the field). A budget-stopped loop revives when the budget is raised above its elapsed age (or 0), matching the cap-raise affordance; the paused-loop denial names the bound that actually stopped the loop. **Arm-failure reporting**: the stateless applier (`session_directive_apply.py::_monitor_start`) returns an explicit human-readable outcome for the model — a `Monitor loop … started …` confirmation, `Monitor loop NOT armed: auto-nudge is disabled on this host.`, or `Failed to start monitor loop: {error}` — rather than the former undifferentiated string that was indistinguishable from the transient MCP reconnects agents are instructed to retry through. The former loopback-token probe (`_local_user_token_with_reason`) and its `_monitor_arm_failure` renderer were removed with the HTTP round-trip. Auditing is preserved in intent: the applier emits a **SEL event for every outcome** tagged `source="mcp-directive"` (`success` / `denied` / `error`), and the `POST /api/autonudge` REST handler and the workflow `ctx.nudge` bridge keep their own `source="dashboard"` / `source="workflow"` audits through the shared `authorize_and_add_nudge` chokepoint. **AUDIT-OR-DENY availability policy (deliberate contract change)**: a CRITICAL `invoked` event is written (synchronously on a worker thread, awaited) *before* `svc.add` arms the loop; if that write fails, the arm is **denied** — `POST /api/autonudge` returns **503** ("audit log unavailable — nudge loop not armed") and the workflow path reports "NOT armed" into the run. Previously SEL unavailability could not prevent arming a loop; now a wedged SEL trades availability for a guaranteed audit trail (no loop may ever exist unaudited), matching the repo's fail-closed security posture. The terminal `success` event is best-effort — an armed loop is already covered by the `invoked` record. **`add()` ordering semantics (observable by all callers)**: `add()` awaits an executor-offloaded persist and arms the timer under a shielded task, so the loop's FIRST timer cycle may complete before `add()` returns — callers must not assume post-`add()` state predates the first fire. A caller cancelled mid-`add()` gets shielded "mutation may have already landed" semantics: it receives `CancelledError`, but the arm+persist completes (writes stay strictly serialized under the service lock). `binding_key_for(session_key)` (in `autonudge.py`) is the single source of truth for "nudge-able" (`dashboard:chat-N-TS`→`chat-N-TS`; `slack:`/`discord:`/`webex:` pass through; `cron:`/`hook:`/`subagent:`/empty→`None`), shared with the `monitor_start` directive applier (via `_binding()` in `session_directive_apply.py`). **Workflow `ctx.nudge`**: dynamic-workflow scripts arm a loop on their originating session — `WorkflowService` threads the launching `session_key` (`start`→`run`→`_exec_validated`→`_RunContext`) and wires a `nudge` port to a gateway-injected `nudge_authorizer` that maps the (caller-influenced) key via `binding_key_for` and calls `authorize_and_add_nudge(…, source="workflow")`; the workflows package never touches `AutoNudgeService`/`state` directly. Best-effort: an unwired authorizer, a non-nudge-able session, an authz rejection, or `svc.add`/no-loop failure degrades to a logged no-op (the arm runs as a `create_task` whose ref is held in `WorkflowService._nudge_tasks` to avoid mid-await GC) — a monitoring convenience never crashes a completed run. This fixes the prior `RuntimeError("ctx.nudge is not available for this run (no nudge port wired)")`. The workflow authoring prompt (`_AUTHOR_SYSTEM` in `workflows/service.py`) advertises ONLY ctx primitives production actually wires; a parity contract test (`test_workflows_nudge_wiring.py::test_author_prompt_advertises_only_wired_primitives`) fails if a primitive is added to the prompt without a wired port (or vice versa), so the advertised-but-unwired crash class cannot silently reappear. The same class is closed at the ENFORCEMENT layer too: the runner calls `validate.check_ctx_surface(source, CORE_CTX_SURFACE | <its wired port names>)` at the exec boundary, so a hand-written or rerun script referencing a primitive the executing host did not wire fails validation with a clear per-line error (`run_failed`, `where="validate"`) instead of starting and dying mid-run — host-aware by construction, so test/companion runners that wire additional ports keep their full surface.
Research Lab worker slot keys have the canonical shape `research-` plus an
eight-character lowercase hexadecimal campaign id. Creation and recognition
share `apps/builtins/auto_research/session_keys.py`. A matching name alone is
not ownership evidence: only a canonical slot carrying the persisted
`auto-research` app provenance retains a stop tombstone. Prefix lookalikes such
as `research-notes`, and user-created canonical lookalikes such as
`research-deadbeef`, are ordinary dashboard slots and retain remove-on-stop
behavior.

**Structured monitor substrate** (`monitoring/models.py`,
`monitoring/decision.py`): `NudgeLoop.monitor` is an optional, versioned
controller record for probe-first monitors. Absence is the durable compatibility
marker for a legacy prompt loop, and serialization omits the absent field so an
unrelated save does not eagerly migrate old records. A prompt-driven zero-token
gate may also carry probe state, but it remains a legacy loop (`gate=true`) on
the AutoNudge control surfaces; only controller records (`gate=false`) use the
structured monitor APIs and recovery path. The record owns target and objective
identity and configuration generation, canonical observation and wake
fingerprints, typed in-flight delivery and completion-evidence deadline,
provider-error streak, completed agent-turn and token totals, probe deadline, and
terminal outcome. Load reconstructs the typed record; a terminal current-version
record with a contradictory active loop is deactivated. AutoNudge dashboard
responses recursively pass every string in the structured monitor mapping through
`redact_via_context`, including provider-controlled observation keys and values,
before they leave the backend. Legacy loop fields retain their existing response
shape. An unsupported monitor version is marked blocked in its compatibility view
and retained for inspection rather than executed under an older policy; its raw
future payload and outer active intent survive an unrelated store rewrite unchanged.
A generic legacy Save with `active=true` likewise leaves that future outer intent untouched. Its inert
compatibility view uses validated current identity values when
present and placeholders otherwise, so a future schema may rename those fields
without making an older reader delete the raw record. Its compatibility view is
inert under the older runtime without rewriting the outer active intent, allowing
a later compatible gateway to resume it. A malformed current-version
monitor mapping is quarantined inactive with `invalid_monitor_record`; its exact
strict-JSON monitor payload remains durable across unrelated store rewrites for
inspection. A non-mapping payload is skipped because it has no preservable monitor
identity. Structured cadence is typed state with a 300-second default;
the legacy loop default remains 60 seconds. This substrate has no delivery
dispatcher: loading, generic updating, arming, or a pre-existing timer all fail
closed without a model turn until the probe controller is wired. It does expose
a dormant completed-turn controller seam. An actionable fingerprint is persisted
in-flight before dispatch; unavailable delivery clears that claim without spend,
while busy delivery retries the claim within its runtime bound; a
correlated completion charges one agent turn exactly once, adds only reported
non-negative token counts, and records token usage as unknown when authoritative
counts are unavailable. Duplicate, removed, replaced, legacy, and mismatched
callbacks are no-ops. Recovery resumes an accepted in-flight wake toward its
persisted completion-evidence deadline and a BUSY claim toward its persisted retry
deadline. A user or budget stop that races an accepted action keeps or re-arms that
finite evidence deadline; completion still charges exactly once, while expiry clears
only the stale correlation and preserves the terminal outcome. A user stop writes its
terminal replacement snapshot before mutating the live record or cancelling its timer,
so a failed disk write leaves memory, disk, and the active schedule aligned instead of
allowing a later restart to resurrect work.
A legacy claim with neither typed delivery nor an evidence deadline
deactivates with `completion_evidence_unavailable` while retaining the
acknowledged fingerprint, so restart cannot duplicate the wake. Completion stops
on the first exhausted runtime, turn, or token bound (in that precedence), and
the completed-turn bound is validated against the universal eight-turn ceiling
when constructed or loaded. Approval-stall completion is terminal and budget exhaustion takes
precedence when both apply.
Claim, budget-stop, completion, and pre-turn dispatch-failure mutations are
applied to a staged copy; the replacement snapshot is persisted before the live
record or timer changes. A failed write therefore leaves runtime state aligned
with the restart snapshot instead of suppressing or releasing a wake only in
memory.

Dashboard structured actions receive a runtime-only completion hook and report
only from `_run_chat`'s `EVENT_COMPLETE` branch when its reason is safe completion
evidence. ACP-synthesized terminal reasons are excluded from accounting. The
stale-stream compatibility path reuses `end_turn` for a synthetic completion, so
that reason is also excluded until ACP events carry provenance that distinguishes
it from a provider result. That correlated completion
is persisted before cancellable token-analytics I/O, so slot cleanup cannot lose
provider completion evidence after the event has arrived. Legacy dashboard nudge
scheduling still returns immediately and still rearms from the existing
`notify_turn_complete` `finally`; the completion hook neither replaces nor moves
that lifecycle call. The pure `decide_monitor` policy performs no I/O. It
checks the non-zero runtime/turn/token/provider-error budgets first, classifies provider errors
without a model turn, suppresses an unchanged observation, and permits
`wake_actionable` only when the actionable fingerprint differs from the last
acknowledged wake fingerprint. The defaults are 14,400 seconds, eight completed
agent turns, 250,000 aggregate input/output tokens, and three cumulative
provider errors. A successful probe resets retry backoff but does not refund the
finite provider-error budget. This substrate does not itself schedule live provider probes or
expose a new MCP tool; those are later RFC implementation slices.

**GitHub pull-request shadow probe** (`monitoring/github_pull_request.py`,
`monitoring/shadow.py`): the first typed provider adapter accepts only exact HTTPS
pull-request URLs on public `github.com` (normalizing `www.github.com`); arbitrary
hosts, enterprise instances, repository URLs, path suffixes, query/fragment identity,
and non-positive pull-request numbers are refused before provider execution. The
adapter resolves and invokes the shared hardened `github_runner` boundary with
`GH_HOST` pinned to `github.com`. It reads lifecycle, review, and mergeability fields
first. For an open pull request it reads `statusCheckRollup` in a separate request
paired with `headRefOid`, then walks GraphQL review threads in pages of 100, stopping
after at most ten pages. A missing Checks permission therefore cannot erase readable
primary facts, and a rollup for a different head is incomplete instead of being
combined with the primary response. A null rollup is a complete empty check set.
Merged and closed primary lifecycle states are terminal without either supplemental
request. An incomplete, malformed-node, repeated/missing-cursor, or capped thread
traversal is a pending fact and can never produce review-ready success. Outdated
unresolved threads are ignored because they no longer block the current diff.
When GraphQL returns errors alongside usable thread nodes, the traversal remains
incomplete but folds those nodes first, so an observed unresolved thread still wins
as actionable evidence. If the supplemental review-thread request itself fails, the
adapter retains primary lifecycle, check, review, and mergeability facts and unresolved
threads observed on earlier pages while marking thread evidence incomplete. A known
blocker therefore remains actionable. Without a known blocker, incomplete checks or
threads remain pending. The supplemental failure is carried separately as a typed
provider error, counted against the bounded provider-error streak, and retried without
discarding the readable primary facts.

The durable observation is stable canonical JSON containing only normalized target
identity, head revision, lifecycle/draft state, sorted check identities by outcome,
normalized review/blocking state, unresolved-thread count/completeness, and
mergeability. Repeated check identities retain their multiplicity because display
labels cannot prove that provider rows describe the same logical job. Provider
ordering, timestamps, URLs, request/cursor ids, titles,
bodies, comments, and logs do not enter it. Provider-controlled check labels pass
through credential redaction, control-character removal, unconditional URL removal,
and fixed identity/count bounds before persistence. Overflow marks check evidence
incomplete rather than producing success. The
GitHub check-run and legacy status-context namespaces remain distinct. A workflow
name plus display name is not a stable logical job identity: independent jobs may
share both, and the rollup does not expose a stable workflow-file identity. Check runs
therefore remain independent so a newer success cannot hide a known failure from a
different workflow definition. Workflowless rows remain independent. Duplicate legacy
status contexts are folded by blocker severity, so a same-named success cannot hide a
failure. `STALE` check-run conclusions are terminal non-blockers. GraphQL owner,
and cursor variables use raw string fields; only the pull-request number uses typed
conversion.
SHA-256 fingerprint covers the compact sorted serialization; a collection reorder or
volatile provider value keeps it stable, while a new head changes it. Only a non-empty
current head may set the changed-head fact. A changed head does not wake while the
observation is pending. An otherwise-green changed head records one
`wake_actionable`; after that head is persisted and observed unchanged, the same green
state reaches terminal success rather than being suppressed as a duplicate.

Classification is conservative: merged is terminal success
(`pull_request_merged`), closed-unmerged is terminal blocked, draft (which takes
precedence over check failures), incomplete/pending or
unknown checks, incomplete review-thread evidence, and unknown mergeability remain
pending. Mergeability succeeds only for the explicit settled `CLEAN`, `HAS_HOOKS`,
and `UNSTABLE` states, so empty or future provider values fail closed as pending;
failed checks, requested changes, unresolved threads, conflicts, a behind head, and
concrete branch-protection failures are actionable. GitHub's generic `BLOCKED`
merge-state is pending because it also represents an otherwise healthy pull request
waiting for required checks or reviews; the specific check and review facts determine
whether the observation is actionable. A requested-changes decision or an unresolved
thread already observed remains actionable even when the unseen review-thread tail is
incomplete; incomplete evidence can prevent success but cannot erase a known blocker.
Rate limits and transport/provider
failures are retryable, while authentication, authorization, not-found, and local
`gh` setup/trust failures on the load-bearing primary request are terminal categories.
HTTP status is classified before repository text, while GitHub's 403 rate-limit forms
remain retryable. Resource-pressure spawn errors are retryable rather than
misclassified as setup. Supplemental errors of any category use the bounded retry
streak because the primary target remains readable. The shadow runner persists only
the canonical observation, decision, next-probe time, and aggregate probe/error
metrics. It checks hard budgets before provider execution, and terminal decisions
persist outcome, reason, stop time, and a cleared next-probe deadline. Persistence is
the commit point: a failed write leaves the live state
unchanged so the same observation remains eligible for retry. It has no action
dispatcher, never sets a wake fingerprint or in-flight claim, and raises before
probing or persisting when `wake_delivery` is requested.
This slice exposes no MCP control tool and spends no model turns.

Every persisted monitor mapping must encode as strict JSON; nested non-finite numbers
and other values accepted only by Python's permissive encoder invalidate the record.
Timestamp-like integers too large for finite float arithmetic are likewise quarantined
with a zeroed compatibility timestamp while their exact raw monitor payload is retained.

A reasonless inactive update is idempotent for a Research Lab stop tombstone:
it preserves the source-owned `autonudge_stop` reason until the watchdog
consumes it. An API retry or unrelated patch therefore cannot downgrade a
deliberate worker stop into a revivable manual pause. The source-owned stop can
still replace an earlier manual app-disable pause.

Fresh Research Lab starts/resumes remove prior-run tombstone and marker evidence before publishing RUNNING. Tombstone removal precedes the potentially unbounded off-thread marker cleanup, so the watchdog can never observe a half-prepared new run. Direct `_launch_loop` callers preserve the same cleanup order before any service/state availability early-return.

The watchdog also establishes a run observation boundary before consuming a
Research Lab stop tombstone. The first watchdog poll for a new `started_at`
seeds the run baseline and defers settlement without re-arming the inactive
loop. A current-run tombstone remains prompt: when the watchdog observed the
run while its worker turn was still active, the first safe poll after that turn
exits can consume it immediately.
If that worker turn publishes a final cycle before it exits, the watchdog first
persists the advanced cycle count, emits the finding event, and advances recursive
exploration through the same bookkeeping path as an ordinary poll. Tombstone
settlement then classifies the terminal state, so its fast path cannot omit the
last finding or leave `total_cycles` stale.
Terminal classification and its SQLite/sidecar status persistence both run in
worker threads; neither the ordinary idle-deadline path nor the tombstone fast
path can block the gateway event loop while settling a campaign. Once terminal
classification is known, the watchdog first persists the captured loop as
inactive, then commits terminal status and durably removes that exact loop. A
failed removal-store write is retried against an idempotent pending-removal
postcondition. If the retry budget is exhausted, the original error propagates,
but the pre-commit inactive state prevents restart from re-arming the terminal
campaign. The transition remains cancellation-safe: shutdown waits for durable
loop cleanup before the watchdog exits.
Settlement and user actions serialize through a per-campaign transition lock.
The watchdog carries the `started_at` value of the run it observed and, after
acquiring that lock, proceeds only while SQLite still reports RUNNING with the
same value. If Resume has already advanced the generation, stale bookkeeping,
status/sidecar writes, loop cleanup, and SSE are all skipped. If settlement wins
the lock, its SQLite commit, sidecar write, and loop cleanup finish before Resume
publishes the later RUNNING sidecar, so an older terminal write cannot land last
and stop the replacement worker. Cleanup also captures the terminating loop's
stable id before persistence begins and removes only that id afterward, keeping
the worker identity invariant explicit even for callers outside the action path.
If status persistence fails before the terminal SQLite commit, cancellation
still propagates and the non-terminal loop remains available for a retry after
restart. SQLite commits before the status sidecar and audit write, so a failure
after that commit is distinguished by re-reading SQLite off the event loop and
matching both the observed `started_at` generation and intended terminal status;
the captured loop is then removed durably before the original error propagates,
preventing a terminal campaign from re-arming after restart.
**Lessons**: GET `/api/lessons`, POST `/api/lessons` (add), DELETE `/api/lessons` (remove by substring)
**Feedback** (`dashboard/handlers/feedback.py`): GET `/api/feedback/eligible?userId=` and POST `/api/feedback/submit` are a same-origin proxy in front of AWS Aperture's session-pulse survey (form `KiroCrew`/`SessionFeedback`/`1.0.1`). Aperture's browser-CORS allowlist is a fixed, known set of domains and Kiro Crew is self-hosted with no single canonical domain, so a direct browser→Aperture call is fundamentally unreachable for this product; the backend forwards both calls server-to-server, where CORS does not apply. `metadataList` order in the POST payload is load-bearing: Aperture's ingestion API validates it **positionally** against the form template's own registration order, not as an unordered set, so reordering the same keys 400s identically to a wrong key. Eligibility is dual-layer — the frontend's own 30-day `localStorage` cooldown is checked first (and short-circuits before this endpoint is ever called), then this endpoint asks Aperture's Prompt API for server-side per-user dedup — so either check being stricter wins. A submission failure (network, non-2xx) returns a coded, non-2xx JSON body rather than raising, and the frontend treats a failed submit as retryable rather than a false success.
**Tips (feature discovery)**: GET `/api/tips/next` (returns `{tip, glow}` — `tip` is `{id, feature, title, body, why, doc, doc_link, cta_prompt, action?}` or `null`; `doc` is the tip's DISMISSAL IDENTITY (doc-level dismiss/snooze key on it) while `doc_link` is a rendering-only "learn more" hint — split so a curated tip can link a catalog-owned doc without its dismissal suppressing the catalog's own tip for that doc (#3524); `glow` true only when the server-side cadence gate (`tips_cadence_hours`, default 6h) is open AND ≥1 eligible tip exists; an offered tip persists across polls until feedback; 204 when `tips_enabled=false` or the user opted out), GET `/api/tips/status` (returns `{enabled_config, opted_out, cadence_hours}` — read-only flags for the settings toggle plus the configured cadence, which the client uses to derive its polling gate as min(20min UI floor, cadence) so sub-20-minute cadence configs take effect), POST `/api/tips/feedback` (body `{id, action}`, action ∈ `shown|ack|dismiss|snooze|helpful|optout|optin`; `shown` is sent by the frontend when a tip is actually displayed — it starts the cadence gate and clears the offered slot WITHOUT dismissing, so a passively-viewed tip is not re-served every turn yet remains eligible for future re-selection; `ack`/`dismiss` permanently acknowledge, `snooze` re-eligible after `tips_snooze_hours` (48h default); any shown/ack/dismiss/snooze closes the cadence gate; non-object bodies and non-string/oversized fields → 400). Tip catalog is pre-generated at release time via `scripts/generate_tips_catalog.py` and bundled as `src/kiro_crew/data/tips_catalog.json`; doc selection is ALLOWLIST-based (`kiro_crew/tips_allowlist.py`, single source shared by the generator and the runtime scanner) — only user-facing feature docs are eligible, internal architecture/incident docs can never surface as tips. At runtime, `get_tips_cache` loads a hand-authored, action-first **curated tips source** (`src/kiro_crew/data/tips_curated.json`) as the PRIMARY candidate pool, then the bundled catalog, then a live `docs/*.md` scan (same allowlist) as fallbacks — curated covers shortcut/toggle features native to Kiro Crew (split view, warm pool, MCP gateway, subagent parallelism, zero-token crons, Dev Fleet, App Store, …) that have no doc page and so can never surface from the doc-scan. A curated tip MAY carry an optional `action` object `{kind:"route", label, route}`: `route` is validated server-side to be an internal dashboard path only (must start with a single `/`, no `//`, no scheme) by `_sanitize_tip_action`, threaded through `_sanitize_persisted_tip` and passed through `_redact_tips` unchanged (non-string fields are not redacted); `TipCard.tsx` renders it as a single accent deep-link button (via `useNavigate`, guarded by a client-side `tipActionRoute` re-check) that jumps to the exact settings tab/control — with `?highlight=` where a control anchor exists — and fires an `ack` feedback on click. Tips with no in-app destination omit `action` and render body-only. Personalized generation runs in the `_bg` session pinned to Haiku (`tips_model`, default `claude-haiku-4.5` via per-session `set_model`) at most once per 6h (`maybe_refresh`, task-handle deduped, catalog-only fallback), reads user memory (preferences/projects/recent history) for relevance. Selection uses an explicit exploration blend (`tips_explore_ratio`, default 0.2, clamped [0,1]): with that probability a tip is picked uniformly at random from all eligible catalog entries (context-independent general features); otherwise the weighted-random newer-biased pick applies. Output is strictly field/type/length-validated, `doc` and `doc_link` sanitized to http(s)/`.md` only, and redacted at point of serving. Parse failures log length only (never raw LLM output). Per-user state (acknowledged, snoozed, offered, `last_shown_ts`, opt-out) in `~/.kiro/crew/tips_state.json` (atomic writes). Implementation: `tips.py`; frontend `TipCard.tsx` renders a single-line ambient strip above the chat input during running turns, yielding to the queued-message stack / question card. Users can disable tips entirely via the "Feature Tips" toggle in Settings → Chat (`ChatPanel.tsx`), which calls the `optout`/`optin` feedback actions with an empty `id`; the toggle reads its state from `/api/tips/status` and renders disabled with a hint when `tips_enabled=false` at the config level.
**Prompts (CRUD)**: GET `/api/prompts` (list; user prompts from `~/.kiro/prompts/` and `<project>/.kiro/prompts/` plus read-only package SOPs), GET `/api/prompts/{name}?scope=global|local` (scoped read), POST `/api/prompts` (create; body `{name, content, scope}`), PUT/DELETE `/api/prompts/{name}?scope=` (update/delete; PUT body `{content, base_hash}`). Every mutation (POST/PUT/DELETE) is dashboard-user-only: the middleware's app-token grants are path-only (verb-blind), so the handlers refuse a non-empty — or absent — app claim with a SEL-audited coded 403 `app_token_forbidden` before parsing any body. Writes address their target by explicit `scope` plus file stem, never by list-order resolution, so a stem present in both user directories can never be written through the wrong one; package SOPs live outside both user directories, so editing one is unrepresentable rather than refused (a write naming one answers a coded 404 and leaves the package file untouched). Each write invalidates the 5s list cache so a created prompt appears immediately, and every outcome — including every refusal, since a refused write can be filesystem probing — is SEL-audited under the same `code` the response carries. The scoped read reports two transformations separately: `redacted` when credential/exfiltration filtering altered the copy, and `lossy` when the bytes are not valid UTF-8 and were decoded with replacements. Editing is refused for either, because both serve a copy that is not the file and saving it would write the transformation over intact bytes. The scoped read also returns `hash` (sha256 of the raw file bytes), and PUT requires it back as `base_hash`: the writer verifies it through `hooks.verified_replace_file_nolink`, a compare-and-swap that hashes the CURRENT bytes off the same opened descriptor whose replacement it stages — verification and replacement share one name resolution — and answers a coded 409 `content_conflict` when the file no longer matches, so an edit started before someone else’s save cannot silently discard their work. A concurrency change landing after verification (an external atomic save swapping the inode, or an in-place rewrite visible through mtime/size) is detected by pre-rename re-checks — resolved through the pinned directory descriptor where the platform supports it, by name elsewhere — and also answers 409: the newer file wins, never the stale edit. (A swap caught earlier, by the ancestor-link guard at staging setup, stays a 403: that predicate is a security refusal, not benign concurrency.) A PUT without a well-formed `base_hash` is a coded 400 — an edit that cannot name its base was seeded from something other than the file. A symlinked or junction entry is refused before anything follows it, with the same 403 `access_denied` whether the link's target exists or not: answering 404 for a dangling link and 403 for a live one would make the endpoint an existence oracle for arbitrary paths. Write safety: the prompt directory is refused outright when its leaf is a link or junction (`platform_compat.is_link_or_junction`); create and delete then walk `<base>/.kiro/prompts` component by component, opening each relative to its parent's descriptor and applying `O_NOFOLLOW` at the leaf only, so a swap after the check cannot redirect the operation while an ancestor link the user chose (a symlinked `~/.kiro`) is still followed — the same rule the leaf check already sets; update goes through `hooks.verified_replace_file_nolink` (sharing `hooks.safe_write_file_nolink`'s engine), which carries the source inode's access-control xattrs onto the replacement. Platforms without `openat`/`unlinkat`/`mkdirat` keep the by-name path and the narrower leaf check.
**Skills (CRUD)**: GET `/api/skills`, POST `/api/skills` (create), GET/PUT/DELETE `/api/skills/{name}` — `kiro-workspace/` entries are scoped to the requesting chat slot's project via `X-Session-Key` (the same scoping steering and `/api/agents` use); without a key the single project shared by every open slot is used, and with multiple distinct projects open the workspace scope fails closed rather than guessing (#2457). That scoping is read-only: because a `kiro-user/` or `kiro-workspace/` key resolves per-machine / per-session on read while the writers join it onto the core skills root, PUT/DELETE on such a key answers 405 (`Allow: GET`, `code: readonly_skill_prefix`) and a create whose sanitized name lands in either territory answers 400 (`code: reserved_skill_prefix`) rather than editing a file the reader was never shown. `GET /api/skills` also accepts `?agent=<name>`, which scopes the listing to that agent's own `skill://` mapping (the same globs `agent_skill_globs` resolves for the prompt-injection path) by keeping only skills whose `loaded_by_agents` includes it; an agent with no explicit mapping — empty globs — keeps the unfiltered listing, so an agent that never customized its skill set does not lose access just because a different, customized agent exists on the same install (#3348). When the agent filter is actually applied (non-empty globs), the response is the envelope `{"skills": [...], "agent_scoped": true, "agent": <name>}` rather than the bare array — a filtered list, especially an empty one, is otherwise byte-identical to the legacy shape, and the picker needs the flag to cue the scope ("Scoped to agent …") and to attribute an empty result to the mapping ("No skills mapped to …") rather than to the catalog (#6028); every unscoped path keeps the bare-array shape. The `$`-token expansion path (`resolve_dollar_skills`) is NOT agent-scoped, so a skill hidden from a scoped picker still expands if its `$leaf` is typed by hand
**MCP Servers**: GET `/api/mcp` (list with enabled/disabledTools state from `~/.kiro/settings/mcp.json`), GET `/api/mcp/active` (per-agent MCP servers — reads from agent config for non-kirocrew agents, global mcp.json for kirocrew), GET `/api/mcp/probe` (cached probe results, non-blocking, 10-min TTL, preserves enabled state), POST `/api/mcp/probe` (live probe all, merges enabled/disabledTools from global config), POST `/api/mcp/sync` (discover + add to both kirocrew.json AND global mcp.json + session reset), POST `/api/mcp/toggle` (enable/disable server in global mcp.json + sync tools/allowedTools to kirocrew.json), POST `/api/mcp/toggle-tool` (enable/disable specific tool via disabledTools in global mcp.json), POST `/api/mcp/toggle-all` (bulk enable/disable all servers in global mcp.json + sync to kirocrew.json)

**Prompt and package-skill read safety — the descriptor gate**: Three content reads on this surface that are not the scoped prompt read go through `hooks.safe_read_file_bytes_nolink` as well, rather than re-opening a validated name. They are the listing's per-entry description (`prompts._extract_sop_description`, run for every package SOP and every user prompt behind `GET /api/prompts`), the UNSCOPED `GET /api/prompts/{name}` — the branch with no `?scope=`, which resolves across the package SOP roots and both user scopes — and the `package/` branch of `GET /api/skills/{name}`. Each arrives holding a path something above it already judged, and a decision about a NAME does not describe the file a later open by that name returns. A HARDLINK breaks the equivalence with no race at all: it shares its target's inode, so `realpath` yields the alias's own innocent path, `is_symlink()` is False and `is_sensitive_path` sees an ordinary file sitting inside the directory it was found in, while the bytes belong to whatever it aliases. That is decisive for the prompt `local` scope, whose `.kiro/prompts` holds content the user CLONED: an alias of `~/.aws/credentials` planted there would have that file's first `#` comment line published as a prompt description and its whole body returned by the unscoped read — a file the agent's own read gate refuses outright. The skills path needs a weaker actor (write access to a capability-seam package skill root rather than to a checkout) but is the same defect. `st_nlink` is the only signal a second name for a protected inode leaves and it is readable only on a descriptor, so the gate opens FIRST with `O_NOFOLLOW`, then `fstat`s that one descriptor and refuses `st_nlink > 1`, a non-regular inode, and an `is_sensitive_path` target; the inode validated is the inode served. All three pass `within_root` as the **canonical path's own parent** rather than as an authorization, and the distinction is worth stating because it looks like the latter: unlike the scoped read — whose `?scope=` names one authorized directory — these three span several roots, so no authorizing root exists to pass. It is passed because it is the only thing that carries the guarantee onto **Windows**, where `O_NOFOLLOW` does not exist at all and the gate's `getattr` for it yields 0: there the open FOLLOWS a leaf swapped for a link after canonicalization, and only the fd-real-path check (`GetFinalPathNameByHandleW`) still sees that the inode opened is not the one resolved. A link the entry legitimately points at is already followed by `validate_file_path`, so its target's own directory IS the root and only a substitution landing after that resolution escapes it. The refusals are reported differently per surface, on purpose, and in every case as the outcome that surface already produced for a file it could not open — so a refusal is not distinguishable from I/O trouble and the endpoint is no oracle for whether a given path is protected. A refused DESCRIPTION yields an empty description and KEEPS the entry, because whether a prompt exists is its caller's decision and a library that dropped a file because its metadata was refused would hide a name the scoped read still serves; that also keeps an unreadable prompt (a bad mode, a transient error) listed with no description, exactly as the by-name read did. A refused unscoped prompt READ answers the same 500 `file not readable` an unopenable file already produced, and a refused package-skill read leaves the content unset, which is the existing 404. Every one of those refusals also writes a SEL `tool_invocation` line (`prompts._audit_unread`, best-effort so an audit write cannot fail the response), which is what keeps the identical HTTP answer from also being invisible to the operator: an entry that lists with no description is otherwise byte-identical to a prompt that simply has none, and a 404 is what an uninstalled skill name produces. The audit line records THAT bytes were withheld and never why — `blocked` for a name `validate_file_path` refused outright (the one knowable cause, since it precedes any open) and otherwise the surface's own outcome (`error`, or `too_large` for the skills read's `FileTooLargeError`) — because the gate judges and reads through ONE descriptor and returns a bare `None`, and re-`stat`ing the path to separate a refused inode from an ordinary read failure would be another by-name look at the input these reads exist to stop trusting. Recovering the cause honestly needs `safe_read_file_bytes_nolink` itself to return a reason, which is a contract change shared with its other consumers. The `package/` skill read runs under `asyncio.to_thread`, like the same handler's delete and update verbs: no caller-supplied cap applies to it, so it reads up to the gate's own 50 MB default off storage that can be network-backed, and this handler shares one event loop with every other session's turn. Bounds move with the reads. The unscoped prompt read's `MAX_PROMPT_BYTES` cap comes off a pre-read `stat` and onto the gate's own `max_bytes`, so the size that refuses is the size of the bytes actually read rather than of a separately-`stat`'d name; `FileTooLargeError` is not an `OSError`, so each site catches it explicitly — the prompt read to keep its coded 413, the skills read so the gate's default 50 MB ceiling cannot escape as an unaudited 500 where an unbounded `read_text` previously succeeded. The description read passes `allow_truncate` instead of a cap, because a description is frontmatter or a first heading, both at the head of the file, and raising there would turn one oversized file into a 500 for the whole listing.

**MCP Discovery** (multi-provider, config-first page's Add Server modal): GET `/api/mcp/discover?q=&provider=&limit=` — concurrent fan-out over registered providers (`official` = public MCP registry at registry.modelcontextprotocol.io via `mcp_providers/official.py`, always registered; `capability` = the edition's CapabilityManager CPP seam, registered only when `available()`); a query under 2 chars short-circuits to `{results: [], providers: [...]}` without provider calls (cheap availability probe); all provider-sourced strings pass `redact_credentials` + `redact_exfiltration_urls`; results carry `installed` cross-referenced against KiroCrew scope. GET `/api/mcp/discover/detail?provider=&id=` — full description plus `install_plan` preview (the exact mcp.json spec install would write: npm→`npx -y pkg@ver`, pypi→`uvx`, oci→`docker run -i --rm`, remotes→`{url}`; priority npm>pypi>oci>remote) and `required_env` (env vars installed as `""` placeholders the user must fill). POST `/api/mcp/discover/install` `{provider, id}` — official: translates the registry entry to a spec (runtime args precede the package target for npx/uvx and are NEVER emitted for oci — publisher-controlled docker flags like `--privileged` would dissolve the container boundary, so the sandbox is pinned to `run -i --rm <image> <package args>`) and writes through the locked `_set_kirocrew_entry` path (409 on name collision with a different spec, name gated by `_is_valid_mcp_name`); EVERY fresh official install lands `disabled` — enabling from the servers table (after reviewing the written spec and filling any env vars) is the informed-consent step, and an identical-spec reinstall never flips the user's enabled state; capability: delegates to `CapabilityManager.install_mcp` then syncs agent config; both SEL-audited as `mcp_discover_install`. The browse modal offers Install only from the detail pane, and the button stays disabled until the detail (with its install-plan preview) has loaded — pending or failed fetches never expose an active Install (list rows are status-only, and the Enter shortcut applies the same readiness predicate).
**Agent Config**: GET/PUT `/api/agent/config` (read/write `~/.kiro/agents/kirocrew.json`, PUT auto-restarts sessions; the PUT — like every mutating verb in the agents handler module (agent CRUD, default-agent write, capability install/uninstall/sync, agent-detail DELETE/PATCH) — is **owner-only** via `is_owner_dashboard_request`, refusing app tokens and non-owner subjects with a SEL-audited 403 before the body is parsed: `~/.kiro/agents/` is machine-global, so a write there installs tool grants and MCP server commands every later session executes; `test_agents_endpoints_owner_auth.py` enumerates the module's mutating routes so the gated set cannot drift)
**Crew model pins**: POST `/api/agents` and PUT `/api/agents/{name}` validate a
supplied `model` before persisting it. The shared role-model validator rejects a
pin the active session's advertised model set proves unusable; an empty or
unknown set still fails open because entitlement is unknowable. The registry
adds the offline, positive case: a spelling it recognizes from another provider
but that kiro-cli does not serve is rejected with `code: "invalid_model"` and a
non-prescriptive mapping hint. Empty/`auto`, valid ACP ids, and unregistered ids
remain accepted. `kirocrew doctor` audits user- and project-scoped agent specs
through the hardened discovery reader and reports the same positive corrections;
an unreadable scan is reported as unchecked rather than clean.
**Crew avatars**: a crew's face is derived from its name unless the record's
`avatar` field (see `config.md`, *Per-crew avatar override*) pins one of two
tiers. Both are edited in the crew editor's avatar builder (`CrewAvatarBuilder.tsx`,
reached from the header face, the Overview hub face, and the Avatar row of the
Triggers pane) and both follow the editor's two-step Apply → Save: Apply commits
to the editor draft only, Save changes writes the record. The **ghost tier** is
pure config — POST `/api/agents` and PUT `/api/agents/{name}` accept
`avatar: {"kind":"ghost","traits":{…}}` (or `{}` to reset) and reject any other
non-empty shape with 400 `invalid_avatar`, mirroring `session_color`. The
**picture tier** adds two owner-only, SEL-audited routes and a staging protocol
whose invariant is that *no request other than a successful config save changes
what the roster serves*: POST `/api/agents/{name}/avatar` (multipart, read into
memory under a 1 MB cap → 413 `avatar_too_large`; format decided by magic bytes,
PNG/JPEG/WEBP only, client `Content-Type` ignored; a body whose container is not
closed — no PNG `IEND`, no JPEG `FFD9`, RIFF length ≠ body — is refused 400
`avatar_bad_format` so a truncated upload can never be promoted; malformed
multipart → 400 `invalid_multipart`; unknown crew → 404 `agent_not_found`) only
**stages** the bytes at `<data home>/run/avatars/<sha256(name)>.pending.<ext>`
(display names never touch a path) and returns a one-shot staging `token`. PUT
`/api/agents/{name}` with `avatar: {"kind":"image","promote":true,"token":…}` is
the single commit point: under the config lock the staged file is installed at a
**content-addressed** path (`<stem>.<16-hex digest>.<ext>`, so an install can
never overwrite the committed file), the record is saved as
`{"kind":"image","v":<mtime_ns>,"file":"<digest>.<ext>"}`, and only then are the
previous variants reaped; a save that raises rolls the fresh install back and
leaves the prior pin live. `promote` and `token` are wire-only and never
persisted. A PUT carrying a plain `{"kind":"image"}` keeps the committed picture
and discards any stale staging (an abandoned pick must not survive into a later
save); a `promote` whose token no longer matches the staged file fails the save
with 400 `avatar_file_missing` rather than silently keeping the old picture, and
so does a `promote` with nothing staged. Leaving the picture tier through the
same PUT (`{}` or a ghost override) reaps the stored files *after* the config
write succeeds, and DELETE `/api/agents/{name}` reaps live and staged files
inside the same lock. GET `/api/agents/{name}/avatar` serves **only** the file
the record's `file` pin selects and only while the record says `kind: image`
(a leftover file the config no longer selects is refused), with a content-hash
`ETag` + 304; the frontend appends `?v=` from the record so a replaced picture is
re-fetched without waiting out the browser cache, and falls back to the seeded
ghost when the image fails to load. All avatar filesystem work runs off the event
loop (`_drained_to_thread`). The image-tier create path is deliberately closed:
POST `/api/agents` with `{"kind":"image"}` is 400 `avatar_file_missing`, since a
crew that does not exist yet cannot have staged a picture.
**Chat**: POST `/api/chat` (SSE stream, or JSON with `?ws=1` — chunks via WebSocket), `/api/chat/slots` (CRUD, POST accepts optional `agent` field to set agent at creation; list responses include `source_links` extracted from slot messages with cached provider/number/state/CI metadata), resume from history, POST `/api/chat/slots/{slot}/generate-title`, POST `/api/chat/slots/{slot}/agent` (switch agent for slot), POST `/api/chat/slots/{slot}/fork` (fork session — copies visible messages into new slot, body: `{at_message_index?, prompt?}`, returns `{ok, key, title, messages, prompt}`, new slot has `forked_from` metadata), POST `/api/chat/slots/{slot}/edit-resend` (edit a user message and re-run; in-place truncation of `slot.messages`, body: `{index?, ts?, content}`), POST `/api/chat/slots/{slot}/rewind` (edit any past user message and re-run; fork-and-swap — truncates `slot.messages`, removes the slot's ACP session via `SessionManager.remove`, deletes orphaned kiro-cli session JSONL at `~/.kiro/sessions/cli/<id>.json[l]`, then runs the edited prompt against a fresh ACP session under the same slot key/title/folder. Mirrors kiro-cli `/rewind`. Body: `{at_message_index?, ts?, content}`), PATCH `/api/chat/slots/{slot}/mode` (switch session mode between `""` and `"orchestrator"` — `_VALID_MODES`; 404 missing slot, 400 invalid mode, 409 while the session is running)
**Agent-switch failure feedback**: every dashboard caller of `POST /api/chat/slots/{slot}/agent` — the chat agent picker, the split-view pane picker, and the global forward/backward agent-cycle shortcuts — surfaces a failed request instead of discarding it. The notice carries the server-provided message when the failure has one, and the shared unexpected-error copy otherwise; it makes no assumption about which failures the endpoint can produce. One shared chat-state notice serves every entry point, the always-mounted App shell renders it on every route, and it expires after six seconds. The picker closes on selection either way — its `onSelect` does not await the switch — so the notice, not the dropdown, is what tells the user the switch did not take.
**Context meter survives a reopen**: `context_usage` WS frames are turn-scoped, so a session opened in a tab that did not witness its turns had no usage to render and the bar sat at 0% until the next message. `GET /api/chat/slots/{slot}` therefore carries `{context_pct, context_stale, context_window_tokens?, context_used_tokens?}`, computed by `_context_snapshot_fields` in two tiers: a provider still resident in the session pool is authoritative, and a session whose ACP process is gone (idle timeout or a gateway restart) falls back to a stored snapshot flagged `context_stale`. **`context_pct` is the load-bearing field and the token counts are optional enrichment** — kiro-cli commonly reports `contextUsagePercentage` with no `usage_update`, so a resident session routinely knows it is 13% full while knowing neither token count; gating the reading on a known window no-ops the feature for the majority of sessions, and the frontend already supplies a model-derived window. Fields are OMITTED, never zeroed, when there is no reading at all — a 0% pct with no window is indistinguishable from a session that never had a turn, and both render an empty bar, so it is reported as absent rather than as a measurement. A **stale** reading additionally withholds `context_used_tokens`: no process measured that count, so shipping it would make any consumer render a never-measured figure as measured; the tooltip derives a `~` approximation from pct instead. A snapshot whose recorded model differs from the slot's current one is discarded, because its pct and counts are denominated in the old model's window. Every failure path degrades to `{}`: the fields ride on the response that carries the whole transcript, so a non-numeric provider reading is gated by `_finite_number` and any exception is swallowed rather than turning a display nicety into a 500 that blanks the conversation. `DashboardState.broadcast_context_usage(slot_key, payload)` is the SINGLE writer for both the frame and the snapshot — every producer (end-of-turn, both compaction paths, the auto-compact callback, and the model-switch/session reset) routes through it, it records a pct-only frame on its own, and an unchanged reading leaves the map clean so the next flush has nothing to write. A post-compaction frame legitimately records `pct: 0`: that is the new truth, not an absence. **All snapshot file IO is off the event loop**, which is what the `no-blocking-call-on-event-loop` rule requires and what `_persist_open_slots` already does for its own sidecar: `broadcast_context_usage` only mutates the in-memory map and sets a dirty flag, `_persist_context_snapshots` runs from `_flush_dirty_slots` (the periodic executor pass) plus once more on graceful shutdown so a restart — the case the reopen seed exists to serve — does not lose the last reading, and the first disk read reaches `ensure_context_snapshots_loaded` through `asyncio.to_thread` from the async slot-detail handler. Deferring the write puts the map on a thread boundary, so every access — event-loop writers, the flush executor, the shutdown thread, the handler's copy-out read (`context_snapshot_for`) — holds `_context_snapshots_lock`; the lock covers dict work and serialization only, never the file write, so a stalled disk cannot block the loop's writers. The loaded flag flips only after the disk merge is in the map, so a concurrent flush can never write a memory-only view over prior-process readings it has not merged; disk fills gaps and never overwrites a live entry. Any flush failure re-arms the dirty flag and is swallowed — the flush loop treats a raising callee as fatal, and the next 5s pass retries. Snapshots live in `<config_dir>/context_snapshots.json` (slot key → `{pct, model, window_tokens?, used_tokens?}`, `0o600`, pruned to the open-slot set on every write) and NOT in the session's metadata line: `ConversationLog.update_metadata` rewrites the whole transcript to edit its first line, so paying that per turn would scale a turn's I/O with transcript size while holding the cross-process lock. Ephemeral (incognito/temporary) slots are excluded, matching `_persist_open_slots`. Frontend: `seedContextUsage` in `chatSlice.ts` is the single hydration path for all three slot-detail reducers (`switchSlot`/`refreshSlot`/`warmSlotCache`) and seeds ONLY a slot with no entry yet — the server broadcasts over WS before the HTTP response lands, so an unconditional write would clobber a live measurement with the older snapshot the request was built from. A token entry is written only when a window is actually known.
**Follow-up suggestions** (`suggest_followup` MCP tool → card above the composer): POST `/api/chat/slots/{slot}/followup` with `{items: [{title, description, prompt, branch?}]}` — broadcast-only (nothing persisted server-side), 404 unknown slot, re-validates `SUGGEST_FOLLOWUP_SCHEMA` server-side (≤3 items; title ≤120, description ≤600, prompt ≤8000, branch ≤80 chars full-matching `FOLLOWUP_BRANCH_RE`; hidden-Unicode stripped), redacts every field through `redact_exfiltration_urls` + `redact_credentials` — including `branch`, which is DROPPED rather than mangled when redaction alters it — and emits `followup_card` over WS. The card goes out on the OWNER-only websocket channel (`deliver_ws_owners`), never the all-clients broadcast, because an app caller can open `/api/ws` and would otherwise receive another user's complete handoff prompts. Returns `{ok, delivered}` where `delivered` is the number of owner-socket sends that actually COMPLETED — the send is awaited (`deliver_ws_owners`), not fire-and-forget, because a socket count is taken before any send runs and a window that drops in that gap would be reported as delivered; failed sends are dropped from the owner set. A card with no listener is reported to the model as not shown instead of a false success. Dashboard sessions only, at both layers: the MCP tool rejects non-`dashboard:` session keys, and BOTH endpoints require the OWNER's own identity via `is_owner_dashboard_request` — the same predicate the source-provider mutations use: an `app` claim of `""` is necessary but not sufficient, since a dashboard credential minted for a different subject carries it too and would raise cards in the owner's composer and create branches in the owner's repositories; when no owner is configured only the signed local bootstrap subjects (`local-app`, `local-startup`) are accepted, which is the standalone-local case. One carve-out: the loopback internal-secret grant sets `request["internal_auth"] = True` and NO app claim (that is the path every MCP call arrives on), so it is permitted. A request with neither marker really did skip authentication and is refused, SEL-audited. POST `/api/worktree/create` with `{repo, branch}` creates `<parent>/<repo>-wt-<slug>` on a new branch off `origin/HEAD` (falling back to `HEAD`) and returns `{ok, path, branch, base, reused}`. Security boundary: `repo` must name or sit inside a directory an existing chat slot is scoped to (the submitted path AND the resolved git toplevel are both checked, and only the server-held root is ever used as a path); sensitive paths refused; git is routed through the `sandboxed_spawn_argv` chokepoint in **strict** mode (OS isolation + scrubbed env; strict because the filter probe passes `--includes` and repo-controlled `include.path` could otherwise make git read `~/.aws/credentials` as config); a host with no sandbox backend and no `agent.sandbox_allow_unsandboxed_exec` opt-in gets a 503 instead of an unisolated spawn. On top of that: an argv list with no shell, `run_limited` (resource limits applied after `exec`), 120s timeout, and `-c core.hooksPath=<os.devnull> -c core.fsmonitor=false` so no repo-supplied program executes (a non-directory device has no hook to find and nowhere to plant one; an in-repo path is repo-writable and a gateway-owned temp dir is still same-uid writable between calls) — a repo declaring a `filter.*.{process,smudge,clean}` driver in EITHER repository config scope (`--local`, and `--worktree` when `extensions.worktreeConfig` is on and a `config.worktree` exists under the per-worktree `$GIT_DIR`) is refused (409) because `-c` cannot disable an arbitrary filter name; the branch must also be a ref git accepts (`foo..bar`, trailing `.`/`.lock`, and `HEAD` are rejected up front rather than after the claim); both probes pass `--includes` (git defaults it OFF for a specific-scope query, so a driver reached via `include.path` was invisible yet still ran on checkout), and an unreadable scope is refused too. The allow-list collection and the sensitive-path/`isdir` screens run on a worker thread, so a slot project on stalled storage cannot block the event loop. Concurrency: the branch is claimed atomically (`update-ref <ref> <base> ""`) and the destination by `os.mkdir`, both before anything else, so cleanup after a failure deletes only what the request proved it created, pruning before the branch delete so an `rmtree` fallback cannot leave the claimed branch behind, and skipping the delete entirely when the post-prune listing shows another worktree holding that branch or cannot be read at all (`update-ref -d` lacks `branch -D`'s used-by-worktree guard, so deleting an adopted branch would strand that worktree on a dangling ref); a failed `worktree list` is treated as unknown (503), never as "nothing registered". Reuse requires the destination to be registered on the REQUESTED branch (`_dir_slug` keeps only a branch's last segment, so `feat/foo` and `fix/foo` collide). Both endpoints are SEL-audited. Frontend: both card actions PRE-FILL a composer and never send, appending below an unsent draft rather than replacing it (the pending-input path also persists the draft, so a plain set destroyed in-progress user text); the worktree action creates the tree, opens a session WITHOUT activating it, scopes it, and only then activates it and prefills — so the composer is never live in the default directory (and fails closed rather than prefilling an unrelated one), and deletes the session it just made if scoping fails. Per-slot card state is pruned on slot delete and in stale-slot pruning. User-facing guide: `src/kiro_crew/docs/followup-suggestions.md`.
**Pull-request sources**: POST `/api/source/pull-request` with `{url, refresh?}` returns the normalized full GitHub PR or GitLab MR payload. POST `/api/source/pull-request/checks` with `{url}` returns `{checks}` through a lightweight one-call GitHub or at-most-two-call GitLab path without rewriting the full-source cache. POST `/api/source/pull-request/status` with `{urls: [...]}` (bounded to `STATUS_URLS_MAX` = 64 canonicalized URLs, non-PR/MR URLs dropped, non-list bodies 400) returns `{statuses: {url: {state?, ci?, mergeable?, mergeStateStatus?}}, refreshing: [...], ttlSecs}` read straight from the same short-TTL chip-status cache the sidebar uses — it never blocks on a provider call, and schedules the bounded background refresh for stale entries, so unknown URLs are simply absent until a later poll. `refreshing` is the scheduler's own report of URLs whose value is expected to change shortly (started or already in flight; pending-cap deferrals excluded) and `ttlSecs` is the cache TTL, so a client paces its steady state by the server's TTL and re-polls within seconds of a refresh landing instead of up to one extra interval later. The Changes-tab source strip polls it to mark every PR/MR tab with its lifecycle state and CI rollup (bounded fast follow-ups, then TTL pacing; failing polls back off exponentially to a 5-minute ceiling rather than stopping, and re-poll on reconnect), and drives the selected tab from the full payload instead. The strip polls rather than riding the WS slots channel that already carries cached `state`/`ci` for sidebar chips: slot `source_links` are capped at a handful per slot while the strip shows up to 64 sources, so widening every slots push for one panel would cost every client more than one bounded request per TTL costs this one. **One truth for two surfaces**: the full-payload cache and the lightweight chip cache are kept coherent instead of expiring independently (which let the sidebar chip and the detail panel render different lifecycles for the same PR while both were nominally "fresh"). A completed full fetch projects its `state`/`draft`/`checks` onto the chip cache via `status_from_full_payload` (`record_full_payload_status`), and a chip refresh that observes a *changed* status conversely drops the full payload for that URL, so the panel's next read cannot serve a lifecycle the chip has already moved past. The chip entry carries the normalized **merge pair** alongside `{state, ci}` — free on the GitHub chip read and already present in GitLab's payload — and `status_from_full_payload` projects it too, because a write-through that dropped it would make every full fetch look like a chip change and drive exactly the mutual-invalidation loop below. Each field is recorded independently and only once it is real, never as `unknown`: independently because GitLab settles `need_rebase` and its branch-protection gates in the detail field while `mergeable` stays `unknown`, and never-as-`unknown` because a still-computing read must not read as a change away from a real answer. Because the pair participates in the chip cache's change detection, a branch that starts conflicting while the panel is open drops the full payload and pushes a delta like any other status change — the panel re-reads and banners it without a manual refresh. A structural loop-breaker guards the two comment-mirrored projections: `_refresh_check_status` records each URL's *changed* transition, and once the identical `(previous → new)` transition repeats past `_CHECK_FLAP_DAMP_THRESHOLD` consecutive refreshes — the signature of a chip↔full vocabulary divergence — it stops invalidating the full payload and emitting deltas for that URL and logs loudly once, degrading a would-be unbounded provider-polling loop to a stale glyph; a genuinely changing PR produces distinct transitions, which resets the counter and clears the damp. **Turn-boundary refresh**: when a chat slot goes idle, `DashboardState.refresh_slot_source_status` re-reads that slot's serialized chip URLs through `request_check_refresh_now`, which bypasses the chip TTL — an agent turn that opened a PR, pushed a revision, or drove a review round is the moment the remote state most likely moved, and TTL rotation alone can lag it by minutes once there are more PR-linked slots than `CHECK_STATUS_PENDING_MAX`. It is gated on at least one owner websocket (status is credential-backed and nobody else can render it, so a headless gateway spawns no provider work), scoped to the one slot that finished, floored to one forced read per URL per `_CHECK_FORCE_MIN_INTERVAL_SECS` (URLs inside the floor fall back to plain TTL pacing), and best-effort — a failure is logged and can never break turn completion. **Status deltas push instead of polling**: whenever a URL's cached status — `{ci, state}` or the merge pair — changes, the owner-only `source_status` WS event carries `{url, origin, ci?, state?, mergeable?, mergeStateStatus?}` to owner sockets (`DashboardState.push_source_status`, registered once as a delta sink at app wiring and unregistered on `on_cleanup`). `origin` (`"chip"` = the lightweight path learned it, `"detail"` = a full fetch's write-through produced it) is **diagnostic only** — the client patches its cached status batch AND invalidates `['pull-request-source', url]`/`['pull-request-checks', url]` for *every* changed delta regardless of origin. It must invalidate on `"detail"` too, because that delta is emitted by the single window whose full fetch ran; only that window received the fresh HTTP payload, so the other owner windows (whose detail query is `staleTime: Infinity`) would otherwise keep rendering the pre-change lifecycle. The initiating window's resulting refetch is harmless and cannot loop: `record_full_payload_status` runs only in the *uncached* fetch path, so the refetch hits the warm 30s cache and emits no further delta. The `origin` field is retained on the wire for diagnostics and possible future requester-aware routing; no consumer branches on it today. The client additionally invalidates pull-request detail queries on `chat_done`, because lifecycle/CI deltas do not cover review comments or mergeability and the detail query is otherwise `staleTime: Infinity` (it would never refetch after mount). The invalidation is **scoped to the finished slot**: for the ACTIVE slot it refetches the MOUNTED detail query with `refetchQueries({queryKey: ['pull-request-source'], type: 'active'})` (the PR on screen, which may lie outside the serialized chip subset; `refetchQueries` marks nothing else stale, whereas `invalidateQueries` with `refetchType: 'active'` would still mark every cached PR stale) and marks that slot's own `source_links` change URLs stale for their next mount; for a BACKGROUND slot it only marks that slot's own URLs stale (`refetchType: 'none'`, never a refetch of an off-screen PR). It never invalidates the whole `['pull-request-source']` family — doing so marked every session's PR stale on every turn anywhere, so a panel reopened while any chat was running always paid the full provider fanout even when nothing about that PR had changed — and the panel's own mutation success handlers invalidate only `['pull-request-source', url]` for the same reason. A background slot with more PRs than serialized chips has its overflow left to the status-delta path. The detail queries (PR and issue) also keep an unmounted payload for `SOURCE_DETAIL_GC_MS` (one hour) instead of React Query's five-minute default, and both detail queries revalidate on mount whenever the retained data is older than the gateway's cache window (`SOURCE_REMOUNT_REVALIDATE_MS`, 30s — `refetchOnMount` answers `'always'` past it, since with `staleTime: Infinity` a plain `true` would never refetch), so a reopened panel renders the retained payload at once and refetches in the background — stale-while-revalidate — rather than presenting an hour-old discussion as current when the only events this gateway sees are its own turns and status deltas. Younger data is not refetched: the gateway would return the same cached bytes, and Code Review Sage mounts its pane and this panel on the same key, which must stay one provider read per open. Past the window the refetch is cheap because the gateway revalidates with conditional GETs. A background revalidation that fails over a loaded payload (expired provider login, registry outage) renders a compact `role="status"` notice above the retained content — the last loaded version is showing, with the login command and a retry — never the full-height "could not load" card, which is reserved for a panel with nothing to show. Polling remains the safety net for a missed event. POST `/api/source/pull-request/resolve` with `{url, threadId}`, POST `/api/source/pull-request/auto-merge` with `{url, confirmImmediateMerge?}`, and POST `/api/source/pull-request/ready` with `{url}` are the three lifecycle mutations, and POST `/api/source/pull-request/submit-review` with `{url, reviewId, event}` is the fourth. Alongside it POST `/api/source/pull-request/pending-review` with `{url}` is a fourth READ, returning `{reviewId, body, commitId, headSha, stale, contentRedacted}` for the caller's own unsubmitted (PENDING) review or empty strings when there is none. The pair exists so a draft review can be published from the dashboard instead of only in the provider UI: GitHub scopes a PENDING review to its author and allows one per user per pull request, so the single PENDING entry the read returns is necessarily the caller's own — there is no way to observe, or therefore publish, someone else's. `event` must be `APPROVE`, `REQUEST_CHANGES`, or `COMMENT` (`DISMISS` and the other review endpoints are deliberately unreachable: this path publishes a draft, it does not act on reviews others submitted), `reviewId` must be a bare positive integer since it is interpolated into the REST path, and both are GitHub-only — a GitLab merge request is a 400 rather than a silently different code path. `reviewId` is REQUIRED rather than resolved server-side: a pending review may equally be one the human started by hand in the provider UI, so submitting whatever draft happens to exist could publish an unfinished review the caller never saw; the id the caller was shown is re-checked against the still-pending one and a mismatch is a 400, which turns a concurrent submit-or-replace into a rejection instead of a surprise. The id is necessary but NOT sufficient: it identifies the review OBJECT, and GitHub lets a pending review's body be edited and its inline comments added or removed under the same id, so an id match alone cannot prove the caller read what is about to go out. The read therefore also returns `contentDigest` -- a sha256 over the review body plus every inline comment's `(id, path, line, body)`, sorted by comment id so a re-ordered read of identical content yields the same digest while an edited body, a moved line, or an added/removed comment all change it -- and the submit REJECTS a digest that no longer matches the current content. `contentDigest` is REQUIRED, not optional: a digest compared only when present would be a one-parameter bypass of the whole binding, so an empty value is a 400 rather than a skipped check. The digest is taken over the RAW text, because raw text is what GitHub publishes. Two further guards fail the submit CLOSED, and the read reports both so the UI withholds the buttons rather than offering a doomed click. **Stale head**: the draft's `commit_id` must equal the pull request's live `head.sha`, because a repository without stale-approval dismissal counts a stale `APPROVE` as a live approval of code nobody read; every event is refused on a moved head, not just the verdicts, since inline comments anchor to lines that may be gone, and an unknown draft or head sha counts as stale. **Redaction mismatch**: submission publishes the draft GitHub stored, NOT the redacted copy the read returned, so a draft whose body or any of its inline comments is ALTERED by `_redact_provider_data` is refused outright -- otherwise the dashboard shows `[REDACTED]` while the published review carries the secret verbatim, which is worse than having no publish button. Inline comment bodies are read separately (`.../reviews/{id}/comments`) because submission publishes every comment the draft holds, not only the body this app can see -- and BOTH that call and the reviews list itself run `--paginate --slurp`, because each returns 30 per page: an unpaginated comment scan clears a draft whose 31st comment carries the credential and then publishes it, and an unpaginated reviews read reports "no draft" for a pending review sitting past page one. **Post-submit head re-check**: GitHub's submit-review API accepts no expected-head parameter, so the staleness check and the submit cannot be one atomic operation. For a GATING verdict (`APPROVE` / `REQUEST_CHANGES` -- a `COMMENT` carries no verdict) the head is re-read AFTER publishing, and a head that moved in that window makes the just-published review get dismissed again (`.../reviews/{id}/dismissals`) with the reason recorded; a dismissal that itself fails raises a louder error naming the review, because an undismissable stale approval is the one state a human must be told about. This is a compensating action, not atomicity: it converts an invisible race into a visible, self-reverting one -- with ONE case it cannot repair, which is why the read also reports `autoMergeArmed` and `APPROVE` is REFUSED while auto-merge is armed on the pull request: an approval then satisfies branch protection and GitHub can merge the unreviewed head BEFORE the dismissal lands, and nothing repairs a merge. The refusal is scoped to that combination rather than removing the verdict, because `COMMENT` and `REQUEST_CHANGES` cannot let a merge through, and an unrecognised `auto_merge` shape counts as armed (fail-closed). **Stale-approval dismissal is the setting that closes the class**: every remaining variant of the race -- auto-merge armed before or after the check, a force-push inside the submit round trip, a manual merge in that window -- needs the base branch to KEEP stale approvals to do harm, because with `dismiss_stale_reviews` on GitHub retracts the approval itself the moment the head moves. So `APPROVE` additionally requires `required_pull_request_reviews.dismiss_stale_reviews` on the pull request's base branch, read from `repos/{o}/{r}/branches/{base}/protection` and reported to the client as `staleDismissalEnabled`. Unreadable protection (no admin rights, or no protection at all) withholds `APPROVE` -- fail-closed, since an unanswerable question about safety is not an answer of safe. The UI withholds the Approve button and states the reason instead of offering a click that 400s. Submitting is irreversible and immediately visible to everyone on the pull request, so it is the most consequential of the provider writes — the LLM review poster is separately forbidden from ever submitting (see the Code Review Sage app), and this endpoint is what makes the HUMAN's own click the only path to a published verdict.. All three go through one auth/audit/error wrapper (`_owner_mutation_response`), so a client disconnect, a rejected request, and a provider failure are recorded identically across them. Every credential-backed endpoint requires the explicit dashboard-user claim `request["app"] == ""`. When `state.owner_id` is configured, all eight endpoints require an exact `request["user"] == owner_id` match. When no owner is configured, only the four read endpoints accept the signed machine-local bootstrap subjects `local-app` and `local-startup`; every mutation still returns 403. App tokens, non-owners, unrelated or missing subjects, and every unconfigured-owner mutation fail closed. Every direct source request makes a best-effort SEL audit attempt with only the caller, operation, coarse outcome/reason, never URL, thread id, provider output, or credentials. SEL write failure cannot weaken a denial or replace the request's response or exception. Provider CLI calls separately emit coarse `invoked`, `completed`, `denied`, or `failed` tool-invocation events with no argv or provider-controlled text. Cancellation while reading a request or awaiting a provider is recorded as `failed/request_cancelled` when SEL is available, then the original cancellation is re-raised; because a remote mutation may already have landed, mutation cancellation is intentionally an uncertain failure outcome. Cache removal, generation advancement, and stale in-flight detachment complete before provider mutation dispatch, so a cancellation cannot preserve or repopulate pre-mutation data. A mutation invalidates **both** caches: the full-source payload and the separate short-TTL chip-status cache the sidebar and `/api/source/pull-request/status` read, which would otherwise keep serving pre-mutation `draft`/CI state for up to one TTL. The chip cache carries its own per-URL generation counter for the same reason resolve advances the full-fetch generation: a status refresh that started before the mutation captures the generation at entry and discards its result if it changed, so an in-flight fetch cannot restore superseded state. The resolve endpoint validates thread ownership and resolves a supported review thread, returning `{resolved: true}`. The auto-merge endpoint arms provider auto-merge and returns `{autoMerge: true, mergeMethod}`: on GitHub it reads the pull request node plus the repository's allowed merge methods, refuses a draft or an already-armed pull request without dispatching, picks the first repository-allowed method in squash/merge/rebase order, and calls `enablePullRequestAutoMerge`; GitLab has no separate switch, so it reads the merge request first, refuses a draft or an already-armed merge request, and otherwise issues the merge call flagged `merge_when_pipeline_succeeds`; because that call merges immediately when no pipeline is pending, the GitLab path is a merge authorization, so with no pending head pipeline the request is refused unless the body carries `confirmImmediateMerge: true`. The field must be a real JSON boolean -- coercing it would let any truthy value, notably the string `"false"`, read as consent -- and any other value is a 400. That refusal is raised as `ConfirmationRequired` and answers with `{error, confirmationRequired: true}`, which is what makes the guard live rather than a constant the client asserts: the dashboard's confirming click sends `false`, and only the server's own refusal escalates the panel to a third, explicitly-worded `Merge now` step that sends `true` and quotes the server's reason. Confirm and Cancel are separate buttons, with Cancel standing where the arming button was, so the second half of an accidental double-click backs out instead of authorizing a merge. The ready endpoint clears draft state and returns `{ready: true}`: `markPullRequestReadyForReview` on GitHub, and on GitLab the GraphQL `mergeRequestSetDraft(draft: false)` mutation, refusing when the merge request is not a draft (`draft`, or legacy `work_in_progress`). Neither ready path rewrites the title: the draft-prefix grammar stays the provider's concern, a concurrent retitle cannot be lost, and a title that merely begins with a draft-like word (`Drafting widgets`) is never mangled. GraphQL reports refusals in the body with HTTP 200, so every mutation response is inspected for transport-level `errors` and the per-mutation `errors` field and raises rather than reading as success. Both read the current draft/auto-merge state before mutating so an inapplicable action is a 400 instead of a provider error, and both invalidate the cache before dispatch on the same rule as resolve. The full payload carries `autoMerge` (GitHub `autoMergeRequest`, GitLab `merge_when_pipeline_succeeds`) so the panel renders armed auto-merge as state rather than an available action. Invalid URLs/thread IDs return 400; provider CLI, authentication, secure-spawn, audit, or direct-fetch-capacity failures return 503. Sidebar source-link extraction ignores every non-durable message role (`chunk`, `done`, `streaming`, `queued`, and `permission`) and indexes only durable message content, preventing partial output, queue placeholders, and permission prompts from scheduling credential-backed provider work. Sidebar provider refresh and cached `state`/`ci` serialization use the same read-only boundary: an exact configured-owner request, or a signed `local-app`/`local-startup` dashboard request when no owner is configured. Non-owner and app-token slot responses retain the source URL/provider/number but cannot trigger or observe credential-backed status.
**Chat Folders**: GET `/api/chat/folders` (list project folders, each enriched with a computed non-persisted `history_count` — the authoritative on-disk archived-session count per folder from `ConversationLog.list_sessions()`), POST `/api/chat/folders` (create; body `{name, parent_id?, project_dir?, default_agent?, color?, tags?}` — `color` is allowlisted against the folder palette (`_FOLDER_COLOR_PALETTE`, pinned to the frontend catalog by test), rejected with 400 `code: "color_invalid"` otherwise; `tags` is an array of tag ids filtered against the live tag vocabulary (deduped preserving order; only a non-array payload is rejected with 400 `code: "tags_invalid"` — unknown and non-string ids are silently FILTERED, not 400ed, matching the slot-tags endpoint, since a dangling id can legitimately exist on a folder and a strict endpoint would brick every subsequent save; the intersection is skipped when the vocabulary is not authoritative (`tags.json` unreadable at boot), failing open so a save cannot wipe stored tags; no count cap), persisted on the folder record only when non-empty (absent means no tags); no LLM generation of any kind runs on the chat-folder lifecycle), PATCH `/api/chat/folders/{id}` (update — accepts `hidden` (bool), `color` (allowlisted; empty string or null clears back to default) and `tags` (validated as on create; an empty array clears the key) alongside `name`/`collapsed`/`order`/`default_agent`/`project_dir`; folders carry no `icon` field — a folder's identity mark is its user-picked palette color, and stale `icon` values in pre-existing folders.json files are ignored (the artifact-library folder emoji generator is a separate system and unaffected); moving or reviving a session into a folder auto-unhides it via `_unhide_folder`), DELETE `/api/chat/folders/{id}` (delete + ungroup its slots) Folders may be linked to a project directory (`project_dir`, validated server-side: absolute, existing, non-sensitive path): `POST /api/chat/slots` resolves the effective directory from the server-held folder tree by walking to the nearest ancestor with `project_dir` set (cycle-guarded), validates the stored path again, and initializes a new slot with it before the first broadcast. The dashboard still carries its client-resolved directory through the existing explicit project endpoint as compatibility defense, but correctness does not depend on the React Query folder cache. **A slot is born filed**: `POST /api/chat/slots` accepts `folder_id` (validated against the folder list, 400 on unknown) and the create handler runs the whole set-up — folder, title, artifact binding, project — inside `DashboardState.suspend_slots_push()`, so exactly ONE coalesced `slots` broadcast is emitted and it already carries the folder. A genuinely NEW slot filed into a folder that carries `tags` also inherits them at creation (copy-by-value onto `slot.tags`, ids re-validated against the live vocabulary, non-string entries skipped; addressing an EXISTING slot never re-tags, and moving a session into a folder later never retro-tags — creation-only, mirroring fork tag inheritance). Inheritance is DIRECT-FOLDER-ONLY: unlike `project_dir`, which resolves by walking up to the nearest ancestor that sets it, a chat created in a subfolder does not inherit an ancestor folder's tags — tags mark the folder itself, not its subtree. The per-channel default-filing path (`channel_slots.surface_channel_session`) applies the same copy on its first-filing branch, so a channel chat born into a tagged folder inherits identically; its restore branch (a persisted `folder_id`) does not. This ordering is load-bearing, not an optimization: `get_or_create_slot` broadcasts the slot list *before* the handler returns, so a folder applied afterwards (the former client-side `setSlotFolder` PATCH) always lost the race — the client received the slots frame for an unfiled slot first and rendered the new session at the top level for ~200ms before it jumped into its folder. `_folder_changed` is deliberately NOT set at creation: the first turn is `is_new`, which already injects the `[FOLDER]` breadcrumb.
**Agents**: GET `/api/agents` (Kiro Crew agent roster ordered **most-used-first** — config agents plus, scoped to the requesting session's project directory via `X-Session-Key`, that project's `.kiro/agents` discoveries, each row tagged `scope: "global" | "project"`; a name in both scopes lists once as the alias, since dispatch resolves aliases first; ordering reorders by `ConversationLog.agent_usage()` (turn count, then recency), falling back gracefully to config-insertion order on any failure so the dropdown never breaks or drops agents), GET `/api/agents/installed` (list all kiro-cli agents from `~/.kiro/agents/` — deliberately global-only, because every consumer is an agent CRUD/editor surface whose "Set as default" persists the selected name into `cfg.agents`, where a project-only name would not resolve; project-scope discovery instead reaches the dispatch surfaces — per-turn resolution, spawn validation, and Slack — with `scope` on each entry reporting which scope won there; `package` field extracted from filename), GET/DELETE `/api/agents/detail/{name}` (full agent config JSON; DELETE removes the config file, protected for kirocrew/kirocrew-lite; DELETE and PATCH are owner-only — see Agent Config)
**Capability Integration** (edition-supplied operations-based `CapabilityManager` seam — the edition owns its CLI grammar, output parsing, and error translation; on a vanilla OSS install `CapabilityManager.available()` is `False` so every endpoint returns HTTP 503 `"capability manager not available"`; every POST here is owner-only — see Agent Config): GET `/api/capability/mcp` (list installed MCP servers), POST `/api/capability/mcp/install` (install an MCP server, pushes `refresh("agents")`), POST `/api/capability/mcp/uninstall` (pushes `refresh("agents")`), GET `/api/capability/mcp/registry` (browse available MCP servers — the manager returns already-parsed entries, which the core passes through as `{"servers": [...]}`), GET `/api/capability/skills` (list installed skill packages), POST `/api/capability/skills/install` (install by `package` only — **no `version_set`**; the manager owns version/source resolution; regenerates agent config, pushes `refresh("agents")`; the manager returns human-friendly errors), POST `/api/capability/skills/uninstall` (pushes `refresh("agents")`), GET `/api/capability/agents` (list installed agent packages), POST `/api/capability/agents/install` + POST `/api/capability/agents/uninstall` (by `package` only, same no-`version_set` rule as skills; both rebuild the agent config off the event loop, clear the `list_agents()` cache — it keys on a `(count, newest-mtime-ns)` signature a same-tick mutation would not bump — and push `refresh("agents")`), GET `/api/capability/plugins` (installed client-plugin packages **plus** `out_of_sync`, the drift set of packages installed as agents but missing their plugin counterpart; both reads are `asyncio.gather`ed so the endpoint stays inside ONE `CAPABILITY_READ_TIMEOUT` — it is polled), POST `/api/capability/plugins/sync` (reconcile that drift). Package names are allowlisted (`_is_valid_capability_package`: length cap, `..` rejection, charset) before crossing into the edition manager, manager messages are `_redact_external`-scrubbed + length-bounded on BOTH success and failure paths, and each mutation emits an explicit SEL line naming the package (the audit middleware logs only the request path). The routes were renamed from the former `/api/aim/*` to neutral `/api/capability/*` vocab so no Amazon-internal name fossilizes in the fork's public API.
**Removed cross-provider bridge** (fully **deleted**): the kiro→removed-provider mirror (`/api/cc/mirror/preview` + `/api/cc/mirror/run`, `mirror_kiro_to_cc`, the `ProviderPanel.tsx` migrate-from-kiro UI, the `kirocrew mirror kiro-to-cc` CLI) was removed when KiroCrew collapsed to a single KiroACP / `kiro-cli` backend. The later cross-provider AIM plugin bridge (`GET /api/cc/aim/missing`, `POST /api/cc/aim/sync`, plus `install_cc_plugin`, `installed_kiro_packages_missing_from_cc`, and the `_VALID_PACKAGE_RE`/`~/.aim/cc-plugins` machinery) was **also removed** with the pure-AIM code — there are no `/api/cc/aim/*` routes. Provider-specific MCP config is now handled uniformly through the `extra_mcp_scopes()` seam (see `platform-context.md`): the core manages the Kiro global only, and a companion re-adds its provider scope for both apply/uninstall AND discovery. On `/api/mcp/apply` the omitted-field default differs by scope family — the core `kiroGlobal` key is `omit → delete` (the bundled SPA always sends it) while every seam `f"{id}Global"` key is `omit → preserve` (defaults to current on-disk presence), so an OSS apply that omits a companion scope never deletes that provider's server; see the `extra_mcp_scopes()` contract note in `platform-context.md`.
**Sessions**: GET `/api/sessions` (paginated list; opt-in `exclude_open=1` drops every session a live slot already holds open — resolved through `slot_history_key`/`slot_transcript_key` so channel tabs whose transcript is their `linked_session_key` are recognised, and applied BEFORE `total`/`has_more` so one page's arithmetic matches what it returned. The sidebar's Older-sessions pane is the only caller: it renders the complement of the tab list above it, while the full inventory stays the default for memory consolidation and the command palette's recents. The same predicate protects a session from DELETE `/api/sessions`), GET `/api/sessions/{key}` (detail), DELETE `/api/sessions/{key}` (permanent delete), GET `/api/sessions/context` (context usage), GET `/api/sessions/usage` (kiro credit usage, cached 10 min; the background refresh tries the real CodeWhisperer RTS `GetUsageLimits` API first — the true used/limit/overage — posting to a hardcoded regional host keyed off the whoami profile ARN (`codewhisperer.us-east-1.amazonaws.com` by default, `q.eu-central-1.amazonaws.com` for `eu-central-1`; any other/missing region falls back to us-east-1); reading bearer tokens from `kiro-cli`'s own SQLite auth store under keys `kirocli:odic:token` (OIDC/Builder ID), `codewhisperer:odic:token` (legacy), `kirocli:social:token` (GitHub/Google social login), and `kirocli:external-idp:token` (Identity Center/org SSO), then falls back to scraping `kiro-cli chat --no-interactive --agent kirocrew-lite /usage` stdout when the API path is unavailable and `dashboard.usage_text_scrape_enabled` is true), POST `/api/sessions/summarize` (one-line LLM summaries for a list of session keys — bounded to 8, generated on an ephemeral background session with the cheap Haiku model, best-effort; backs the `list_sessions` MCP tool's opt-in `summarize=true`), GET `/api/sessions/memory` (per-session and per-task memory footprint; returns `{sessions, tasks, totals, history}` — each session row carries: `key`, `title`, `slot_key`, `untitled`, `agent`, `channel` (the grouping dimension, resolved by `telemetry_channel_of(key)` from `kiro_crew.messaging.link` — bounded cardinality, same taxonomy as telemetry metrics), `pid`, `owns_runtime`, `rss_mb`, `procs`, `mcp`, `cpu_cores`, `prompts`, `uptime_s`, `credits` (cumulative kiro credits consumed over the spend window — `SPEND_WINDOW_DAYS` in `dashboard/handlers/usage.py`, which the Telemetry spend tab's `cost_breakdown` shares so the two surfaces cannot report different totals for one session; `null` when no measured turn exists in the window, which is semantically distinct from zero; the join is by session key, and because a slot bound to a channel or cron conversation runs its turns under `linked_session_key` while its usage rows are still filed under the dashboard `slot.key`, the lookup falls back to `DashboardState.spend_slot_by_session()` — without that reverse index those sessions report `null` despite having spent), `turns` (count of completed turns in the same window — `null` same semantics as `credits`); `totals` carries `rss_mb`, `host_mb`, `host_pct`, `runtimes`, `rss_is_upper_bound`; `history` is a ring of `{t, mb}` snapshots; `tasks` is the subagent task list from `SubagentManager.task_memory_rows()`)
**Logs**: GET `/api/logs` (SSE), GET/POST `/api/logs/level` (runtime log level control)
**Task Runner**: GET `/api/taskrunner` (status with runs[], includes `agent`), POST `/api/taskrunner` (start, optional `agent` field), POST `/api/taskrunner/cancel` (per-task or all), DELETE `/api/taskrunner/{task_id}` (remove finished run), POST `/api/taskrunner/refine` (dynamic multi-turn with tool access), GET `/api/taskrunner/refine` (status with `waiting` field), POST `/api/taskrunner/refine/cancel`, POST `/api/taskrunner/refine/answer` (answer clarifying question)
**Approvals**: GET `/api/approvals` (pending list), POST `/api/approvals/{id}/approve`, POST `/api/approvals/{id}/reject`
**Agent questions** *(legacy blocking stack — retained in code but no longer used by the now non-blocking `ask_question`; see "Agent Questions" above; slated for removal)*: POST `/api/ask-question` (**blocks** until answered; body `{session_key, questions, timeout_secs?}`; returns `{status: "answered", ask_id, answers}` or `{status: "timeout", ask_id}`; 404 on unknown slot so a caller never blocks on a card nobody renders, 400 on invalid payload), POST `/api/ask-question/{ask_id}/answer` (body `{answers}` or `{dismissed: true}`; 404 once the question has been answered or has expired), GET `/api/ask-question/pending` (`[{ask_id, slot, questions, ts}]` — rehydration source, since `question_card` is a one-shot broadcast: without it a reload or WS reconnect leaves the agent blocked with no card on screen until its window elapses; the frontend re-syncs it on WS open exactly as it re-syncs `GET /api/approvals`). All three are **owner-only**, and denying app tokens alone is not sufficient: a dashboard session token is also minted for every allowed Slack user (`!dashboard`) and carries an empty app claim, so it clears the app gate while belonging to a non-owner who could otherwise address a card at any slot (phishing the owner with crafted options, then reading the typed answer out of its own blocked response) or resolve a card the owner is still looking at. `is_owner_dashboard_request` is reused rather than re-derived, so "owner" has one definition — an exact `owner_id` match, or a signed `local-app`/`local-startup` bootstrap subject when no owner is configured, which is the identity the `ask_question` MCP tool itself carries since its token is minted as `generate_token(owner_id or "local-app")`. **The WS side is owner-scoped too**: `DashboardState.broadcast_ws_owners` sends `question_card` and `question_card_resolved` to `_owner_ws_clients` only. Gating the endpoints alone would be cosmetic — a non-owner dashboard session registers as an ordinary WS client, so a plain `broadcast_ws` would deliver the owner's question text and options to it regardless.
**Reveal**: POST `/api/reveal` (body `{path, action?}` where `action` is `"open"` or `"reveal"`, default `"reveal"`). Sensitive paths are denied (403). A direct-local-request gate (`is_direct_local_request`) restricts native openers to loopback callers with no forwarding headers; remote/tunneled callers receive `{ok: true, copy: "<path>"}` instead — the shared `revealOrOpen` helper writes the path to the clipboard silently (the affordance that routed there already promised a copy), with no rendered confirmation. GET `/api/dashboard/branding` exposes `direct_local: boolean` so the UI can conditionally render Open/Reveal affordances.
**Terminal**: POST `/api/terminal/sessions` (create PTY session, returns `{session_id, shell}`; capped at `dashboard.terminal.max_sessions`). The shell every spawn site launches resolves through one helper (`_resolve_shell`): the configured `dashboard.terminal.shell` (Settings → Display → Terminal; declared in the config schema, editable via the config PATCH allowlist with an off-loop save-time executable check whose 400 carries `code: "shell_not_executable"`), else `$SHELL` (POSIX), else the platform default (`/bin/bash` / `powershell.exe`) — each candidate validated with `shutil.which` and pinned to the RESOLVED absolute path (a bare name re-resolved in the spawn's project cwd could differ), so a configured value that no longer resolves falls back rather than failing the open. A rejected configured shell is surfaced at the API level — the create response adds `shell_fallback: true` plus `configured_shell`, and each spawn site logs a warning; the dashboard panel itself spawns via the WS handler and does not read those fields, so the user-visible typo surface is the save-time Settings validation, GET `/api/terminal/sessions` (list), DELETE `/api/terminal/sessions/{session_id}` (kill), `/api/ws/terminal/{sessionId}` (per-session WebSocket: binary frames carry raw PTY I/O forwarded BYTE-FOR-BYTE — the server does not decode, scan or rewrite the stream at all. Scanning it was removed. The panel renders into the authenticated operator's own browser, showing bytes their own shell just wrote, so any threat that reaches it reaches the terminal app beside it and the scan protected nothing the user could not already see; meanwhile it cost real correctness (hiding a token printed on purpose via `gh auth token`, swallowing a device-code login or presigned URL mid-flow, mis-firing on high-entropy build output such as an npm `integrity sha512-…` line) and it forced a decode whose read boundaries corrupted output: a PTY read ends wherever the kernel had bytes, so a multi-byte character split across two reads became two U+FFFD, permanently destroying CJK, emoji and a TUI's box-drawing glyphs with no way for the client to recover the original bytes. Forwarding bytes moves that reassembly to xterm.js, which runs its own incremental decoder, so no read boundary can corrupt a character and the server holds no decoder state that could desynchronize from the client's. The reconnect replay is the same byte copy (`bytes(sess.scrollback)`) — a truncated ring buffer can begin mid-character and the client renders that head as it finds it, the lead byte being genuinely gone. The credential boundary is `POST /api/terminal/redact` below, which is where output reaches a model; a source guard (`test_redactors_are_called_only_by_the_selection_handler`) pins the redactors to that single call site so a scan cannot reappear on the streaming path, and an integration test drives the real loop with an unbroken 9 KB run of 3-byte characters — one line, no newline, because a shell that writes line by line hands the reader whole lines and every read then lands on a character boundary by accident, which is how an earlier version of that test passed against the corrupting code. Every send goes through a WebSocket captured into a LOCAL and revalidated after the lock await, never `sess.ws` directly — the WS handler sets that field to `None` on disconnect, and `AttributeError` is not caught by the loop's `except OSError`, so touching it after a suspension point would kill the reader task and stop PTY draining and scrollback capture for a session the client may still reconnect to (a source guard pins `sess.ws.send` out of the module). A second source guard (`test_session_reservation_has_no_await`) pins the reservation region await-free: the max-sessions check and the placeholder assignment together are the reservation (the handler marks the boundary with its own "Reserve slot synchronously before any await" comment), and an await in there lets two concurrent opens for one session id both pass the check and both spawn a PTY, leaking one untracked; it deliberately starts at that comment rather than at `registry.get`, because the stale-entry cleanup above it awaits `_kill_session` and is pre-existing. JSON control frames are client→server `resize {cols, rows}` and server→client `title {text}` (foreground command name while one runs, else cwd basename), `cwd {path}` (the shell's full live working directory), `error {message}`, `pong`). A singleton poller (`poll_terminal_titles`, ~1s) probes each connected session off-loop for title and cwd, pushing each frame only on change; cwd resolution reads `/proc/<pid>/cwd` on Linux and falls back to `lsof -d cwd` on macOS/BSD executed ONLY from trusted absolute paths (`/usr/sbin/lsof`, `/usr/bin/lsof` — never PATH resolution), failing closed when absent. The poller captures and revalidates the session's WebSocket after every executor hop so a mid-probe disconnect can never crash it, and reattaching a WebSocket resets the per-session title/cwd dedup markers so reconnected clients are re-pushed current values. POST `/api/terminal/redact` (body `{text}`, 256 KiB cap → 413) scans a COMPLETE selection before the frontend inserts it into the chat composer, and is the WHOLE credential boundary for the web terminal: the live stream is forwarded unscanned and the scrollback is replayed only to the browser, so this is the one path by which terminal output reaches a model. It therefore runs unconditionally, with no configuration that skips it, and a contiguous selection is also the only input the regex redactors can be accurate on — a secret split across two 4096-byte reads is invisible to a per-chunk scan by construction; the frontend fails closed (no insertion, visible retry state) unless it returns 200. The selection toolbar (frontend `CliPanel.tsx`) appears on text highlight with Send to chat (appends the redacted selection to the composer draft annotated `Terminal output (path):` — live `cwd` frame value preferred, spawn dir fallback — wrapped in a backtick-run-escaping code fence; never overwrites the draft, never auto-sends) and Copy (confirms only after `navigator.clipboard.writeText` resolves, with an explicit failure state). POST `/api/terminal/complete` (body `{session_id, token, folders_only?}`, where `token` is the DEQUOTED literal path the cursor sits in — `"../Kiro"`, `"src/"`, `""`, and `my dir/` for an on-screen `my\ dir/`, since the client decodes backslash escapes before asking) returns `{dir, prefix, entries: [{name, dir, at}], truncated}` and backs the panel's inline path completion: a word longer than `_COMPLETE_TOKEN_MAX` (4096) is **413**, an unparsable body or a non-string `session_id`/`token` 400, an unauthenticated caller 401, the feature flag off 403, and an unknown session id 404; SEL audit event: `terminal.complete`, emitted for EVERY outcome including success, with a fixed reason word as its only resource (`feature_disabled`, `invalid_body`, `token_too_long`, `unknown_session`, `no_cwd`, `sensitive_path`, `listed`, plus the command tier's `invalid_argv`, `cmd_unknown`, `cmd_none`, `cmd_listed`, plus `completion_disabled`) — deliberately coarse, because the route fires per keystroke and the token, prefix, resolved directory and entry names are all user filesystem contents that would turn the audit trail into a transcript of the user's typing and disk layout. The session's working directory is probed per request (`_session_cwd_cached`, a ~0.4 s TTL memo run off-loop) rather than reused from the ~1 s title poller's `last_cwd` — a completion issued immediately after a `cd` would otherwise resolve against the previous directory — while the memo keeps a held-down key from spawning an `lsof` per keystroke on macOS. The token's directory part resolves against that cwd with a leading `~` expanded, so `dir` is the absolute directory actually listed; when the cwd is unknowable (non-POSIX host, or the probe failed) the answer is a well-formed empty one with **`dir: null`** as the "nothing resolved" signal rather than an error the client must special-case. Matching inside that directory is a case-insensitive **substring** search, so a long name is reachable by its distinctive middle (`termi` → `KiroCrew-terminal-completion`), and each entry's `at` is the offset the fragment matched at, which both ranks the results (earliest match first, so a true prefix still wins; then directories, then name) and lets the client highlight the span. A fragment that *starts* with a dot is matched as a **prefix** instead, because there the dot is what unhides hidden entries rather than a distinctive part of a name — substring-matching it would pull in every `foo.bar` and defeat the filter it just switched on. Hidden entries appear only once that leading dot is typed, `folders_only` drops plain files (`is_dir` follows symlinks, as the shell does), entries whose name contains a C0/DEL/C1 control character or a lone surrogate are filtered at the source (a surrogate survives JSON but `TextEncoder` turns it into U+FFFD, so the client would type a path that does not exist) (the client TYPES an accepted completion into the PTY, so a name holding CR/LF would submit an executed command line and an ESC would inject an escape sequence — no escaping makes those safe to type), and a missing or unreadable directory yields an empty list rather than an error — at keystroke rate an unreadable path simply has no completions. Two independent caps bound the work: candidates are pulled lazily into a `heapq.nsmallest` of `_COMPLETE_MAX_ENTRIES` (200) so a directory with 100k entries is never materialized, and `_COMPLETE_MAX_SCAN` (20000) bounds how many entries are EXAMINED, since retention alone would still walk a million-entry directory while holding a pool thread; `truncated` is true for either cap. The target directory is vetted through `hooks.validate_file_path` — the named chokepoint the backend security rules require every file read to pass through, which canonicalizes with `realpath` and then refuses via `is_sensitive_path()` — before any scan (`_vetted_completion_dir`) — canonicalization comes FIRST because a benign-named symlink, or a symlinked parent component, whose target lands inside the governance trust-root would otherwise pass a name-based check and be enumerated through the link; a refusal (like a `realpath` failure) returns the same well-formed empty answer, disclosing nothing about whether the path exists. The enumeration is then PINNED TO A DESCRIPTOR (`_open_vetted_dir` opens the vetted directory `O_RDONLY|O_DIRECTORY|O_NOFOLLOW` and `os.scandir` is handed that fd, never the path string) because vetting a name and scanning that name are two resolutions of the same string: swapping the directory for a symlink to `~/.ssh` in between would otherwise enumerate the target. The open is itself verified — the fd's `(st_dev, st_ino)` must equal what the vetted name resolves to, so a swap in that remaining window fails closed — and the descriptor is closed on every path out, since `os.scandir(fd)` does not take ownership of it. The scan runs on `discovery_executor()`, not the `subprocess_executor()` shared with PTY teardown, so a slow directory cannot starve the `os.close` of session teardown, which can itself wedge in the kernel. The authority model is the live session id: the route lists a directory on behalf of an authenticated caller who already owns a PTY in this gateway, i.e. an interactive shell with the same filesystem access, so it grants nothing that session's own `ls` does not — which is what keeps it from being a general filesystem-enumeration endpoint, and why paths resolve without a root restriction, exactly as the shell would. **Command tier** (`dashboard/terminal_commands.py`): the same route answers SUBCOMMAND and FLAG completions when the body carries `argv` (`{session_id, token, argv: ["gh", "pr"]}`), returning entries shaped `{name, desc, kind: "sub"|"flag", nospace?}` with `dir: null` — the per-entry `kind` is what lets a client tell a command answer from a path one, so the path tier's top-level response shape is unchanged and the two tiers never fall back into one another (the client picks, because only the client can see the screen row). A malformed `argv` is **400** (`invalid_argv`): it must be a non-empty list of at most `ARGV_MAX_WORDS` (24) strings of at most `ARGV_MAX_WORD_LEN` (256) characters, none containing a control character, DEL, C1 or lone surrogate, whose FIRST element matches `^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$` — a bare name, never a path. This branch is ordered BEFORE the unknown-cwd refusal, because a subcommand list does not depend on the working directory. The authority argument is narrower than the path tier's and is what the whole tier rests on: **it runs only what pressing Tab in that same shell already runs.** The entire body of a cobra-generated completion script is a call to `<tool> __complete <argv…> <word>` (`value\tdescription` lines then a `:<directive>` line; the ERROR bit 1 is the only reliable failure signal since cobra exits 0 regardless, and the NoSpace bit 2 rides through to `nospace`), and git's is `git --list-cmds=list-mainporcelain,others,list-complete,alias` for subcommands — chosen over `--help` scraping because git's `--help` advertises only ~22 of ~150 completable commands and a subcommand's `--help` renders a man page. Git subcommand FLAGS are deliberately NOT offered, and that removal is the one place this tier's authority claim did not hold: the obvious probe, `git <sub> --git-completion-helper`, reaches a builtin's parse-options only when `<sub>` IS a builtin, whereas for an alias git expands it first and a `!`-prefixed alias body is then EXECUTED via `sh -c` — so `git wipe --⎸` under `alias.wipe = "!git reset --hard && git clean -xfd"` would have destroyed the session's working tree, which the strict sandbox does not protect (it hides credentials, not the repository). Git's own completion script RESOLVES an alias through config lookup rather than invoking it, so Tab would never have run that body while this probe would have; `_probe_argv` therefore returns None for the git flag case and no probe is made at all. A guarded form is conceivable (probe only when `<sub>` appears in `git --list-cmds=builtins`, since git ignores aliases that shadow builtins) but was rejected as the wrong bargain inside a security boundary. Those two protocols are all that is ever executed. A probe is additionally REFUSED outright when any word on the line would point the tool at a different endpoint or identity (`_REDIRECTING_FLAG_STEMS` — `--server`, `--host`/`-H`, `--hostname`, `--kubeconfig`, `--context`, `--token`, `--user`, `--as`, the TLS/cert/proxy family, matched on the stem before any `=`; a one-dash word is parsed as a flag CLUSTER rather than a stem, since `-shttp://host` is kubectl's short `--server` with an attached value and an exact stem match read it as the stem `shttp://host` and let it through): a cobra completer at a leaf position makes a live request, and those flags choose WHERE it goes and WHO as, so `kubectl --server=<url> get pod ⎸` would otherwise have a keystroke send an unsolicited request to an arbitrary host with the gateway as the client. The list is deliberately broad — a false refusal costs one menu, a miss costs an outbound request. Five controls keep that claim true: an **allowlist, not discovery** (`_KNOWN`, extensible via `dashboard.terminal.completion.commands`, whose values are ignored unless they name an implemented protocol) — speculatively running `<whatever was typed> __complete` is what would make this dangerous, since `make __complete` builds a target called `__complete`; **bare names resolved against a sanitized PATH** — three classes are dropped: every empty and relative entry; every project-local tool directory (`.venv/bin`, `node_modules/.bin`, `.tox`, `vendor`, matched segment-wise on BOTH separators so an installed prefix like `/opt/venv-tools/bin` survives and so the filter cannot silently stop matching on a host whose `os.sep` differs); and **everything a third party could write** — every component of the canonical path must be owned by uid 0 or by the uid this gateway runs as, never world-writable, and group-writable only for an administrator group (`_ADMIN_WRITE_GIDS`, gid 80 `admin` on macOS only, whose members can already `sudo`; no gid qualifies on Linux, where `root`/`wheel` membership does not imply sudo rights) — walking upward, because a trusted `bin` inside a parent someone else can write can simply be swapped (the same reasoning `sudo`'s secure-path handling applies). A gateway running as **root** gets no group-write exemption at all: its children are root too, so admitting a group-writable node would let a merely-admin account substitute a binary that then executes with root privileges. The predicate is deliberately ownership+mode rather than `os.access(W_OK)`: the latter answers "can THIS process write here", which is *yes to everything* when the gateway runs as root (routine in containers), so it would silently disable the tier on those hosts while looking like a hardening win. Requiring a ROOT-owned chain is not tenable on a developer machine — Homebrew installs into a prefix owned by the installing user (`/opt/homebrew`, group `admin`), so uid-0-only leaves macOS with the handful of tools in `/usr/bin` and drops every one this tier exists for (`gh`, `docker`, `kubectl`), which is an unused code path rather than a security win; this is the policy `github_runner.validate_provider_executable` already applies to provider CLIs (accept `st_uid in (0, uid)`, refuse other accounts, refuse world-writable, refuse the agent-writable trees), reached there for the same reason. The residual cost is stated plainly in `_sanitized_path`: the agent shares the gateway's uid, so a directory this filter keeps is one the agent could plant a binary in. Three things bound it — `_KNOWN` is closed, so a plant must SHADOW a specific real tool name *and* win PATH order; the plant does not choose the argv; and an agent that can write files already holds more reliable execution paths (`~/.zshrc`, a git hook, a LaunchAgent) that no PATH filter touches. The trees the agent most plausibly writes are refused by LOCATION regardless: `_under_agent_writable_root` drops any entry inside `KIROCREW_PROJECT_DIR` or `workspace_root()`, which is what covers a checkout's own `bin/` or `scripts/` — a name the segment filter cannot see, commonly PREPENDED to PATH by a `.envrc` or dev shell so it would win resolution, and the one substitution vector the model itself controls (the same trees `github_runner.agent_writable_roots()` refuses for provider CLIs). Root discovery FAILS CLOSED: when the workspace lookup raises or a root cannot be canonicalized, the root set is `None` and every PATH entry is refused (no completions, one warning) rather than the unresolved root being silently dropped, which would admit exactly the tree the filter exists for; membership compares against `root + os.sep` so a sibling like `…/workspace-other` is outside `…/workspace`. There is deliberately NO opt-in to widen this — an earlier revision offered an operator-declared trusted-directory list, and review was right that it reinstates the whole vector for anyone who uses it, since "the operator consented" does not make an agent-writable directory safe to execute from mid-keystroke. The resolved binary is held to every test its PATH entry was — name-based, location-based and ownership — since a symlink in an allowed prefix pointing somewhere writable would reintroduce what the filter removed and the target is the file that executes. Resolution FAILS CLOSED when nothing survives, because `shutil.which(cmd, path=None)` silently falls back to the unsanitized `$PATH`. The honest limit: ownership bounds *who could have planted* a binary, and cannot bound a same-uid actor, which no permission bit can. so a planted `./gh` in the session's cwd can never be the file that runs; **no shell ever** (an argv list handed to `execve`, so a metacharacter in a half-typed word has nothing to escape into, and `_probe_argv` is separated from execution precisely so the exact command line is assertable without spawning); **sandboxed as an agent-influenced spawn at the STRICTEST tier** — routed through `sandbox.sandboxed_spawn_argv(..., "strict")` (every credential directory hidden, not merely the non-workflow ones the default `standard` tier covers, plus an ALLOWLISTED env — built from nothing rather than filtered down, because a denylist covers the credential names it knows and by construction cannot cover the ones it does not, so `GH_TOKEN`/`KUBECONFIG`/`NPM_TOKEN` and every future tool's variable would otherwise have reached a speculative child; the child gets only `TERM=dumb`, `NO_COLOR`, the pager vars, the locale, and the SANITIZED `PATH`, with no `HOME` at all — verified to cost nothing, since `git --list-cmds` still returns 65 commands and `gh __complete pr ""` still returns every subcommand with descriptions, those tables being compiled into the binary; one consequence is that git cannot read `~/.gitconfig`, so user ALIASES no longer appear in the listing, which is coherent with removing the alias-executing flag probe) and spawned with `create_subprocess_limited` (resource limits), because the allowlist bounds WHICH program runs but not what an argument can ask it to do, and a terminal line is writable by the agent (the run-in-terminal affordance) as well as by the user — `standard` would leave the kube and ssh config dirs readable and let an argument aim the tool at them (`kubectl --kubeconfig <kube config> config use-context ⎸`), while a probe needs no credential at all to read a table that is static in the binary, so the strictest tier costs no correctness; `test_spawn_audit.py` is the gate that keeps this routed; the wrapper is built on `discovery_executor()` rather than inline because it writes a temp launcher/profile (`mkstemp`/`os.write`/`os.close`) and a slow filesystem would otherwise stall the gateway loop at keystroke rate — and on `subprocess_executor()` it could starve PTY teardown's `os.close`, the same reasoning the path tier's scan follows — with the cleanup unlink offloaded fire-and-forget for the same reason; and `SandboxUnavailableError` is CAUGHT rather than propagated, because the chokepoint fails closed where a host has no usable backend (an AppArmor-restricted userns is the common case) and an uncaught raise would surface as an HTTP 500 on a keystroke, whereas this tier's contract already makes "no completions" a normal answer — so an unbuildable sandbox yields no menu and, verifiably, no child process at all; **bounded** (stdin `/dev/null` so a prompting probe sees EOF instead of hanging, stderr discarded, stdout capped at `PROBE_MAX_BYTES`, a `PROBE_TIMEOUT_S` wall clock that kills the process TREE via `start_new_session`, and a module-wide `PROBE_CONCURRENCY` semaphore); and the working directory passed through the SAME `hooks.validate_file_path` chokepoint as the path tier — a session sitting somewhere the gateway will not READ is somewhere it will not RUN a program either, answered `sensitive_path`. Values are **validated, not escaped** (`is_command_token`: `^[^\W_][\w.+:@-]*$` for a subcommand, `^--?[^\W_][\w.-]*=?$` for a flag, enforced on both sides) — a subcommand is a token the TOOL defined from a closed vocabulary, so anything needing escaping is not a real one and is refused at the parser, which is both safer than smuggling it through as an escaped literal and correct on screen, since escaping would turn the tool's own `--message=` into `--message\=`. Latency comes from **caching, not from a fast probe**: the candidate list is a pure function of `(binary realpath, st_mtime_ns, st_size, argv path, flags-or-subcommands)`, so one subprocess (~36 ms measured) serves a whole argv position and every later keystroke narrows the cached list in-process by case-insensitive PREFIX (not the path tier's substring search — a subcommand is short and typed from the front, so matching `m` inside `comment` would offer rows that look unrelated). Keying on binary identity rather than name expires a tool's listings when it is upgraded or a version manager repoints its shim; negative results are cached too (at a shorter TTL) so a failed probe is not re-paid per keystroke, and the LRU is bounded at `CACHE_MAX_ENTRIES`. Deliberately NOT offered: completion of the command NAME itself, which needs a PATH-wide executable scan with a different cost profile and its own disclosure question. Positional VALUES are not a target either, though the accurate statement is narrower than "excluded": at a leaf position a cobra tool answers a bare `__complete` from its own `ValidArgsFunction`, so those values arrive through the same wire format and, when shape-valid, render as subcommand rows — bounded by `strict` sandboxing (which starves the cluster- and account-backed completers of the credentials they need, so `kubectl get pod ⎸` resolves to nothing rather than to a live API call from the gateway) and by `is_command_token` (which drops anything not shaped like a token, as most identifiers are not). Client side (`TerminalCompletion.tsx` + `utils/terminalCompletion.ts`, POSIX-only following the PTY backend): the word being completed is read back out of xterm's screen buffer on cursor movement, never mirrored from keystrokes, because a mirror drifts the moment the shell rewrites the line itself (zsh completion, autosuggestions, Ctrl-R, paste, vi mode) whereas the rendered row is by definition what the shell believes the line to be. The command word is located from an OSC `133;B` or OSC 697 `NewCmd`/`EndPrompt` prompt marker when the user's shell integration publishes one, and from a last-prompt-terminator heuristic otherwise. `completionMode` then selects the tier, and the two are disjoint with PATH winning wherever it already applied, so no word that previously produced a menu can regress. The PATH tier keeps its two triggers — a path-shaped word (contains `/`, or starts with `~` or `.`) for any command, and a bare word only when the command is a known path command (`ls ⎸` lists the cwd) — with flags and `$`/backtick starts never triggering it, and `cd`/`pushd`/`mkdir`/`rmdir` requesting `folders_only`. Everything the PATH tier refuses goes to the COMMAND tier when there is a command word, the word cannot be a path (no `/`, not `~`-rooted), the command is not a known path command (so `python ⎸` keeps listing the cwd rather than probing a tool with no protocol), and the word is not a `$`/backtick expansion — which is how a flag word gets completed at all, since the path tier rejects it outright. With no command word yet, neither tier applies. `commandArgv` builds the context from the same escape-aware word split as `commandWord` (one `commandWords` helper, so the two can never disagree about where the command starts), keeping flag words because a cobra tool's position in its own tree depends on them and decoding each word because argv entries are literal. An accepted value is typed under the rules of the tier that was ASKED for, never the tier a response claims: an entry whose `kind` does not match the requested tier is DROPPED rather than reinterpreted, and a command value that is not a protocol-shaped token is dropped too. That distinction is load-bearing for the `./` guard, which becomes a property of the entry's KIND rather than its spelling — a FILE named `--force` must be typed `./--force` or the program reads it as an option, while the FLAG `--force` must be typed verbatim because `./--force` names a path that does not exist. Accepting a subcommand appends a space and deliberately does NOT suppress the next empty word (the opposite of the file case), because re-opening one level deeper is how the tree is walked without typing; `nospace` suppresses the separator where the protocol says the value follows immediately. The menu deliberately produces NOTHING rather than something plausible-but-wrong whenever the row cannot be reasoned about: on the alternate screen buffer (vim/less/htop sweep the cursor over arbitrary text, a redrawn `> cd ./src` satisfies the prompt heuristic, and an open menu would then steal Escape/Enter/Tab/arrows from the TUI), on a wrapped row, or one with a non-single-width cell or a multi-code-unit (combining) cell at or before the cursor (`translateToString` returns one physical row while `cursorX` counts cells, so the two coordinate systems only agree otherwise), mid-word (the chosen name would be inserted in front of the surviving suffix), and for a word V1 cannot parse — one containing a quote character, or ending in an unfinished escape. Backslash-escaped words ARE handled: the tokenizer does not break at an escaped space and the word is decoded before the request, so a name accepted with escapes can still be walked into. Accepted text is backslash-escaped through a single choke point (`buildInsertion`) against an allowlist of shell-safe characters, so an unforeseen metacharacter is escaped by default, and a name that would be read as something other than a local path additionally gets a `./` prefix when the word has no directory part — a leading `-`/`+`, or a `:` anywhere — those leads make the argument an OPTION rather than a path and no escaping changes that (`\-c` is still `-c`, and `vim +:!id` executes `id`); backslashes rather than quotes keep the result ONE shell word with nothing left open, so the echo still tokenizes as one word and accepting a directory can re-trigger on its contents. Enter and Tab re-read the live word before acting and abort (returning the key to the shell untouched) if it no longer matches the word the suggestions were computed for — and for the COMMAND tier the identity includes the argv, not just the token, because `gh pr c` and `git c` share the token `c`, so a token-only check let a menu computed for one tool be accepted into the other's command line, and a failed request closes the menu rather than leaving the previous word's entries acceptable. Keys the menu claims (↑/↓, Tab, Enter, Escape) are cancelled at the DOM level, not merely by returning `false` from xterm's custom key handler: that only makes xterm return early *without* calling `preventDefault`, so Tab would still move focus out of the terminal and Enter would still fire `keypress` and reach the PTY as a CR, executing the line instead of completing it. All terminal endpoints require an authenticated caller and the feature flag (`dashboard.terminal.enabled`), and emit SEL API-access events. **Completion has its own switch**: `dashboard.terminal.completion.enabled` (default `true`) silences the inline completion popup alone — both tiers, so the `cd ` path menu goes quiet with the subcommand and flag menus — while the panel and its PTY keep working, which `dashboard.terminal.enabled = false` cannot do because it kills the whole terminal. It is read ONCE per request above the tier split, off the event loop on `discovery_executor()` alongside the `completion.commands` read it now shares, and deliberately NOT memoised in `_is_enabled`'s 30s `_enabled_cache`, whose slot belongs to the whole-panel flag. A disabled completion answers the ordinary empty listing (`{dir: null, prefix, entries: [], truncated: false}`) rather than the 403 the panel flag returns — the client already renders no popup for that shape, so a configured silence needs no frontend change, and the outcome audits as `ok`/`completion_disabled` because nothing was refused. The gate sits AFTER the body, token-length and session checks, so a malformed request still gets its 400/413/404 rather than a spurious 200, and BEFORE the cwd probe, so a suppressed keystroke does no filesystem work. Both nesting levels are type-checked (`"terminal": false`, `"completion": false`) and a non-boolean `enabled` degrades to the default, since `bool("false")` is `True` and an HTTP 500 from a hand-edited typo would land on a per-keystroke route. **Windows fail-fast**: PTY/fork are POSIX-only, so on Windows `POST /api/terminal/sessions` (`terminal.py:api_terminal_create`) returns **HTTP 501** with body `{"error": "The web terminal is not supported on Windows.", "reason": "unsupported_platform"}` before allocating a `session_id`. The gate is guarded on `platform_compat.IS_WINDOWS` and runs after auth + feature-enabled but before the `max_sessions` check, so the platform verdict is unambiguous (501 wins over 429). The wording is shared with the WebSocket open path (`api_terminal_ws`) via the module constants `_UNSUPPORTED_PLATFORM_MSG` / `_UNSUPPORTED_PLATFORM_REASON` so both surfaces render identically. SEL audit event: `terminal.session.create` with `outcome=denied`, `resources=unsupported_platform`.
**Terminal input readiness**: opening `/api/ws/terminal/{sessionId}` is transport readiness only; it is not permission for an automated caller to write a command. The additional server→client `ready` control frame marks input readiness. For Bash (the Linux/WSL Run-in-terminal target), the PTY is launched as a REAL login shell (`-l`) and inherits a `PROMPT_COMMAND` that emits a randomized OSC marker; the raw reader matches that marker across read boundaries without decoding or rewriting output and sends `ready` only after the profile chain returns. A login shell runs an inherited `PROMPT_COMMAND` after the profile files return and before its first prompt, which is why the signal can live there and still be a post-profile one; it also holds while a profile is blocked in a shell builtin such as `read`, where foreground-process-group inference would release too early. The marker CANNOT ride an injected `--init-file` instead, because Bash reads an rc file only for a NON-login shell, and a non-login shell is the regression in #5885: `shopt -q login_shell` is then false, so every profile stanza guarded on login-ness silently no-ops and the user's environment never loads (that option is read-only, so sourcing the same files from an rc file cannot substitute for it). The injected snippet is single-shot and self-removing — it emits only while its token variable is still set, unsets that token so neither a later prompt nor a child shell repeats the sequence, and withdraws itself from `PROMPT_COMMAND` only while that variable is still exactly the SCALAR that was exported — `${PROMPT_COMMAND[1]+x}` separates a Bash 5.1 array whose element zero is still the hook, so a profile that appended keeps its own half either way, and the check avoids Bash 5.1-only syntax that macOS's `/bin/bash` 3.2 cannot parse. The mirror variable the ownership check compares against is unset on both branches whenever the hook runs. An operator who EXPORTED `PROMPT_COMMAND` into the gateway's own environment keeps it: the exported value is the hook followed by the inherited command (so it runs from the FIRST prompt, after the marker), the mirror is that whole value, and the withdrawal RESTORES the inherited command instead of unsetting the variable. Replacing it would be data loss rather than a lost nicety -- `PROMPT_COMMAND='history -a'` is the standard way to make concurrent shells append to `HISTFILE`, and without it an exiting shell overwrites that file with its own in-memory list, dropping what every sibling shell appended. A profile that ASSIGNS `PROMPT_COMMAND` outright drops the hook and with it the marker; that session's barrier never opens and the frontend's own bounded timeout reports the failure without writing, the same fail-closed outcome the barrier already has for a profile that never returns (tracked as #7657). One case is NOT fail-closed and the spec states it rather than implying the set is clean: a profile that installs its own hook only when the variable looks unset (`[ -z "$PROMPT_COMMAND" ]`, which is how the RHEL-family `/etc/bashrc` installs its terminal-title updater, reached through the default `~/.bash_profile` -> `~/.bashrc` chain) sees the exported hook, skips its own install, and then the snippet withdraws — so that session ends with no prompt command at all, silently. No carrier choice avoids it: every hook a login shell inherits is visible to the profile chain, and the mechanisms invisible to it (`BASH_ENV` non-interactive only, `ENV` POSIX mode only, `INPUTRC` cannot execute commands) do not run at the post-profile point the marker needs. Also #7657. There is deliberately NO second release on inferred progress. Two were tried on #7641 and both could release while a profile was still reading input, which hands it the queued command: a timeout cannot tell a dropped hook from a slow profile, and inferring the prompt from the PTY's line discipline is fooled by a profile sitting in `read -n 1` (clears `ICANON`) or `read -s -n 1` (clears `ICANON` and `ECHO`, i.e. indistinguishable from readline at a prompt) -- where the command loses its first byte and the remainder executes corrupted, which is worse than not running. Configured POSIX shells without an injected protocol retain transport-ready behavior rather than becoming unusable, and a ConPTY session sends `ready` after its first shell output reaches the client. Reconnects to an initialized session replay scrollback before `ready`. The frontend keeps `onTerminalReady` waiters parked until that frame; its existing bounded timeout reports failure without sending when Bash initialization never completes.
**File Picker**: POST `/api/upload` (macOS only — opens native osascript file picker, returns absolute paths)
**Screenshot**: POST `/api/screenshot` (macOS only — `screencapture -i`, returns path to `~/.kiro/crew/screenshots/`)
**File diffs**: GET `/api/file-diff?path=...` returns the working-tree diff and HEAD content for the requested file, plus a `status` field — `not_git` (the repository preflight failed, so the file is outside any git work tree and there is no baseline; only the preflight may claim this), `untracked` (inside a repo, all-added `--no-index` diff — including every file of a freshly-initialized repo with no commits yet), `modified`, `clean`, or `error` (a git invocation past the preflight failed or timed out; distinct from `clean` so a git failure is never reported as "no changes"). Every reachable `status` value is served with HTTP 200; a sensitive path is 403 `{"error": "Access denied"}`, and the empty-path / file-not-found early returns reply `{"diff": "", "original": ""}` with no `status` key. Snapshot reads and every Git subprocess decode text explicitly as UTF-8 with replacement for malformed bytes, so Windows locale code pages cannot corrupt non-ASCII file content.
**Misc**: `/api/spawn`, DELETE `/api/spawn/{id}`, POST `/api/spawn/clear`, `/api/notifications`, DELETE `/api/notifications/{ts}`, POST `/api/notifications/clear`, DELETE `/api/sessions/clear`, `/api/update/check` (git remote check), `/api/update` (pull + rebuild + restart, dirty-tree guard returns 409 if uncommitted changes), POST `/api/update/channel` (move a feed-checkable install onto another release lane — validates against the `RELEASE_CHANNELS` allowlist because the value becomes a feed-URL path segment, writes `$KIROCREW_HOME/channel` atomically, then re-checks against the new feed; 409 on a git checkout or an externally managed layout, which have no channel to switch), POST `/api/restart` (restart the gateway WITHOUT updating — how a wheel install picks up code a terminal-run installer already replaced on disk; valid on every layout, unlike `/api/update`, and replies before re-exec so the client can tell "restarting" from "failed")
**Webhook Hooks**: POST `/api/hooks/agent` is the shared external ingress endpoint, but credentials are first-class **sources**: every newly minted source owns `{label, agent, enabled, require_signature}` and one bearer credential, and maps to one operator-selected installed agent. Admission order is global switch → auth throttle → bearer lookup without stamping → per-source `enabled` gate (before body read; paused returns 503 `source_disabled`) → bounded raw-body read → HMAC/replay → last-used stamp → JSON/body validation → mapped-agent resolution → in-flight/capacity gates → background task. A body `agent` may be omitted or equal the mapping; a conflict returns 409 `agent_conflict`. A deleted/unknown mapped agent returns 409 `destination_agent_unavailable` and never falls back. Bearer lookup, source admission, and signing-secret lookup remain separate store reads so concurrent revocation fails closed; all store and agent-discovery reads are off the event loop. Dashboard management: POST `/api/webhooks/tokens` `{label, agent, require_signature?}`, PATCH `/api/webhooks/tokens/{id}` with the exact allowlist `{agent?, enabled?, label?}`, DELETE the same id. Signing policy and credentials are immutable after mint, preserving one-time bearer/signing-secret reveal. Legacy `hooks.webhook_token` is a synthetic read-only source; pre-routing stored rows normalize to `agent: ""`, `enabled: true` and retain historical caller/default routing until explicitly assigned. The top-level Webhooks/Activity UI keeps source configuration separate from contexts/runs; the source rail contains no token table or operational rows. `sessionKey` must start with `hook:`, max concurrency is 6, sessions are destroyed after each turn, and registered context comes from `~/.kiro/crew/hooks.json`.

**MCP Custom Servers** (manual JSON path — Add Custom modal + per-server Edit JSON): POST `/api/mcp/custom` `{servers: {name: spec}, enable?: bool=false}` — user-authored specs (stdio `command`/`args`/`env` XOR remote `url`/`scopes`/`clientId`/`headers`, unknown keys rejected by name; header names/values are shape-checked and a fresh spec carrying the redaction marker as a header value is refused — it means the user pasted a redacted read payload), validate-all-then-write (no partial batch), collision → 409 with `conflicts` list, servers land disabled unless `enable: true` (the modal's "Enable immediately" tick is the consent act). GET `/api/mcp/custom/{name}` — full editable spec including env (the list endpoint omits env; prefilling from it would drop vars on save). PUT `/api/mcp/custom/{name}` — replace spec, 404 when not Kiro Crew-managed, always preserves enabled/disabled state (editing is not consent to run; a pasted `disabled: false` cannot smuggle an enable). Non-allowlisted keys already on the entry (`disabledTools`, `autoApprove`, …) round-trip: an unmodified GET→PUT save succeeds and their on-disk values are preserved verbatim — they cannot be edited or removed via this endpoint (dropping `disabledTools` would silently widen the tool surface); modifying one → 400. `headers` straddles that line: an entry WITHOUT stored headers may author them on PUT like a fresh add, but once values exist on disk they join the carried set — reads redact header values, so an editable headers key would let the redaction markers overwrite the real credentials (modifying stored headers → 400 `stored_headers_not_editable`; changing them means remove + re-add, same as a `url` change with stored headers). Fresh POSTs carry nothing, so the tight allowlist still applies. All three SEL-audited; writes share the mcp.py file lock + entry helpers with discover-install. Consent-disabled entries (custom adds + registry installs) surface in the servers table as `Disabled` rows — `list_servers()` marks Kiro Crew-scope `disabled: true` entries (incl. when config sync mirrors the disable into the agent file) and a disabled server is never probed (a probe would spawn the unconsented process); the table's enable action is the reachable consent step. The refusal is enforced inside **`probe_server()`** — the one function every probe passes through, ahead of its local/remote dispatch — so a new per-server entry point cannot become a way around the consent gate by forgetting to pre-filter. `probe_all()` keeps its own `disabled` filter as defense-in-depth and to shape its result (disabled rows are omitted from `GET /api/mcp/probe` rather than returned with `status="disabled"`); a caller may add a friendlier error on top (Mochi's `GET /api/apps/mochi/mcp-tools/{name}` answers 409 `server_disabled`), but that is UX, not the safety property. The refusal deliberately does NOT write the probe cache: it is keyed by name and shared with `GET /api/mcp`, so recording an empty `disabled` result would erase the tool list an earlier real probe stored. `GET /api/mcp` rows carry `kirocrewManaged` (gates the Edit JSON action).

### Frontend (React SPA)

`KiroPrerequisiteGate` wraps the main dashboard route (the independent
`/worlds-popout` route is not gated). `DashboardBootstrap` mounts the proactive
auth-cookie refresh scheduler outside this gate, so a stale access cookie can
still refresh while the dashboard body is blocked. On a new gateway it:

1. displays the connected gateway's OS so a remote browser does not imply the
   CLI is needed on the browser machine;
2. links out to Kiro's official setup page (`OFFICIAL_INSTALL_DOCS_URL`,
   `https://kiro.dev/cli/`) — a link, never a button;
3. once a viable CLI is found, names the commands the USER runs to sign in
   (`KIRO_CLI_LOGIN_COMMAND`, `kiro-cli login`, for a personal account, and
   `KIRO_CLI_SSO_LOGIN_COMMAND`, `kiro-cli login --use-device-flow --license pro`,
   for organization SSO) and offers **Check again**;
4. records first-run completion when `ready=true`.

**Kiro Crew performs neither setup step, and there is no code path that could.**
Both belong to Kiro CLI. Deleted for install: the installer download
(`https://cli.kiro.dev/install`), its pinned SHA-256 pair, the bash/PowerShell
interpreter plan, `POST /api/kiro-prerequisite/install`, the `_install`
operation, `can_auto_install`, and the `.kiro_cli_binary_trust.json`
attestation. Deleted for sign-in: `POST /api/kiro-prerequisite/login`, the
`_login`/`start_login` device-flow spawn, `extract_secure_login_url` and its
trusted-host allowlist, the progress/ANSI scrubbers that rendered the CLI's
spinner, `_capture_operation_output`, `can_login`, and the whole
`OperationStatus` concept — with no operation left to run, the gate has no
progress UI. The payload still carries an `operation` key, but it is a
**compatibility shim** (`legacy_idle_operation()`), permanently idle: a dashboard
loaded before this change reads `status.operation.status` unconditionally in its
refetch-interval callback (the optional chain there guards `status`, not
`operation`), and that callback runs for every user, not only the first-run gate —
so a tab open across a gateway upgrade would throw on its next poll and drop to
the root error screen. It is served to owner and non-owner alike so the shape
never varies by caller, and can be deleted once no shipped client reads it.

The reasons are the same for both: the vendor's tooling does it better and stays
correct as it changes, owning it here meant owning a privileged surface (a
remote shell script executed unsandboxed; a credential-writing child process),
and each copy on the Kiro Crew side was one more thing to drift — the installer's
pin silently broke setup on any upstream change.

What remains is detection only. `_run_process` no longer accepts `sandboxed=`,
`stdin_data=`, or `on_output=`, so every spawn is one of two read-only probes
(`--version`, `whoami`), sandboxed, with a fixed argv, and nothing can be piped
into an interpreter or scraped out of a child's stdout.
`TestKiroCrewNeverSetsUpKiroCli` pins the absence of each symbol.

The sign-in card therefore shows the commands in `<code>` elements, rendered
verbatim from the backend-supplied `login_command` and `sso_login_command`. They
are **code constants, not catalog values** — a translated command cannot be typed
(see `website/docs/i18n-catalog.md`). Both tiers are labelled and offered
together because the browser portal `kiro-cli login` opens presents a free
Builder ID as a peer of organization SSO: a user on an SSO plan who picks the
wrong one authenticates successfully and only discovers the mismatch later, as
models missing from their account. Kiro Crew does not detect which tier applies —
that would mean inspecting the host's identity configuration — so the card
describes the choice and the user makes it. The card's only control is Check
again, which is also why the screen no longer carries a second Check again button
in its footer.

`repair_required` therefore has ONE producer now, the missing-agent-spec overlay.
A missing CLI is not a repair: the user obtains it from Kiro, so the gateway reports
`installed=false` and offers no action of its own rather than claiming a remedy it
does not have.

Any Kiro CLI that runs is directly usable for sign-in regardless of how it was
installed (toolbox, Homebrew, winget, Kiro's installer, or a self-updated
bundle). Trust is "the CLI runs, and it has a valid login" — install source,
owner, and path do not gate setup or ACP launch — so an installed-but-signed-out
CLI always offers an enabled **Sign in to Kiro** rather than a button-less
"repair" dead end.

**The first-run gate is the one screen that polls the HOST rather than the latch.**
`kiroPrerequisiteIsBlocking(status)` is true only while the full-screen first-run
gate is the whole UI (not `ready`, not `initial_setup_complete`, owner). In that
state the gate polls every 5s AND passes `?refresh=1`, because the two steps it is
waiting on — installing from kiro.dev and signing in, possibly from a terminal —
touch the gateway not at all, so a latch-reading poll could never observe either
and the gate would hold until the user pressed Check again. The cost is bounded to
exactly this screen: two short `kiro-cli` spawns per interval, ending the moment
`ready` flips, and a returning user never reaches it. This does NOT reintroduce
reauthentication chrome for an established install — that decision is unchanged
(see "The dashboard does not guide the user to sign in").

Ready dashboards continue prerequisite polling every 30 seconds, but that poll
is a **free read of the gateway's latched state** — it spawns no `kiro-cli`.
Only `api.kiroPrerequisite(true)` (`?refresh=1`), wired to the gate's Refresh /
Check again buttons via a one-shot `forceProbe` ref, asks the host to re-probe.
A later sign-out is therefore surfaced by the failing *turn*, not by the poll:
`chat_runner`'s `AcpAuthRequired` handler puts the actionable `kiro-cli login`
message straight into the transcript.

**The dashboard does not guide the user to sign in.** There is no
reauthentication banner, no "Sessions paused" state, and no disabled composer or
session-creation control. The `ReauthenticationBanner`, the
`UnavailableStatusBanner`, the copyable-terminal-command block, and the whole
`KiroReadinessContext` seam (`useKiroSessionReady`, consumed by `ChatInput`,
`ChatSidebar`, `SideChat`) are **deleted**. Rationale: latched readiness is only
refreshed at boot and on explicit request, so it is never fresh enough to
disable UI on — a user who signed in from a terminal would sit behind a dead
input box, and a persistent banner nagged every surface (including ones that
never start a session) for a state the dashboard cannot keep current. The error
card is in-context, appears only when the user actually tries to use the agent,
and is self-explanatory, so it fully replaces that chrome.

Ordinary send paths (main chat, prompt optimization, side turns, and
`POST /api/chat/slots/{slot}/continue`) no longer reject
on readiness — a stale latch must not
block a send the CLI would serve, and they mutate nothing before the turn, so a
failed turn costs only an error card. A queued successor turn runs rather than
parking on readiness.

`continue` belongs in that list because it IS a send: it queues one synthetic
continuation and lets the runner dispatch it, so the ACP attempt is its authority
exactly as for a typed message. Gating it produced the sharpest form of the
stuck case this section describes — the latch behind
`verified_ready` is refreshed by re-probing `kiro-cli`, and a probe that merely
TIMES OUT is indistinguishable from signed-out, so on a host where the
`--version` probe runs slow the Continue button was refused with a 503 forever
while typing the same request by hand still worked. `test_not_readiness_gated`
pins it.

**The destructive reruns are the exception and still fail closed.** `regenerate`,
`edit-resend`, and `rewind` truncate `slot.messages` and **persist** the result
(`_save_slot_to_history`, `_pending_rewrite`) *before* dispatching the background
turn, so "let the ACP attempt be the authority" does not hold for them: by the
time the turn raises `AcpAuthRequired` the history is already rewritten and no
error card can undo it. All three therefore call `reject_if_kiro_unverified`
BEFORE any mutation, returning the shared `kiro_prerequisite_required` 503.
(`switch-variant` is exempt — it swaps an already-stored variant and starts no
turn.)

**`POST /v1/chat/completions` also fails closed**, for a different reason: it has
no transcript the caller reads. Its collectors pick up only `chunk`/`assistant`
roles, so the `error` card an `AcpAuthRequired` turn appends is invisible and the
request would return **HTTP 200 with empty content** — an OpenAI SDK client
cannot distinguish that from a model that legitimately said nothing. It returns
the `kiro_prerequisite_required` 503 in OpenAI error shape until the endpoint
learns to translate `AcpAuthRequired` itself.

**An unresolved check is never rendered as "setup required."** The cold probe
spawns two sandboxed `kiro-cli` subprocesses (`--version`, then `whoami`), which
takes long enough to read; rendering first-run setup chrome across that window
flashed the full setup screen at returning users, who then watched it disappear.
Two layers close that window:

- **The dashboard never waits on the check.** Kiro readiness gates nothing in the
  dashboard, so an unresolved check mounts the app immediately and **fully
  usable** (`statusQuery.isPending` → children rendered directly). Whichever way
  it resolves, the user is already where they need to be: only a *confirmed*
  first-run status shows setup chrome; a signed-out established install shows
  nothing at all.
  `/api/ready` likewise does **not** gate on Kiro state — that would only delay
  first paint (and would not do what it appears to: the desktop splash polls
  `/api/status` and accepts any status `< 500`). `warm_up()` stays
  fire-and-forget and failure-contained, and its task is cancelled by the service
  shutdown hook; it yields `_WARM_UP_DELAY_SECS` first because its `kiro-cli`
  spawn racing the concurrent app-backend spawns measurably lengthened and
  destabilized boot (~2.7s → 2.8-5.6s on one real home). The delay is injectable
  so tests need not sleep it.
- **First-run completion is known without probing** — derived from the data home
  at construction (`initial_setup_complete`) and echoed by the probe-failure
  backstop, so a failed probe cannot demote a returning user to first-run. The SPA
  also remembers completion locally (`kirocrew:kiro-setup-complete`) so a COLD
  load with an empty query cache can still tell a returning user from a genuine
  first run. That memory only ever suppresses first-run chrome — it never grants
  session readiness, which stays server-driven via `ready`.

Accordingly, a status-check failure for a user who has completed setup leaves the
dashboard mounted and fully usable with **no banner at all** — an unreachable
status check is not evidence the CLI is broken, and the turn reports the truth
either way. A genuine first run — nothing remembered, no established home — still
gets the full setup gate, and `initial_setup_complete` short-circuits ahead of the
non-owner branch so an established non-owner is never shown the owner-restore
screen either.

**Boot latency: app backends start in parallel.** `start_enabled_app_backends`
vets apps serially (cheap) but spawns the admitted set **concurrently**
(`_start_backends_concurrently`, capped at `_BOOT_SPAWN_MAX_WORKERS`), and each
spawn's post-fork survival check (`_survived_spawn`) ends as soon as **our own
child owns the listening socket** rather than always sleeping its full ~1.6s grace
window. Previously both costs were paid serially per app, so gateway boot grew by
~1.6s for every installed app; measured on one real data home, boot to
`/api/ready` went from ~5.6s to ~2.9s. Three invariants make this safe:

- Ports are claimed **before the bind, under the lock** — auto ports via
  `_reserve_free_port`, and a fixed manifest port via `_claim_port`. Concurrent
  select-then-spawn would hand the same port to two apps, and the loser would
  crash-loop on EADDRINUSE — the exact failure the survival check exists to catch.
  The fixed-port case matters even though every in-tree app is `"auto"`:
  `_find_free_port` skips only ports already in `_allocated_ports`, so a fixed
  port recorded after binding is invisible to a concurrent auto allocation.
  Conversely, because a port is now reserved before it is bound, a FAILED spawn
  must release it (`_clear_failed_spawn_state`) or that port is retired from the
  pool for the life of the process — one leaked port per retry of a broken app.
  The release is conditional on the app having no live record, so it can never
  revoke a running backend's reservation.
- The early exit requires **ownership**, not just an open port: `_spawn_owns_listener`
  attributes the LISTEN socket to our pid or a descendant (the sandbox launcher
  execs the real server as a child). "Something is listening" is a different claim
  from "our child bound it" — with a fixed manifest port, another app or an
  unrelated process may already hold it, making our child the one about to die of
  EADDRINUSE; accepting that would report a doomed pid as started and route two
  apps at one backend. Mere elapsed liveness is likewise not accepted, so a child
  that crashes a few polls in is still caught. With no port to observe, or no
  port→PID tool on the host, the check polls the full budget exactly as before.
- The ownership probe shells out to `lsof` (~150ms), so it sits behind a cheap
  loopback-connect gate and the loop is **wall-clock bounded**: charging the probe
  to every poll interval made the failure path take ~2× the original budget, i.e.
  it regressed boot for exactly the apps slowest to start.

Per-app failure isolation is unchanged: one app raising or returning `None` never
affects the others or boot.

The query fails open for a rolling deployment whose older gateway does not yet
provide the endpoint, preserving dashboard access during frontend/backend
version skew. Once first-run setup has completed, later sign-out or CLI damage
leaves the dashboard mounted, fully usable, and shows **no chrome at all** — not
the full-screen gate and not a banner; the turn's own `kiro-cli login` error card
is the only signal. Non-owner browsers poll the
redacted readiness endpoint every three seconds while waiting for the owner, so
they observe completion without a reload; a manual **Check again** covers
external changes immediately.

React 18 + TypeScript + Vite 5 + Redux Toolkit + React Router v7 + Tailwind CSS 3 + DOMPurify. Source in `frontend/`, builds to `src/kiro_crew/static/dist/`.

**Styling** — Tailwind CSS with custom theme in `tailwind.config.js`. CSS custom properties (design tokens) defined in `index.css` for dark/light themes. `darkMode: ['selector', '[data-theme="dark"]']` enables Tailwind `dark:` variant with the `data-theme` attribute. Tailwind utility classes used throughout components (no separate CSS files per component). PostCSS + autoprefixer for processing. Theme toggle smoothly crossfades via `transition: background-color .25s, color .25s` on `body`.

**Design tokens** — All colors use CSS custom properties mapped in `tailwind.config.js`:
- Core: `--bg`, `--card`, `--text`, `--muted`, `--border`, `--accent` (amber/orange)
- Semantic: `--ok` (green), `--warn` (amber), `--danger` (red), `--info` (blue)
- AIM: `--aim` / `--aim-subtle` (purple) — used for AIM agent badges, MCP server pills
- Clarify: `--clarify` / `--clarify-subtle` (amber) — used for task refine Q&A box
- Diff: `--diff-add/del/hunk/meta` — theme-aware diff colors for MarkdownRenderer

**CSS utilities** — Defined in `index.css`, used across components:
- `.topbar-glass` — frosted glass effect (`backdrop-filter: blur(12px) saturate(1.4)`) on topbar
- `.scroll-shadow` — gradient mask fade at top/bottom of scrollable panels
- `.table-striped` — alternating row backgrounds via `nth-child(even)`
- `.skeleton` — shimmer animation placeholder for loading states
- `.focus-ring` — unified focus outline (`border-color + box-shadow + glow`) for all inputs/textareas
- `.card-glow`, `.stat-accent`, `.btn-sweep`, `.streaming-cursor`, `.think-bar`, `.typing-dots`

**Shared UI components** (`website/src/components/`):
- `ui.tsx` — `Card`, `CardTitle`, `Btn`, `SendBtn`, `Input`, `Badge`, `AimBadge`, `StatCard` (with skeleton loading), `Skeleton`, `EmptyState`, `PageHeader`
- `AgentSelector.tsx` — reusable agent dropdown with portal positioning, ARIA roles (`listbox`/`option`/`aria-selected`), `AimBadge` source pills, outside-click-to-close
- `ProjectAnimation.tsx` — image-based animation: KiroCrew logo (`icon-192.png`) with orbiting rings and pulsing glow. Theme-aware via `var(--accent-glow)`. Used in ProjectsPage empty state.
- `PixelCanvas.tsx` — pixel-art office canvas (256×256 at 3× scale) with 7 character sprites. States: typing (working), looking (reviewing), celebrate (passed), idle, empty. Loads `/sprites/floor.png` + `char{0-6}-frame{1,2}.png`. Renders via `requestAnimationFrame` loop.
- `PixelCanvasWidget.tsx` — 🎮 button with active-agent badge + modal overlay wrapping `PixelCanvas`. Maps `ProjectRun` task statuses to character slots.
- `SubAgentActivity.tsx` — live subagent status table with polling (2s). Status pills: Running (warn, animate-pulse), Done (ok), Failed (danger). Shown below running projects.
- `layout.ts` — `LAYOUT` constants: `NAV_WIDTH` (220), `NAV_COLLAPSED_WIDTH` (56), `CHAT_SIDEBAR_WIDTH` (260), `MAX_MESSAGE_WIDTH` (820), `AGENT_LIST_HEIGHT` (420), `LOG_LINE_CAP` (500), `TOPBAR_HEIGHT` (52)
- `InfoTip.tsx` — portal-rendered `?` tooltip
- `MarkdownRenderer.tsx` — block-assembled rendering: `useBlockAssembler` hook splits raw text into structured `ContentBlock[]` (markdown/code/diff/mermaid/excalidraw) via state machine, then renders each block with specialized renderer. During streaming, unclosed code fences render as provisional blocks; on completion, full reparse from rawText produces clean final output. react-markdown + remark-gfm + rehype-raw for markdown blocks, `HighlightedCode` for syntax-highlighted code blocks, `DiffBlock` for diffs, `MermaidBlock` and `ExcalidrawBlock` for diagrams. `fixCodeFences()` repairs malformed LLM output. `fixCjkAutolinkBoundaries()` closes a bare `http(s)://` run that GFM's autolink-literal extension over-ran, which it does whenever CJK prose puts punctuation straight after a URL with no space; the cut requires positive evidence, because CJK punctuation also reaches real URLs raw. Three rules supply it: a CJK bracket closing an opener from before the URL, a separator followed by a backtick, or a strong-emphasis delimiter that the prose prefix left OPEN (classified by the flanking rules the loaded `remark-cjk-friendly` amendment actually applies, not by counting) and that is itself followed by CJK punctuation. The third rule's evidence is deliberately the fullwidth character class rather than "any non-ASCII": an ideograph is legal mid-path, so `?q=a**中文` must not read as a boundary. Consequently an all-ASCII paragraph is never cut even when it shows the same defect, and a bare CJK sentence continuing off a URL is not covered either — it is character-for-character identical to a legitimate CJK-titled URL. Skipped in `sourcePos` mode, where inserting `<…>` would shift the inline-comment anchors.
- `ExcalidrawBlock.tsx` — read-only Excalidraw scene renderer for ```excalidraw fences, drawing to **inline SVG** (not an iframe, so the diagram reflows with the chat column and stays selectable). Delegates to `lib/excalidrawScene.ts`, which draws via **rough.js** — the same engine Excalidraw itself uses — and preserves each element's `seed` so a diagram is stable across re-renders instead of jittering on every paint. The renderer is pulled in with a dynamic `import()` so rough.js stays out of the entry chunk, matching the `MermaidBlock` precedent. Covers rectangle (incl. roundness)/diamond/ellipse/line/arrow/freedraw/text/image/frame; `isDeleted` and unknown element types are skipped so a scene from a newer Excalidraw degrades rather than fails. Read-only by design: no editor, and opening a scene never mutates it.
  - **Schema baseline** — matched against the Excalidraw scene format `version: 2` and the element set as of Excalidraw **0.18.x**. Fidelity is faithful, not pixel-exact: bound-text layout inside containers and arrow re-routing are approximated, and `embeddable`/`iframe` elements plus shape libraries are out of scope. Record the baseline here when the matched version moves, so a "diagram looks wrong" report has a diff to work from.
  - **Colours are canvas-relative, not theme-relative** — the scene is painted onto its own canvas (its `viewBackgroundColor`, else white) and rendered as a self-contained picture, the way an exported PNG would be. This is deliberate: scenes carry the author's explicit near-black `strokeColor`, so compositing onto the dashboard put a near-invisible diagram on the dark theme, while recolouring per theme would distort the author's palette (and inverting would invert embedded images too). An explicit `transparent` canvas is treated as unspecified, because a transparent canvas cannot guarantee legibility against an arbitrary surface. Consequently there is no `dark` render option and no theme state for the render effect to go stale against.
  - **Accessibility** — `role="img"` hides descendants from assistive tech, which would drop the diagram's own `<text>` labels (the only readable meaning a sketch carries). The graphic is therefore named with its joined text via `aria-label`; when a scene has no text the role is omitted entirely rather than shipping an unnamed image. `data-excalidraw-scene` is the stable hook for identifying a rendered diagram, since `role` is conditional.
  - **Failure** — malformed JSON, a truncated stream, or an empty scene renders a one-line explanation (`components.excalidrawBlock.render_failed`) above the source, muted and height-capped. Not danger-red and not full-bleed: scene JSON is hundreds of lines of machine data, so an unannotated red wall reads as a crash rather than a fallback. A diagram never costs the user the content.
  - **Security** — scene JSON is untrusted (model-generated or arbitrary file content). Embedded `files[].dataURL` is restricted to a **raster** `data:image/*;base64` allowlist (`svg+xml` deliberately excluded); colors are pattern-validated before reaching attributes; text is assigned via `textContent`. The SVG is built with `createElementNS`/`setAttribute` throughout, never string concatenation.
- `DiffBlock.tsx` — dedicated diff renderer with a single colored line-number gutter (adds/context show the new number, deletions the old; the number carries the add/del color and changed rows get a 2px inset edge bar — no `+`/`-` sign column). Raw `@@` hunk headers are not rendered: between hunks a slim "N unchanged lines" separator appears instead — a pill-shaped count bubble flanked by zigzag rules (`UnchangedSeparator.tsx`, shared; the zigzag is the `.zigzag-rule` CSS mask over `currentColor`, no SVG element in TSX) (`DiffLine.hidden`, computed by `parseDiffLines`; per-file state resets at `---`/`+++`/`diff --git` headers so multi-file patches never fabricate a cross-file separator; first hunk renders nothing). Unified and split views with forced line wrap (`whitespace-pre-wrap break-words` — these surfaces are width-constrained, so no horizontal scroll; the Monaco editor diff is the full-width surface), file meta headers, "Copy patch" button (raw patch, signs intact), provisional "generating diff…" indicator for incomplete streaming blocks. Supports both standard unified diff and kiro-cli `+N:`/`-N:` format. The same gutter treatment applies to the Changes panel's `PullRequestPanel.tsx` `DiffView` (shared `DIFF_NUM`/`DIFF_EDGE` classes from `diffUtils.ts`).
- `TypewriterText.tsx` — animated title reveal

**Syntax highlighting** — `highlight.js` (tree-shaken: js/ts/py/bash/json/yaml/html/css/sql/rust/java/md). Custom One Dark / One Light theme in `index.css` using design tokens. `hljs.highlight()` for known languages, `hljs.highlightAuto()` for unknown. Output sanitized via DOMPurify.

**Streaming rendering** — Two-layer architecture: (1) `useBlockAssembler` hook parses raw text into `ContentBlock[]` via state machine tracking `paragraph`/`fenced_code` states; (2) `BlockRenderer` dispatches each block to specialized renderers (`MarkdownBlock`, `CodeBlock`, `DiffBlock`, `MermaidBlock`, `ExcalidrawBlock`). During streaming (`streaming=true`), unclosed code fences produce blocks with `complete: false` that render as provisional views. Diagram blocks are held at the placeholder until the fence closes — a half-streamed Mermaid graph or Excalidraw scene is not yet valid, so drawing it would only flash the fallback. On completion (`streaming=false`), full reparse from content produces clean final blocks. `ChatMessage.rawText` preserves the original unprocessed text as source of truth for reparse.

**Security** — All `dangerouslySetInnerHTML` content sanitized via DOMPurify (`website/src/api/helpers.ts`):
- `md()` — renders markdown-like formatting (code blocks, bold, italic) + DOMPurify sanitize
- `sanitize()` — DOMPurify wrapper for pre-escaped HTML
- `esc()` — plain text HTML escaping
- CLI: `/etc/hosts` update uses `sudo tee -a` (not `sh -c echo`) to prevent shell injection

**State management** — Redux store with three slices:
- `dashboardSlice`: SSE/WS connection state, chat slots array, approval mode (synced from backend `yolo` field in status — no localStorage), optimistic slot add/remove reducers (`addSlotOptimistic`/`removeSlotOptimistic`), async thunks for slot fetch / approval mode change. YOLO state is backend-authoritative: `sseStatus` reducer syncs `approvalMode` from `status.yolo`; page load fetches `/api/status` immediately for instant sync.
- `chatSlice`: active slot, messages, session history (paginated), WS chunk/done handling (accumulate chunks into streaming, finalize on done), optimistic slot mutations, WS-ahead guard on `switchSlot.fulfilled`, async thunks for all slot/history CRUD. **Idempotent append**: a `chat_message` frame is dropped when the target list already holds a row with the same server row id (`meta.mid`, stamped by `_ChatSlot.append`) — `isRedeliveredMessage`, called from ONE chokepoint per path (`sseChatMessage` active slot, `applyNonActiveFrame` background pane) placed so it dominates every branch that creates OR MUTATES a row: the `tool` insert (which splices before a trailing `streaming` row), the `assistant` reconcile (which overwrites that row, so a late redelivery of an old frame would clobber a NEW segment's live content), the `user` echo reconcile, and the generic push. The `assistant` reconcile merges the frame's `meta` onto the finalized row, which is how a client-minted streaming row acquires its server id. A frame with NO `mid` is never deduped — client-minted rows have no server identity yet, and channel-replayed rows carry no `meta` at all — because declining renders a duplicate at worst while guessing would drop a real message. Identity is deliberately NOT (`ts`, role, content): a coarse OS clock stamps two same-tick appends identically (the collision `mergePreservedClientTs` pass 1 already guards) and two byte-identical messages are legitimate, so that tuple either misses a redelivery or silently discards a distinct row. `switchSlot.fulfilled`'s re-attach of a local trailing reply tests the id EXCLUSIVELY when the local reply has one — a content fallback running alongside it would let a stale snapshot row with identical text (a different row, different id) match and drop the newest reply. Content equality applies only to a reply with no id yet (one `_done` finalized without an `assistant` frame), which the server history cannot hold under a different id anyway. Preferring the id also survives the redaction asymmetry — that endpoint redacts on emit while the streamed copy is raw. Invariant: one persisted transcript row renders exactly once, no matter how many times its frame is delivered (the post-restart-reconnect duplicate-answer bug). Each drop increments the diagnostic counter `_redeliveredFramesDropped` (nothing renders it): the dedup makes at-least-once delivery invisible, and the duplicate bubbles were the only user-facing signal that something upstream re-emits frames after a restart, so a non-zero count is that signal — readable in a Redux state dump, 0 in steady state
- `notificationsSlice`: notification list with add/delete/clear/ack/unack, async thunks for fetch/delete/clear. `addNotification` deduplicates by `ts`. `ackNotification`/`unackNotification` are optimistic (`.pending` case) and awaited, so `.fulfilled` means the backend holds the write. WS events `notification_ack`/`notification_unack` sync ack state across tabs; `ackNotificationByTs("*")` handles bulk ack-all; `notifications_clear` (broadcast by the backend after a clear-all) dispatches `clearAllNotifications` so every view — not just the one that cleared — empties its list and the bell badge converges to 0. On WS reconnect, `fetchNotifications()` re-fetches to recover missed notifications during disconnect. **`fetchNotifications.fulfilled` merges rather than replaces**: membership, ordering and every non-ack field come from the response, but an item whose ack flag changed locally after the request began keeps the local flag. A monotonic `ackSeq` counts local ack-state changes (optimistic flip, its confirmation, and every ack/unack frame — including the view's echo of its own ack, since the backcast has no originator exclusion), `ackSeqByTs` records the `ackSeq` per item, and the thunk snapshots `ackSeq` at request start alongside `clearSeq`. Without this, a response rendered before an ack reverts it and the row reappears as unread. Stamps are dropped whenever their item leaves `items` (delete, WS remove, clear, ring-cap eviction, or the merge itself), so the map is bounded by `NOTIFICATIONS_RING_CAP`. **One rule governs every write to an item's ack flag**, whatever produced it: a response may apply only to an item whose stamp has not advanced since that request began. The fetch merge is one instance (snapshot = whole-state `ackSeq`); the ack/unack/ack-all confirmations are the other (snapshot = the item's own stamp, read after `pending` wrote it), which is what stops tab A's slow ack confirmation from overwriting a newer unack that already arrived from tab B, and stops an ack-all from marking a notification that arrived while it was in flight

**Real-time updates** — Single WebSocket at `/api/ws` (`useWebSocket` hook) multiplexes all events: `dashboard`, `slots`, `slot_title`, `notification`, `notification_ack`, `notification_unack`, `notifications_clear`, `refresh`, `chat_message`, `chat_chunk`, `chat_done`, `log`, `refine`, `sessions_restarting`, `heartbeat`, `tool_call`, `context_usage`. Exponential backoff reconnect (1s→2s→4s→max 10s); on reconnect re-fetches slots via Redux dispatch — **no page reload** unless the server `version` field in the `dashboard` status message changes (actual code update). This preserves unsent messages, scroll position, and form state across transient disconnects. `WsContext` provides log subscribe/unsubscribe to `LogsPage`. SSE survives only for logs (`useLogSSE` at `/api/logs`); the `/api/stream` route is still served but has no frontend consumer. Chat send uses `AbortController` with 10-second timeout — if the backend is busy starting kiro sessions, the fetch times out gracefully without showing an error (the message was received server-side; chunks arrive via WS when the session is ready).

**Routing** — `App.tsx` uses React Router `<Routes>` with paths: `/chat`, `/notifications`, `/overview`, `/worlds`, `/system`, `/capabilities`, `/projects`, `/logs`, `/hooks`. `/agents` and `/mc-agents` redirect to `/capabilities` (Agents is the panel's first tab). Default redirects to `/chat`. SPA fallback middleware in `server.py` catches 404s on non-API GET requests and serves `index.html`.

**Request a Feature** — The header action creates a chat slot and queues the self-contained feature-request seed through the slot's pending-context endpoint. It then sends and persists only the localized, user-facing request text. Pending context is consumed by that turn without becoming a transcript message, so internal workflow instructions never appear as user-authored history. Every drained frame also carries a silent-consumption contract line (`_CONTEXT_FRAME_CONTRACT` in `chat_runner.py`, between the `[Background context from "<source>"]` delimiter and the content): follow the block, never quote/echo/reveal it, reply only to the user's visible message. Without it the agent recited the injected workflow verbatim as its visible reply on every click (#4780) — the contract is part of the frame so every producer (app-kit inject, artifact companion, Slack thread backfill, feature-request) is covered without restating it per payload. The contract is best-effort model compliance, not a confidentiality boundary — a model can still ignore it, so pending-context payloads must never carry secrets or content that would be harmful if echoed. If context injection fails, the visible request still sends and the chat remains usable.

**Nav sidebar** — Collapsible: full mode (236px with labels) or icon-only mode (74px). The sidebar toggle lives in the rail's own top "menu row" (hamburger, plus a `panel-left-close` collapse control on the right while expanded) — not in the topbar, which shows only the brand. State persisted in `localStorage('mc-nav')`. Three vertically-stacked regions: (1) **top-fixed** — menu row, then Sessions (the chat surface, formerly labeled "Chat"), Schedule, Artifacts, Knowledge, then an **Apps** section header whose right side is an accent-colored **Explore** link (lucide `layout-grid`) to the App Store at `/apps` (the store's surface is `hiddenFromNav`; the link carries the `data-onboarding-nav="apps"` anchor); (2) **Apps frame** — the enabled-apps list scrolls in its own frame (`flex-1 min-h-0 overflow-y-auto`) so many apps never push the pinned sections; drag-reorderable via dnd-kit sortable (`SortableAppNavRow` + `DndContext`/`SortableContext`/`DragOverlay`, `MouseSensor` 8px distance + `TouchSensor` press-and-hold so a plain click still navigates): rows reflow to open a gap as one is dragged, the source dims, and a `DragOverlay` ghost follows the cursor; order persists to `localStorage('mc-app-nav-order')` (`arrayMove`); reorder is scoped to the currently visible Apps rows (the "N more" overflow collapse hides the rest); (3) **bottom-fixed** — Agent Capabilities, Developer (dev mode only), Settings, and a **Contact Us** row with icon links to kiro.dev, the GitHub repo, and the Discord community (folds away — `max-h-0` + `inert` — while collapsed). Collapsed mode renders icon-only rows with portaled hover labels; the Explore row fades in and slides up into place. The former Shortcuts nav row moved to **Settings → Shortcuts** (`ShortcutsPanel`, sharing `ShortcutsContent` with the Alt+K `ShortcutsModal`; tab sits above Developer).

**Preview-gated surfaces** — a registry entry may carry `previewFlag`, the localStorage key of a per-device opt-in (`utils/previewFlags.ts`, all keys prefixed `mc-preview-`). While the flag is off the surface is not advertised **anywhere**. The safe list has a name: consumers read **`getAdvertisedSurfaces()`** (= `getBuiltinSurfaces()` minus gated entries) rather than filtering per call site, because a call site that reaches for the unfiltered list and forgets the filter leaks an unreleased surface silently. `App.tsx` derives `advertisedNavItems` once and feeds BOTH rail list paths (Main and the Apps group) from it; the Search Everywhere Pages provider reads the same accessor. The bottom-fixed rows are looked up by id (`settings`, `capabilities`) and are core surfaces that are never gated. Gated surfaces deliberately REMAIN in `getBuiltinSurfaces()` so registry-wide invariants (every surface carries a translatable `labelKey`) still cover them. Their route stays registered, which is what makes the surface reachable once the flag is on. The flags are listed as one card per feature on **Developer → Feature Previews** (itself behind Developer Mode; the pane is `FeaturePreviewsTab`, which keeps the registry's "surface" vocabulary in code while its copy says "features" and "pages"), which fires `mc-preview-flag-changed`; `usePreviewFlagRevision()` turns that into a revision number the rail uses both to re-render and as a memo dep, so the row appears without a reload. A cross-tab `storage` event on any `mc-preview-` key does the same. Two occupants today: `/webhooks` (the endpoint is supported, the page is not finished) and **crew** — `PREVIEW_CREW` gates BOTH doors into that feature, the `/members` Crew Members rail item and the sidebar create-menu's "New Crew Mode chat" entry, from one card. Crew is the case a `previewFlag` alone does not cover, in two ways. A create-menu item is not a surface, so `ChatSidebar` reads the flag directly through `usePreviewFlag`. And the **browser-tab attention count** reads the registry directly rather than `getAdvertisedSurfaces()`, so `selectAllSurfacesAttention` applies the predicate itself — the tab title is an advertisement too, and a gated surface that still contributed would show a `(1)` with no rail row to trace it to. `hiddenFromNav` is the deliberate opposite there: it keeps contributing, because it IS advertised, just on the topbar bell rather than the rail. The gate is on the INGRESS only: a session already in crew mode keeps its `Crew` row badge and keeps running, and `/members` stays routable, so turning the flag off never orphans existing work. Unlike Webhooks, the crew surface is NOT also `hiddenFromNav`, because the rail is where it belongs once released — so dropping its `previewFlag` (plus the `ChatSidebar` read and the card) IS the release. Retiring a gate is deleting the `previewFlag` and the feature's card. This is distinct from `hiddenFromNav`, which is permanent and means "rendered elsewhere" (the App Store's Explore link, the topbar notifications bell).

**Agent monitor — `Ctrl+G`** (`useKeyboardShortcuts.ts`, predicate `isAgentMonitorChord`): opens the **Subagents** activity tab (`openActivityToTab('subagents')`) and routes to `/chat`, since the activity panel is owned by the chat page. This is the one chord that is **literal Ctrl on every platform** rather than ⌘-on-Mac: the kiro-cli backend emits `Press ctrl+g to monitor progress.` into its crew-pipeline tool result, that string lives inside the backend binary and cannot be re-worded per OS, so the chord the user is told to press must be the chord that fires (on macOS find-next is ⌘G, leaving ⌃G free). It requires exactly one primary modifier and no Alt/Shift, and deliberately fires **inside text fields** — the hint is read while a crew is running and focus is normally in the composer, so an input bail-out would make it dead exactly when it is needed. It is skipped for `.xterm` targets, where Ctrl+G is BEL and belongs to the PTY. Because the branch requires `ctrlKey && !altKey`, `KeyG` is deliberately **not** added to `RESERVED_PANEL_CODES`: it cannot shadow a downstream Alt+G panel registration, so reserving it would over-claim the panel-navigation extension seam.

**Panel toggles — user-rebindable, one id per panel** (`lib/panelToggleShortcuts.ts`): four ids — `left-sidebar`, `session-panel`, `side-panel`, `terminal` — each carrying a `Chord` whose `mod` resolves to Cmd on macOS and Ctrl elsewhere. Defaults: session list `mod+B`, activity/side panel `mod+\`, while the nav rail **and** the docked terminal both ship **unbound** (`null`) for the user to opt into. `localStorage('mc-panel-toggle-shortcuts')` holds **overrides only**, so a code default reaches every user who never touched it, while an explicit `null` is a deliberate "cleared to unbound" the loader must preserve rather than collapse back to the default; a write broadcasts `mc-panel-toggle-shortcuts-changed` so the live keydown handler, the Alt+K reference and the Settings → Shortcuts rows re-read without a reload. An unbound id renders its row with the `unset` ("Not set") state rather than being hidden, so it stays discoverable. The terminal's row is hidden when `dashboard.terminal.enabled` is false, matching the nav rail. **Why the terminal alone has no proposed default**: its chord is on the skip-shell allowlist below, so by construction it is taken from the PTY — meaning any default spends one of the user's shell keystrokes for them. `mod+J`, the natural pick (VS Code's Toggle Panel, which HOSTS its integrated terminal), is `^J` on Windows/Linux, i.e. readline's `accept-line`, so a user pressing it instead of Enter would close the panel mid-command; VS Code's terminal chord proper is literal `Ctrl+`` on every platform, unrepresentable in this `Chord` model (`mod` is Cmd on macOS by definition, and ⌘` is the macOS window cycler). Widening the chord model for one binding was rejected, and so was choosing a shell keystroke on every user's behalf — the binding is left to whoever wants it. **Skip-the-shell allowlist**: these chords otherwise yield to any `.xterm` target, because a keystroke aimed at a shell belongs to the shell — but a terminal toggle that yields would open the panel, focus its own shell, and then be unable to close it. `PANEL_TOGGLES_SKIPPING_SHELL` names the ids exempt from that yield (currently just `terminal`), mirroring VS Code's `terminal.integrated.commandsToSkipShell` as a per-COMMAND allowlist rather than a per-key rule, so a future panel with the same need joins a set instead of adding a second special case. The allowlist is honoured by a **capture-phase** `keydown` listener scoped to terminal targets, not by the bubble-phase dispatch: a control-character chord such as `Ctrl+J` is consumed by xterm, so a bubble listener never runs — the same structural point VS Code makes by consulting its own list inside the terminal's key handler. That listener calls `preventDefault` + `stopPropagation`, so the PTY never receives the keystroke; everywhere outside a terminal the bubble path still owns these chords, leaving `defaultPrevented` deference and handler ordering untouched. A panel whose action is absent — terminal disabled, or a popout/embed with no such panel — is treated as unbound on **both** seams, so the chord falls through untouched instead of being swallowed.

**Session titles** — auto-generated after a few turns via background LLM call in `chat_title.py` (`_maybe_auto_title`), pushed to all clients via `slot_title` WS/SSE event, persisted in chat history JSONL metadata via `ConversationLog.update_metadata()` together with `title_origin` (`auto` = background titler, `user` = manual rename; a legacy title with no stored origin rehydrates as `user`). Title input scans reserve a bounded allowance for the dashboard's 20-file upload limit, remove generated image/file references, and then cap retained user text before prompting or fallback selection. Non-image metadata is validated, length-limited, and stored in token-index order so each generated reference resolves directly without scanning every path. Manual trigger via `POST /api/chat/slots/{slot}/generate-title` — it prompts from the *last* `_TITLE_PROMPT_WINDOW` conversational (user/assistant) messages (the user regenerates because the name no longer fits, so the prompt must reflect the current topic, not the opening turns that initial titling reads; filtering before slicing keeps a tool-heavy tail from starving the window); generation errors use the same sanitized first-user-message fallback, while attachment-only placeholder results remain untitled so a later automatic attempt can retry. Cancellation releases the in-flight guard without starting a pending retry. Max 5 auto-title attempts before falling back to the first usable user message. **Background title refresh** (`chat_title.py:maybe_refresh_title`, fired from `chat_done` for already-titled slots): an `auto`-origin title is re-examined at the user-turn milestones in `_TITLE_REFRESH_MILESTONES` (8 and 24) through the same `_bg` one-liner path, with a `KEEP` reply leaving the name alone; each milestone fires at most once (attempt-counted — KEEP/error consumes it) and the consumed mark is persisted as `title_refresh_mark` so restarts cannot re-spend the budget. A manual rename records origin `user`, which locks the refresh out permanently, and bumps `_title_epoch` so an in-flight background attempt stands down instead of clobbering the rename (the reveal animation is cosmetic-only and never assigns `slot.title`). The generated name follows the **workspace UI language** (`dashboard.language`) rather than the conversation's: `_ui_language()` resolves the tag via `context.ui_language_tag()` and `_build_title_prompt()` interpolates a language directive outside the delimited transcript, with `""` (auto) omitting it and leaving the prompt byte-identical — see `config.md` § Dashboard UI language for the prose-guard and reveal-animation consequences of naming in an unspaced script.

### UI Pages

- **Chat** (`/chat`, default) — multi-session parallel chat, Slack-style grouped messages with timestamps (MMM DD, YYYY, HH:MM), KiroCrew logo as assistant avatar, full Markdown rendering via `react-markdown` + `remark-gfm` + `rehype-raw` with Mermaid and Excalidraw diagram support (```mermaid and ```excalidraw fences both render as inline SVG in the message flow; `.excalidraw` scene files opened in the file panel render as diagrams rather than raw JSON), syntax-highlighted code blocks (highlight.js), and clickable file paths (inline `<code>` containing paths → reveal in Finder via `/api/reveal`), session sidebar with titles and scroll-shadow panels (notifications moved to dedicated page), collapsible history section (default collapsed) with source tags (🖥 dashboard / 💬 slack) and creation dates, session delete from history, `EmptyState` component when no session is active. The tabbed SidePanel includes a singleton Changes view when a conversation contains supported PR/MR URLs; it renders provider metadata, lazily expanded diffs, review discussions, and CI checks, while sidebar source chips show provider/number plus cached state and CI. A chip's CI rollup is suppressed once its lifecycle state is **terminal** (`merged` or `closed`) — the lifecycle glyph is the only meaningful signal there, and a closed pull request's rollup can stay pending indefinitely (GitHub parks fork-PR checks in `PENDING`/`ACTION_REQUIRED` when the PR is closed before a maintainer approves the run), so a chip that still rendered CI would spin a "checks running" spinner forever on work nobody is waiting for. The sidebar chip (`ChatSidebar.tsx::showsChipCi`) and the panel's source-strip tab (`PullRequestPanel.tsx::SourceTabState`) apply the SAME rule; an absent `state` is not terminal (the provider status has not been read yet), so such a chip still renders CI. For open non-draft sources the panel header surfaces a merge-blocker banner derived from the normalized merge-state fields (allow-list gated on the raw provider state being `open` (GitHub) or `opened` (GitLab), so merged/closed/locked/other states never banner): `mergeable === 'conflicting'` renders a danger "Merge conflicts" banner whose "Add to chat" handoff asks the agent to resolve conflicts — preferring a merge of base into head on shared branches and reserving rebase + `--force-with-lease` for unshared ones; `mergeStateStatus === 'need_rebase'` (GitLab fast-forward-only projects) renders a warning "Rebase required" banner whose handoff explains that a merge commit cannot unblock the MR and asks the agent to coordinate before rewriting a shared branch, rebasing with `--force-with-lease` otherwise; `mergeStateStatus === 'behind'` renders a warning "Branch is behind" banner whose handoff prescribes a no-history-rewrite update (merge base into head or the provider's update-branch affordance, never an unprompted force-push); and `mergeStateStatus === 'blocked'` renders a warning "Merge blocked" banner for branch-protection gates (human-actionable, no handoff). The panel's own payload is pinned (`staleTime: Infinity`) and never refetches on its own, so a merge state that settles *after* the payload loaded is carried by the server-driven chip↔payload invalidation protocol rather than by any client-side comparison: the merge pair is part of both the chip-status projection and the `source_status` delta, so a chip refresh that observes a changed merge pair drops the full payload server-side and notifies owner dashboards, and every window refetches. Each merge field is recorded into the chip entry only once it is *real* — an unanswered field is omitted rather than written as `unknown` — and the two fields are recorded independently, because GitLab settles `need_rebase` and its branch-protection gates in the detail field while `mergeable` stays `unknown`; dropping the detail because its sibling is unknown would leave exactly those banners invisible. Omission alone is not enough for "still computing" to be harmless, though: every writer replaces the chip entry WHOLESALE, so an omitted field would ERASE a settled one rather than read as "no news" — and because both providers evaluate lazily, an unsettled read of an already-known conflict is the common case, not a rare one. Both writers (chip refresh and full-payload write-through) therefore carry a settled merge field forward when the fresh read has no answer, the same keep-known rule already applied to the `ci` glyph. A real answer always wins, including one that CHANGES the value, so the carry only fills a gap and cannot pin a stale verdict; it stops once the source leaves an open state, where the pair is meaningless and permanently unanswered. The full-payload projection (`status_from_full_payload`) must therefore project the merge pair too: a field the chip path records but the full-payload path omits would re-appear as a changed transition on every chip refresh and spin the invalidation loop, which the flap damper (`_CHECK_FLAP_DAMP_THRESHOLD`) bounds structurally. Without this the conflict banner appeared only once the user hit refresh. Frontend discovery ignores every `chunk`/`streaming` message regardless of its array position and publishes a URL only after that message becomes durable, so appended thinking/tool/stop events cannot settle a numeric URL that an earlier stream is still extending. Extraction, incremental index state, and `PullRequestPanel` rendering preserve first-seen order while enforcing the same 64-source cap. **First-mention attribution** decides Changes vs Resources: a PR/MR becomes a Changes *source* only when its FIRST durable mention was agent-authored (assistant / tool / thinking output — e.g. a `gh pr create` result). A PR whose first mention was a **user** message is treated as a referenced link and surfaces in the Files-tab **Resources** list instead — it never auto-opens the Changes tab or fires a new-source notification. Because the dedup map records the first mention, a later agent echo of a user-referenced PR cannot reclassify it as a Change (and a later user reference cannot demote an agent-surfaced one). The 64-source cap is applied **per role**, so a flood of user-referenced links cannot starve agent Changes sources; user-first links are retained (bounded) solely for echo suppression and are excluded from the emitted Changes sources. The emitted `PullRequestLink` shape is unchanged, so backend source APIs and the Changes panel are unaffected. **Files-tab inline file preview**: opening a file from the SidePanel **Files** tab shows it **inline** (the file list is replaced by the file's content plus a "Back to files" bar) via the shared `MarkdownPanel` editor — the same view / Edit-Preview toggle / dirty tracking / Save / discard-guard used by document tabs — instead of spawning a separate document tab. The inline working copy lives in a module-level draft store (`usePanelTabs`, keyed by slot + path — the same file edited in two chat slots keeps independent drafts; `usePanelTabs` owns the key format) that sits ABOVE the SidePanel subtree, so an in-progress edit survives everything that unmounts the panel (close control, activity-tab switch, chat-slot switch, and the automatic force-collapse on window resize) and is restored on reopen; it is in-memory (parity with document-tab content, which is likewise stripped on persist and re-read on reload) and is cleared on save and on explicit discard. Exactly **one editor per path** is enforced at open time (there is no runtime "yield" of an already-open editor): opening a path from the Files list that is already open as a `file:${path}` document tab focuses that tab instead of opening a second inline editor, and a chat-link open of a path already open inline routes back to the inline editor (`ChatPage.handleFileOpen`) rather than spawning a document tab. A failed read (HTTP error or network-level rejection) renders a retryable error rather than an editable placeholder (so a save can't overwrite the file with empty/placeholder content), and a successful save refreshes the shared `['file-read', path]` cache so a reopen never seeds stale pre-save content. Per-slot seen-source state is also mirrored to a quota-safe, globally bounded localStorage record, so route remounts and page reloads do not reinterpret historical links as new sources or override a persisted panel dismissal. During uncached slot switches or reloads, source reconciliation preserves the persisted Changes tab while `slotLoading` is true and only closes it after hydrated history confirms that the slot has no supported source URLs. Chat uses WS for streaming (`?ws=1` mode): POST returns immediately, chunks arrive via WebSocket. Auto-approved tool calls broadcast via WS as ephemeral cards (not persisted to messages), inserted before streaming message in Virtuoso list. **Agent selection**: WelcomeView (pre-first-message) has agent picker that sets `pendingAgent` state — on first send, slot is created with that agent via `POST /api/chat/slots {agent}`. Agent selector dropdown also in top bar next to session title for mid-session switching. Agent badge (aim-colored pill) in sidebar slot list. **MCP info button** shows per-agent MCP servers: non-kirocrew agents show only their own MCPs from agent config; kirocrew shows all global MCPs. **Tool/approval payload viewer** (`ToolDetails`, website SPA): tool-call and pending-approval cards render the payload with a **Raw / Formatted** toggle (beside the Input/Output control, shown only for JSON-ish payloads where the two modes differ). Formatted renders the parsed JSON object as a key→value table — multi-line command values show real line breaks and quotes (JSON-decoded), with bash syntax highlighting on command-bearing keys (`command`/`cmd`/`script`/`shell`/`bash`) via the shared worker highlighter; Raw shows the exact verbatim payload with escaping intact (and is the fallback for truncated/streaming or non-object payloads).
- **Notifications** (`/notifications`) — dedicated page with left/right split layout. Left: category tabs (All/Cron/Hooks/Heartbeat/Agent/Approval/Subagent/Tasks), search filter, date-grouped list (Today/Yesterday/This Week/Older). Right: detail panel with source label, full timestamp, Read/Unread badge, markdown-rendered body, and jump-to-source buttons. Jump logic: `slot` meta → "💬 Go to Chat" (active tab) or "💬 Resume Chat" (from history); `slack_link` meta → "💬 Open in Slack" (deep link); `task_id` → "💬 Continue in Chat"; `job_id` → "⏰ View Cron Jobs". Cron notifications have `CronAckBar` for acknowledge/delete. Notification meta includes `slot` (subagent/heartbeat from dashboard), `slack_link` (subagent from Slack), `session_key` (webhook), `job_id` (cron), `task_id` (task runner). `_notif_meta()` helper on `GatewayOrchestrator` builds meta from `parent_key`. StatCard row: Total/Unread/Cron/Hooks/Heartbeat. Nav badge shows unread count.
- **Overview** (`/overview`) — `StatCard` components (with skeleton loading) + tabbed management console:
  - **Memory tab**: editable preferences.md / projects.md with Save buttons, read-only daily history. **Memory Graph Explorer**: sigma.js (WebGL) visualization of memory relationships (nodes = memory entries color-coded by group; edges link entries to the projects they mention). Node positions come from a one-shot client-side d3-force layout (time-bounded, then stopped — no live physics solver); the server sends only nodes/edges.
  - **Cron tab**: add job form (with shared `AgentSelector` component) + striped job table with Pause/Resume/Delete actions
  - **Lessons tab**: add lesson form + lesson table with Delete actions
  - **Skills tab (CRUD)**: + New button with create form (name + SKILL.md editor), installed skill list with click-to-view, ✏ Edit button with inline textarea editor + Save, ✕ Delete with confirmation, name sanitized to lowercase + hyphens. AIM Skills section shows skills from `~/.aim/` grouped by package with Uninstall button per package. Skills are fully AIM-managed — no bundled skills; `AIPowerUserCapabilities` installed by default via setup/update.
  - **MCP Servers tab**: Controls `~/.kiro/settings/mcp.json` (global config that kiro-cli ACP loads at runtime). Server-level enable/disable sets `disabled: true/false` in global config and syncs `@server` to kirocrew.json `tools`/`allowedTools`. Per-tool enable/disable sets `disabledTools` array in global config. Probe All discovers tools per server, preserves enabled/disabledTools state across probes. Enable All / Disable All bulk buttons. Tool chips: green = enabled (clickable to disable), strikethrough = disabled (clickable to enable). Apply & Restart at top bar resets all active sessions. Live server badges (🔌 color-coded by status).
  - **Agent Config tab**: JSON editor with Save + warning about `kirocrew setup --agent-only`
- **Settings → Voice** (`/settings/voice`) — the Speech-to-Text card (`website/src/pages/settings/SttSettings.tsx`, rendered inside `VoicePanel.tsx`, so voice-in and voice-out settings share one tab). Controls: enabled, provider (`local` / `apple` / `transcribe`), model, language, and the dictation knobs `streaming` / `silence_ms` / `partial_interval_ms` / `idle_evict_secs` / `endpointing` / `dictation_panel`. **Nothing in the offered set is hardcoded in the frontend**: `providers`, `models`, `language_codes` and `streaming_providers` all come from `GET /api/config/stt`, because what a host can run differs per host (`_stt_providers()` omits `apple` entirely where macOS is too old or the Swift toolchain is absent, rather than offering an option that cannot be selected) and it is the single source of truth for what GET advertises AND what PUT accepts. `streaming_providers` is served from `stt_stream._STREAMING_PROVIDERS` so the streaming controls gate on a CAPABILITY rather than on a provider name; a hardcoded copy goes stale in the permissive direction, offering a provider this host cannot run or hiding a control for one it can. PUT applies **floors only** on the three millisecond/second knobs, each floor owned by the module that reads the value (`vad.MIN_SILENCE_MS`), because a value above the detector's own cap is inert rather than unsafe; a `bool` is rejected explicitly since it subclasses `int` and a checkbox value would otherwise persist as `1`.
  - **Runtime state is a different endpoint from settings.** `GET /api/stt/status` serves what a panel polls: the availability reason as a `code` plus advisory prose (`transcribe.availability_detail()`), the resolved model with `model_present` and its byte size, `engine_loaded` (whether a model is resident, which is the difference between a ~30 ms transcription and one that pays a load first), and the live `download` state. `POST /api/stt/prepare` starts **or joins** the one-time model download and answers 202 with the current transfer state; concurrent callers share one transfer because the model store serialises them behind its own lock, so the in-flight check is only there to stop a polling panel accumulating tasks. `POST /api/stt/prewarm` answers 202 and loads plus warms the recogniser when the user REACHES for the mic rather than when they release it: a first-ever load compiles a GPU pipeline (measured at 7.4 s) and the first decode after any load allocates its graph (154-528 ms), so both are paid while the user is still speaking.
  - **Those four are dashboard-only.** `_deny_app_token` refuses an app token on `status` / `prepare` / `prewarm` / `ffmpeg/download`: `request["user"]` is truthy for an app token too, so a cookie check alone does not separate a browser from an app that named the path in its manifest, and these start a download and warm a resident model inside the gateway, which is operator setup rather than something an app earns by naming a path. It requires `request["app"] == ""`, so an ABSENT key is refused along with a non-empty one and an unauthenticated route can only fail closed. The transcription surfaces (`GET /api/ws/stt`, `POST /api/stt/transcribe`) are deliberately NOT gated this way, so a shipped app can still transcribe.
  - **There is no dependency-install button.** Desktop releases already carry the recognizer and pinned `imageio-ffmpeg` decoder; the only user action is selecting an on-demand model and clicking **Download now**. A source install can need a `voice`-extra command in the gateway's OWN interpreter plus a system FFmpeg command for compressed input; project-venv binaries are not trusted executable storage. These commands are suppressed where no channel into this interpreter exists (frozen build, code-signed app bundle, pip-less or PEP 668 externally-managed python): `transcribe_unsupported` makes the page show an unsupported notice instead of a command that cannot succeed. `bundled_interpreter` also prevents any Homebrew/Winget/Apt decoder advice inside a desktop app. A platform with no prebuilt recognizer wheel is not pip-actionable; the availability `detail` on `/api/stt/status` names it. `ffmpeg_missing` remains independent of `available` for diagnostics, because a corrupt payload can leave a provider ready while an uploaded WebM cannot be decoded. The decoder itself is no longer a shell command on a host whose distribution packages no ffmpeg: `GET /api/stt/status` carries an `ffmpeg` object (`present`, `source` = bundled|system|store, `auto_fetch` = available|unsupported|bundled, the gateway's `os`/`arch`, and the live `download`), `POST /api/stt/ffmpeg/download` fetches the pinned upstream executable into the digest-verified store at `<data home>/models/ffmpeg/`, and `_ffmpeg_install_commands` returns `[]` rather than an `echo` of the ffmpeg.org URL when nothing is actionable. On a fetch failure the page hands the failure to a chat session through the existing prefill hand-off; see [stt-streaming](../features/stt-streaming.md) for the trust model, which the store does not widen. `transcribe` additionally renders `AwsConsentGate service="transcribe"`, which is the only way the paid-service grant is recorded (deliberately no CLI verb).
  - Endpoints: `GET/PUT /api/config/stt`, `GET /api/stt/status`, `POST /api/stt/prepare`, `POST /api/stt/prewarm`, `POST /api/stt/ffmpeg/download`, `POST /api/stt/transcribe`, `GET /api/ws/stt`. The live dictation surface these settings drive is specified in [stt-streaming](../features/stt-streaming.md).
- **System** (`/system`) — live metrics (1s refresh): CPU %, memory used/total, network RX/TX stat cards; host info with correct Apple Silicon arch detection, load averages; memory, process, network, storage detail cards; uptime ticking every 1s via `useUptime` hook (client-side from `start_time`)
- **Agent Capabilities** (`/capabilities`; `/agents` redirects here) — merged Agents + Capabilities destination, bottom-pinned in the nav. `SidePanelLayout` tabs in order: **Agents** (agent → workspace → memory store bindings, `KiroCrewAgentsPage` embedded), **Agent Templates** (installed agent configurations/packages, `AgentsPage` embedded), **Integrations (MCP)**, **Skills**, **Hooks**, **Prompts**. The templates tab keeps the side-by-side layout: installed agents list (left) with detail panel (right, height from `LAYOUT.AGENT_LIST_HEIGHT`). Installed agents card shows each agent with name, `SourceBadge` source pill (neutral `package`/`kirocrew`/`project`/`local` sources — the former `aim` source was generalized to `package`), model, description, skill count, MCP server count. Click to view full agent config in detail panel: system prompt, tools, auto-approved tools, MCP servers with `--aim` token colors (hover tooltip showing tool list), expandable denied commands list (`<details>` with all patterns). **Agent-package install / uninstall now routes through the capability seam** (`installPlugin`/`uninstallPlugin` in `providers/adapters/acp.ts` dispatch `type === 'agent'` to `/api/capability/agents/{install,uninstall}`). This replaced the earlier "intentionally NOT offered" stance: the pure-AIM `/api/aim/agents/*` + `/api/aim/update` routes were removed, but leaving `GET /api/capability/agents` as list-only meant the seam could SHOW installed agent packages and never manage them — an asymmetry with skills/MCP that forced an edition to shadow the core or mount its own routes. Bulk "Update All" remains unoffered (no `update_*` op on the seam). Skills/MCP install/uninstall still route through the capability seam (see Capability Integration); the manager returns human-friendly errors for invalid packages. The MCP registry browser is hidden entirely when no external capability manager is configured (registry → 503); when present it offers click-to-expand descriptions with all detail lines, clickable URLs (DOMPurify-sanitized), a direct Install button, and tier badges at `text-[11px]` minimum. Striped subagent table with `EmptyState` when empty, kiro credit usage card, context window usage bars per session (agent name in `--aim` color).
- **Tasks** (`/tasks`) — redirects to `/projects`
- **Projects** (`/projects`) — autonomous multi-step task execution with left/right split layout (260px sidebar + detail/compose area). Sidebar shows compact project cards with status icons, progress bars, cancel/delete buttons, and "＋ New Project" button. Compose area has two modes: ✨ Compose (free-text with refine-to-spec) and 📄 From Spec (paste/upload). Shared `AgentSelector` for agent selection. Plan generation with cancel, auto-polling for planned runs. Selected project detail view (`ProjectDetailPage`) with Idea/Tasks tabs: Idea tab shows spec content read-only with "Edit in Chat" button; Tasks tab has DAG/Phased view toggle. **🎮 button** (right-aligned in tab bar) opens pixel-art office animation modal (`PixelCanvasWidget`) showing character sprites working at desks based on task status. Action buttons: Execute/Chat/Discard (planned), Cancel (running), Restart/Schedule (completed/failed). Execute stays on project (no navigation). `SubAgentActivity` table shown below running projects. `ProjectAnimation` shown in empty compose state. Session storage persistence for mode, input, spec text, and planning state. 3-second auto-refresh polling.
- **Hooks** (`/hooks`) — script hook management following standard page layout: `PageHeader` + `StatCard` row (Total, Enabled, Total Runs, Errors) + `Card`/`CardTitle`/`InfoTip` wrapping a `table-striped` hooks table with `SearchInput` filter. Toggle switches for enable/disable, `Badge` status pills (ok/err/warn), `Btn` actions (▶ Test, Edit, ✕ Delete). Create/edit form uses `Card` wrapper with `Input`, styled `select` for event type, `SendBtn` for save. Test results shown in card-like panel below table with `Badge` exit status and dismissible stdout/stderr output.
- **Logs** (`/logs`) — live gateway log stream via WebSocket (subscribe/unsubscribe via `WsContext`), server-side log level control (DEBUG/INFO/WARNING/ERROR buttons). SSE (`/api/logs`) remains as a secondary transport.
- **Worlds** (`/worlds`) — agent world scenes with themed 3D-style environments (neural, wizard, underwater). Decorative page for visual personality.

### Agent Selector Component

Shared `AgentSelector` component (`website/src/components/AgentSelector.tsx`) used by Chat, Tasks, and Cron:
- `createPortal` renders to `document.body` — escapes `overflow` clipping
- `fixed` positioning with viewport-aware placement (flips up if overflows bottom, aligns right edge)
- `z-[9999]`, `max-w-[340px]`, agent name truncation for long names
- `AimBadge` source pills (aim=purple, kirocrew=amber, builtin=gray) using `--aim` design token
- ARIA: `role="listbox"`, `role="option"`, `aria-selected`, `aria-expanded`, `aria-haspopup`
- Outside-click-to-close via `setTimeout` + `document.addEventListener`
- Props: `agents`, `value`, `onChange`, `exclude` (filter out agent names)
- Agent list refreshes on `refreshTrigger` (WebSocket-driven after AIM mutations)

### InfoTip Component

Reusable `?` button (`website/src/components/InfoTip.tsx`) for contextual help across all pages:
- Portal-rendered to `document.body` — escapes `overflow: hidden` on `card-glow` parents
- `fixed` positioning with viewport-aware placement
- Solid background (`var(--card)`), strong shadow, `z-[9999]`
- Click to toggle, outside-click to close
- Used on: Sessions (Chat), Preferences/Lessons/Cron/Skills/MCP (Overview), AIM/Agents/Context/Usage (Agents), Task Runner (Tasks), Process (System)

### MCP Info Button (Chat)

The ℹ button next to chat session titles shows MCP servers for the current agent:
- Calls `GET /api/mcp/active?agent=<name>` — for non-kirocrew agents, reads from agent config in `~/.kiro/agents/`; for kirocrew, reads from global `~/.kiro/settings/mcp.json`
- Per-agent MCP scoping works: `kiro-cli acp --agent <name>` loads only that agent's `mcpServers`
- Footer note explains scoping: custom agents load only their own MCPs; kirocrew loads all global MCPs
- Visual: green dot = enabled, gray dot + "disabled" label = disabled
- Count shows `enabled/total`

### MCP Global Config (`~/.kiro/settings/mcp.json`)

For the default kirocrew agent, kiro-cli ACP loads MCP servers from the global config. For non-kirocrew agents (e.g. AIM-installed agents), kiro-cli loads only the `mcpServers` defined in the agent's own config file in `~/.kiro/agents/`. The dashboard MCP tab controls the global config used by kirocrew:
- `disabled: true` on a server prevents kiro-cli from loading it (kirocrew only)
- `disabledTools: [...]` on a server prevents specific tools from being registered (kirocrew only)
- `kirocrew-cron` and `kirocrew-core` are synced to global mcp.json at gateway startup (`_sync_kirocrew_mcps_to_global`)

#### `kirocrew-cron` Notable Parameters

- `cron_add`: accepts optional `silent` (boolean), `hide_in_chat` (boolean, keeps the cron out of the active session list — result still goes to Slack/bell + History), `script` (Python callable path), `command` (shell command — mutually exclusive with `script`), `timeout` (seconds), `strict_schedule` (boolean, disables jitter), `timezone` (IANA name), `skip_dates` (list of YYYY-MM-DD strings). When `silent=true`, results are not auto-delivered — the agent decides when to notify via `send_message`. When `script` or `command` is set, the job bypasses the LLM entirely (deterministic execution).
- `cron_update`: accepts `agent_id`, `timezone`, `skip_dates`, `strict_schedule`, `hide_in_chat` in addition to existing fields.
- `cron_trigger`: on-demand execution of a specific job by ID. Returns `{ok, name}`. Non-blocking (fires via `create_task`).
- `cron_list`: returns a compact one-line-per-job summary by default — id, name, status, schedule, next-run, kind (`script`/`command`/`agent`), optional `agent` / `channel` / `last=<status>` / `err=<preview>` / `result=<preview>` extras, message preview (<=80 chars), last_error preview (<=200 chars), last_result preview (<=120 chars). Full bodies are intentionally omitted so the response stays under ~30 KB for 50-job registries (the LLM tool-call budget would otherwise drop calls on large registries — `_render_cron_list_compact` in `mcp_cron.py`). Callers opt back into the legacy multi-line format with `verbose: true` (regression-safe — byte-identical to the pre-change shape, including the `[kind]` tag and `last error` / `last result` lines), or pass `ids: ["<job_id>", ...]` to drill into specific jobs and receive full bodies for matches only (`ids` takes precedence over `verbose`). Both modes go through `redact_credentials` / `redact_exfiltration_urls` on every user-supplied string. Sanitize-then-truncate ordering is enforced for the message, last_error, and last_result previews so a credential straddling the truncation boundary cannot leak as a partial fragment. Schema: `validation.CRON_LIST_SCHEMA` enforces `_JOB_ID_RE` on `ids` items (max 200) and rejects non-bool `verbose`.
- Auto-inject: when `persistent_session=True` (and `hide_in_chat=False`), cron results are auto-injected into a linked dashboard chat slot (`cron-{job_id}`). The slot is auto-created on first delivery for persistent crons — no user action required. Dashboard notifications include `meta.slot` for frontend "Continue session" navigation. For dedup-suppressed and silent runs, injection only occurs if the slot already exists (avoids creating slots for suppressed output). When `hide_in_chat=True`, all three injection call sites are skipped, so no slot is created and the notification CTA shows "View last result" (no-slot branch) instead.
- Injection is synchronous and hydrates a newly linked slot from the `cron:{job_id}` transcript, which means parsing the whole file. Its `history` parameter is therefore **required, with no default**: every caller is async, and a default would let one hand that parse back to the event loop. `handlers/cron.py` prefetches through `asyncio.to_thread`; the gateway's dedup-suppressed and silent paths use `prefetch_cron_history`, which reads off the loop and returns `None` (read skipped) when the slot is already linked and the injection would therefore not consume it. A forgotten prefetch is a `TypeError` at the call site, not a loop stall in production.

#### `kirocrew-core`: `local_knowledge_search` Tool

Searches the Knowledge Library for relevant content. Escalated from App Store to built-in tool in `mcp_core.py`.

- **Trigger rules**: strict — only fires on explicit user signals (e.g. "search my knowledge", "check my docs on X"), not on general questions
- **Schema**: `LOCAL_KNOWLEDGE_SEARCH_SCHEMA` in `validation.py`
- **Parameters**: `query` (required string), `limit` (optional integer, default 3, hard max 5), `source_id` (optional string — scopes the FTS5-keyword and vector seed legs to one source; graph traversal stays unfiltered. Unknown ids return a guidance message naming `knowledge_list_sources`, the no-argument companion tool that lists each source's `name — id (N item(s))` with active-item counts)
- **Confidence threshold**: `MIN_SCORE=0.012` filters noise
- **Output format**: source + content only, no score metadata (~2500 token budget)
- **Graceful degradation**: returns helpful message when knowledge DB not configured
- **Security**: credentials/exfiltration URL redaction on all results; SEL audit events for all outcomes (`success`, `no_results`, `not_configured`)
- **Store/embedder cache**: the `KnowledgeStore` (schema DDL + orphan-cleanup migration + in-memory graph load) and the embedder are cached process-wide in `mcp_core.py` (`_get_knowledge_search`) instead of rebuilt per call. Keyed on a signature of `knowledge.db`, its `-wal` sidecar, and `config.json`, so out-of-band dashboard ingestion (which writes the DB/WAL) or a config change triggers a rebuild on the next search; the prior connection is closed on rebuild. Avoids the per-call DDL/migrate/graph-load and the embedder availability probe (which may lazily load the ~700MB in-process model).

#### Knowledge De-duplication (`knowledge/dedup.py`)

Collapses the same document ingested from more than one source (e.g. an upload AND a folder-synced copy) down to a single canonical copy so retrieval stops returning duplicates.

- **De-dup key**: an `items.content_hash` column (whole-doc extracted-text sha256, stamped on every chunk of a document at ingest, on both ingest paths; index `idx_items_content_hash`). Legacy rows have NULL `content_hash` and fall back to the fuzzy tier.
- **Two-tier match** (`dedup_sweep`): Tier 1 -- identical `content_hash` (name-independent). Tier 2 -- a filename near-match (`filename_near_match`: date-free normalized stems equal, or a difflib ratio >= 0.9, after stripping `(1)`/`copy`/`copy of` modifiers and dates) AND a doc-level mean-pooled embedding cosine >= `DEFAULT_FUZZY_THRESHOLD` (0.95), compared only between docs with the same `embedding_sig`. Dates are not collapsed to a placeholder -- they are parsed out and compared separately: when both filenames carry a date, the closest pair must be within `_DATE_MATCH_MAX_DAYS` (7) or the fuzzy match is rejected, so distinct instances of a series that share a title (e.g. `...Apr 2026` vs `...Dec25`) do not collapse. Month-only dates pin to mid-month, so adjacent months land ~30 days apart and are rejected while a few-days drift (including across a month boundary) still matches; extraction uses non-alphanumeric boundaries so underscore-delimited names (`Status_Apr_2026`) are not missed. The filename gate is an AND on Tier 2 -- it can only narrow matches, never create them.
- **Document granularity**: one folder file (a `folder_file_state` row) for folder sources, the whole source for upload/chat sources. All chunk rows of a document share one `content_hash`.
- **Priority** (`pick_winner`): a persistent source (`PERSISTENT_SOURCE_TYPES` = `local_folder`/`obsidian_vault`/`quip`) beats a transient one (upload/chat); within a class the newest `mtime` wins; on an `mtime` tie the oldest-resident copy wins.
- **Action**: the losing document is hard-deleted -- `delete_source_cascade` for a whole-source doc, or `delete_items_batch` + its `folder_file_state` row for a folder file. The file on disk is never touched; there is no soft-supersede and no resurrection.
- **Triggers**: ingest-time after a successful whole-source ingest (`IngestionPipeline._maybe_dedup`, gated by `dedup_enabled`, default on); a sweep at the end of a `FolderWatcher` scan that ingested/changed files; and a one-time backfill via the CLI/MCP tool. All call the same idempotent `dedup_sweep`.
- **Surfaces**: MCP tool `knowledge_dedup` (`apply` bool, default false = dry-run preview; schema `KNOWLEDGE_DEDUP_SCHEMA`) and CLI `kirocrew knowledge dedup [--apply]` (dry-run by default). Both emit SEL audit events and redact credentials/exfiltration URLs in their output.

#### `kirocrew-core`: `send_message` Tool

Sends a message to the user via Slack DM and dashboard notification. Exposed as MCP tool in `mcp_core.py`, backed by `POST /api/send-message` in `handlers.py`.

- **Parameters**: `text` (required), `title` (optional), `blocks` (optional), `session` (optional: `"origin"` or `"slack"`), `channel` (optional), `user` (optional)
- **Delivery** (default, no session/channel/user): dashboard notification only via `state.notify()`
- **session="slack"**: Slack DM + dashboard notification
- **session="origin"**: inject into the dashboard session that spawned this cron. Falls through to notification-only if origin is unreachable.
- **Security**: blocks content is deep-walked through `redact_exfiltration_urls()` and `redact_credentials()` via `_sanitize_blocks()`, truncated to 50 blocks with recursion depth limit
- **Response**: `{ok: true, slack: bool, session: bool}`
- **Primary use case**: silent cron jobs where the agent controls notification timing

#### Silent Cron Flow

1. User (or agent) creates a cron job with `silent: true` via `cron_add`
2. Cron timer fires → agent session runs the job message as normal
3. Gateway skips auto-delivery (no Slack post, no dashboard notification)
4. Agent processes the result, applies judgment (e.g. "nothing changed, skip")
5. When the agent decides the user should know, it calls `send_message` with the relevant output
6. `send_message` → `POST /api/send-message` → dashboard notification (default) or Slack DM (if `session="slack"`)
- Toggle syncs to `kirocrew.json` `tools`/`allowedTools` for consistency
- ACP `session/new` passes `mcpServers: []` (required field); `set_mode` activates the agent
- MCP server init drain: 10s after `set_mode`/`set_model` to wait for all servers to load
- Drain logs loaded MCP server names at INFO level

### Context Window Usage

The Agents page context window section shows per-session info:
- Agent name (purple) for custom agents, hidden for kirocrew
- Model read from agent config file when `_model` is "auto" (custom agents)
- `agent` field in API response from `GET /api/sessions/context`

### Structured monitors

Structured monitors share AutoNudge's one-record-per-session store and timer
ownership but never enter its legacy prompt-cycle accounting. A record is
identified positively by `NudgeLoop.monitor is not None`. `NudgeLoop.next_due_ts`
is the scheduler authority and `MonitorState.next_probe_at` is its inspection
mirror; every transition writes them together under the service lock before the
off-loop fsync. Active version-1 records re-arm toward that deadline after a
restart. An accepted in-flight wake persists its finite completion-evidence
deadline and resumes that deadline after restart; an older claim with no deadline
is retained, inactive, and blocked. A persisted `BUSY` claim intentionally has no
completion deadline and resumes its existing `next_due_ts` retry after restart.
Future versions also fail closed, are persisted inactive, and are never armed;
their opaque monitor payload remains preserved for a newer gateway. Malformed
current-version payloads are replaced once with a valid, inactive
`invalid_monitor_record` quarantine row, so later restarts do not repeat the
same repair.
Replacing a monitor is committed to the in-memory registry only after its atomic
snapshot succeeds. A persistence failure restores the previous active record and
its deadline-backed timer, so a failed create cannot silently stop the watch or
leave restart state behind the live service.
The gateway imports the controller and provider adapters only after the
AutoNudge feature gate accepts initialization, so disabling AutoNudge keeps
provider clients off the gateway boot path. Dashboard route registration keeps
only monitor models at module scope; the pull-request target parser loads when a
monitor mutation route is actually invoked, not while the disabled gateway
assembles its HTTP application.

`MonitorController` currently accepts only a public GitHub pull request with the
`review_ready` objective. The typed provider probe runs off the event loop. The
controller persists canonical allowlisted facts, fingerprint, error counters,
decision, and next deadline before returning. `NO_CHANGE`, `RECORD_ONLY`,
`RETRY_PROVIDER`, and all terminal decisions dispatch zero agent turns. Retryable
provider errors use bounded exponential backoff; terminal provider, success,
blocked, and budget outcomes remain inspectable with stable reason codes.
An incomplete persisted or cancelled handoff with no typed delivery marker is
normalized onto the bounded BUSY retry path; an untyped dispatcher result fails
closed as unavailable instead of orphaning the durable claim. Transient Slack or
Discord setup failures before provider acceptance also retry as BUSY, while
accepted turns alone enter the completion-evidence window.
Every recovery-relevant probe decision is applied to a staged copy and its
replacement snapshot is fsynced before the live record or timer changes. A
strict `NO_CHANGE` observation updates only in-process inspection counters and
the next in-process deadline; after a restart the prior durable deadline may
cause one harmless early probe. A failed probe write therefore
leaves the prior deadline, observation, and actionable-claim state intact in both
memory and the restart snapshot.
The same replacement-before-publication rule covers configuration edits,
session-close retirement and rollback, wake claims, typed handoff results, raw
completion accounting, missing-evidence retirement, and unwired-controller
deactivation. Timers are cancelled or re-armed only after the replacement is
durable, so a failed write leaves both the live record and its existing timer
unchanged.
Monitor wake instructions are length-checked again after credential and URL
redaction, so a replacement marker cannot expand a valid input into an invalid
persisted record. An active record loaded without a wired controller is retained
as a terminal blocked outcome instead of an inactive nonterminal state.
Known actionable GitHub facts (failed checks, requested changes, unresolved
review threads, and merge blockers) take precedence over simultaneous pending or
unknown facts. For an actionable classification its deduplication fingerprint
contains the known blockers but excludes unrelated pending/unknown check churn;
the full allowlisted canonical observation remains available for inspection.

A new actionable fingerprint atomically records `last_wake_fingerprint` and
`wake_in_flight=True` before delivery. Concurrent or restarted ticks cannot
dispatch it twice. Immediately before transport handoff, the controller
revalidates that exact claim under the service lock; a user stop or session-close
transition queued while probe persistence was in flight therefore wins and no
transport action starts. Each adapter revalidates the claim again at its final
turn-start boundary: dashboard inside the runner after acquiring any unattended
background-turn permit and completing pre-turn setup, immediately before the
provider stream is created, and Slack/Discord in `TurnDriver` immediately before
entering the provider stream.
A dashboard handoff is recorded as `DISPATCHED` only after its queued coroutine
acquires the permit, revalidates the active in-flight `(monitor_id,
fingerprint)` claim, crosses SessionManager's synchronous shutdown gate, appends
the wake, and accepts its completion hook. A background-cap wait that times out
or a shutdown-gate refusal returns `BUSY` without appending to chat history,
preserving the same claim for the controller's bounded retry; a final
authorization refusal returns `UNAVAILABLE`. A claim already persisted as
`DISPATCHED` cannot pass admission again. The accepted dashboard invocation runs
at nested prompt depth so stale-turn, tool-stall, pipe-death, empty-response, and
other dashboard recovery paths cannot enqueue an additional provider turn beyond
the structured monitor's durable action-turn budget.
A runtime acceptance marker survives the `DISPATCHED` transition until raw
completion settles it. A user stop preserves only a claim carrying that marker;
after a restart, a recovered `DISPATCHED` claim has no live accepted turn and is
cleared so a replacement monitor can be created.
A stop that lands during channel setup or while a dashboard turn is queued behind
the background cap therefore cannot start a stale action turn. After any setup
await, a structured transport that can no longer construct the claim-bound
completion hook returns `UNAVAILABLE` before dispatch; it never reclassifies that
wake as an ordinary or legacy turn. Dashboard, Slack, and Discord receive the same
ephemeral,
redacted `[Monitor wake]` envelope; it is capped at 4,096 characters and is
never stored as `loop.message`. Check results enter that agent-facing envelope as
status counts only; provider-controlled check identities remain available to the
human inspection surface but never become prompt text. Only Task 2's raw
provider-completion hook clears the claim and its delivery marker, and charges
the action-turn/token budgets. Dispatch or stream return is
not completion evidence. Genuine provider `end_turn` is successful completion;
the ACP compatibility path marks its fabricated `end_turn` terminal with
`synthetic_completion`, and every surface rejects that provenance together with
synthetic timeout/error terminals before shared monitor accounting. A raw
completion received before a transport timeout
remains authoritative and is charged once even when the enclosing stream later
times out. A pre-turn delivery failure retires the record as
target unavailable without charging a turn or immediately retrying the same
fingerprint. Every adapter returns `DISPATCHED`, `BUSY`, or `UNAVAILABLE`.
Webex retains its finite legacy prompt-loop adapter, but structured creation is
refused before persistence because it has no typed dispatch and completion-
correlation contract.
Dashboard crew/member slots are likewise refused because their ingress is a
durable crew queue rather than a direct provider turn. Incognito and temporary
slots cannot host any persisted automation loop; admission rechecks both the
slot identity and these mode boundaries immediately before arming.
`BUSY` persists a short retry for the existing claim without probing or entering
the model, and the retry checks the runtime budget again before dispatch so an
expired claim cannot start another turn; `DISPATCHED` persists a bounded
completion-evidence deadline. Discord marks the correlation accepted when its
hook-bearing `TurnDriver` starts; an exception after that boundary still reports
`DISPATCHED` so the evidence deadline owns recovery. Only
`UNAVAILABLE` is terminal. Expiry without a raw completion event retains the
terminal `completion_evidence_unavailable` outcome and clears the claim without
charging or redispatching it. Late delivery or evidence-expiry callbacks cannot
replace a terminal outcome accepted while the handoff was in flight. Cadence
changes update the policy used for the next
future probe but never replace or re-arm a current BUSY retry or completion-
evidence deadline, so repeated edits cannot postpone expiry or runtime checks.
The durable public `wake_count` increments once on the first `DISPATCHED`
transition, or on raw completion when it wins the handoff race. BUSY attempts and
retries, restart recovery, duplicate reports, and `UNAVAILABLE` do not increment
it.

Session close is a retained terminal transition, but history persistence owns
whether that close commits. Retirement preserves any accepted wake claim,
delivery marker, and completion-evidence deadline, and keeps only the bounded
expiry timer needed to release that claim when raw completion never arrives. If
retirement persistence fails, its transactional rollback leaves the active
structured record in place and the generic close rollback never routes it
through legacy `add()`. If history persistence fails after retirement commits,
the structured rollback restores that exact evidence deadline (or a short
same-claim retry before dispatch) rather than starting a fresh cadence;
the raw completion can therefore still account for the accepted turn exactly
once. The rollback rechecks the caller's slot-generation admission predicate
under the service lock before reactivation, so a concurrent close cannot restore
a monitor after that slot was removed again.

The stateless MCP surface is `monitor_watch`, the extended `monitor_update`,
`monitor_inspect`, and `monitor_stop`. Create/update/stop directives carry no
session or loop identifier and are applied to the consumer's authoritative
binding after the same dashboard/Slack/Discord authorization and critical SEL
audit as legacy loops. A structured create or update that is disabled,
unsupported, refused by authorization, or otherwise not applied records a
`denied` directive outcome rather than a successful application.
The same rule covers a structured stop whose authorization or audit-before-stop
step fails; only an idempotent stop with no structured record is a success.
`monitor_inspect` alone is a direct read: it requires a
strict authenticated session key and reports unavailable rather than using
ancestor fallback. Inspect, structured stop, its directive consumer, and the
strict-internal session read all use the narrower structured binding resolver,
so a Webex key that remains valid for finite legacy loops cannot reach a
structured record. Internal read failures carry the MCP failure marker and are
audited as failed rather than completed. Changing target/objective clears comparison, decision, and
wake baselines and increments the durable configuration generation; a probe result is
discarded if the captured generation no longer matches. Target/objective edits
are refused while a wake is in flight. A second `monitor_watch` is likewise
refused with 409 while the existing monitor has a wake in flight, preserving the
old monitor ID until its correlated completion has been accounted. Cadence,
positive budgets, and wake-instruction edits preserve the baseline and generation.
Budget updates remain sparse through REST/directive authorization and merge with
the current budget record only while holding the service lock, so independent
concurrent edits cannot replace one another with values from stale snapshots.
Terminal records are read-only. `monitor_stop` records `user_stop`; legacy
`autonudge_stop` delegates to that durable outcome only when the record is
structured. An optional stop reason is credential-redacted, bounded, and
retained separately as `user_stop_reason`; it never replaces the stable
machine-readable `stopped_reason`. When the directive is consumed by an in-flight structured action,
the record becomes inactive and terminal immediately but retains that wake's
fingerprint, claim, and delivery marker until the same turn's raw completion
charges its turn and token usage exactly once. If raw completion never arrives,
the user-stop outcome remains terminal and inert: it is never re-armed,
redispatched, or charged from synthetic evidence. Closing a session records
`session_close`; a close rollback restores only that close-owned transition.

The dashboard contract is separate from legacy `/api/autonudge` mutations:
`GET/POST /api/monitors`, `GET /api/monitors/slot/{slot}`, `PATCH
/api/monitors/{id}`, `POST /api/monitors/{id}/stop`, and the sole explicit
revival route `POST /api/monitors/{id}/restart`. Reads include terminal records.
Every browser monitor route requires the configured dashboard owner before it
reads a caller-selected slot or id, parses a mutation body, or touches the
service. A signed allowed-user dashboard session is not sufficient. A stale
machine-local bootstrap session receives `stale_session_reauth`; other denied
subjects receive the stable `dashboard_owner_required` code, and each decision
is best-effort SEL audited without target or provider data. The MCP-only
`GET /api/autonudge/session-monitor` route is strict-internal: middleware
requires loopback plus `X-Internal-Secret`, the handler reasserts that trust
before accepting `X-Session-Key`, and browser-cookie fallback is forbidden.
Both the internal-secret decision and a missing or unsupported session binding
are best-effort SEL audited before the handler returns.
Creation uses the same positive defaults and bounds as MCP; zero is never
unlimited. Dashboard creation is create-only under the service lock: if any
automation already occupies the slot, the API returns 409 without replacing its
evidence, even when that record was armed after the dashboard's last read.
Structured creation never resolves or unlinks the legacy loop stop sentinel, so
a rejected structured replacement cannot disable an existing legacy kill switch.
Restart rejects an unsupported future monitor version with
the stable `unsupported_monitor_version` code and does not rewrite its exact
retained raw payload. Restart also conditionally replaces the exact monitor id
and configuration generation read by the request; a concurrent restart or edit
returns 409 instead of silently replacing the winner. Structured AutoNudge
websocket payloads use the owner-only dashboard
channel; legacy websocket frames keep their existing authenticated-client
broadcast. Legacy `/api/autonudge` list/get routes exclude structured records
entirely, which remain readable only through the owner-gated monitor routes.
Legacy `PATCH
/api/autonudge/{id}` rejects structured ids before mutation. Legacy DELETE routes
a structured id through an owner check and the monitor stop authorizer's
audit-before-mutation ordering. Browser legacy creation is create-only under the
same service lock as structured creation, so a stale empty snapshot cannot
replace an automation armed by another tab. Other legacy callers retain explicit
replacement semantics, but the agent-facing `monitor_start` and `monitor_watch`
directives are create-only across both record kinds, with one deliberate split.
Dashboard REST creates keep any-record occupancy: a 409 that never discards a
retained record, active or stopped, preserving inspection evidence. The two
session-directive arms additionally opt into `replace_stopped`, which narrows
their refusal to records that still occupy the session — an ACTIVE automation,
or a retained stop that is evidence. Only system-imposed stops are re-armable:
`approval_stalled`, `cycle_cap`, `runtime_budget`, and terminal-subject records
(a merged or blocked watched subject, including structured `BUDGET`, `SUCCESS`
and `BLOCKED` outcomes). Consumer-recorded stops are preserved on every path —
a manual pause, a structured `USER_STOP` or `SESSION_CLOSE` record, and the
auto_research `autonudge_stop` tombstone its watchdog consumes — and an unknown
stop reason fails closed to preserved. A future-version record is never
replaceable (it belongs to the newer gateway that wrote it), and a terminal
record whose accepted wake still awaits completion evidence keeps its own
wake-in-flight refusal, so an unfinished correlation is never orphaned.
Create-only directives cannot silently replace an active
legacy loop with a structured monitor or discard an active structured monitor's
durable evidence. The generic service update also fails closed for structured
records.

### Security Enforcement

`_enforce_denied_commands()` in `agent.py`:
- Injects `deniedCommands` from bundled defaults into ALL agent configs (security)
- Runs at install, gateway startup, and every ~60s
- MCP server isolation removed — kiro-cli ACP ignores per-agent `disabled` overrides; control is centralized in global mcp.json

### Build & Development

- **Dev mode**: `./dev-fullstack.sh` starts the live-source backend plus a Vite dev server on port 3000 that proxies the API to it
- **Production build**: `npm run build` in `website/` runs `tsc -b && vite build` into `website/dist`, which is then staged into `src/kiro_crew/static/dist/`. `setup.py` only COPIES an already-built dist into the wheel; it never runs the build.
- **Static serving**: `server.py` serves `/assets` from `dist/assets/` (Vite hashed bundles), `/static` from the static dir (theme images, logo)
- **Runtime dist resolution**: at gateway start `frontend.ensure_dev_dist_symlink()` reconciles `src/kiro_crew/static/dist` for a source-tree run — a populated real dir is a no-op, an existing directory link is validated (dangling or index-less targets are replaced), otherwise `website/dist` (or a legacy sibling `KiroCrewWebsite/dist`) is linked. The link is created through `platform_compat.symlink_or_junction`: a symlink on POSIX, a **directory junction** on Windows, since a Windows symlink requires `SeCreateSymbolicLinkPrivilege` that an ordinary account does not hold. Link detection/removal likewise route through `platform_compat.is_link_or_junction` / `unlink_link_or_junction` — `is_symlink()` reports `False` for a junction, and `shutil.rmtree` refuses one.
- **Missing-bundle behavior**: if `static/dist/index.html` is absent, `handlers.py` (`index()`) serves a static "not found" guidance page (restart/rebuild hint). The legacy `static/dashboard.html` server-rendered fallback was removed (stored-XSS follow-up); the React SPA is the only shell.

### Resilience

- **Standalone fallbacks**: Memory and Skills APIs work even without `ContextBuilder` — handlers create standalone `MemoryStore` and `SkillsLoader` instances pointing to `~/.kiro/crew/` defaults
- **Agent config path discovery**: `_find_agent_config()` searches `$KIROCREW_PROJECT_DIR` → `~/.kiro/crew/project_dir` saved path → package-relative fallback
- **Static system info caching**: Hostname, OS, arch, CPU count, total memory computed once; only dynamic metrics (load, vm_stat, netstat) fetched per request

### Tool-Refusal Recovery

When `_run_chat` refuses a tool call for a **recoverable, system-side** reason —
a host-gate policy deny (`hooks.on_tool_call` → `TOOL_DENY`), the read-only
bash safety gate (`is_read_only_bash` / `unsafe_bash_reason`), or a PreToolUse
script hook block (`exit 2` → `BLOCKED:<hook>:<stderr>`, or any other nonzero
exit → `BLOCKED:<hook>:<error>`, since only 0 and 2 are verdicts a gate can
deliver) — kiro-cli ends the
turn early by emitting the attribution-free marker `Tool uses were interrupted,
waiting for the next user prompt`. Without recovery, the reason reaches only the
dashboard pill and SEL audit log, so the model cannot adapt.

`_run_chat` records each recoverable refusal as a redacted `(title, reason)`
tuple in a per-turn `_refusal_reasons` list. PreToolUse block reasons use the
first non-empty hook STDERR value and fall back to a generic policy-hook reason
for deny-by-default malformed results. When the turn ends — and the user
did **not** stop it (`slot._stopping` is false) and no session reset is already
re-queuing — it builds a continuation via `context.build_refusal_recovery_prompt`,
prepends `REFUSAL_RECOVERY_PREFIX`, and `queue_insert(0, …)`s it. The existing
finally-block dequeue loop renders it as an `inject` message (not a user bubble)
and re-dispatches it on the same session, so the model receives the reason and
can adapt (an allowed alternative, a different tool) or stop on its own with a
stated reason. The synthetic prompt is never mirrored to a linked Slack thread
as user input (`_is_recovery` guard).

The continuation **leads with remediation guidance** when the refusal classifies:
`deny_guidance.remediation_for` supplies one paragraph per deny class,
de-duplicated so several refusals of the same class contribute it once. Without
it the model receives a reason and no sanctioned path, which is what produced the
reported failure mode — an agent that retries the same shape under a different
reader and then reports the capability missing.

The class is resolved by asking which PRODUCER refused. A producer that names a
catalog rule — the regex tier and the argv-structural self-protection floor lead
with the rule's pattern, the git-publish gated floor with its id — yields the
class from that rule's identity (`_RULE_CLASSES`, else its category via
`_CATEGORY_CLASSES`), and nothing else is consulted for such a refusal, including
when the rule has no guidance to offer. A producer that refuses generically — the
un-weakenable fnmatch overlay, the sensitive-path floor, the exfiltration-shape
audit — yields it from anchor phrases in the refusal text plus the subject, which
is the only signal those have. Inferring the class from a rule's REGEX SOURCE
mis-keys ten `credential-exfil` rules: their patterns name the AWS credential
environment variables, so rules that block moving credentials OUT draw
credential-READ prose inviting the caller to run the command it wanted. The
subject is read on the generic path only, because a command refused for moving a
credential necessarily contains that credential's name. A census in
`test_deny_guidance.py` fails when a rule in a remediation category resolves to no
guidance, and when any rule's class comes from the anchor scan rather than its
identity.

That guidance is rendered as **indented prose, never as `- ` bullets**. The
frontend's `RecoveryCard` counts every bullet in this body as one blocked tool
call (`BULLET_RE`), so a guidance paragraph written as a list makes one refusal
plus its advice read as two blocked calls. A future edit that tidies the prose
into a list would reintroduce that miscount silently; `toolBlockedCard.test.ts`
pins the count.

By design there is **no retry cap**: the model decides when to stop, and the
user's Stop button remains the hard breaker (a stop clears the queue and aborts
the chain). All four permission paths that can process PreToolUse script hooks
(declarative auto-approve, normal gating, trusted/YOLO, and interactive approval)
route `BLOCKED:` reasons through the same recovery funnel.

### Reset-and-Requeue Recovery

A reset-and-requeue path must not replay a turn that already emitted assistant
output or dispatched a tool. `chat_utils.build_recovery_requeue()` returns the
original request only before any output; otherwise it returns a continuation that
tells the model to resume from restored conversation state without repeating
completed work.

That decision does not depend on why the session was reset, but the continuation
does. The helper takes a required `ResetCause` — `CONNECTION_LOST` or
`SESSION_BUSY` — and returns the continuation for that cause, because the marker
it carries is what the transcript renders: a session that was merely busy must
not be reported to the user as a lost connection. The enum has no default, so a
reset site added later cannot inherit another cause's label by omission.

Every automatic requeue retains `kind="synthetic_recovery"`. Queue consumers use
that structural provenance—not message text—to prevent merging with user input,
continue draining while user turns are held, preserve the pending session-reset
notice, and render the dequeued row as an `inject`. This keeps a user who happens
to type the same marker ordinary user input. Each continuation starts with its own
marker (`CONN_RECOVERY_PREFIX`, `BUSY_RECOVERY_PREFIX`), is folded by the frontend
`RecoveryCard`, and is not mirrored to a linked messaging surface as user speech.

### Design

- OpenClaw-inspired: Space Grotesk + JetBrains Mono, dark/light theme with amber accent
- Tailwind CSS 3 with custom theme (`tailwind.config.js`) — design tokens as CSS custom properties, utility classes throughout
- **Typography scale**: body 14px, descriptions/details 14px (`text-sm`), labels/buttons/sidebar 13px, badges/captions 12px, decorative icons 10-11px. Minimum readable text: 11px. Code blocks: 13px mono. No text below 10px anywhere.
- CSS grid shell: topbar + nav sidebar + content area
- Nav sidebar: collapsible (220px full / 56px icon-only), prominent logo (frameless, radial gradient accent wash, 80px with drop-shadow)
- Animations: rise, slide-up, slide-in, scale-in, shimmer thinking bar, dot-breathe health indicator, brand-glow

### Custom Domain

`kirocrew setup` uses `kirocrew.localhost` (RFC 6761 reserved, resolves to loopback natively) for `http://kirocrew.localhost:5476` (macOS/Linux via `sudo tee -a /etc/hosts`). Gateway startup also prints the hostname URL for remote desktop access.
