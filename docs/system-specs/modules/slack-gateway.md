# Slack Gateway Module

## Overview

The Slack integration (`kiro_crew/slack/`) connects KiroCrew to Slack via Socket Mode. DMs are routed through ACP to kiro-cli with real-time streaming and interactive tool approval.

Independently scheduled agent runs admit their exact execution key as durable
work before provider allocation, publishing its privacy mode in the canonical
session execution record. Single and sequential-agent paths share that
admission, so first-turn child creation does not require a dashboard slot or a
previous transcript. A damaged committed mode refuses allocation; a key prefix
alone never grants a mode. Origin-chat injection keeps the chat's own policy.
Cron execution binding is published off the event loop before mode admission
and provider allocation, using the run's already captured execution context.

Startup wires memory objects behind one gateway-lifetime in-process barrier.
Both dashboard and API-only servers receive the orchestrator's existing context
builder. Post-bind workflow initialization uses that same object for essentials
and store-bound context; it does not construct a second memory stack or add
pre-bind memory reads.
After the dashboard binds, one tracked worker activates pending V1 and V2 restores before opening any memory database or
markdown/FTS store. It clears a previous gateway's cached handles, initializes the
already-wired Global store and rebuilds FTS before releasing memory access.
The gateway publishes that task to dashboard state and emits `KIROCREW_READY`
without yielding to it, then awaits it before arming cron, heartbeat, automatic
memory work or channel transports. Persisted Crew work and restored legacy
channel agents resume after the same wait. Agent-backed dashboard turns shield-wait on
the same task at their central admission seam before identity, provider or
metadata work. A cancelled turn therefore cannot cancel preparation or record
the transient fence as a failed turn. The bound dashboard remains available for
status and recovery while preparation runs; its memory content operations
refuse access until the pass settles. A journal or
activation failure is recorded against that canonical store, and the worker
continues restoring later stores. Once the pass completes, healthy Global,
named V1 and private V2 stores become usable independently. A Global restore or
initialization failure fences only Global and skips its migration; private
repair and automatic backups of healthy member V2 stores still run. Structural configuration or worker
initialization failure can keep the whole preparing fence closed.
Failed-store context, HTTP, direct/cached store handles and backups refuse with
a named reason; HTTP returns `503` and `code: store_unavailable`.
Store status, backup listing and cancellation remain available for owner recovery.
Failure preserves the journal and prior data. Owner backup and cancellation
responses report `activation_failed`, `restore_error` and `restart_required`
for the affected store, even when its journal parses or has been cancelled.
Cancellation does not unlock that store in this gateway; a subsequent restart
retries recovery before access. Once the preparing pass completes, an owner can
stage a known-good backup for a failed store, including when its current database
is unreadable. Staging validates ownership and the backup without opening live
memory, and retains the existing pending-journal lock. It does not clear the
failure fence or activate that copy until the next restart. Preparing, stopped
and structurally failed gateways still refuse new staging.
The failure map is process-local and lasts only
for that gateway. V2 product store users hold a shared POSIX admission lock outside the replaceable directory; restore activation requires the exclusive lock. Windows relies on native open-handle replacement refusal. This does not claim coordination with arbitrary external writers that bypass the product protocol. A stopped worker
closes any late handle before its barrier is released and cannot release a
successor gateway's barrier.

After successful memory readiness, one gateway-owned repair loop visits the
active Global store and cached named V1/V2 stores in round-robin order every 30
seconds on the embedding executor. Each visit revalidates readiness and the
named store's declaration and ownership, uses only an already-ready backend and
repairs at most 16 missing vectors per memory kind using existing bulk pacing.
Bounded cursor pages move past failed rows and wrap for retries. Later seeds,
queued writes and model reconciliation therefore receive repair without a
restart. Successful pages append to the resident native index instead of rebuilding and writing the entire index on every page. Shutdown stops new visits and fences late embedding commits. The loop
waits for Global's boot migration and full repair sweep before visiting that
store, never opens a store and adds no per-member task or model load. V1
retrieval, admission, decay, consolidation and capacity behavior remain
unchanged.

The first heartbeat after memory becomes ready schedules a tracked background
backup pass for every active memory store: the default store first, then declared
named V1 stores and active member V2 stores.
Existing per-store backup freshness prevents duplicate copies across
restarts; later checks retain the daily cadence at tick 30 modulo 1440. A large
store does not delay subsequent heartbeat ticks or idle-session checks.
Only one backup pass belongs to a heartbeat service at a time. Shutdown signals
its worker to finish at most the current atomic copy, then skip pruning and all
remaining stores. Stopping the async waiter never resets that worker's stop flag.
Automatic backup enumeration excludes archived, unbound private stores.
Manual all-store backups visit the same set. Archived files
and backup listings remain available for owner inspection; restore requires an
active exclusive binding and there is no archive reattachment UI.

Explicit member deletion and committed package-agent pruning release that store's
SQLite handle, FAISS/scoring arrays and markdown/lesson caches off the event loop.
An in-flight construction cannot republish a handle across the cache's eviction
generation. Existing files and rollback copies remain intact; recreating a member
receives a fresh store identity. Superseded restore trees remain outside automatic
`backup_keep` retention and require explicit owner cleanup. Their UUID names and
file timestamps do not establish completed recovery or safe deletion order.

During operation, member cron jobs, linked DMs, nudges and completion injections validate
their own recorded memory identity before acquiring a provider. Completion
injections use the parent conversation's memory; delegates keep their target's
member-scoped memory for the delegated run and retries.

Memory-operation refusals retain their named recovery reason in channel replies,
but pass through the shared credential/exfiltration and local-path redactors
before truncation. Both native Slack and its transport dispatcher apply the same
protection as Discord and Telegram. Native Slack sanitizes the accumulated reply
before final rendering and conversation persistence; an operating-system error
must not expose its data-home path to channel readers.

Native and transport Slack dispatch resolve persisted agent/project overrides
off-loop. Only the event loop updates the live override maps, retaining a newer
command or completed hydration that arrived during the read. Both dispatch paths
recheck thread ownership after hydration and store admission before provider
allocation. Unlinking returns to the canonical Slack conversation; pinned answers
retain their asker. Transport also retains its privacy-boundary owner check.
Cached overrides keep the existing synchronous no-I/O fast path.

**Thread parent for a new Slack-born session.** A reply can open a Slack-born
session (`slack:<ts>`) in a thread it did not start: the owner answering an
agent's `send_message(session="slack")` DM, a reply under a cron post, a reply
in someone else's channel thread. When that session is fresh and its transcript
has no user or assistant row yet, both dispatch paths read the thread's first
message once (`slack/thread_parent.py`, via `SlackClientOps.fetch_message_detail`;
the native path asks through `slack/handler_runtime/turn_context.py`):

- The model gets it only as `thread_parent_text`, inside the fenced,
  injection-screened `[SLACK THREAD CONTEXT — UNTRUSTED DATA]` block. A parent
  matching an injection pattern stays withheld there.
- The transcript gets one `notice` row above the reply, attributed to its author
  (the posting app's name, else the user's real name), which the dashboard draws
  as a notice card with its line breaks kept. A `notice` is display-only
  (`history_projection.DISPLAY_ONLY_ROLES`): it is outside `RECALL_ROLES`, and
  `recent_with_provenance`, memory consolidation and auto-skill detection skip it,
  so no replay, recall, compression or memory pass hands it to a model.
  Consolidation still moves its offset past the row. An injection-matching
  parent's text is withheld from the row too, and the row's text goes through the
  prompt block's marker neutralizers. Incognito and temporary sessions get no row.

Dashboard-linked threads and sessions with prior turns fetch and record nothing.
The transport path persists the user's row at receipt, so it builds the prompt
with `exclude_last_n=1`; otherwise the history fallback replays the reply as the
thread's history.

**Thread replies since the last turn.** Every turn that arrives as a reply in a thread, on either dispatch path (natively through `slack/handler_runtime/turn_context.py`), reads the thread's replies with one `conversations.replies` call (`slack/thread_replies.py`, via `SlackClientOps.fetch_thread_replies` with `oldest`/`latest` bounds) and hands them to the model as `thread_replies_text`, inside a fenced `[SLACK THREAD REPLIES — UNTRUSTED DATA]` block. A session with no turn in the thread yet sees every reply before the one it answers, its own app's included. A later turn sees only replies after the message its last turn answered (remembered in process), or after this app's newest reply in the thread when that is not known, and leaves out this app's own replies. The thread's first message and the current message are never in the block. Of the replies that one 200-message page returns, it keeps the newest 20, 1,500 characters each and 8,000 bytes together, with a count of what was left out of that page. Each reply is redacted, a reply whose text or author name matches an injection pattern is withheld whole and audited, and the block's markers are neutralized. The watermark moves only after a turn whose read succeeded has landed, so a failed read is asked for again next turn. This is context only: which messages the bot answers is decided before it runs.

## Architecture

Channel startup diagnostics receive setting names and boolean presence checks,
never credential values. Each channel keeps its existing enablement predicate;
missing settings are named once, and configured or disabled channels stay silent.

```
Slack Socket Mode → events.py (dispatch) → handler.py → SessionManager → AcpClient → kiro-cli
                  ↘ interactive payloads → interactions.py (dispatch) → approve/reject/ack
                  ↘ member_joined_channel → allowlist.py (prompt_allowlist) → owner DM
```

## Files

| File | Purpose |
|------|---------|
| `slack/__init__.py` | Package (no eager imports to avoid aiohttp at import time) |
| `slack/client.py` | `SlackClientOps` ABC + `RealSlackClient` (slack-sdk wrapper) |
| `slack/files.py` | Slack adapter over shared attachment ingestion — authenticated downloads, inlineable images/text/documents, and byte-identical opaque files with local path + metadata; caller-owned cleanup and SEL audit |
| `slack/format.py` | Markdown → Slack mrkdwn conversion (headings, links, strike, tables, mermaid, ANSI strip, truncation). `render_for_slack` is the one way text reaches Slack: `strip_ansi` → redact → pre-split → convert → redact → display-settle → redact → split, with each converted block, split piece and truncation settled under the Slack mrkdwn reading |
| `slack/handler.py` | The native turn path's composition facade: `handle_message()` — orchestrates one turn and streams the ACP response, `handle_interaction()` — button clicks (with None provider guard), and every name the module exported. See [Native handler composition](#native-handler-composition) |
| `slack/handler_runtime/` | Private owners the handler facade composes, one responsibility each (see [Native handler composition](#native-handler-composition)); nothing else imports them |
| `slack/gateway.py` | `GatewayOrchestrator` — the composition facade: service construction and boot, the cron/heartbeat/subagent/task callbacks, approvals and the redacting delivery legs, shutdown, the update apply chain. Entry point: `run_gateway()`. See [Composition](#composition) |
| `slack/gateway_runtime/` | Private owners the facade composes, one responsibility each (see [Composition](#composition)); nothing else imports them |
| `slack/events.py` | Socket Mode event routing — dedup (`SeenCache`), slash commands, `member_joined_channel` tracking, message dispatch |
| `slack/interactions.py` | Block Kit button routing — tool approval, OPTIONS choices, cron/subagent ack, session resume, track channel approve/deny |
| `slack/blocks.py` | Reusable Block Kit dict builders for slash command UIs (session list). Action IDs: `mc_<command>_<action>[_<id>]` |
| `slack/allowlist.py` | Tracking-channel allowlist prompts (`prompt_allowlist`, `prompt_track_channel`) + config persistence (`persist_allowed_user`, `persist_tracking_channel`) |
| `slack/scope_probe.py` | Tracked-channel history-readability probe (`warn_unreadable_tracked_channels`) — warns when the installed token cannot read a tracked channel (e.g. a private channel on an install predating `groups:history`) |
| `slack/enterprise.py` | Enterprise Grid workspace validation — `validate_enterprise()` (startup auth.test + cache) + `check_message_origin()` (per-message team_id check). SEL audit on all outcomes |
| `slack/channel_resolver.py` | Channel ID → human-readable name resolution (in-memory + on-disk cache), because `ChannelConfig` stores no name field |
| `slack/outbound.py` | Lifecycle of a posted OPTIONS control. Holds no rendering of its own — `slack/format.py` owns that, so the redaction pipeline exists once |
| `slack/retry.py` | `open_dm_with_retry` — one bounded DM-open retry with a single retryability classification and backoff. Reached through `GatewayOrchestrator._open_dm_with_retry`; other DM-open sites still call `SlackClientOps.open_dm` directly, so coverage is the orchestrator paths, not every sender. `post_message` stays single-shot per call site |
| `slack/renderer.py` | `SlackRenderer` — maps the neutral `messaging.TurnDriver` `OutputEvent` stream onto Slack streaming + Block Kit |
| `slack/transport.py` | `SlackTransport` — Slack as a concrete `MessagingTransport` with a deny-by-default `authorize`. Constructed pass-locally by `channel_lifecycle._replay_spooled_inbound` for refused-turn spool replay, and deliberately not registered in `channel_transports` |
| `slack/transport_dispatch.py` | The new-path dispatch `events.py` routes to when `messaging.use_transport` is on: `handle_message_transport` builds a `TurnDriver` and `SlackRenderer` over the existing Slack client. It does not go through `SlackTransport.receive` or `authorize` |
| `slack/sessions_view.py` | Slack half of the recent-sessions list shared by the slash command, the DM keyword and the App Home tab; collection lives in `messaging/sessions_view.py` |
| `slack/thread_parent.py` | The first message of a thread a new Slack-born session was opened in: fetched once for the fenced prompt block and recorded once as a display-only `notice` transcript row (see "Thread parent for a new Slack-born session") |
| `slack/thread_replies.py` | Thread replies a turn has not seen yet, bounded, redacted and injection-screened, for the fenced `[SLACK THREAD REPLIES — UNTRUSTED DATA]` prompt block (see "Thread replies since the last turn") |

## Composition

`slack/gateway.py` is the gateway's composition facade. `GatewayOrchestrator`,
`run_gateway` and every name the module exported stay importable and patchable
there; the responsibilities below live in private owners under
`slack/gateway_runtime/`, and nothing but the facade imports an owner.

| Owner | Responsibility |
|---|---|
| `slack/gateway_runtime/tool_policy.py` | Which tool calls an unattended turn may run: the `--approval reads` verb test (`hooks.py` imports it through the facade), `HEARTBEAT_SAFE_TOOLS` and `_is_heartbeat_safe_tool`, the heartbeat-scoped hooks, `_BACKGROUND_APPROVAL_SOURCES`, tool-title normalisation |
| `slack/gateway_runtime/cron_dispatch.py` | What a cron run clears before and while it dispatches: the bounded fire-time gate and its retention marker, the reserved-env screen, the first-run tab, the claim-time re-vet with its handoff, the one-shot post-token resume |
| `slack/gateway_runtime/cron_verdict.py` | What a cron run's tool-gate outcomes and result add up to: the per-run tally and its refusal summary, the banner on a partially blocked result, the dedup hash and reminder windows |
| `slack/gateway_runtime/delivery.py` | Where an unattended result is routed: the origin key, the channel conversation behind it, the channel leg that hands a result to that conversation, the dedup anchor a confirmed delivery advances, OPTIONS bookkeeping, the bounded DM open, whether a job is silent |
| `slack/gateway_runtime/channel_lifecycle.py` | The connect-time `channels` governance gate, the governed Slack connect, the live-config appliers and one-channel restart, boot-time re-hoisting from the watcher, readiness badges, inbound spool replay |
| `slack/gateway_runtime/mcp_broker.py` | The MCP broker's lifecycle: launch approvals, the agent-overlay rewrite, start/stop, the npm pre-resolve prefetch, the dashboard enable/stub callbacks |
| `slack/gateway_runtime/memory_lifecycle.py` | Memory preparation behind `MemoryStartup`, the paced member-store repair, embeddings and the model download, the legacy migration and re-embed sweep |
| `slack/gateway_runtime/admission.py` | Opening subagent dispatch and the dashboard workers after the memory fence, child liveness, the adaptive controller and its overload-health sources, the dependency coordinator, runner task admission |

**One namespace.** `gateway_runtime.compose`, called once after the class body,
rebinds every function an owner defines -- its module functions, the orchestrator
methods it holds (bound as the `GatewayOrchestrator` attribute of the same name)
and the methods of the classes it defines -- onto the facade's module globals. A
patch of `kiro_crew.slack.gateway.<name>` therefore reaches owner code exactly as
it reached the one-module file, and `__module__` / `__qualname__` still read
`kiro_crew.slack.gateway` / `GatewayOrchestrator.<name>`. The orchestrator is the
only holder of state: an owner keeps none, so a `GatewayOrchestrator.__new__`
fixture or an unbound `GatewayOrchestrator.<method>(stub, ...)` call reaches an
owner method unchanged. An owner imports the facade only under `TYPE_CHECKING`,
so the facade is the one import edge; `test/test_slack_gateway_composition_contract.py`
sweeps every owner function's bytecode for globals the facade does not bind.

**What stays in the facade, and why.** Repository guards read these constructs in
`slack/gateway.py` by path, text, AST or `inspect.getsource`, so they live there:

- construction and boot: `__init__`, the per-channel `_hoist_*`,
  `_register_config_appliers`, `_start_channel_transports`, `_init_services`,
  `run`, the signal handlers, `_shutdown`, `_shutdown_and_exit`,
  `_write_marker_worker` (boot-order, readiness, hot-reload and exit-path audits);
- the cron callback (`_init_cron`) with `_apply_gate_verdict`, `_init_heartbeat`
  and the subagent completion path (`_init_subagents`): usage-row, runtime-death,
  dispatch-site, memory-store and reap-race audits;
- AutoNudge: `_init_autonudge`, every `_fire_*_nudge` adapter and the fire paths
  they delegate to, and the loop-stop notices: composer, turn-ceiling, event-log
  and wake-judge audits;
- the approval callbacks and every delivery leg that renders or redacts before
  egress (`_interactive_approval`, `_heartbeat_approval`, `_deliver_channel_reply`,
  `_deliver_cron_response`, `_deliver_result` with its heartbeat Slack rendering,
  the failure alerts): the security-posture sink row and the baseline-log census;
- the dependency repair and the whole update path, its checks included
  (`_check_missing_deps`, `_check_console_script`, `_warn_if_kiro_cli_outdated`,
  `_run_update_checks`, `_check_for_updates`, `_check_for_updates_via_provider`,
  `_auto_apply_update`, `_auto_apply_wheel_update`, `_restart_after_update` and
  its fence): spawn-site and restart audits;
- the ACP/provider import lines the agent-SDK boundary baseline counts,
  `_persist_turn_row`, and the predecessor run-directory sweep helpers.

The contract test lists the constructs those guards enumerate and fails when an
owner grows one. A guard whose rule spans code by path rather than naming
constructs covers the owners with the facade: `test_no_config_dir_in_async.py`
scans each owner that defines a coroutine, and the `AUTOSDE.yaml` rule
`no-new-work-on-gateway-boot-path` matches `slack/gateway_runtime/` because the
boot path reaches `_init_mcp_gateway`, `_start_embeddings` and the runner
admission there.

`compose` is not `subagent_manager._component.bind_component_globals`, which
rebinds the `*_impl` methods of coordinator objects a manager holds: here the
owner functions ARE the orchestrator's methods and module functions, so there is
no object a `__new__` fixture could miss. Nor is it the write fan-out facade of
`apps/backend.py`, which copies a patched name into every module holding it; one
rebound namespace leaves one binding to patch.

## Native handler composition

`slack/handler.py` is the native Slack turn path's composition facade.
`handle_message`, `handle_interaction` and every name the module exported stay
importable and patchable there, and `handle_message` stays DEFINED there as the
orchestrator of a turn. The responsibilities below live in private owners under
`slack/handler_runtime/`; nothing but the facade imports an owner.

| Owner | Responsibility |
|---|---|
| `slack/handler_runtime/access.py` | Who may drive the bot and the live references the handler reads: the owner, allowlist and tracked-channel predicates and their setters, per-session Trust and YOLO (kept by `messaging.session_trust` and `safety_override`), the orchestrator config and dashboard state the gateway installs after import, the background-task set shutdown cancels |
| `slack/handler_runtime/inbound.py` | What an inbound message resolves to before a turn: the `!temporary` / `!incognito` modifiers, the thread agent and project overrides and their off-loop hydration, the default agent and the channel-config writes, the hand-off of a linked thread's message to its dashboard slot |
| `slack/handler_runtime/commands.py` | The command surface: `_handle_slash_command`'s deprecation notice and dispatch, one coroutine per `!` command (`_bang_<name>`), the sender gate and `!compact` routing `handle_message` calls (`_route_bang_command`), `_handle_compact_command`, the `sessions` keyword predicate, the `spawn` / `run` / `cron` keyword wrappers |
| `slack/handler_runtime/turn_context.py` | The Slack thread context a turn's prompt carries: the thread parent for a fresh Slack-born session, the replies since the last turn, the `conversations.replies` fallback line |
| `slack/handler_runtime/stream.py` | The answer's Slack wire: `_AnswerStream` (the stream message and its rotation, the rolling credential redactor, delivery debt, task cards and their elapsed-time timer, the text / reasoning / tool-call projections, the approval pause, the final flush and seal) and the OPTIONS / control-tag holds and bounded edits it uses |
| `slack/handler_runtime/approvals.py` | Approval prompts and what a click means: the Block Kit prompt, the pending and linked registry entry classes, the linked-slot Trust proof and grant, the mirrored dashboard prompt, and `handle_interaction`'s linked-click and late-Trust branches |
| `slack/handler_runtime/reactions.py` | Status reactions: `StatusReactionController`, the tool-to-phase mapping, the live phase-table accessors, the one-shot reactions the commands add |
| `slack/handler_runtime/voice.py` | Voice replies: loading `voice_reply` into the live voice state, and the reply a finished turn starts |
| `slack/handler_runtime/finalize.py` | What a finished turn leaves in the thread: the timing footer and its OPTIONS / Link-to-Dashboard controls, the review-mode draft post and store, the dashboard mirror, the auto-title task |

**One namespace.** `handler_runtime.compose` (pinned byte-identical to
`hook_runtime.compose`), called once at the foot of `slack/handler.py`, rebinds every
function an owner defines -- its module functions and the methods of its classes --
onto the facade's module globals. A patch of `kiro_crew.slack.handler.<name>`
therefore reaches owner code exactly as it reached the one-module file, and an owner
function's `__module__` still reads `kiro_crew.slack.handler`; an owner's classes keep
their own module. The facade is the only holder of state: the approval registries, the
per-thread maps, the voice state, the phase table and the privacy, trust and auto-title
tracker aliases are facade globals, and no owner keeps one. An owner imports the facade
only under `TYPE_CHECKING`. `test/test_slack_handler_composition_contract.py` pins the
base surface, sweeps every owner function's bytecode for globals the facade does not
bind, and replays recorded Slack / SEL / session-manager transcripts captured from the
one-module file.

**What stays in the facade, and why.** Repository guards read these constructs in
`slack/handler.py` by path, AST, text or `inspect.getsource`, so they live there:

- `handle_message`'s turn decisions: the early dispatch order and the first OPTIONS
  expiry (`test_slack_options_lifecycle`), inbound admission
  (`test_update_check_install_aware`), session acquisition and the thread claim
  (`test_options_click_validation`), the memory-store resolution
  (`test_memory_v2_isolation`), the re-injection consume / rearm beside
  `check_context_usage` (`test_reinjection_gate`), the turn-ceiling gate
  (`test_turn_ceiling`), both hook consultations and the four approve sites of the
  permission ladder (`test_hooks`, `test_transport_permission_floor`), the except arms
  (`test_runtime_death_is_a_process_event`), the verdict and permit region, the
  decorator re-redaction and the two credential log lines (`test_security_posture`'s
  log census, the SAST baseline), every persistence site (`test_persist_off_loop`), the
  OPTIONS token and footer record, and the auto-title pin and claim
  (`test_messaging_auto_title`);
- `_request_approval`, `_reject_orphaned_tool`, `_steer_host_deny` and
  `handle_interaction`'s claimed region: every `reject_tool` site and its steer window
  (`test_messaging_deny_notice`), and the click's approve site;
- `_handle_sessions_command` (the log census), `maybe_handle_keyword_command` (the
  persistence-site count), `_should_auto_approve_spawn` (`test_name_grant_surfaces`
  reads the module's source), and `_resolve_agent_name` / `_discover_project_agents`
  with the companion-plugin agent discovery beside them (`test_agent_spec_hardened_reads`'s
  call-site tables);
- `_build_phase_emojis` and the import-time phase table, because the facade's body runs
  before `compose`; `_VoiceConfig`, `MessageContext` and `_condense_thinking`, whose
  defaults read facade constants; `_display_redactor`, the canonical-order redactor
  that `handle_message`'s fallback egress passes to `redact_for_display`, kept beside
  the registered "Slack messages" sink it serves; `_CompactionReplay`, the type of
  `handle_message`'s own `_compaction_replay` parameter, constructed only by its
  compaction-replay arm;
- the ACP and provider import lines the agent-SDK boundary baseline counts.

Two path-keyed guards whose scanned code moved scan the owners as well, each with a floor
that fails if that code leaves the scan: `test_run_config_write` (the `!agent` /
`!channel` config writes) and `test_safety_override` (the `!yolo` grant-lifetime copy).
`stall_attribution` names `slack/handler_runtime/` beside `slack/handler.py` for the
Slack surface, and `security_posture.NON_EGRESS_REDACTION_MODULES` lists the redacting
owners in the facade's class: `slack/handler.py` stays the registered "Slack messages"
sink.

**Where new code goes.** A new `!` command is a `_bang_<name>` coroutine in
`commands.py` plus its entry in `_handle_slash_command`'s table. New per-turn Slack
presentation of the answer is an `_AnswerStream` method; new prompt context gathered
from Slack goes in `turn_context.py`; an access predicate or grant in `access.py`; the
approval prompt's shape and click handling in `approvals.py`; reactions in
`reactions.py`; voice in `voice.py`; what a finished turn posts after its answer in
`finalize.py`. A new turn decision that a repository guard reads by path stays in
`handle_message`.

## APIs

### Slack App OAuth Contract

The bundled `slack-manifest.yaml` is the setup source of truth. Its bot scopes
are `app_mentions:read`, `channels:history`, `channels:read`, `chat:write`,
`commands`, `files:read`, `files:write`, `groups:history`, `groups:read`,
`im:history`, `im:read`, `im:write`, `reactions:write`, and `users:read`.
`message.groups` is subscribed alongside `message.channels` so private-channel
turns and thread continuation are delivered.

The manifest also requests user scopes `channels:history`, `channels:read`,
`groups:history`, `groups:read`, `im:history`, `im:read`, `mpim:history`,
`mpim:read`, `search:read`, and `users:read`. These scopes apply only to a
separately configured Slack MCP/search integration's `xoxp-...` token. The
gateway constructs every Slack client with `SLACK_BOT_TOKEN`; it does not read
or store the user token.

### `run_gateway(cfg: KiroCrewConfig, *, no_dashboard=False, no_crons=False, no_tunnel=False, no_open=False, port_override=None, json_ready=False, approval_mode=None, test_mode=False) -> None`

The keyword arguments mirror the `gateway` flags; [cli](cli.md) owns the flag table.

Starts the Socket Mode listener. Blocks until SIGINT/SIGTERM. When `no_crons=True`, the `CronService` is instantiated but not started — cron jobs are visible in the dashboard but not executed. Use for multi-instance setups where a single primary instance handles cron execution. On shutdown, calls `dashboard_state.close_all_ws()` before `AppRunner.cleanup()` to prevent 30s hang from blocked WebSocket `async for msg` loops. Its `👻` status lines are plain `print()` calls; the `gateway` entrypoint line-buffers a non-terminal stdout once before this runs, so they reach a service manager's log as they are printed — the contract is in [cli](cli.md#gateway-stdout-is-line-buffered-off-a-terminal).

### Automatic apply on a managed-venv install

When the update coordinator applies an update unattended on a `cli.sh`
managed-venv install (`auto_update` on, or a policy `min_version` floor that
outranks it; the check's snapshot carries an installer command,
`running_from_managed_venv()` is true, and `update_capability.auto_update_effect()`
yields `route=wheel` — see [cli](cli.md), which owns that effect),
`_auto_apply_wheel_update` runs
`wheel_apply.run_wheel_apply`. `POST /api/update/approve` runs the same helper,
and `kirocrew update` drives the same engine (`wheel_engine.apply_wheel_update`);
the engine itself is described in
[rfc-update-architecture](../../request-for-change/rfc-update-architecture.md).
Nothing on this path re-runs `cli.sh`, which moves the live venv aside and
rebuilds it in place.

The gateway takes its running tree's liveness hold immediately after the
optional `KIROCREW_READY` print and approval-ready signal, through
`asyncio.to_thread`, fail-open with a debug log. Neither the import nor the
filesystem work runs on the pre-readiness boot path. The update coordinator
starts only later, after channel transports, so its first check-and-apply cycle
runs after the hold completes. Other CLI commands, including MCP servers,
take the hold in `cli.main` after argument parsing.

- **One preflight, one order** (`wheel_apply.preflight_bases`, shared by all
  three callers): the policy source pin on the feed base and then the artifact
  base, then the shape of `KIROCREW_CDN_BASE`. A refusal relights the badge and
  fetches nothing. A release version outside the engine's grammar
  (`wheel_engine.check_release_version`; the unsigned feed is read before the
  signed manifest, and the feed check admits a wider grammar) is refused before
  anything names its tree: the coordinator logs it and relights the badge, the
  CLI exits on its failure path, and the approve route answers `approve_refused`
  and releases the update lock.
- **Operator-only promotions wait.** When the host restricts unprivileged user
  namespaces and the `kirocrew-userns` AppArmor profile applies to the launcher
  today (`apparmor.service_profile_attachment`, the predicate `kirocrew doctor`
  reports), promotion would move that launcher into the new tree and the next
  fresh service start would run unconfined (`wheel_apply.userns_reattach_needed`).
  The unattended apply stops before it builds and sends one notice per version
  naming the two commands (`kirocrew update`, then `kirocrew service install`).
  A policy floor in that state is retried on the short cadence rather than left
  for the check interval. The approve route and the CLI proceed, and after
  promotion name only `kirocrew service install`
  (`wheel_apply.userns_reattach_after_apply`), since the update itself is done.
- **Off the stable link before the build.** A gateway an earlier version
  restarted as `crew-venv-current/bin/python3` keeps that spelling in
  `sys.prefix`, so every module it imports later resolves through the link, and
  a promotion under it would load the new version's code into the running one
  for as long as a busy restart waits. When `wheel_apply.relaunch_before_apply`
  reports that shape, the coordinator builds nothing and runs the pending restart
  below onto `respawn_executable()` (the link's resolved tree, the same version),
  under the same mandatory grace; the successor builds on its next cycle. It is
  skipped when that restart would land on the link again.
- **Built beside the served tree, with admission open.** The apply runs on the
  single-worker `mc-update` executor; a second apply in the same process
  answers `busy` before it is submitted, unless its caller already holds the
  update lock (the approve route takes it before it spends the nonce), since
  every other apply then loses on that lock. The approve route also refuses
  (`approve_restarting`, before the lock and the nonce) while a gateway restart
  is under way, whose exec would stop the apply before its outcome is audited.
  Turns, crons and spawns keep running for the whole build; nothing pauses until
  the restart. A caller-held update lock is released exactly once by the
  `mc-update` worker's `finally`, after the engine finishes, including when a
  cancelled caller's grace expires first; loop cleanup and completion callbacks
  never unlock or close it. If executor submission fails, the caller releases
  it through `asyncio.to_thread`. Approve refusals also release through
  `asyncio.to_thread`; those cleanup jobs are shielded from caller cancellation.
- **Restart through the bracket.** On promotion the coordinator sets
  `_pending_update_respawn` (with `_pending_update_mandatory` and its key) and
  calls `_retry_pending_update_restart`, which pauses admission through
  `_prepare_auto_update_apply`. A busy gateway defers to the short cadence, and
  every retry runs under the same mandatory grace, so a floor's grace warning
  still fires. An apply still in flight (an approved in-app one) counts as
  in-flight work, so a pending restart never cancels it; its own restart owns
  the exec. `_restart_after_update` claims `_gateway_restart_in_progress`, the
  flag `_restart_gateway` claims, so only one restart sequence runs at a time.
  The interpreter is `respawn_executable()`: the stable link's RESOLVED tree.
  No restart is scheduled when that would not exec the promoted tree
  (`wheel_apply.restart_reaches`; `respawn_executable` falls back to the running
  interpreter when the link cannot carry a restart): the successor would run the
  old version, find the same update and restart again for ever. The coordinator
  sends one notice naming the installer re-run instead; the approve route pushes
  it as its `failed` step.
- **Outcomes** (`wheel_apply.classify`, shared with the CLI): `busy`, `deferred`
  (memory is still being prepared) and `cancelled` retry on the short cadence,
  with nothing pushed onto another apply's progress feed. `incompatible` comes
  only from the SIGNED release metadata (today `python_requires` against the
  build interpreter), decided again before any download on every cycle; the
  notice goes out once per process per release and names the installer re-run
  (`wheel_apply.incompatible_remedy`), the only way such a host moves onto a
  newer Python. `kirocrew update` prints the same command and the approve route
  appends it to its `failed` step. `failed`, `timed_out` and
  `snapshot_failed` push a `failed` step whose text is redacted in full, then
  capped to the step and the tail of its detail; a pip "no wheel" failure is an
  ordinary `failed`.
- **Bounded.** `wheel_apply.APPLY_DEADLINE_SECS` (30 min) caps the apply from
  the moment it holds the update lock, through its cancel; the wheel download
  has its own total bound (`_WHEEL_FETCH_TOTAL_SECS`) besides the per-read
  timeout, both checked after every `read1`.
- **A stop owns the apply.** Every exit path calls
  `platform_compat.cancel_wheel_applies_in_flight(reason)`, which looks
  `wheel_apply` up in `sys.modules` (never importing it) and calls its
  `cancel_wheel_applies`; with the module not loaded no apply can be running and
  the call does nothing. It runs in `_on_signal` (both signals), at the start of
  `_shutdown`, in both exec seams (`reexec_launcher`, `reexec_python_module`)
  and in `platform_compat.hard_exit`, which the second-signal force exit and
  the owner's `/kirocrew restart` use. A
  deferred or refused restart therefore never cancels an apply: only an exec
  that is about to happen does. Setting the cancel kills the build child's whole
  process group and shuts a download's socket synchronously, so the child is dead
  before any exit path runs. `_shutdown` then waits up to
  `wheel_apply.STOP_GRACE_SECS` for the applies and the coordinator, concurrently
  with the rest of its teardown and without importing the apply module (it is
  read from `sys.modules`). Each build child also holds the update lock's
  descriptor, so a child orphaned by a hard kill keeps the lock and the
  successor's apply answers `busy` instead of clearing a tree still being
  written. Windows locks are not inherited, and the managed venv is POSIX-only.
- **Memory is copied before promotion.** Readiness is checked before anything is
  downloaded (`wheel_apply.check_memory_ready`); the copy is the engine's last
  step before the flip (`wheel_apply.memory_snapshot_hook`). See
  [memory-skills-hooks](memory-skills-hooks.md).
- **Differences from a direct installer run.** This route keeps the current
  interpreter, the same as the CLI and approve routes. Moving onto the managed
  Python, a release whose `requires-python` this interpreter fails (once
  `cli.sh` provisions a series that meets it; see
  [release](../../build/release.md#raising-the-python-floor)), and retiring
  a venv nested inside the data home all need a direct `cli.sh` run, and that run
  still rebuilds the fixed `crew-venv` in place (under the same update lock).

### Restart after update

Automatic-update restarts select and validate the composed gateway launcher before
saving state or draining callbacks/sessions. Without a launcher they retain the
core-managed interpreter resolver loaded before apply. Launcher selection and the
companion integration contract are defined in
[platform-context](platform-context.md#gateway-restart-launcher); the callback
fence and final yield-free drain-to-exec handoff apply to both launch paths.

Both launch paths, and the dashboard's own `/api/restart`, reach `os.execv` through
`platform_compat.reexec_launcher` / `reexec_python_module`, and those seams cancel
the loop-stall alarm (`arm_process_alarm(0)`) immediately before the exec, with no
await in between: `execve` preserves `ITIMER_REAL` while it resets a caught
`SIGALRM` to its default disposition, so the deadline the last heartbeat armed
would otherwise reach the successor gateway as a lethal signal it never armed,
during its own boot, with no dump and no log line. The successor clears its own
side too: the `gateway` entrypoint calls `loop_watchdog.disarm_inherited_alarm()`
as soon as faulthandler is enabled, cancelling any deadline that still arrived,
but only while `SIGALRM` is at its default disposition (the same ownership rule
`exit_mechanism()` applies: a Python handler on `SIGALRM` means another owner's
`ITIMER_REAL`, which is left alone).

### Stopping an in-flight update installer

An apply that replaces the install in place cannot be killed outright. `cli.sh`
moves the managed venv aside before it rebuilds it and restores it from its
interrupt handling. Inside a step, `_run_step`'s own trap stops the step. The
venv-create and wheel-install steps then restore from their failure branch
(`_venv_restore_after_failure`); the pip-upgrade step has none and exits into
the EXIT rollback below. That failure-branch restore runs with INT, TERM and HUP
ignored and disarms the EXIT rollback only once it has returned, so a second
INT, TERM or HUP cannot cut it short between its delete and its rename. For the
rest of the rebuild, from just
before the move-aside until the wheel lands, `cli.sh` arms an EXIT-trap
rollback (`_venv_rollback_on_exit`, gated on the rename having happened), and
INT, TERM and HUP simply exit into it. A SIGKILL skips all of that and leaves no
venv and no console script. So both arms that stop an apply mid-run stop it gracefully: the
cancellation arm (shutdown) and the timeout arm of the policy route
(`CommandProvider.apply`). The gateway's managed-venv route
(`_auto_apply_wheel_update`) runs no installer: it builds beside the live tree
(see "Automatic apply on a managed-venv install"). The policy route calls
`update_provider._stop_installer`, which calls
`platform_compat.terminate_and_reap`. `kirocrew update`'s installer
(`cli_server._update_wheel`) runs in a session of its own for the same reason,
and its timeout and Ctrl-C go through the blocking sibling,
`terminate_and_reap_sync`. It sends SIGTERM to the installer's process
group, drains and discards its pipes, and waits for the GROUP to empty: pipe EOF
alone is not the end of a trap, because a member that holds neither pipe (`cmd
>log 2>&1`) can still be rolling back. Only then does whatever is left get
SIGKILL. The grace differs by arm:

- **Cancellation arm:** `UPDATE_INSTALLER_TERM_GRACE_SECS`, a share of
  `GRACEFUL_SHUTDOWN_SECS` (both in `gateway_shutdown_budget.py`).
- **Timeout arm:** `update_provider.INSTALLER_TIMEOUT_TERM_GRACE_SECS`, which is
  longer because no shutdown cap applies there.

`cli.sh` runs its pip step in a session of its own (`setsid`), outside the group
the gateway signals. Its TERM trap is what stops that step, so a `cli.sh` that
ignores SIGTERM past the grace can leave pip running after the arm returns.

**Shutdown starts the stop first.** `_shutdown` cancels any in-flight wheel apply
and then `_update_check_task`, before the rest of the teardown. The early steps run alongside the stop because they do not
touch the install. Before the handler and service teardown, `_shutdown` waits
for the stop until `UPDATE_INSTALLER_STOP_SECS` after the cancel. Without that
wait, the 10 s cap's force-exit can orphan the installer mid-write.

**Once a stop is signalled (`shutdown_event` is set):**

- No new apply is admitted. `_prepare_auto_update_apply` returns False, and
  `SessionManager.pause_turn_admission_for_update` refuses, checked under its
  lock so a stop that lands while it waits is still seen. The gate cannot be
  `_closing`, because `close_all()` sets it only at the end of the shutdown.
- `POST /api/update` answers 503 `shutting_down`.
- `SessionManager.resume_turn_admission_after_update` keeps turn admission
  paused (checked under the same lock), so inbound turns keep being spooled
  instead of being admitted and then cancelled.
- `_restart_after_update` does not exec. An applied update takes effect at the
  next start instead of overriding the stop.

The widest window for these races is boot. The first coordinator cycle starts
before the MCP probe finishes, and the main flow reaches `shutdown_event.wait()`
only after it.

`systemctl stop|restart` on the generated unit already recovered before this:
the unit's control-group SIGTERM reaches `cli.sh` directly. The paths that
stranded the venv were `kirocrew stop`, Ctrl-C, `POST /api/shutdown` and the
installer's own 300 s timeout.

Not covered here:

- The git route reinstalls through `dep_sync.sync_or_reinstall` in an executor
  thread, and a cancelled await does not stop that thread, so its pip can
  outlive a shutdown.
- A policy apply started by `POST /api/update` runs in the request handler. The
  shutdown does not stop it first.

### Shutdown Sequence

1. First Ctrl+C sets `shutdown_event` → graceful shutdown begins (10s deadline)
2. Second Ctrl+C calls `platform_compat.hard_exit(0)` immediately (force exit: cancels any apply in flight, then `os._exit`)
3. `_shutdown()` cancels every managed-venv apply in flight (see "Automatic apply on a managed-venv install") and **stops the update coordinator** (see "Stopping an in-flight update installer"), then **disarms the loop-stall watchdog** (`dashboard_state._loop_watchdog.stop()` + cancels `_loop_heartbeat`), then saves active chat slots, cancels handler tasks, stops cron/heartbeat, closes sessions. The watchdog MUST be disarmed before `close_all()`/`cancel_all()` because that teardown deliberately kills every kiro-cli child — the same `os.waitpid` reaping burst the watchdog guards against — and a slow teardown would otherwise let the armed stall alarm (`setitimer(ITIMER_REAL)` with faulthandler's `SIGALRM` handler) end the process mid-shutdown (a clean quit would look like a crash). The watchdog's own `on_cleanup` hook fires too late (inside `AppRunner.cleanup()`, gathered concurrently with the reaping).
4. The gateway clears its port-keyed run marker in both dashboard and API-only
   modes, then `cleanup_orphaned_sessions()` kills any kiro-cli PIDs tracked in
   the PID file before `os._exit(0)`.

**Self-initiated exits carry a non-zero status.** `_shutdown_and_exit` composes
`shutdown_exit_code(watchdog) or listener_guard_exit_code(...)`, so an operator
stop still exits 0 while a shutdown the gateway asked for itself does not —
a restart-on-failure supervisor never relaunches an exit 0:

| Status | Source | Meaning |
| --- | --- | --- |
| 0 | operator (SIGTERM, `systemctl stop`, Ctrl+C) | stay down as asked |
| 75 (`EX_TEMPFAIL`) | stale-asset watchdog | the served assets vanished and no update step this gateway is running owns the gap. Not while the service manager could not relaunch the gateway (`supervisor_reentry`), or after an update chose to stay up instead of restarting |
| 69 (`EX_UNAVAILABLE`) | listener guard (`dashboard/listener_guard.py`), primary or secondary listener | the TCP listener could not be restored, so the process was alive but unreachable; a sidecar listener that cannot be withdrawn gives up with 69 before any rebind attempt |
| 78 (`EX_CONFIG`) | gateway lock refusal (`gateway_lock.LIVE_HOLDER_EXIT_CODE`), before the gateway runs — not a shutdown | the serving-holder predicate (`GatewayLock._serving_verdict`) is True: the process `/proc/locks` positively identifies as holding `gateway.lock` is running, holds the configured dashboard port with its OWN socket at the address this gateway is configured to bind, and answers HTTP there — a sibling gateway already serves this home. The systemd unit's `RestartPreventExitStatus=` names this one status so it is NOT relaunched (see [cli](cli.md), *Service Management*); every other lock refusal — a holder no surface can identify, however the recorded pid looks; a holder whose own socket at the probed address is silent (a wedged gateway); a holder on the port only at another address, or one the platform did not report (the residual row, unasserted by design, so a stranger's answer there is never credited to it) among them — exits 1 and is relaunched |

The listener-guard path is Windows-only in practice: CPython's proactor loop
closes the LISTEN socket after one failed `accept()` and never re-arms it. The
guard rebinds first and only sets this status when rebinding keeps failing, or
when the rebind binds yet the loopback `/api/live` probe still gets no answer —
a state no rebind can fix.

**The stale-asset watchdog stands down for this gateway's own update steps.**
Some update steps leave the served bundle missing while they run: a policy
`apply_command` replacing the install in place (the managed venv's unattended
apply builds beside the live tree, so the bundle it serves stays put), and a frontend build while `static/dist` is still the dev-mode link into
`website/dist` (Vite empties its output directory first; once
`_stage_dist_locked` has made `static/dist` a real directory, a rebuild leaves
it intact). Shutting down then cancels the step mid-write, and a cancelled
installer leaves a venv without its console scripts. So each such step the
gateway runs registers with `update_ownership` for as long as it rewrites the
install: the git auto-update (from the reset),
`CommandProvider.apply`, and the dashboard update's worker. So do
`_restart_after_update` and the dashboard's `_restart_gateway`, so a restart's
teardown is not raced. An update step hands the gap to the restart it awaits:
its own entry ends as the restart's begins, with no yield between. A restart
deferred while callback work drains stays owned for `DEFERRED_RESTART_MAX_SECS`
counted from the FIRST deferral; the coordinator's retries do not extend it, and
it ends early only when a restart commits (`restart_committed`, right before the
sessions close) or finds no usable interpreter (`clear_restart_deferral`), never
when a restart merely starts and then coalesces or refuses with an interpreter
still in place. A restart deferred for want of a usable interpreter is not owned
either: the pruned tree took the bundle with it, and the watchdog's exit is what
lets the supervisor relaunch through its own command. Both `_restart_after_update`
and the dashboard's `_restart_gateway` end the deferral on that refusal. Each kind has a generous
maximum duration (`update_ownership.Step`); an entry past it stops counting,
with a WARNING, so a step wedged on an unbounded wait cannot switch the
watchdog off for good. An expired entry is skipped and the search goes on to
older live ones, whatever its kind: the registry is shared by every task, so
the entry before an expired restart can be an unrelated update step (the
dashboard's worker next to the coordinator's restart) that still owns the gap.

The watchdog reads the registry (on the loop, no I/O) on every missing sample
and once more as the last thing before it signals, with no await in between, so
a step that starts inside the confirm or drain window still stands it down; it
names the owner in a WARNING. A bundle missing at startup while a step owns it
is waited out before the arming check; if it is still missing once no step owns
it, the update made that gap, so the watchdog arms and treats it as a vanish
(only a bundle missing at startup with no owner ever seen is a dev install that
leaves it disarmed). The registry is in-process only: a
shutdown can cancel only this gateway's own steps, never another process's
installer, so coordinating with a terminal `kirocrew update` belongs to a
cross-process lease, not to this.

Right before it signals, the watchdog also asks whether the gateway could be
relaunched (`gateway_restart.supervisor_reentry`). Its exit is a request to the
service manager, which runs its OWN command, so that command is what is tested:
on Linux the `ExecStart` systemd has loaded for the unit whose main pid is this
process (or the launcher that spawned it), read with a bounded `systemctl show`
so drop-ins and the scope that actually runs it count (a unit changed on disk
since it was loaded is not judged); on macOS the launchd agent's launcher
target. It must exist through its links and be executable, and when it is a
Python script its interpreter must import `kiro_crew.cli` and `kiro_crew.cli_server` in a bounded probe run
the way the relaunch runs it: without `-I`, under the unit's own `Environment=`
and `EnvironmentFile=` (or the plist's `EnvironmentVariables`), from `/`.
`systemctl` is resolved with `platform_compat.trusted_system_bin`, never from the
gateway's `PATH`; none there is inconclusive. A live target the relaunch would
exec into (`live_target.json`) is tested the same way, except that an exec of it
that would fail (its entry point or interpreter missing, unreachable or not
executable) is not a refusal: `live_target.maybe_reexec` catches that and boots
the supervisor's own build, so the command's verdict stands. A target that
starts and then cannot import is refused.
An apply that pruned the tree this process runs from while the command's stable
link points at a healthy new one therefore relaunches as before; a venv killed
mid-install, whose interpreter is present but whose package or console script is
not, is refused, and the refusal names a repair: `pip install -e` only for a pip
checkout's own venv, re-running the installer otherwise. `env NAME=VALUE ...
/abs/program` is followed to the program, with its assignments, once `env`
itself is present and executable (so is the launchd launcher before its target); a native binary
is judged only when it runs as the gateway (`<binary> gateway ...`). A shell
wrapper, any other native binary, an `env` option or `PATH` lookup, a service definition or `EnvironmentFile` it cannot read, or an
I/O error (`EIO`, `ESTALE`) checking a path is inconclusive. A command or
interpreter this user cannot reach or read (`EACCES`/`EPERM`, an exec-only `#!`
script included) is refused, because the service runs as this user and its
exec could not run it either; so is a command the kernel will not run (neither
a `#!` script nor an ELF / Mach-O image, as a console script truncated
mid-write is). Whether a generated service definition launched the gateway is read
from the launch marker even after `start_dashboard` consumed it
(`config.loader.launched_as_managed_service`), and the marker is handed back to
the gateway's own exec successor (`platform_compat.keep_for_reexec`), so an
in-app restart is still a managed launch. Without a service manager (a
foreground run, the desktop app, a container) nothing relaunches through a
command it can read, and nothing is refused.

An update that chose to stay up rather than restart records why
(`update_ownership.refuse_restart`): once it has moved the tree and its
dependency sync failed, or it raised after the move. The move counts from the
moment its `git merge` / `git reset` child started: a spawn that raised wrote
nothing, so it records nothing. A later update that synced the dependencies of
the tree it moved clears it, so an attempt that fails before replacing anything
keeps it. So does a re-entry check whose import probe of the supervisor's
command succeeded (an install repaired out of band), but only for a
refusal recorded before that check began (`update_ownership.refusal_generation`).
Under a managed launch the watchdog reads it with no await before the signal and
refuses on it as well; without a service manager it is not applied, so that exit
behaves as before.

A refusal is logged once per reason and keeps the gateway up on its loaded code;
it is asked again on a backoff (doubling to `_REENTRY_RECHECK_MAX_SECS`, a few
check intervals) without re-running the confirm and the drain; a positive answer
runs them again, and the check after the drain decides. A check that fails for
any other reason, or does not answer within its bound, is inconclusive and its
reason is logged: not a refusal, but not an end to a standing one either. The
check runs on its own worker; one still running is waited on again rather than
started twice, one that answered after its ask stopped waiting is read at the
next ask, and a healthy sample drops it. Work admitted while the check ran is
drained again, within what the first drain left of `_DRAIN_TIMEOUT_SECS` (one
budget covers both, so a wedged turn holds the signal for at most one drain's
worth), and that drain is the last await: the presence, owner and stay-up reads
that follow it, and the signal, do not yield.

### Event-loop stall watchdog & blocking-work executors

The gateway runs a single asyncio loop, so any blocking call on the loop thread freezes the whole backend. App Home skill loader construction and listing run together in a worker: listing can initialize/read the persistent SQLite metadata index. Two mechanisms contain this (see `dashboard/loop_watchdog.py`, `executors.py`):

- **Lag enrichment** — when the heartbeat measures lag over 1 s it claims one lag enrichment per episode (`claim_lag_enrichment`: skipped when `check()` already enriched or a capture is in flight, with a 60 s cooldown) and writes it through `log_lag_enrichment` to the logger only, never to the dump file.
- **`LoopStallWatchdog`** — armed only when `faulthandler.is_enabled()` (the real `gateway` entrypoint; not `chat`/`tui`). The async heartbeat (`dashboard/server_runtime/heartbeat.py`, 5s interval) `beat()`s it each tick, re-arming the kernel's per-process alarm (`setitimer(ITIMER_REAL)` for `exit_after` seconds, `platform_compat.arm_process_alarm`) with `faulthandler.register(SIGALRM, chain=True)` on the crash-dump file: if the loop goes silent, the alarm dumps all thread stacks from inside the signal handler — in C, with no GIL, so it fires whether the loop thread is blocked in a syscall or holding the GIL inside a long C call — and then hands `SIGALRM` to its default disposition, which ends the process. **A suspend is not a stall:** the alarm pauses while the host sleeps (Linux runs `ITIMER_REAL` on `CLOCK_MONOTONIC`; macOS schedules it on the absolute mach timebase), and the loop's own monotonic clock stands still too, so a laptop resume misses no beat and fires no deadline. faulthandler's own `dump_traceback_later` timer cannot be that decider on every platform: it waits on an interpreter lock whose deadline clock is fixed when CPython is built (`sem_clockwait(CLOCK_MONOTONIC)` with `HAVE_SEM_CLOCKWAIT`, otherwise `sem_timedwait` on `CLOCK_REALTIME`, which jumps by the whole suspend on resume and fires any pending deadline the instant the host wakes, whatever its budget — the branch every portable interpreter build and every macOS build takes). Windows has no process alarm, and a process that already handles `SIGALRM` from Python (pytest-timeout in a test worker, an embedding host) owns `ITIMER_REAL` too; in both cases that timer carries the exit at the same budget and the alarm is never armed or cancelled (`exit_mechanism()`; the startup line says `exit_after=<budget> (alarm|faulthandler)`). The mechanism is decided once per arm and latched (`_armed_mechanism`), and each beat's cancel targets the latched one, so a `SIGALRM` owner that appears between two beats moves the exit onto faulthandler's timer at the next re-arm instead of leaving the pending alarm to fire beside it. Each alarm arm releases faulthandler's `SIGALRM` registration and registers it afresh: a repeat `faulthandler.register` reinstalls nothing while faulthandler believes it still holds the signal, so a temporary owner that handed `SIGALRM` back with `SIG_DFL` would otherwise leave the next alarm to end the process without a dump. On Windows nothing new is lost: its `time.monotonic()` counts a sleep as well, so a sleep already reads as silence there. The exit is by `SIGALRM` rather than status 1 and the dump carries no `Timeout (` preamble line; no consumer of either exists. `SIGALRM` and `ITIMER_REAL` belong to the watchdog in the gateway process, and to no successor image: the exec seams cancel the alarm before `os.execv` and the successor's entrypoint clears any deadline that still arrived (see "Restart after update"). A daemon thread measures the silence since the last beat on the **monotonic clock** (`time.monotonic()`) for the observability layer — enrichment, then the soft dump — on its 5s poll; each poll also samples the suspend-inclusive clock `platform_compat.boottime_now` (`CLOCK_BOOTTIME` on Linux, the wall clock on macOS, `None` where none exists) and an advance there of `SUSPEND_SKEW_MIN_SECS` (2s) or more beyond the monotonic advance is logged once at INFO as a resume; it decides no exit. Desktop/foreground launches automatically use 25s; managed systemd/launchd gateways automatically use 90s because they have no Electron probe and WSL, VM, or heavy disk pressure can suspend scheduling long enough to make 25s a false death. The config value is nullable/automatic so an unrelated full config save cannot pin either launch-class default; any explicit `dashboard.loop_stall_exit_after_secs` value, including 25, overrides both. Older full-config saves may have materialized the former 25-second default; Kiro Crew reports that through the read-only superseded-default warning and `doctor` rather than guessing whether the value was deliberate. The managed path emits a non-fatal all-thread dump to stderr at `stall_after=30s`, never to the fatal crash-sentinel file, then exits at its service budget if the loop has not recovered. If the alarm is off (`exit_after=None`) or fails to arm or re-arm, no fatal capture can follow, so that soft-only dump is written to the dedicated dump file as well as stderr to remain discoverable. `KIROCREW_SERVICE_MANAGED=1` in the generated systemd unit or launchd plist is the sole managed-launch authority; inherited systemd metadata is deliberately ignored because descendants receive it too. `kirocrew doctor` detects an installed definition without the marker and tells the operator to run `kirocrew service install` once to regenerate it and adopt the managed-service default.
- **Cautious boot** (`dashboard/cautious_boot.py`, `dashboard.cautious_boot`, default on, restart-only) — when startup finds a loop-stall crash dump with thread stacks under 30 min old (`RECENT_DUMP_MAX_AGE_SECS`), `pause_before(group)` staggers the startup battery (MCP servers, cron scheduler, app backends, session restores): `MAX_DELAY_SECS` (10 s) on a `tight`/`critical` `resource_status` posture, `MILD_DELAY_SECS` (2 s) otherwise. A third signal downgrades it one step: both sites that publish `DashboardState.ready` dispatch (never await, so a stalled data home cannot hold `KIROCREW_READY`) `crash_dump_store.record_healthy_boot`, which writes this process's `(pid, domain, start_id)` to `<dumps dir>/last-healthy-boot` through `atomic_write`; `dump_owner_reached_healthy` matches it against the dump header, and when the dump's writer had reached ready the next boot runs un-staggered on a calm host and mildly staggered on a pressured one. The match fails closed toward staggering: a missing marker, a start identity absent on either side, a recycled PID or another host's marker all answer no, and the read uses `O_NOFOLLOW`/`O_NONBLOCK` plus an `S_ISREG` check, bounded to 256 bytes, so a planted link or FIFO reads as absent. Any evaluation error boots normally (#16954).
- **Bounded executors** — blocking work is offloaded off the default executor (which the loop uses for DNS) into dedicated bounded pools defined in `executors.py`; for example `maintenance_executor()` (`mc-maint`, fast orphan-reaping sweeps + agent-overlay rewrites) and `cron_executor()` (`mc-cron`, long/concurrent cron command & script jobs), kept separate so a burst of cron jobs cannot starve the orphan sweeps. MCP `probe_all()` fan-out is bounded by `PROBE_MAX_CONCURRENCY` (5).
- **`init_socket_mode` is a coroutine awaited ON the loop, never offloaded whole** — `WSSocketModeClient.__init__` ends in `asyncio.ensure_future`, which requires a current event loop in the constructing thread, so running the function in a `to_thread` worker would crash every Slack-enabled boot with `RuntimeError: There is no current event loop` (under systemd the unit would crash-loop into `StartLimitBurst` and stay `failed`). Its two blocking calls — the YOLO grant's profiles-dir walk (`set_yolo_mode` → `grant_declared_yolo`) and the enterprise `auth.test` network call (`validate_enterprise`) — are offloaded individually *inside* the coroutine, which keeps the security-relevant early-return ordering (owner check → YOLO grant → enterprise validation) intact. Pinned by `test_slack_events_coverage.py::TestInitSocketMode` — including a test that constructs the **real** `WSSocketModeClient` (a mocked constructor would hide the defect) and a source-level pin refusing `to_thread(init_socket_mode, ...)` at the gateway call site.

### `handle_message(slack, sessions, channel, text, thread_ts, msg_ts, user_id, approval_mode, ..., subagent_manager) -> None`
Processes a single incoming message with streaming. It stays in `slack/handler.py` as
the turn's orchestrator; the phases it delegates run in the
[owners](#native-handler-composition) -- the `!` routing (`_route_bang_command`), the
thread context (`turn_context.py`), the Slack wire of the answer (`_AnswerStream`), the
review-mode draft and the dashboard mirror (`finalize.py`) and the voice reply
(`voice.py`):

**Session key discipline:** the handler derives two values at entry —
`reply_ts = thread_ts or msg_ts` (the bare Slack thread timestamp, used for
posting replies and as the key of thread-indexed maps: `SessionMap`'s
thread→session index and the dashboard `_slack_to_slot` map) and
`session_key = canonical_key(reply_ts)` (the namespaced `slack:<ts>` form,
used for everything session-scoped: `SessionManager` registry, conversation
log, per-thread override maps, trust set). The canonical form is stable
across all messages of a thread; the legacy bare form is folded onto the same
live session by `SessionManager._fold_key` (see session.md).

`slack.dm_single_session` (default off) splits those two for a 1:1 DM. A
message in a `D…` channel runs under `slack:<channel_id>` —
`flat_dm_session_key`, one session for the whole DM instead of one per
message — and a top-level message posts at channel root, so `post_thread_ts` is
`None` while `reply_ts` keeps its thread-index and reaction meaning. A THREADED
reply in that DM joins the same session: in a 1:1 DM a thread is a layout habit
rather than a new topic, so splitting it off would leave the branch without the
conversation it answers. Only the session merges — the reply, the `!stop` ack and
a privacy modifier's confirmation all still post where they were addressed, back
inside the thread. The session is bound to
the channel (`set_channel`) and NOT to a thread: a flat conversation has no
thread for `set_slack_link` to claim, claiming one would give the dashboard
mirror a thread to post into while the conversation itself is flat, and with
several threads the scalar `slack_thread_ts` would flip to whichever spoke last.
Routing needs no claim regardless: the flat key is DERIVED from the channel, so
it is recomputed rather than looked up. That holds
for every writer of the link, not just the turn's own self-link:
`maybe_apply_privacy_modifiers` takes a separate `link_thread` flag, which is
false in flat mode, so `!temporary` / `!incognito` register no thread while still
confirming in place. A thread already claimed by its own per-thread session — the
shape this feature replaces, e.g. from before the flag was on — is ignored so it
cannot pull the turn back out of the merged conversation; any OTHER owner (a
dashboard send-to-Slack) still wins.
Group channels and group DMs (`mpim`) are excluded — a thread there is
a deliberate scope boundary, and an `mpim` is shared with other people. The key
keeps the two-segment `slack:<scope>` shape on purpose, so callers that treat a
Slack key as opaque or reverse-derive from it are unaffected.

One consumer needs the shape spelled out: `file_send`'s upload handler resolves
its target from the session map, and its thread-first branch requires a thread
before it will use the linked channel. A flat DM has a channel and no thread, so
that branch alone would fall through to the owner's DM — a file sent to a different
conversation than the one that asked. The handler therefore also accepts "channel, no thread" when the
session key IS that channel's key (`slack:<channel_id>`), delivering at the DM's
root. Deliberately not broader: a thread-scoped or dashboard session that merely
knows a channel keeps failing closed to the owner DM rather than broadcasting at
the root of a channel it does not own.

`_route_message` derives the same key for its busy/queue bookkeeping; keyed on
the message ts instead, a second DM would read as not-busy, skip the queue and
block inside `get_or_create` with none of the queued-message feedback. That
derivation (`_dm_single_session_enabled`) additionally requires the turn to
take the messaging-transport path, because only `handle_message_transport`
honours the flat key: with `messaging.use_transport` off, or in a review-mode
channel that `_route_message` deliberately keeps on native for its privacy
gate, the turn runs under `canonical_key(msg_ts)` and the bookkeeping keys the
same way. Both conditions live in that one helper so `!stop`, the queue check
and `message_deleted` cannot disagree.

1. Check hooks for auto-reply
2. Route a linked thread to its linked session, and apply privacy modifiers
   (`!temporary` / `!incognito`)
3. Check `status` keyword — reply with stats summary
4. Route `!` commands through `_route_bang_command` (owner-only, with
   `!dashboard` / `!stop` / `!title` open to allowed users)
5. Check keyword commands (`maybe_handle_keyword_command`: `sessions`,
   `spawn`/`bg`, `cron …`, `task run <spec>`)
6. Initialize `StatusReactionController` → set phase "queued" (👀)
7. The answer message opens lazily (`_AnswerStream.ensure_started`); there is no
   "Thinking…" post
8. Acquire per-session semaphore (via `get_or_create`) to serialize concurrent messages
9. Create `Task` for lifecycle tracking
10. Stream events from provider
11. Progressive message edits (~1/sec) with cursor indicator (▍)
12. On `text_chunk` event: accumulate response text, set phase "thinking" (🤔)
13. On `thinking_chunk` event: accumulate thinking separately, set phase "thinking" (🤔)
14. On `tool_call` event: set phase based on tool type — coding (👨‍💻), browsing (🌐), or generic tool (🔧)
15. On `permission_request` event: pause stall watchdog, auto-approve or post Block Kit buttons, resume watchdog
16. On `complete`: record success, check context usage
17. On error: record a breaker failure (trips at 5 consecutive) only when
    `runtime_death.caused_by_this_session` is true; a shared runtime's
    `AcpProcessDied` is counted on a shared-death streak instead, and at the
    threshold the handler resets the session and clears the streak
18. Finalize status reactions in `finally` block → done (🦞) or error (😱); release semaphore
19. Strip inline `<thinking>` tags from accumulated text
20. Final update with mrkdwn-converted response (split into multiple messages if over 3900 chars by `split_message` in `slack/format.py`)
21. Post thinking content as 💭 thread reply (if any, and `slack.show_thinking` is true)

### `StatusReactionController`
Phase-aware Slack reaction manager with stall detection, defined in `slack/handler_runtime/reactions.py`; the phase table it reads (`_PHASE_EMOJIS`, from `slack.reactions`) is built at import in `slack/handler.py`. Manages emoji lifecycle per message:
- **Phases**: queued (👀) → thinking (🤔) → coding (👨‍💻) / browsing (🌐) / tool (🔧) → done (🦞) / error (😱). All phase emojis are configurable via `slack.reactions` in `config.json`.
- **Debouncing**: Intermediate phase transitions debounced at 700ms to prevent flickering from rapid tool calls. Terminal states fire immediately.
- **Stall detection**: Soft stall (🥱) at 15s, hard stall (😨) at 45s of no progress. Resets on any ACP event. Paused during tool approval waits.
- **Tool mapping**: `_tool_to_phase(tool_name, tool_kind)` maps tools to phases — prefers `tool_kind` from ACP, falls back to tool name with MCP `__` separator handling.

### LLM-Initiated Commands

The LLM executes cron and spawn operations via bash using the `kirocrew` CLI:
- `kirocrew cron add "name" "message" --every 300` — writes to crons.json, gateway auto-detects via mtime sync
- `kirocrew spawn run "task"` (subcommands `run`, `list`) — POSTs to dashboard API at localhost:5476, gateway spawns subagent

### `handle_interaction(channel, msg_ts, action_id, user_id="", thread_ts="", slack=None, sessions=None) -> str | None`
Routes Block Kit button clicks to pending tool approvals:
- `approve_tool` action → `AcpClient.approve_tool()`, resumes streaming
- `reject_tool` action → `AcpClient.reject_tool()`, stops streaming

A click on a linked dashboard slot's prompt (`_resolve_linked_click`) and a Trust click
whose approval already resolved (`_grant_late_trust`) are `slack/handler_runtime/approvals.py`;
the claimed region that answers the wire stays in `slack/handler.py`.

### `SlackClientOps` (ABC)
Testable interface for the Slack Web API. `slack/client.py` owns the method list
(posting, updating and deleting messages, reactions, pins, `upload_file`,
`open_dm`, `views_*`, streaming, the `fetch_*` readers, `download_file`, …).

## Per-Channel Activation Modes

Each channel can have its own activation mode controlling when the bot responds:

| Mode | Behavior |
|------|----------|
| `always` | Process every message from allowed users |
| `mention` | Only respond when @mentioned; continue in thread replies if bot has active session |
| `observe` | Passively record all messages with deep history buffer; respond only when @mentioned (like `mention` but with richer context) |
| `off` | Ignore all messages completely — no history recorded |
| `review` | Generate a response and show the owner an ephemeral draft to approve before it is posted. Set in config only; `!channel` accepts `always\|mention\|observe\|off` |

**Defaults**: DMs (`D`-prefix) default to `always`. Group channels (`C`/`G`-prefix) default to `mention`.

**Config** (`config.json`):
```json
{
  "slack": {
    "channels": {
      "C0123ONCALL": { "activation": "always", "agent": "ops" },
      "C0456REVIEWS": { "activation": "mention", "agent": "reviewer" },
      "C0789GENERAL": { "activation": "off" }
    },
    "dm_activation": "always"
  }
}
```

**Per-channel agent override**: Each channel can specify an agent that overrides the global default. The agent is passed to `SessionManager.get_or_create()`.

**Thread reply behavior** (`thread_follow`, in mention, review and observe mode): When the bot is @mentioned in a group channel, it responds in a thread. A later reply in a thread is admitted without an @mention when the bot has a session for the thread, a session link for it (`get_session_for_thread`), or a conversation log for it; it is then skipped if it is addressed to someone else (below). Replies in threads the bot has none of these for are ignored.

**Replies addressed to someone else** (`thread_follow`, in mention, review, and observe mode): one admission rule covers every followed-thread reply that does not arrive as an @-mention. A reply whose text, after leading whitespace, starts with one or more @-mentions of other users or bots and none of this bot is addressed to them and is skipped (SEL `slack.message` denied, `thread-follow: addressed to another user`), so a reply handing the thread to someone else is not talked over. The check reads the message text after forward and Block Kit recovery. A reply with no leading mention is answered, including one that only names someone in passing (`please retry the deploy, cc <@U…>`), and so is a reply that starts with a mention of this bot: Slack also delivers that as a plain `message` event, which reaches this rule and is admitted by it. The bot's own user id comes from startup `auth.test` (`enterprise.validated_self_user_id()`); when it is unknown the check is skipped and the reply is answered.

**Owner commands** (`!channel`):
- `!channel` — show current channel activation mode and agent
- `!channel always|mention|observe|off` — set activation mode, persisted to `config.json`
- `!channel agent <name>` — set per-channel agent override
- `!channel agent off` — remove per-channel agent override

**Implementation**: `events.py:_route_message()` checks `orch._cfg.channel_config(channel)` before dispatching. The `@mention` prefix is stripped from text before sending to the LLM. `_persist_channel_config()` (`slack/handler_runtime/inbound.py`, re-exported by `slack/handler.py`) writes to `config.json` atomically via tmp+rename.

## Tracking Channel Monitoring

### Slack Commands

#### Slash Command (`events.py`)

Command name configurable via `slack.command` in config (default: `kirocrew`).

| Command | Handler | Purpose |
|---------|---------|---------|
| `/<command> #channel` | `_handle_slash` | Tracking-channel prompt (Track/Ignore) to owner |
| `/<command> sessions [all\|ended]` | `_handle_slash` | List the last `slack.sessions_limit` recent sessions as task cards; each card's Resume button (`mc_session_resume_<key>`) resumes it in the current thread |
| `/<command> dashboard` | `_handle_slash` | Generate presigned dashboard link (DM'd to user) |
| `/<command> restart` | `_handle_restart` | Restart the gateway (owner-only; requires an `INVOCATION_ID` / systemd supervisor, else refuses). SEL-audited (approved/denied). Best-effort `save_all_slots_to_history` + `close_all` + `sel.flush` (each bounded by `wait_for`), then `platform_compat.hard_exit(1)` (cancels any apply in flight, then `os._exit(1)`) so the supervisor respawns |

#### Owner-Only `!` Commands (`handler.py`)

Only the owner may use the bot over Slack. Restricted to `KIROCREW_OWNER_ID`.
Processed before keyword commands. Each `!` command is one coroutine in
`slack/handler_runtime/commands.py` (`_bang_<name>`), dispatched by
`_handle_slash_command`; the sender gate in front of it is `_route_bang_command`,
which `handle_message` calls.

| Command | Purpose |
|---------|---------|
| `!yolo on/off/renew/status` | Toggle, renew or report global auto-approve for all tool calls |
| `!agent <name>` / `!agent off` | Switch kiro-cli agent globally (all new sessions) |
| `!ta <name>` / `!ta off` | Switch agent for current thread only |
| `/agent <name>` | Same switch as `!ta <name>`, owner-only, routed by `_route_bang_command` before the `!` gate. In a thread linked to a dashboard chat it reaches that chat's runner instead, which switches the chat's agent |
| `!voice …` | Voice reply on/off/global/<name> and engine/speed/pitch controls |
| `!project <path>` / `!project off` | Thread-scoped agent-discovery directory |
| `!channel always\|mention\|observe\|off` / `!channel agent <name>` | Per-channel activation mode and agent |
| `!link-to-dashboard` | Import the Slack thread into the dashboard |
| `!allowlist` | Replies that multi-user access is disabled; grants nothing |
| `!restart` | Restart the gateway. Bang alias intercepted in `events.py` before the LLM session; delegates to `/kirocrew restart` (`_handle_restart`) so owner-check + supervisor guard stay a single source of truth (`handler.py:_BANG_TO_SLASH`) |

#### Allowed-User `!` Commands (`slack/handler_runtime/commands.py`)

Available to any allowed user (in practice the owner, since multi-user access is
disabled).

| Command | Purpose |
|---------|---------|
| `!dashboard [duration]` | Get a presigned dashboard link (DM'd to you) — **deprecated, use `/kirocrew dashboard`** |
| `!title` | Set or generate the Slack thread title |
| `!stop` | Force-halt the active agent execution in the current thread. Sends cooperative `session/cancel`; falls back to hard kill if not acked within `agent.soft_stop_budget_secs`. Posts ephemeral Block Kit stopping message with Kill Now button. If no execution is running, replies "Nothing running." |

#### Keyword Commands (`handler.py`)

Available to all allowed users.

| Command | Handler | Purpose |
|---------|---------|---------|
| `status` | `handle_message` | Runtime stats summary |
| `sessions` | `maybe_handle_keyword_command` | Same as the `sessions` slash command |
| `spawn <task>` / `bg <task>` | `_handle_spawn_command` | Run subagent (blocking / async) |
| `spawn list` / `spawn status` | `_handle_spawn_command` | List active subagents |
| `cron list` / `cron remove <id>` / `cron pause <id>` / `cron resume <id>` | `_handle_cron_command` | Manage cron jobs |
| `task run <spec>` (alias `project run <spec>`) | `_handle_run_command` | Start autonomous task runner |

### Channel Monitoring
- Config: `config.json → slack.tracking_channels` — list of channel IDs to watch
- Event: `member_joined_channel` — fires when a user joins a channel the bot is in
- Requires `channels:read` scope (for public channels) and `groups:read` (for private)
- A user joining a monitored channel prompts nothing: multi-user access is disabled, so there is no allowlist prompt to send
- If `tracking_channels` is empty, no monitoring occurs
- Tracked channels are capability-probed (`slack/scope_probe.py`, one `conversations.history` call with `limit=1`) after the socket connects at startup and whenever a channel is added to tracking. A `missing_scope`/`channel_not_found` result logs a warning and pushes a dashboard notification — a private channel tracked under an install predating `groups:history` would otherwise fail silently. Deferred (`asyncio.create_task`), best-effort: transient network errors report nothing
- `/<command> @user` refuses for the same reason
- `/<command> #channel` adds a tracking channel via owner approval

## File Attachment Processing

Slack `file_share` messages are processed in `_route_message()` after dedup + auth. The classes (from `messaging.attachments`) are handled in order:

### Voice / Audio (`kiro_crew/transcribe.py`)
- **Mimetypes**: `audio/*`, `video/webm`
- **Flow**: Download via `SlackClientOps.download_file()` → `transcribe.transcribe_audio()` → transcription text prepended as `[Voice memo transcription]...[End of transcription]`
- **Config**: Enabled by default (`stt.enabled = true`). `stt.provider` decides where recognition runs, and the default `local` runs it in this process on a resident whisper.cpp model, so a memo costs one model download (`stt.model`, `base` by default) and nothing after that. A stored retired provider degrades to `local`; there is no binary to put on `PATH`. Availability per provider comes from `transcribe.availability_detail()`, which distinguishes a missing `voice` extra from a platform with no prebuilt recognizer and from a macOS too old for the `apple` provider, because those need different fixes. The pinned `imageio-ffmpeg` wheel decodes the memo's ogg/Opus or webm internally and is bundled in desktop releases; users do not install system FFmpeg. Setup: [configuration](../../../src/kiro_crew/docs/configuration.md) § Speech-to-text.
- **Provider-independent guards**: `transcribe_audio` refuses a sensitive `audio_path` and redacts every provider's output before returning, both before/after dispatch rather than inside a branch, so a provider cannot be added that skips either. See [stt-streaming](stt-streaming.md).
- **Security**: Transcription output run through `redact_credentials()` + `redact_exfiltration_urls()` before injection. Audio file suffix sanitized to alphanumeric only. `_transcribe_files` records a `slack.download_file` and a transcription SEL entry per memo.

### Images (`files.py`)
- **Mimetypes**: `image/png`, `image/jpeg`, `image/gif`, `image/webp`, `image/bmp` (aligned with `AcpClient._send_prompt()` regex)
- **Size limit**: 10 MB (checked from Slack metadata before download and actual bytes after download)
- **Flow**: Download to temp file → inject local path into message text → `_send_prompt()` detects path, base64-encodes, sends as `{"type": "image"}` content block to kiro-cli
- **Temp lifecycle**: Caller (`_route_message`) owns cleanup. Done callback on `handle_message` task cleans up after `_send_prompt()` reads the file. Early-return paths and `create_task` failures also clean up. Queued messages carry their paths in the entry's `image_temp_paths` kwargs; `_dispatch_queued` unlinks after the turn consumes them, and the queue-discard paths — `cancel_queued`, `clear_queue`, `dequeue`'s cancelled-skip, and the `_pending_queue` drops in `_handle_message_deleted` and the `!stop` handler — unlink via `session.unlink_queued_temp_paths()` so entries that never dispatch don't leak files. Known gap: session-teardown paths (restart/remove/destroy/idle sweep) drop `session.queue` without unlinking.
- **Non-inlineable images** (`image/svg+xml`, `image/tiff`, etc.) use the opaque-file path below; they are never injected as ACP image blocks

### Documents (`messaging/attachments.py`)
- **Formats**: documents `doc_parser.is_parseable_document` accepts (PDF, DOCX, …)
- **Size limit**: 20 MiB (`max_document_bytes`)
- **Flow**: text extracted by `doc_parser.extract_text` in a worker thread, then handled as text below

### Text / Code Files (`files.py`)
- **Mimetypes**: `text/*`, `application/json`, `application/xml`, `application/javascript`
- **Size limit**: 512 KB download cap, 50 KB injection cap (truncated with `[… truncated]` marker)
- **Flow**: Download to temp → read with `errors="replace"` → redact credentials/URLs → inject as `[File: name]\ncontent\n[End of file]`
- **Temp lifecycle**: Always cleaned in `finally` block (text content is read into memory, file not needed after)

### Opaque Files
- **Mimetypes**: `video/*` and every format not handled as inlineable image, text/code, document, or audio; this includes ZIP, binary payloads, SVG, and TIFF
- **Size limit**: 50 MB per file, checked against Slack metadata before download and authoritative bytes after download
- **Flow**: Stream authenticated bytes to a randomized `tempfile.mkstemp()` path → inject the bare local path plus `[Attached file: name]` metadata (original mimetype and actual byte count) → expose the complete file to agent tools
- **Integrity and lifecycle**: Bytes are not transformed. The current or queued turn owns the path and unlinks it after the agent turn completes, or when a queued entry is discarded; early-return and task-creation failures also clean it up
- **Passive by default**: Opaque content is never automatically parsed, extracted, or executed. An inlineable image suffix (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`) is stripped from the temporary path, because the ACP encoder types a path by suffix alone — otherwise a file named `photo.png` but declared `application/octet-stream` would be inlined as an image without passing content-signature validation. Agent tool access remains subject to normal permissions and hooks
- SEL audit logs successful downloads, pre/post-limit skips, and failures

### Safety Controls
- Type-specific size limits are checked from Slack metadata *before* download and against actual bytes *after* download
- Filetype suffix sanitized to alphanumeric only (prevents path traversal)
- `tempfile.mkstemp()` for all downloads — never uses original Slack filename
- `redact_credentials()` + `redact_exfiltration_urls()` on all text content
- SEL audit on every download, skip, and error

## Streaming UX

- Response streams in real-time via progressive Slack message edits
- Edit throttled to ~1/sec to avoid Slack rate limits (Tier 3: ~50 req/min)
- Cursor indicator (▍) shown during streaming, removed on completion
- Tool calls shown inline as 🔧 _tool name_
- **Thinking/reasoning content** filtered from the main response — accumulated separately and posted as a 💭 thread reply after the main message. Inline `<thinking>` / `</thinking>` tags are also stripped as a safety net. The thread reply is suppressed when `slack.show_thinking` is `false` (default `true`).
- Final message split into multiple posts if over 3900 chars (via `split_message()`)
- A top-level reply in a flat DM is not streamed (`chat.startStream` streams only into a thread); it falls back to `chat.update` edits
- The transport stream holds back a trailing partial word of up to `_WORD_HOLD_MAX` (32) characters until the next chunk or the end (`release_held_word`), so a word is never split across two edits
- When part of a reply could not be delivered to Slack and not restored, the turn appends a "Part of this reply did not reach Slack…" delivery-debt notice
- The per-turn wire state -- the stream message and its rotation, the rolling redactor, the delivery debt and the task cards -- is one `_AnswerStream` (`slack/handler_runtime/stream.py`) per turn; the delivery verdict that reads its flags stays in `handle_message`
- **Redaction notice** — when the delivered text (answer or thinking) still carries a `security.CREDENTIAL_REDACTION_TAGS` placeholder or a `security.EXFILTRATION_REDACTION_TAG_PREFIX` (suspicious-URL) placeholder, one `messaging.renderer.redaction_notice` message is posted in the thread after the answer is committed, so the reader knows a command or link they copy will not run as pasted. Worded by kind (credential → re-enter the secret; URL → re-check the link), and byte-identical to the prior `credential_redaction_notice` sentence when only credentials were rewritten. Redaction is NOT relaxed — Slack is an egress path. Counted from the tag in the sent text rather than the redactor's warnings list, which is empty on the streaming path because each chunk was already redacted upstream. **One notice per turn**: answer and thinking share a single tally. Approving a review-mode draft (`interactions.py`) posts the same notice for the same reason, since that publishes to the whole channel. Both posts are best-effort — a failed notice must never turn a delivered answer into a failed turn. The transport-path renderer (`slack/renderer.py`, the default `messaging.use_transport` delivery) posts the same one-per-turn notice: the final display-safe answer body and the posted 💭 reasoning share a single tally, counted with `messaging.renderer.count_redaction_tags` over the form the reader is left with — which can carry placeholders the driver's byte-level stream scan never wrote, because `_display_safe` re-redacts against what Slack renders

## Message Queue (`session.py` + `events.py`)

When a message arrives while a session is actively processing, it's queued instead of spawning a competing session:

- **Session-level queue**: `enqueue()` / `dequeue()` on `SessionManager` using a per-session `deque` + cancelled set
- **Orchestrator-level queue**: `_pending_queue` dict for the startup race (task running but session object not yet created)
- **⏳ reaction**: added to queued messages so the user sees visual feedback
- **FIFO drain**: `_on_done` callback drains both queue levels after each handler completes
- **Cancellation**: `message_deleted` event removes queued messages or marks in-flight messages as cancelled; a `!stop` sent as a thread message (`_route_message`) clears the CALLER's queued messages only — every enqueue site and the `_pending_queue` stash tag each entry with `queued_owner` (`owner_token("slack", (sender_id, channel))`, via `_queue_tags`). The handler detaches both queues at the press into a per-key hold beside `_session_tasks` (`_stops_in_flight`, read through `_key_busy`) and calls `stop_turn(..., preserve_queue=True)`; a soft or idle outcome records the presser's owner token in the hold's drop set with the index of the snapshot that stop detached, so only entries detached at or before it are dropped (a message sent after one's own `!stop` survives another member's overlapping stop). The hold is released from one `finally` on every path. While any stop on the key is in flight `_key_busy` marks it busy, so neither turn-end drain, `_drain_slack_queue`, nor the busy check starts a message a stop would cancel. When the last overlapping stop settles, `_settle_stop_hold` merges the detached snapshots in press (= arrival) order, drops each stopping member's entries up to their stop's snapshot (`clear_queue(only=...)`, unlinking their temp files) and puts the rest back at the head of their queue, then runs `_drain_slack_queue` (which falls back to `_pending_queue`), so a kept entry is dispatched even when the stopped turn was never registered there. A stop that escalates to a hard reset still drops every member's queued messages (the forced repeat on a compacting session instead carries them all to the successor); the other Slack stop entry points (`handler.py`'s inline `!stop`, the interaction buttons) still clear the whole queue. An untagged entry (a dashboard-linked session's own queue) is nobody's to drop. The owner is the sender in the channel, not the thread: a thread session is one thread, and a single-session DM merges its threads into one key by design. The `message_deleted` drop in `_handle_message_deleted` stays keyed by timestamp — a deleted message is one entry, not one person's.
- **`is_cancelled()` check**: handler checks before responding and before the LLM call to suppress responses for deleted messages

## Linked Thread Sync (`handler.py` + `interactions.py`)

Bidirectional message mirroring between dashboard chat sessions and Slack threads:

- **Slack → Dashboard**: `handle_message()` asks `maybe_route_linked_thread` (`slack/handler_runtime/inbound.py`), which checks `_slack_to_slot` reverse lookup; if linked, routes message to dashboard slot's `_run_chat()` queue
- **Dashboard → Slack**: `_run_chat()` mirrors user messages and agent responses to the linked thread via `start_stream()` / `append_task()` / `stop_stream()`
- **Link to Dashboard button**: `LINK_DASHBOARD_ACTION` in timing footer imports thread history into a new dashboard slot
- **`!link-to-dashboard` command**: same as button but triggered via bang command inside a thread
- **Session resume**: shows Thread/DM choice buttons; `_handle_resume_choice()` with per-session lock for idempotency
- **Automatic link** (`dashboard/chat_slack.py` `maybe_auto_link_slack`, called from the send handler when `slack.auto_link_sessions` is on): a person's own dashboard session gets a thread in the owner DM on its first message through the same `link_slot_to_slack` helper the Connect to Slack row uses. There is no target setting: the automatic thread always opens in the owner's DM with the bot, which only the owner can read. Eligibility reads slot facts only (USER origin, no agent creator, not channel-born or remote, persistent memory, exactly one user row with none older on disk, no link yet) and skips a harness slash command. The automatic path runs the fail-closed channel governance check before any Slack call, escapes the anchor title, and holds the send at most `AUTO_LINK_HOLD_SECS`. A link that lands inside the hold is read by the turn's start-of-turn link read, so that turn is mirrored live. A slower link finishes as a tracked task and is still made, but owes the thread nothing: the turn that ran during the hold is not replayed, and live mirroring starts with the next turn. Nothing is backfilled into an automatic thread. `link_slot_to_slack` runs the governance check unless a caller passes `governed=False`, which only the Connect row does. A Stop pressed during the hold finds no task to cancel, so the send handler compares `_stop_generation` across the hold and does not dispatch the turn (`{ok, stopped: true}`); nothing is replayed for it. One weak-value lock per session key (`state._slack_link_locks`) serialises automatic and manual attempts; a session closed mid-anchor is not linked (`slot_closed`, 409), and a link whose session-map write fails is taken back down in memory for this slot alone (its map entry, its slot fields, and the thread -> slot index entry while it still names this slot) and refused (`link_not_saved`, 500), so no turn mirrors into a binding a restart would drop. A thread the failed link took from another session is not handed back; that session relinks by hand.
- **Fresh-anchor title** (`dashboard/chat_slack.py` slack-link endpoint): the new-thread anchor message title uses the fallback chain slot.title → first-prompt snippet (60 chars, whitespace-collapsed) → `"New session"` — the raw slot key is never user-visible (untitled slots default their title to the key, so the endpoint gates on `display_title != NEW_SESSION_TITLE`)

## Sessions View (`sessions_view.py`)

Shared data-collection and Block Kit rendering for recent sessions, used by three surfaces:

- **`/<command> sessions` slash command** — `_handle_sessions` in `events.py`
- **`sessions` keyword in DMs** — `_handle_sessions_command` in `handler.py`
- **App Home Tab** — 🧵 Sessions section in `_publish_home_tab` (split into "Main chat" and "Task runner" sub-lists)

The channel-neutral collection lives in `messaging/sessions_view.py`; `kiro_crew/slack/sessions_view.py` wraps it and renders the Block Kit, so both `events.py` and `handler.py` can import it at module top-level without forming a circular import. `sessions_view.py` depends only on `kiro_crew.slack.blocks` and `kiro_crew.security` — it knows nothing about `events` or `handler`, which is what keeps the import graph acyclic.

All three surfaces call `await _collect_recent_sessions_off_loop(sessions, *, limit, kind, include_ended=False)` — the required entry point for async callers, which runs the synchronous collector `_collect_recent_sessions` in a worker thread via `asyncio.to_thread` — to read JSONL files under `~/.kiro/crew/sessions/`, classify them as `dashboard` (main chat slots), `taskrunner` (task runner steps), or `other`, and `_build_sessions_blocks(rows, *, for_home_tab=False)` to render them. The sync collector does unbounded-size transcript reads and is worker-thread-only: never call it directly from an `async def`. It pre-scans the directory (kind from the filename stem, rank from each candidate's line 0) and reads only the newest `limit` matching transcripts in full. `include_ended` and the third skip reason are covered under "Ended rows leave the list" below.

**The rank is a session's last HUMAN turn, not its file mtime.** The key is `last_user_at` on the metadata line when the transcript carries one and `st_mtime` when it does not. mtime records the last WRITE, so a cron wake, a monitor loop, a subagent turn, an auto-title refresh or any bulk maintenance pass over the directory reorders the whole list although nobody read those sessions — and a pass that visits them in activity order inverts it outright, because the freshest session is rewritten first and ends up holding the oldest stamp. That is why the rank costs one `readline` per candidate rather than nothing: `stat` cannot answer it. Line 0 is always the metadata line, and a stamp that is missing, malformed, or not on a metadata line falls back to mtime instead of raising. A stamp that cannot be parsed must NOT rank: `transcript_sort_key` reports unparseable through its BUCKET and pairs it with a fallback epoch of `0.0`, so a rank taken from its seconds alone would pin the session to 1970 and bury it below every other row permanently. The file's mtime is a real instant, so a corrupt stamp costs the session its precision, not its place in the list. A decode failure is caught too, at BOTH read sites (`UnicodeDecodeError` is a `ValueError`, so an `except OSError` does not stop it): the rank read touches line 0 of every candidate, so one transcript of invalid bytes would otherwise raise out through the collector and render "Sessions unavailable" on every surface, on every scan, until someone deleted the file. So is a stamp that PARSES but cannot be resolved: `transcript_sort_key` resolves a naive value with `astimezone()`, which raises at the representable boundary (measured: `year 0 is out of range` for `0001-01-01T00:00:00`, `year 10000` for `9999-12-31T23:59:59`), and the unparseable path never sees those because they parse fine. Both the rank read and the writer's own fold guard the conversion, the writer per stamp so one bad row cannot abort a slot save.

`last_user_at` is derived in `dashboard/slot_persistence/metadata_line.py` (entry point `chat_persistence._save_slot_to_history`; see [history](history.md)), during the slot save, which already rebuilds the metadata line and already holds the window, so it costs no extra I/O. It is derived from the newest window row carrying `history.HUMAN_TURN_META_KEY`, folded MONOTONICALLY against the value on disk, and deliberately absent from `SLOT_OWNED_META_KEYS`: the window is bounded, so a save whose window has scrolled past the last user row derives nothing, and an owned key's absence would erase a real turn.

**The marker is an ALLOWLIST, and it has to be.** `role == "user"` does not mean a person typed the row: the gateway drives agent turns through the same shape, and `_ChatSlot.enqueue_or_run_prompt` appends `("user", prompt, "msg msg-u")` for an Issue Radar wake — identical in role AND in presentation class to a typed message. A reader that excluded the machine callers it happened to know about would be broken by the next one, silently, with background sessions displacing human-active ones. So the send paths a person actually reaches set the marker (`chat_handlers` ordinary send, `chat_delivery` steer, `channel_slots` channel turn projection, the `chat_runner` queue drain of a human-origin turn, and the Slack-to-linked-slot mirror in `slack/handler_runtime/finalize.py`) and everything unmarked simply does not count. An app token reaches `api_chat` as well, so the ordinary-send marker is gated on the same empty-`request_app` signal that handler already reads for `user_origin` and `turn_actor` — an app's send is not a human turn and must not advance the stamp. The steer marker is gated on `user_origin`, because the `session_send` peer steer reaches the same path with `user_origin=False`. Under-counting is the safe direction: a session with no marked row keeps ranking by `st_mtime`.

The slash command and keyword (which post via `chat.postMessage`) use the shared `blocks.session_task_card` builder. The Home Tab calls with `for_home_tab=True` and uses `section` blocks instead — Slack's `views.publish` API rejects `task_card` with `unsupported type: task_card`. Both paths keep the canonical `mc_session_resume_{key}` action ID handled by `interactions.py:_handle_session_resume`.

The Home Tab requests up to `_HOME_TAB_SESSIONS_PER_KIND = 5` rows per kind so both surfaces stay well under Slack's 100-block view limit. The slash command and keyword each request `slack.sessions_limit` rows, default 10 — the collector's own `_SESSIONS_DEFAULT_LIMIT`. A configured value below 1, or one that is not a number at all, falls back to that default INSIDE the collector: the read loop breaks on `len(rows) >= limit` before it opens a file, so a 0 would render an empty list forever, and an uncomparable value would raise inside each surface's try block and turn a bad number into "Sessions unavailable" plus an error audit. The guard sits at the one chokepoint every surface passes through, so no surface can skip it. The UPPER bound is Slack's own and therefore lives in the Slack module: `chat.postMessage` rejects a payload over 50 blocks, the message layout costs 3 blocks per row less the trailing divider, and 17 rows render exactly 50 (measured against `_build_sessions_blocks`, and pinned by a test that measures it rather than restating the arithmetic). So `_message_surface_limit` clamps the DM keyword and the slash command to `MAX_MESSAGE_SESSION_ROWS`; an over-budget payload is rejected WHOLE, so an unclamped `sessions_limit: 18` would render no list at all, which reads as the feature being broken rather than as one number being too high. The Home Tab is unaffected: it posts through `views.publish`, whose budget is different, and asks for `_HOME_TAB_SESSIONS_PER_KIND` per kind.

**At most `_HOME_TAB_COLLECT_CONCURRENCY` Home Tab collections run at once.** Every `app_home_opened` from an allowed user schedules its own publish with no dedupe, and each collection reads up to `limit` transcripts on the process-wide default executor — shared with history appends, cron store writes and session storage. Ungated, a burst of tab opens fills that executor with multi-MB reads and unrelated `asyncio.to_thread` callers queue behind them. The gate wraps only the collection; the Slack API calls around it stay unserialized. It is created lazily rather than at import, because a module-level `asyncio.Semaphore` binds to whichever loop is current when the module loads and the gateway's loop does not exist yet.

Each surface emits a SEL audit event for the data-access via `sel.log_api_access`:

- Slash command: `slack.sessions_slash_data_access` (caller = Slack user id)
- Keyword: `slack.sessions_data_access` (caller = session key)
- Home Tab: `slack.home_tab_sessions_data_access` (caller = Slack user id)

Sharing the builder also means the `sessions` keyword displays the same 🟢 active / ⚫ inactive marker as the slash command.

### Ended rows leave the list

`⏹️ End` (`mc_session_end_{key}`, handled by `interactions.py:_handle_session_end`) records a **dismissal** on the row's transcript: `closed: True` plus a `closed_at` epoch on the metadata line, written through `ConversationLog.update_metadata_if` and therefore mtime-preserving. `messaging/sessions_view._row_is_ended` reads that flag back and the collector leaves such rows out unless the caller passes `include_ended=True`.

Three details are load-bearing:

- **The record is written whether or not a session is live.** The soft remove above it only kills a process, and a cluttered list is mostly idle rows — for those the removal branch resolves no key and does nothing, so without the record End would have no observable effect at all.
- **The skipped row frees its slot.** Dismissed rows are skipped inside the read loop the same way empty and unreadable files are, so the list still fills to `limit` with live sessions instead of shrinking. The cost is one read per skipped row: with the *n* highest-ranked rows dismissed, *n* transcripts are read and discarded before the first kept row. Unlike the corrupt-file skips this is an ordinary state, so it is reachable in normal use; it is bounded by the directory, and `with_messages=False` reduces each such read to line 0.
- **`closed_at` is stamped after the teardown**, because consolidation and skill extraction write the transcript on the way out of an End. Nothing in this list compares it (see below); it is written because the dashboard's reader does, and a flag with no instant makes every close there permanent.

A live session outranks the flag, so a resumed conversation is listed immediately. `▶️ Resume` also clears the flag outright (`ConversationLog.clear_closed`), so the row stays listed once that process exits.

This is deliberately **not** the rule `dashboard/channel_slots._close_stands` applies to the same field. That one asks whether a channel conversation outran a closed tab and compares the close against the channel's last write. This one asks whether the user still wants the row, and background housekeeping — consolidation, skill extraction, an auto-title — writes the file without the user doing anything, so any write-based rule would put a dismissed row straight back at the top.

The opt-in is `sessions all` / `sessions ended` (DM keyword) and `/<command> sessions all` (slash). `sessions_view.SESSIONS_INCLUDE_ENDED_ARGS` is the one vocabulary, read both by `sessions_view.sessions_include_ended` and by `handler._is_sessions_keyword` — the matcher has to admit the argument or the message is never routed to the sessions handler at all. Opted-in rows render 🛑 in both the task card and the Home Tab layout so they are distinguishable from merely idle ones. The Home Tab has no argument surface and always uses the default.

## `!compact` Command (`slack/handler_runtime/commands.py`)

Triggers in-place ACP `/compact` on the current thread's session (`_handle_compact_command`):

1. Refuses when a turn holds the session (`sessions.try_acquire`) or the backend
   cannot compact (`compact_unsupported_backend`)
2. Posts "Compacting context…"
3. Runs `provider.compact()` bounded by `wait_for(..., 120)`, then
   `provider.wait_for_compaction(timeout=sessions.compact_wait_budget_secs())`
   (the `session.compact_wait_secs` budget)
4. Posts result (✅/❌) + timing footer
5. On failure: `sessions.discard_conversation(session_key)` — kills the session and drops only the resume sid, so the next message cold-starts. The session-map ENTRY survives, keeping the thread↔session linkage `get_session_for_thread` routes later replies through; `destroy` here would fork the thread into a fresh session with none of its context. Housekeeping never removes a channel identity (see [session](session.md))

## Wedged-Session Recovery (`AcpPromptBusy`)

When kiro-cli reports a prompt is still in flight ("already in progress" — a tool stall, timeout, or message race), `AcpClient` raises `AcpPromptBusy` (`acp/transport_errors.py`, re-exported by `acp/client.py`) with a friendly "I'm still processing a previous request… it clears on its own once the stale turn expires" message. `handle_message` catches it and auto-resets the wedged session via `sessions.reset(session_key)` so the next message cold-starts cleanly, then records the failure (the reset itself is best-effort — a reset failure is logged, not raised). The message deliberately names no command: the auto-reset above is what recovers the session, so the text has nothing to ask the user for (`!restart` would be wrong here: it is Slack-only, owner-gated, and restarts the gateway rather than the session -- see `common/error-handling.md`).

## OPTIONS Buttons (`format.py`)

LLM responses ending with `[OPTIONS: choice1 | choice2 | choice3]` are rendered as interactive Block Kit checkboxes with a Send button:

1. `extract_options()` parses the `[OPTIONS: ...]` tag from the response text
2. Tag is stripped from the displayed message
3. `build_options_blocks()` creates Block Kit checkboxes (max 10) + primary Send button
4. Checkboxes posted as a follow-up message in the thread
5. Send click → `_handle_options_submit()` → reads checkbox state → posts styled selection → routes combined selection to handler
6. Legacy single-choice buttons still supported via `OPTIONS_ACTION_PREFIX`

Action IDs: `options_checkboxes` (toggle), `options_submit` (send). Checkbox `value` contains the choice text.

Beyond the reply-finalization path in `handler.py`, two other Slack delivery paths also render `[OPTIONS: ...]` as buttons: the dashboard `send_message` MCP tool (`api_send_message` in `dashboard/handlers/messaging.py`) and cron subagent delivery (`_deliver_cron_response` in `gateway.py`). Both call `extract_options()` / `build_options_blocks()`, skip the tag parse when the caller supplies explicit `blocks` (those own their own layout), and wrap the follow-up options post in `try/except` so a failed options post never fails the primary message.

### Inline action values (`action::`)

`action::` is an inline-action **value** protocol inside legacy OPTIONS controls, not a general Block Kit routing protocol. `slack.interactions.dispatch` calls `_handle_options` only for action IDs carrying `OPTIONS_ACTION_PREFIX`, which `slack.format` defines for OPTIONS choices; every other action ID reaches the tool-approval fallback when the interaction supplies a channel and message. `test_unknown_action_id_falls_through_to_tool_approval` locks that fallback.

Two gates run before any handler: `is_allowed_user(user_id)` on the dispatcher, and `channel_inbound_permitted("slack")` for OPTIONS interactions. Both are load-bearing because the action value becomes agent-visible context and a routed turn.

An OPTIONS choice whose `value` starts with `action::` enters the action branch of `_handle_options`. The remainder of `value` is an opaque payload — the handler neither parses nor requires JSON — and the visible label comes from `action["text"]["text"]`, falling back to the selected overflow option's text. `_route_action_to_session` then performs the shared delivery:

1. Redact exfiltration URLs and credentials from the label, then attempt to replace matching elements in the source message with a context label.
2. Post the redacted label as a visible reply in the source thread. A failed post aborts routing, so an agent turn never runs without its visible Slack message; `test_post_message_failure_aborts` locks that ordering.
3. Redact and bound the payload per `_ACTION_PAYLOAD_CAP`, record the Slack access event, and build an `Action button clicked` context entry.
4. Call `slack.handler.handle_message` with the source message's `thread_ts`, the new reply timestamp, the visible label, and `action_context`.

`ContextBuilder.build_message` appends a non-empty `action_context` ahead of the message text, so the payload arrives as context rather than displayed verbatim in the thread (`test_redaction_applied_to_payload`). The source-message update is best-effort: `_route_action_to_session` logs and continues when `update_message` fails, so a successful route does not guarantee the original button was visually replaced.

`_mark_button_clicked` walks every `actions` block; for each block containing the supplied action ID it removes every matching element, inserts a `context` block holding `✓ {label}` immediately before that actions block, and omits the actions block once no elements remain. Blocks without a matching element survive untouched. The identifier match is the load-bearing link between Slack's interaction payload and the rendered message, so an action ID reused across separate actions blocks produces one context label per matching block. `TestMarkButtonClicked` covers replacement, no-match input, and empty-block removal.

`_handle_options` also carries a direct-handler branch for an `action_id` beginning with `action::`: it parses the suffix as a JSON object, obtains a selection through `_extract_selected_value` (which handles `selected_option`, date, time and datetime fields), adds `selected_value`, derives a label from `placeholder.text` plus the selected display text, and routes through `_route_action_to_session`. Malformed JSON or a non-object payload stops the branch without routing. **That branch is not reachable through the Slack dispatcher** — `dispatch` forwards only `OPTIONS_ACTION_PREFIX` action IDs, so an `action::` action ID falls through to `_handle_tool_approval`; `test_extended_element_happy_path`, `test_malformed_json_in_action_id_no_crash` and `test_non_dict_json_in_action_id_no_crash` exercise `_handle_options` directly. An element with an `OPTIONS_ACTION_PREFIX` action ID whose selected value starts with `action::` enters the value branch instead, where that value is the opaque payload and no base JSON object is merged with `selected_value`. Agents must not treat `action::` in an extended element's `action_id` as an available Slack protocol.

`test/test_action_interactions.py` covers the direct action-handler path, payload redaction, audit logging and the block-transforming helpers; `test/test_slack_interactions_coverage.py::TestDispatchPayloadParsing::test_unknown_action_id_falls_through_to_tool_approval` covers the dispatch boundary that excludes arbitrary action IDs.

## Messaging Transport (`messaging.use_transport`)

A channel-neutral dispatch path that replaces the native `handle_message` stream loop with a shared `SlackTransport → TurnDriver → SlackRenderer` pipeline. Gated by `messaging.use_transport` (`MessagingConfig`, default `True` in KiroCrew — the transport abstraction is the canonical path; set `false` to fall back to the legacy native handler — `config/loader.py`). When the flag is on, `events.py:_route_message` routes the message to `handle_message_transport`; when off, nothing in the live gateway path imports the transport (it is purely additive).

- **`SlackTransport`** (`slack/transport.py`): wraps `SlackClientOps` in the neutral `MessagingTransport` contract (dependency direction `slack → messaging`; the `messaging` package never imports Slack). `authorize()` is **owner-only, deny-by-default** — an empty allow-list authorizes nobody, and it SEL-audits **every** rejection (`operation="slack_transport.authorize"`, `outcome="denied"`), including empty/missing `user_id`, so the deny-by-default control is observable.
- **`TurnDriver`** (`messaging/driver.py`): channel-neutral turn loop converting provider `AcpEvent`s into abstract `OutputEvent`s. Approval ladder mirrors the native `APPROVAL_*` contract — `APPROVAL_AUTO` / `APPROVAL_TRUST` (approve all), `APPROVAL_TRUST_READS` (approve `tool_kind == "read"`), `APPROVAL_INTERACTIVE` (deny-by-default unless the injected decider approves). Two injected predicates keep the driver channel-neutral: `auto_approve_tool` (the `spawn_run` / `auto_approve_subagent_spawn` hook predicate) and `auto_approve_session` (per-session Trust). Interactive buttons are rendered only when a decider is present — without one, `_approve()` denies by default so posting buttons would leave dead controls.
- **`SlackRenderer` + `SlackApprovalDecider`** (`slack/renderer.py`): renders abstract output onto a Slack thread and holds the underlying `SlackClientOps` so the dashboard→Slack mirror keeps working. Approval buttons use `mc_tool_approve_` / `mc_tool_trust_` (per-session Trust) / `mc_tool_deny_` action prefixes. `SlackApprovalDecider` maintains a process-global `_REGISTRY` keyed by request id so the module-level interaction handler can `resolve_global()` a click without a direct reference to the per-turn decider; `session_for()` maps a click back to its session for per-session Trust. The decider is **deny-by-default** — it `wait_for`s the button future and returns `False` on timeout.
- **`handle_message_transport`** (`slack/transport_dispatch.py`): agent resolution order is thread override (`!agent`) → per-channel override (`slack.channels.<id>.agent`) → configured default → canonical `"kirocrew"` (`_DEFAULT_KIROCREW_AGENT`). The final fallback matters: without it an empty `agent.default_agent` makes kiro-cli launch its bare built-in default with no `kirocrew-core` server, so `spawn_run` would be missing. Fires the ack reaction + working status before the (cold-start) session acquisition, matching native ordering.
- **`_resolve_approval_mode(orch)`** (`events.py`): the single per-message chokepoint that folds runtime YOLO (owner-toggled `/kirocrew yolo`, TTL-capped `safety_override`) into `APPROVAL_AUTO`, evaluated fresh each message. The transport `TurnDriver` only sees this resolved mode, so both the native and transport paths honor the runtime toggle consistently rather than an unconditional auto-approve. Deny-by-default unless auto-approve is explicitly active.

## Tool Approval Flow

1. ACP sends `permission_request` event during streaming
2. `events.py:_resolve_approval_mode()` evaluates runtime YOLO, then the CLI `--approval` override, then `agent.approval_mode`; only an explicit auto policy yields `APPROVAL_AUTO`, otherwise it yields `APPROVAL_INTERACTIVE`. Native and transport dispatch both use this chokepoint, preventing an operator policy from being silently bypassed.
3. Handler posts a Block Kit message with ✅ Approve / 🤝 Trust session / 🚫 Reject buttons in a DM, and ✅ Approve / 🚫 Reject in a group channel; there is no YOLO button (YOLO is owner-only via `!yolo on`) (`_request_approval` in `slack/handler.py`; the blocks are `_build_approval_blocks`, `slack/handler_runtime/approvals.py`)
4. `events.py` routes `interactive` Socket Mode event to `interactions.dispatch()`
5. Approval/rejection sent to ACP, streaming resumes or stops
6. Approval button message replaced with outcome text
7. Timeout — steers an in-band approval-timeout notice into the running
   turn (`deny_notice.steer_refusal_notice`: capability-gated, cause
   `approval_timeout`, bounded by `constants.STEER_NOTICE_BOUND_SECS`,
   best-effort), then auto-rejects. The model is told the prompt expired
   unanswered instead of reading kiro-cli generic denial text as a human
   refusal (the dashboard does the same). Both Slack paths do this: the
   native `_request_approval` arm (120s) below, and the transport path, where
   `SlackApprovalDecider` records `last_deny_cause = approval_timeout` on
   expiry and the channel-neutral `TurnDriver` steers it before `reject_tool`
   (see the messaging spec's approval ladder).

### Claim-winner invariant (timeout arm ↔ `handle_interaction`)

The pending-approval registry entry is claimed with `pop(key)` BEFORE any
await, on both sides:

- `_request_approval`'s timeout arm pops first; only when it wins the claim
  does it steer and answer the wire (`reject_tool`). A lost claim means a
  click owns the answer; the arm then awaits the click's real outcome via the
  shielded waiter future until it resolves -- no bound, no fabricated
  rejection, nothing on the wire. Every way the click can end resolves that
  future: its approve/reject completes, its write raises (the click
  self-answers the wire), or a backend that stopped reading stdin is torn
  down by the ACP tool-stall watchdog, which raises out of the parked write.
- `handle_interaction` pops at lookup. If its `approve_tool`/`reject_tool`
  raises after claiming, it answers the wire itself (`_reject_orphaned_tool`)
  and resolves the waiter — a timeout arm that already returned can never
  claim again.

Exactly one side ever answers a given `request_id`: a second answer lands in
the ACP client's popped-options cancelled-outcome fallback, which cancels the
whole turn. Every fallback rejection that reaches the wire is recorded in the
SEL audit trail by `_reject_orphaned_tool`. Editors of either function (both in
`slack/handler.py`) must preserve this contract.

## Session Management

See `session.py` module spec. Each Slack thread_ts maps to a separate AcpClient instance with idle timeout cleanup.

### Message Queue

Messages arriving while a session is busy are queued with ⏳ reaction and drained FIFO after each handler completes. See [Message Queue](#message-queue-sessionpy--eventspy) above.

### Startup

`start_pool()` creates the background session for cron/heartbeat. Chat sessions cold-start on first message — no warm pool, no MCP reset hack.

## Live configuration

`GatewayOrchestrator` is the process's channel host, so it owns two config
appliers, registered in `_register_config_appliers` on the shared `ConfigWatch`
(the appliers, `restart_channel` and the boot re-hoist are
`slack/gateway_runtime/channel_lifecycle.py`; the hoists stay in `gateway.py`)
(`config/live.py`). The `Subscription` objects are kept on `self._config_subs`
because the watcher holds a bound method WEAKLY — an orchestrator a test builds and
discards must not pin itself into the registry. See
[messaging](messaging.md) § Live configuration for the shape every channel shares.

### The hoist is one function per channel

Boot reads each channel's enable flag, credentials and options out of the config
and onto the orchestrator (`_wecom_enabled`, `_telegram_bot_token`, and so on)
before `_start_channel_transports` runs. That work is one
`_hoist_<channel>(cfg, creds)` per channel — `_hoist_wecom`, `_hoist_telegram`,
`_hoist_weixin`, `_hoist_whatsapp`, `_hoist_feishu`, `_hoist_discord`,
`_hoist_webex`, `_hoist_imessage`, `_hoist_teams` — called from `__init__` in
roster order. One function per channel is what makes a reconnect possible at all:
`restart_channel` re-runs exactly one of them against a fresh config instead of
re-deriving every channel's state, so restarting Telegram cannot disturb Discord.

### `restart_channel(channel_type, *, cfg=None)`

The in-process equivalent of a gateway restart for ONE channel, in boot's order:
bounded close of the old handle (`registry.shutdown_tasks`), drop the handle and
its legacy `_<channel>_client` mirror, re-run that channel's hoist against `cfg`
plus a fresh credential read off the loop, re-evaluate the `channels` governance
gate and the readiness badge, then `desc.start(orch)` and store the new handle. A
channel whose new config disables it, leaves it uncredentialed, or is denied by
policy ends CLOSED with its badge explaining why — exactly as it would after a
real restart.

The channel's section on `self._cfg` is replaced with `cfg`'s, because the
`maybe_start_*` factories and the dispatchers they build read their allow-lists
and options from `orch._cfg.<channel>`; without that the restarted transport would
authorize against the boot-time roster. The close, the hoist and the publish of
the new handle run under `_channel_restart_lock`; the connect between them does
not, so a disable's inline close is never queued behind a slow connect, and the
per-channel restart generation (bumped by every close) decides whether the
connected client is published or torn down as superseded. A superseded start
also takes back what its factory already published -- the transport
registration on `DashboardState.channel_transports` and the legacy
`_<channel>_client` mirror -- by identity only (`_forget_superseded_start`),
so a closed transport never keeps answering `get_channel_transport` while a
newer start's registration is left alone.

`_on_channel_config_change` decides when to call it: a channel restarts only when
a changed path names one of its descriptor's `boot_keys`
(`registry.changed_boot_keys`, `messaging/registry.py`). Live fields of the same
section — allow-lists, thresholds, render toggles — are applied by that channel's
own applier without a reconnect, so a change touching only them leaves the socket
alone. Before `_channel_transports_started` the applier raises `ConfigDeferred`
instead of restarting, because the boot loop starts every channel from the hoist
and a restart there would race it; the watcher keeps the paths stale and re-runs
the applier every tick against its CURRENT snapshot, so the first tick after
`start_channels` flips the flag performs the restart the edit asked for. The boot
loop itself never calls the applier: a replay outside `ConfigWatch._apply_one`
would skip the degraded check, and a document with a discarded channel section
retained during the window would then raise straight out of boot instead of
being deferred. That deferral
covers boot keys only, so live fields
edited in the same window — an allow-list revocation between the watcher arming
at dashboard init and the transports starting — are covered differently: the boot
loop re-hoists every bootable channel from the watcher's CURRENT snapshot
(`_adopt_channel_sections_from_watcher`) before the enabled census, so a channel
switched on in the window is started at all, and then re-hoists EACH channel
again (`_adopt_channel_section_from_watcher`, the `before_start` hook of
`registry.start_channels`) synchronously, immediately before that channel's
factory. The second pass exists because channels start one after another and a
connect can take seconds: a revocation that lands while an earlier channel is
connecting has no applier yet for a channel that is not constructed, and a single
read at the top would have left the later channel building from a document the
earlier connects had let go stale. The hook is synchronous and every
`maybe_start_<channel>` constructs its dispatcher — which subscribes to the
watcher — before its first await, so nothing can be dispatched between that read
and the channel's own subscription. A snapshot whose
channel section is degraded leaves the boot copy alone — fail-closed, like every
applier. The whole-config marker alone does not: the snapshot never carries a
torn document's defaults (the watcher keeps the previous values while the file
does not parse), so on a snapshot `*` is the loader's process-long memory of a
repaired tear, and refusing on it would freeze the roster until a restart.

### The Slack applier

Slack is deliberately NOT in the restart loop. Its socket client is owned by
`_connect_slack` under the `channels` governance gate (a deny must DROP the
client), and its tokens live in the credential store rather than `config.json`, so
no `slack.*` write can change the connection. `_on_slack_config_change`
(subscribed on `slack` + `messaging`) reconciles everything else in place:

- `slack.tracking_channels` / `slack.open_channels` → the orchestrator's sets AND
  the `handler` module globals, mutated IN PLACE so the Slack-native modal, which
  edits those same set objects, and a CLI write converge on one set rather than
  two that disagree.
- `slack.channels` / `slack.dm_activation` / `messaging.*` / `trusted_bot_*` /
  `home_tab_sessions_per_kind` / `forward_to_agent_callback` → the shared config
  object every Slack read reaches through `handler.slack_cfg()`, updated
  section-by-section in place so `orch._cfg` and `handler._orch_cfg` cannot
  diverge.
- `slack.reactions` → `handler.refresh_phase_emojis`, which rebuilds `_PHASE_EMOJIS`
  in place; the four read sites call `phase_emojis()` rather than the module global,
  so a reaction rename lands on the next status update.
- `slack.observe_*` → the live `ChannelHistory` caps, and observe-mode registration
  follows the new channel activations.
- `slack.allowed_enterprise_ids` → `enterprise.reload_allowed_team_ids` off the
  loop, which re-runs the VALIDATED `_load_allowed_team_ids` rather than a raw
  read, fails closed on a degraded file, and SEL-audits the change. It runs
  whether or not the workspace has been validated yet: before validation the
  module is default-open, so a reload that skipped that state would leave a
  freshly written allowlist unapplied and every workspace admitted; the
  validated read adds the validated team id only once there is one, and
  `validate_enterprise()` re-runs it when the workspace is known. Never widening
  is the point: this list is what keeps another Grid workspace out.

Fail closed as a whole: when the loader DISCARDED the `slack` section
(`degraded_sections`) nothing under it is applied, the previous sets stay in force,
and the change is logged by PATH only — a `slack` section contains tokens, so no
applier logs a value. A change to `slack.trusted_bot_ids`, `open_channels` or
`tracking_channels` is SEL-audited as its own event, because those sets widen who
may drive a turn; the per-message admission decision is still audited where it is
made.

`slack.command` is the one Slack field marked `restart=True` in
`config/sections.py`: the slash command is registered with Slack's app manifest, so
no in-process apply can change it. No channel CONNECTION field is marked, because
`restart_channel` applies those without a process restart.

## Subagent & Cron Acknowledgment

Subagent completion and cron execution results post to both dashboard (WebSocket) and Slack (DM with ack button). Shared `ack_button()` helper in `interactions.py` handles button replacement:

1. Try `response_url` first (instant, works for 30 min)
2. Fallback: `chat.update` via Slack API (works indefinitely)
3. Section text truncated to 2990 chars (Slack's 3000 char limit)

Bidirectional sync: Slack ack → resolves dashboard approval future + broadcasts `notification_ack` WS event. Dashboard ack → resolves Slack pending future.

### Subagent Slack Replies

When a subagent with a Slack parent session completes, the synthesized LLM response is posted to the owner's DM thread. Long replies are split into multiple messages using `_split_message()` from `handler.py` (3900 chars per chunk, split on newline boundaries), matching the behavior of final chat messages.

A parent session born on any other channel (Telegram, Discord, `unified:` DM buckets, …) delivers the same synthesized reply through the governed cross-surface transport ladder instead (`_deliver_channel_reply` in `gateway.py`): the conversation is resolved via origin link (recorded by Discord's inbound dispatch) → non-Slack mirror link (e.g. a Telegram `/link` binding) → for direct (1:1) sessions only, the stored `"{namespace}:{user_id}"` channel value resolved through `transport.resolve_configured_target`; the target is vetted by `_resolve_channel_target` (SEL-audited, fail-closed, capability-gated on `supports_proactive_send`), then redacted and chunked to the transport's `max_message_chars`. Delivery is best-effort and fail-closed on ambiguity — group/forum sessions without an origin or mirror link, dispatchers that record neither, and denied egress all degrade to the dashboard notification (never a cross-conversation send), and the injected ACP turn still keeps the parent session aware of the result.

## Tool Approval via Slack

### Structured monitor completion adapters

The AutoNudge router keeps its historical `on_fire -> bool`, `cycle_count`,
`fired`, and rearm contracts. A separate runtime-only hook is supplied only when
a structured monitor already has an actionable fingerprint marked in-flight;
legacy loops and ordinary channel messages receive none. `MonitorController`
runs the typed GitHub probe off the event loop, persists the decision and
in-flight claim, and calls the Slack/Discord or dashboard adapter only for
`WAKE_ACTIONABLE`. The adapter receives the already formatted envelope and does
not add the legacy cycle tag. Every non-actionable, retry, and terminal decision
dispatches zero turns.

A Slack message routed into a linked dashboard slot retains channel provenance on
the immediate turn, queue entries, and recovery turns. A monitor directive produced
there persists `channel` as its creation surface even though its storage binding is
the linked chat key, so the link cannot confer dashboard owner credentials on its
provider probes.

Terminal observer notifications are deduplicated for structured monitors within
one gateway process. The retained monitor record also stores whether the dashboard
durably appended its terminal notice. Startup schedules every terminal notice without
that delivery marker as a supervised background task, so notification persistence
cannot delay gateway readiness. The task persists the marker only after the captured
notification append future succeeds, giving the persist-then-notify boundary at-least-once crash
semantics: a crash or append failure can repeat a notice, but cannot suppress the
only notice permanently. A failed notification creation or append releases the
process-local deduplication claim, allowing a later observer event to retry without
requiring a gateway restart. Gated
legacy loops use only their existing `expired` notification; the following `fired`
event must not deliver the same terminal notification again.
Terminal notices identify the watched pull request by its stored target URL,
including channel-bound watches with no dashboard jump link. The completed body,
including the retained target, passes through shared URL and credential redaction
before dashboard notification persistence. The stored stop
reason distinguishes a merged pull request from one ready for review: only a
merge says no action is needed. A `pull_request_closed` blocker states that the
pull request was closed unmerged and offers reopen-or-abandon recovery. Other
known blockers name the credentials, permission, setup, approval, completion,
conversation, or saved-record problem; unknown reasons point to retained details
without guessing that the pull request closed. An unavailable-session notice
directs the operator to start a new watch from an active conversation.

Slack's structured inline nudge runs through `TurnDriver` with the shared,
session-bound directive consumer. Genuine core-MCP `monitor_update`,
`monitor_stop`, and structured `autonudge_stop` tool results therefore mutate
the authoritative Slack monitor before any later raw completion; forged or
sub-agent results retain the driver's fail-closed behavior. Legacy nudges keep
their collector path. Both paths consume `provider_last_turn_usage(client)`
exactly once. That one `TurnUsage` object is fanned out to the existing usage-row
writer and, when the stream observed safe completion evidence, the monitor hook.
ACP-synthesized terminals are excluded. Because stale-stream synthesis reuses
`end_turn`, that reason remains uncharged until ACP events expose provenance;
other safe reasons determine cancellation or failure.
Stream exhaustion and timeout before that event still write the existing usage
row but do not report monitor completion or charge the monitor budget. Callback
or usage-row persistence failure does not change the Slack delivery result. A
structured stream that started reports `DISPATCHED` even if it exhausts or raises
before `EVENT_COMPLETE`; the controller's persisted evidence deadline resolves
the missing callback. Legacy callers retain their historical boolean result.

Discord synthetic nudge injection passes the same hook through
`DiscordDispatcher` to `TurnDriver`. Only a safe `EVENT_COMPLETE` reason reports
completion; a command return, dispatch exception, or renderer
`close()` is not completion evidence. Thus dashboard, Slack, and Discord all
reach the same typed controller callback even though their transport lifecycles
remain different. A queued dashboard turn revalidates its claim after background
admission and before entering `_run_chat`, so a stopped monitor cannot run
prompt-submit hooks; `_run_chat` revalidates again immediately before provider
entry to cover revocation during turn setup. Their pre-completion delivery
contract is also shared:
`DISPATCHED`, `BUSY`, or `UNAVAILABLE`; BUSY is an ordinary durable retry of the
same claimed wake, while only UNAVAILABLE terminates the monitor.

Background task approvals (subagent, cron, task runner, and AutoNudge) post approval buttons to Slack DM via `_interactive_approval()`, racing with dashboard approval:

1. Posts ✅ Approve / 🚫 Reject buttons to owner DM
2. Creates `_PendingApproval` entry for interactive handler
3. Dashboard callback resolves Slack future on dashboard approve
4. Slack button click resolves dashboard future
5. `handle_interaction()` guards against None provider and double-set on futures

### Background Deny-Fast (Unattended Sources)

`_interactive_approval(source)` is used by both interactive UI/slack and
**unattended** background sources. For background sources there is no human
responder, so waiting the interactive approval window on every approval would
stall cron, heartbeat, task-runner, or AutoNudge turns.

- `_BACKGROUND_APPROVAL_SOURCES = {"cron", "heartbeat", "taskrunner", "autonudge", ""}` (module
  constant in `slack/gateway_runtime/tool_policy.py`, re-exported by `gateway.py`). `is_background = source in _BACKGROUND_APPROVAL_SOURCES`.
- `subagent` is **NOT** background: subagent approvals route to the dashboard
  where the spawning human is present (via the parent slot), so they keep the long
  interactive window.
- When `is_background`, both the Slack `wait_for(pending.future, ...)` and
  `DashboardState.request_approval(..., is_background=True)` use
  `DashboardState._BACKGROUND_APPROVAL_TIMEOUT_SECS` and then **deny** on expiry —
  letting the turn proceed/fail rather than hang. `test/test_dashboard_approval.py::TestBackgroundApprovalDenyFast` pins the bounded background window and the unchanged interactive window.
- The Slack and dashboard windows reference `DashboardState._BACKGROUND_APPROVAL_TIMEOUT_SECS`
  / `DashboardState._APPROVAL_TIMEOUT` as the single source of truth.

### Heartbeat Tool Allowlist (`HEARTBEAT_SAFE_TOOLS`)

Heartbeat sessions run unattended and cannot prompt a human for tool approval. `_is_heartbeat_safe_tool(event_title)` (`slack/gateway_runtime/tool_policy.py`) checks whether a tool is safe to auto-approve using a strict **exact-match** against the `HEARTBEAT_SAFE_TOOLS` frozenset — no verb/heuristic fallback (deny-by-default, per security-controls).

**Title normalization** (applied before the set lookup):

1. Strip leading status prefix (`Running: `) via `_HEARTBEAT_STATUS_PREFIXES`.
2. Strip ACP `mcp__<server>__<Tool>` prefix.
3. Strip runtime `@<server>/<Tool>` prefix (kiro-cli titles arrive as `Running: @internal-mcp/ReadInternalWebsites`).

Only the **bare tool name** (e.g. `ReadInternalWebsites`) is tested against the frozenset. Unknown tools are denied and a SEL audit event (`outcome: denied`, `reason: not_in_heartbeat_safe_tools`) is emitted so operators can tune the list. SEL failure on the approve path fails closed (denies the tool).

## Dashboard Token Authentication

### `!dashboard [duration]` Command (deprecated → `/kirocrew dashboard`)

Allowed-user command (`_bang_dashboard`, `slack/handler_runtime/commands.py`) that generates a time-limited token URL for dashboard access:

1. Parses optional duration argument via `parse_duration()` — accepts `<N>h` or `<N>m` format (default: `1h`)
2. On invalid duration, replies with usage message
3. Calls `generate_token(user_id, ttl)` to create an HMAC-SHA256 signed token
4. Constructs URL using configured host from `dashboard.url`, or machine hostname for remote access, or `localhost` for local-only
5. Logs via SEL with `operation='slack.dashboard_token'`
6. DMs the URL to the user (`send_dashboard_link`); the thread only gets "Dashboard link sent via DM."

### Token Auth Middleware

`token_auth_middleware(local_only)` in `token_auth.py` — aiohttp middleware in the explicit middleware chain:

- **Auth required**: on every gated request, loopback included — loopback is not exempt (local port forwarders make remote traffic appear as 127.0.0.1)
- **Bypassed for**: static assets and a list of exact paths; [dashboard-token-auth](dashboard-token-auth.md) owns the exhaustive bypass list
- **Token sources**: `?token=` query param (first use) or `mc_token_{port}` cookie (subsequent requests)
- **First query-param use**: binds token to client IP, marks consumed, sets an `HttpOnly; SameSite=Lax; Path=/` cookie (`Secure` over HTTPS), plus a refresh cookie
- **Cookie use**: validates token + IP binding, allows repeated access
- **Rejection**: returns 403 HTML page with instructions to run `/kirocrew dashboard` in Slack; API paths get JSON error

Token format: `base64url(payload).base64url(HMAC-SHA256-signature)`, signed with a persistent secret at `<config_dir>/token_signing.key` (mode 0600). Cookie flags, refresh tokens and the signing secret are specified in [dashboard-token-auth](dashboard-token-auth.md).

### Dashboard URL Config

Single `dashboard.url` field on `KiroCrewConfig` (default: `""`), loaded from `config.json → dashboard.url`.

The public build always binds loopback: `dashboard/urls.py:is_local_only()` returns
true unless a managed proxy is detected, and there is none in the open-source build.
`KIROCREW_BIND` (env) overrides the bind address only, for containers. Token auth
applies to every gated request regardless (see [security](security.md)).

```json
{
  "dashboard": {
    "url": "http://my-host.example.com:8080"
  }
}
```

### Tunnel URL in Slack Links (`slack.use_tunnel_url`)

`SlackConfig.use_tunnel_url` (bool, default `False`) gates whether the AEA
tunnel URL is used when building dashboard links posted to Slack:

- `false` (default) — `send_dashboard_link()` ignores any active tunnel and
  builds links from `dashboard.url` (if set) or the resolved host:port.
  Disabled by default until the tunnel mechanism is scaled for general use.
- `true` — `send_dashboard_link()` prefers `get_tunnel_url()` when a tunnel is
  active, falling back to `dashboard.url`/host:port when the tunnel is down.

The same opt-in picks the origin for the other Slack-posted dashboard links — the
chat-mirror links, the Slack backfill's dashboard markers and the `send_message`
Open-session button (`urls.tunnel_origin_if_opted_in` / `urls.dashboard_link_origin`);
only `send_dashboard_link` keeps the host:port fallback.

The setting is independent of `tunnel.enabled` (which controls whether the
tunnel itself runs). A user may run a tunnel for direct browser access while
keeping Slack links pointed at the local origin.

`--no-tunnel` overrides it. When `use_tunnel_url` is on, the box is
localhost-only and no tunnel is live, `send_dashboard_link()` offers a composed
edition an on-demand provisioning seam (`current_context().tunnel
.ensure_available()`) — a second door out that bypasses `setup_tunnel` entirely,
provisioning straight on the provider without ever constructing a
`TunnelManager`. On a process booted with `--no-tunnel` that seam is not reached
at all (`tunnel.publish_disabled()`), the refusal is SEL-audited as
`tunnel.provision_denied` / `no_tunnel_boot_flag` — the same control as the boot
refusal, so neither door's denials are missing from the trail — and the link is
composed from the local origin instead. The DM also carries a line naming
`--no-tunnel` and the `ssh -L` form: every other route to a local link can still
become reachable (the edition seam re-issues once its tunnel connects), but this
one never will, so without it the requester taps a link that times out every time
with the explanation only in the log. Without that check the flag would be a
promise the product does not keep: an instance that refused to publish at boot
would publish the first time anyone asked for a dashboard link.

**Slack connect is non-fatal** (`GatewayOrchestrator._connect_slack`): the
initial socket-mode `connect()` is wrapped so a network/proxy/timeout failure
(e.g. a stale `HTTPS_PROXY` in the launching shell — slack_sdk's aiohttp client
honours proxy env vars via `trust_env`) logs a warning and the gateway
continues in **dashboard-only mode** instead of crashing the whole process.
Only ordinary `Exception`s are swallowed; `CancelledError` (BaseException)
still propagates so real task cancellation is not masked. There is no
background retry of the initial connect — Slack DM stays disabled until the
next gateway restart. The "connected to Slack" banner prints only after a
confirmed connect.

Config example (remote access via URL):
```json
{
  "dashboard": {
    "url": "http://my-host.example.com:8080"
  }
}
```

## Security

- Owner-locked via `KIROCREW_OWNER_ID` in `.env` (supports W/U prefix cross-matching)
- **Enterprise Grid validation** (`slack/enterprise.py`): Two-layer defence against data exfiltration to personal/external Slack workspaces:
  1. **Startup gate**: `validate_enterprise()` calls `auth.test` with the bot token, and, when `slack.allowed_enterprise_ids` is set, verifies the workspace's `enterprise_id` (or `team_id`) is on it; with no list configured it is default-open. Caches `team_id` and `enterprise_id` in memory. Clears cache before each validation attempt so re-validation failures are fail-closed. Gateway refuses to connect if validation fails.
  2. **Per-message gate**: `check_message_origin()` compares each incoming event's `team` field against the cached `team_id`. Catches `.env` hot-swap while running. Zero-cost in-memory string comparison, no API call. Deny-by-default: empty `team` field is rejected.
  - **One list, two id spaces — Enterprise Grid needs BOTH kinds in it.** `auth.test` returns an org-level `enterprise_id` (`E…`) *and* the install workspace's `team_id` (`T…`), while each inbound event carries the child workspace `team_id` it was sent in. The startup gate checks `enterprise_id or team_id`, so on Grid the **org id** must be listed or validation refuses and Slack is disabled; the per-message gate only ever compares the event's **workspace id**, which an `E…` entry can never equal, so **every child workspace id** must be listed or its messages are denied. Supplying either kind alone fails, and the two failures look nothing alike: workspace-ids-only refuses loudly at boot, while org-id-only passes validation (`Enterprise validation OK`) and then denies every DM — armed, because any entry leaves default-open, with nothing inbound able to match. `_diagnose_allowlist_id_spaces()` warns at load time for the org-id-only case (SEL `error=allowlist_admits_no_inbound_workspace`), and the startup refusal names the missing org id for the other, so neither state is silent or points at the wrong remedy. Both are DIAGNOSTIC: admission is unchanged, because treating an `E…` entry as org-wide admission would widen the allowlist this gate exists to keep narrow.
  - **Corrupt-config fail-closed**: `KiroCrewConfig.load()` degrades a torn/corrupt `config.json` (or `config.local.json` overlay) to a defaults object rather than raising, so `slack.allowed_enterprise_ids` would come back empty. `_load_allowed_team_ids()` positively detects that degraded read (a config file that exists on disk but does not parse) and fails CLOSED -- the allowlist stays enforced and admits NO origin (not even the just-validated workspace, which would answer the allowlist's own question) so startup is refused, and the degradation is SEL-audited (`operation=slack.allowed_team_ids_load`, `error=config_load_degraded_fail_closed`) -- instead of silently reverting to default-open. A genuinely unconfigured allowlist (no config file, or a clean file listing none) stays default-open.
  - **One reader owns the allowlist**: the admitted set comes only from that validated read of `slack.allowed_enterprise_ids`. Caller-supplied `extra_ids` -- the caller's own earlier `KiroCrewConfig.load()` snapshot of the same key -- does not contribute to it. The validated read is never older than the snapshot, so ids the snapshot holds and the read does not are ids the operator REMOVED, and unioning them would undo the removal. Consequence in both directions: removing one id takes effect at validation, and emptying the list returns to default-open, matching what a restart does. `extra_ids` does not contribute on the `auth.test`-failure path either, so the validated read is the sole source on every path: that path decides fail-open vs fail-closed by asking whether a restriction is configured, and counting an older snapshot there would manufacture a restriction the file does not list. An UNREADABLE config still refuses there -- a config that cannot be honoured is not one that honestly lists no restriction -- and a configured allowlist still fails closed.
  - All validation outcomes logged to SEL (`operation=slack.enterprise_validation`)
  - `kirocrew doctor` includes workspace validation check
- **Deny-by-default**: if `KIROCREW_OWNER_ID` is unset or empty, Slack is disabled entirely at startup (`init_socket_mode` refuses to connect). The access check in `_route_message` also rejects all messages when owner ID is missing, as a secondary guard.
- **Interactive payload access check**: `interactions.dispatch()` uses deny-by-default — rejects unless the clicking user is positively confirmed as allowed. Non-allowed users receive an ephemeral message ("⛔ You are not authorized to use these buttons.") and the original buttons remain intact for the owner to click later.
- Dedup cache (`SeenCache`) prevents processing duplicate Slack events
- Bot self-message filtering via `bot_id` check
- **Trusted bot IDs** (`slack.trusted_bot_ids` in config): allows specific bot IDs to bypass the blanket `bot_id` filter, enabling multi-node mesh communication. Empty list = all bot messages dropped (default), and a bot id NOT in the list is denied exactly as with no list (fail-closed, `error=untrusted_bot`). Admission requires a positive `bot_id` match against the allowlist; the match sets `from_trusted_bot`, which lets the `bot_id` stand in as `sender_id` and grants access equivalent to an allowed user — authorization is explicit via the `trusted_bot_ids` config allowlist, not the `slack.allowed_users` list. All trusted-bot permission decisions emit SEL audit events (allowed decisions carry `resources="trusted_bot"` so the decision basis is traceable). Echo protection: error replies to trusted-bot messages are suppressed on both dispatch routes — the native path (`from_trusted_bot` in `handle_message`) and the default transport path (`from_trusted_bot` in `handle_message_transport`, threaded through the immediate call, both session queues, and `_dispatch_queued`; the error message is suppressed but the thread status is still cleared). Successful-reply loops are bounded by the **per-thread turn cap** (`slack.trusted_bot_turn_limit`, default 5, minimum 1): a thread that has run that many consecutive trusted-bot turns admits no more (`error=trusted_bot_turn_limit_reached`) until an allowed human posts in it, which resets the count — without the cap, two mutually trusted gateways would admit each other's replies as fresh turns indefinitely. Only a message that actually dispatches a turn moves the count (Slack retries, message/app_mention duplicate pairs, and activation-dropped messages do not). Review-mode channels deny trusted bots outright (`error=trusted_bot_denied_in_review_channel`): the review draft flow delivers via an ephemeral to the sender, which requires a human user id. The gateway's own bot id (cached from the startup `auth.test` that enterprise validation already performs) is never trusted even when listed (`error=own_bot_id_never_trusted`) — otherwise every reply would re-enter the handler as fresh input, a self-reply loop; when `auth.test` was unavailable the self identity is unverified and the admission FAILS CLOSED, trusting nobody (`error=trusted_bot_requires_verified_self_id`) — the same posture enterprise validation takes for a configured allowlist with unverifiable workspace identity. The (unwired) `SlackTransport.receive` inbound path and this gate call ONE owner of the admission rule, `slack.enterprise.trusted_bot_admission` — positive allow-list match, own-bot exclusion, fail-closed unverified self id, audited decisions, trust before the subtype filter — so the two Slack inbound paths cannot drift about which peer bots are admissible. What each site still owns is the READ TIMING of the allow-list it passes in: this gate passes the live config, so an operator's edit takes effect on the next event, while the transport freezes a constructor snapshot to match its `allowed_users` pattern.
- Socket Mode — no public URL exposed
- Credentials stored in `~/.kiro/crew/.env` with `chmod 600`

## Dependencies

Versions are owned by `setup.cfg` `install_requires`.

| Package | Purpose |
|---|---|
| `slack_sdk` | Socket Mode + Web API |
| `aiohttp` | Dashboard HTTP server |
| `websockets` | Socket Mode transport |
| `croniter` | Cron expression matching |
| `snowballstemmer` | Snowball stemming for semantic KV keyword scoring |
| `pysqlite3-binary` | FTS5/UPSERT compat (Linux x86_64 only) |
