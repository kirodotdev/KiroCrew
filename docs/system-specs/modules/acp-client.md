# ACP Client Module

## Overview

The primary ACP session transport path spans **five** entry modules: the legacy per-session client (`acp/client.py`, one subprocess per session), the multiplexed runtime (`acp/runtime.py`, one subprocess fanned out to N sessions), the per-session handle (`acp/session_handle.py`, one `sessionId` + queue + prompt/approve/reject loop), a shared dispatch parser (`acp/_dispatch.py`, pure frame-shaping/redaction helpers all paths route through), and the session provider (`acp/session_provider.py`, `AcpSessionProvider` adapting an `AcpSessionHandle` to the `LLMProvider` ABC so runtime-backed sessions are interchangeable with `AcpClient`). All are JSON-RPC 2.0 over stdio for a registry-selected ACP harness, managing subprocess lifecycle, session initialization, prompt streaming, and tool permissions. Protocol constants live in `acp/types.py`; the complete backend and host-capability matrix is in [agent-host-contract.md](agent-host-contract.md#column-meaning).

Module-level code moved out of the two transports lives in the owner modules
below: `acp/runtime_start.py` serves the runtime alone, the five host adapters
serve the client alone, and the rest serve both.
`acp/client.py` and `acp/runtime.py` keep every moved name readable and patchable
under its old path: a moved name their own code reads and nothing patches is an
ordinary import, and every other moved name is forwarded, so a patch through the
old path lands on the owner, where the owner's own callers read it. The moved
process-tree and session-start helpers read the modules they probe with
(`platform_compat`, `sys`, `Path`, `subprocess`, `os`, `time`, `weakref`) and the
two session-handle names the start collector uses from the facade their code came
from, imported when the helper runs, so a rebinding on the old module still reaches
them. A moved exception class keeps `kiro_crew.acp.client` as its `__module__`, so
error chains and tracebacks still name the path callers import it from.

| Owner | Owns |
|---|---|
| `acp/transport_framing.py` | stdio JSON-RPC framing: the oversize-line drain, the no-progress bound on request, response and notification writes, and the failure-path stderr settle (`settle_drain`) |
| `acp/transport_errors.py` | the `AcpError` family and the reading of a harness failure: the stderr, auth, throttle and sandbox classifiers, `classify_provider_error`, and the text and retry verdict `_format_acp_error` / `_raise_acp_error` produce |
| `acp/runtime_models.py` | the advertised model catalog: `DEFAULT_MODEL`, `model_is_unusable`, `resolve_usable_model`, `resolve_pin_spelling`, `resolve_pin_spelling_on`, `pick_served_default`, the model-substitution advisory |
| `acp/runtime_process_tree.py` | descendant enumeration, child identity capture, the escaped-child sweep and RSS-tree measurement |
| `acp/runtime_start.py` | cold-start admission, `SessionStartGate` / `StartPermit`, `StartCollector` and the session-start timeouts |
| `acp/launch.py` | the launch tail both drivers hand their plan to (`launch`): pod bundle swap, scratch window, sandbox wrap with the host's credential mask, cgroup scope, the child's environment and the suspended spawn; plus what several per-process hosts share before it -- the Node-adapter and self-served binary ladders, the resolution-cache generation fence, the enforced-host preflight and the read-back child plumbing |
| `acp/harness/{claude,opencode,goose,pi,deepseek}.py` | one `ProcessAdapter` per host the client starts one process per session for (`process_adapter_for`): its binary resolution and caches, argv and labels, credential mask, gate sealing and routing read-back, and its additions to the child's environment |

`AcpClient`'s own behaviour lives in `acp/client_*.py` owner modules, one
responsibility each. A member's body is a module-level function there whose first
parameter is `self` (annotated `AcpClient` for type checkers only), and the class
binds it by name under one comment header per owner (`set_model =
_client_model_selection.set_model`, `staticmethod(...)` for a static method). Every
member therefore stays an attribute of the one class, so a patch of a method on the
class or on an instance reaches every caller, an `AcpClient.__new__` build works, and
the session's state stays on the instance; each owner's docstring names the instance
fields its members write and read. A bound member's `__qualname__` and `__module__`
name its owner (nothing reads them), and its `self: AcpClient` annotation is there so
mypy checks the moved body: `AcpClient` is imported for type checkers only, so
`typing.get_type_hints` on a bound member needs it passed in (`localns`). A class an
owner defines keeps `kiro_crew.acp.client` as its `__module__`. `_CLIENT_OWNERS` lists
the owners lowest layer first: an owner imports only owners listed before it. Owners log under the `kiro_crew.acp.client`
logger, never import `acp/client.py` at run time, and read their globals in their own
namespaces: `acp/client.py` keeps binding every name it bound before, and a write
through it reaches every owner binding the same object (`kiro_crew._facade_mirror`).
`test/test_client_bindings.py` pins the member inventory, the binding and the reach.

| Client owner | Owns |
|---|---|
| `acp/client_timeouts.py` | every protocol timeout and turn budget of the direct client, the respawn backoff, and the resolution of a prompt's timeout from its ceiling or deadline |
| `acp/client_spawn_env.py` | a spawn before the process exists: kiro-cli and codex-acp binary resolution, the child's environment repair (pod HOME remap, bundle swap, session identity), `finish_suspended_spawn` and the `_exit_origin` reading of how a child exited, the launch tools, and the workspace, `agent.env` and `--agent` argv staging |
| `acp/client_claude_session.py` | the claude-agent-acp session contract: adapter version floors, leaving a project-owned settings file out of the setting sources, the `_meta` envelope, Side Chat read-deny rules and the starting-mode pin |
| `acp/client_claude_seed.py` | the session's `settings.local.json` seed: render, create, adopt, re-seed, share and retract |
| `acp/client_seed_ownership.py` | proving and handing back Crew's ownership of the seed on disk: bounded no-follow reads, move-aside and restore, settle, discard and release |
| `acp/client_model_selection.py` | the session's model: advertised list and entitlement re-probe, served-default correction, `set_model`, the config-option push ladder (`_push_model_via_effort_split`, `_is_config_value_rejection`) and effort levels |
| `acp/client_permissions.py` | permission requests and in-band tool restrictions: the permission event, approve and reject, the auto-approve identity gate, zero-tool and spec-disabled refusals, identity-drift refusals, the pi, DeepSeek and goose-mode tripwires and the routing read-back |
| `acp/client_elicitation.py` | an MCP server's elicitation on this client: the parse-or-decline of a request on either wire, the decline without a consumer or on a text-only prompt API, `answer_elicitation` with its form validation, and the cancel of what is still pending at a turn boundary (see *MCP elicitation*) |
| `acp/client_mcp_array.py` | the session's `mcpServers` array: member mounts, pooled broker stubs, the per-harness translations, the pi handoff, pruning of servers that cannot start, the MCP registration report and the spec's zero-tools answer |
| `acp/client_transport.py` | JSON-RPC over the child's stdio: request ids, bounded request and response writes, frame reads, waiting for a response while deferring other frames, draining notifications, and the idle reader between turns with its stdout claims |
| `acp/client_tool_events.py` | tool-call frames: text chunk, tool call, update and refinement extraction with their provenance caches, the ACP-layer SEL audit record, the Pre/PostToolUse hook fires and the tool results read back from kiro-cli's session JSONL |
| `acp/client_usage.py` | usage, metadata and compaction frames: usage and prompt-token updates, context-window metadata, claude and codex inline compaction and `wait_for_compaction` |
| `acp/client_turn_control.py` | a turn controlled from outside it: kiro, claude, goose and opencode steers, cancel, slash commands and the turn-state queries callers poll |
| `acp/client_start.py` | the session start: the spawn (kiro-cli's own arm, and every other host's plan through the launch tail both drivers share), the `initialize` handshake and its params, new, loaded, forked and substituted sessions, and `ensure_ready` |
| `acp/client_turn.py` | the turn loop: sending the prompt, `_prompt_loop`, reading the prompt response, dispatching and processing frames, `send_message`, `send_message_stream`, `stream_events` and `stream_unsolicited`, the tool-interrupted marker, OpenCode's deferred shell permission and the liveness consult |
| `acp/disposed_session.py` | not a member owner: the per-harness texts that mean a session's backend was disposed in a live process, and the two answers `_prompt_loop` takes from it (see *A session the harness disposed*) |
| `acp/relaunch_reload.py` | not a member owner: relaunching a live client's process and loading the same session in it, and the MCP mark a relaunch-reload harness applies at its next turn boundary (see *An MCP change applied by relaunch*) |
| `acp/client_class.py` | the class statement: the constructor, the properties, the class defaults, the member table that binds every owner's members, and the members that stay class-resident |
| `acp/client_lifecycle.py` | the child after it is spawned: the stderr drain, process-tree tracking, kill, failed-start classification, state reset, the workspace and sandbox discards, re-binding a live process, and shutdown |

The class statement sets `__module__ = "kiro_crew.acp.client"`, so reprs, pickles and
`typing.get_type_hints` name the facade, and `acp/client.py` re-exports it:
`kiro_crew.acp.client.AcpClient` is the same class object, and a patch of it reaches
the class's own code. The class keeps the three seed static methods that read the
`AcpClient` global (`_settings_path_holds`, `_claim_pathname_if_ours`,
`_restore_aside_without_clobber`) and `is_process_alive` (the pid-reader ratchet counts
its name). The class homes of `AcpRuntime` and `AcpSessionHandle` are below. `acp/client.py`
keeps `_mise_which`, `_resolve_ssh_auth_sock` and `_resolve_spawn_env` (the spawn audit,
the subprocess-encoding baseline and the cross-platform gate key them on this path) and
the compatibility facade. The session start reads `_resolve_spawn_env` as a global of
its own module, which `acp/client.py` fills in right after defining it, because an
owner never imports the facade; a patch through `kiro_crew.acp.client` reaches it
through the mirror.

`AcpSessionHandle`'s behaviour lives in `acp/session_handle_*.py` owner modules the
same way, one responsibility each. A member's body is a module-level function there whose
first parameter is `self` (annotated `AcpSessionHandle` for type checkers only), and the
class binds it by name in one block above `__init__`, one comment header per owner. The
constructor, the properties and the `_last_steer_monotonic` class default stay in the
class, and the handle's state stays on the instance, so a patch of a method on the class
(also when spelled through `kiro_crew.acp.runtime.AcpSessionHandle`) or on an instance, an
`AcpSessionHandle.__new__` build and an unbound `AcpSessionHandle.<member>(stub, ...)` call
all reach the one function; each owner's docstring names the instance fields its members
write and read. `own_methods` names each bound function after its class attribute, so its
`__qualname__` (what a coroutine's and a task's repr print) and `inspect.signature` read as
before the move, and only its `__module__` names the owner. The runtime exception family
keeps `kiro_crew.acp.session_handle` as its `__module__`, so error chains name the path
callers import it from; `AcpRuntimeProtocol` and `WatchdogSettings` keep their owner's,
which nothing reads. `_SESSION_HANDLE_OWNERS` lists the owners lowest layer first: an owner
imports only owners listed before it. Owners log under the `kiro_crew.acp.session_handle`
logger and never import `acp/session_handle.py` at run time. The facade keeps binding every
name it bound before -- its own unused imports are exempt from flake8's F401, and a frozen
import inventory stands in for that check -- serves the moved private names nothing outside
the family reads, and a write through it reaches every owner binding the same object
(`kiro_crew._facade_mirror`). `test/test_session_handle_bindings.py` pins the member
inventory, the binding, the reach and the import inventory.

| Session-handle owner | Owns |
|---|---|
| `acp/session_handle_runtime_contract.py` | the contract between a handle and its runtime: the `AcpRuntimeError` family (`AcpRuntimeDead`, `AcpRequestTimeout`, ...) and `AcpRuntimeProtocol`, the interface the handle needs from `AcpRuntime` |
| `acp/session_handle_watchdog.py` | the per-session stale/stall watchdog: `WatchdogSettings` and `_load_watchdog_settings`, the turn park clocks, the liveness-oracle consult, the WORKING deferral log and metric, the stalled-tool end and the input-wait status |
| `acp/session_handle_mcp.py` | the session's MCP servers: the launch roster the liveness oracle attributes a tool by (`mcp_launch_roster`, `record_mcp_launches`), the init drain, the readiness wait, OAuth and sign-in offers, and the failed-server reattempt |
| `acp/session_handle_kas.py` | what KAS speaks beyond ACP on a session: its hooks requests (list, session start, execute), the checkpoint rewind of an unanswered prompt (`discard_unanswered_prompt`), and its own session-update discriminants (mode, session info) |
| `acp/session_handle_native_children.py` | harness-native child sessions (H16): the KAS sub-agent roster and child chunks, child-session bookkeeping, the resume refusal and the uncharged report |
| `acp/session_handle_model_selection.py` | the session's model: the advertised list and its parsers (`parse_advertised_models`, `models_from_config_options`, ...), its storage with the session config and read-path revalidation, mode, model and effort selection through the backend's own verbs, and the served-default backfill |
| `acp/session_handle_steering.py` | steering a live turn: `steer`, the codex steering-request protocol, settling abandoned answers, and the injected, proven and denied steer bookkeeping the dispatch loop reads |
| `acp/session_handle_lifecycle.py` | the session outside its turn loop: awaited control requests and slash commands, cancel, death attribution, `destroy` and transcript cleanup |
| `acp/session_handle_compaction.py` | compaction on every backend (`compact`, the KAS compaction verb, codex and claude inline compaction, `wait_for_compaction`) and the usage and metadata frames it is measured by |
| `acp/session_handle_permissions.py` | permission requests and in-band refusals: the permission event, approve and reject, the spec-disabled deny and tripwire, the unidentifiable, unreadable and unretained MCP identity refusals, the reject audit (`_audit_handle_reject`) and session permission routing |
| `acp/session_handle_elicitation.py` | an MCP server's elicitation on the multiplexed handle: the same parse-or-decline, consumer gate, `answer_elicitation` and pending cancel as `client_elicitation.py`, over the shared runtime's `send_response` |
| `acp/session_handle_updates.py` | `session/update` frames as `AcpEvent` values: tool calls and the per-tool-call caches they keep, child-tool frames and structured status frames |
| `acp/session_handle_turn.py` | one turn end to end: `prompt`, `stream_command`, the per-turn reset and pre-turn drain in `_run_turn`, the abandoned-turn close and `wait_turn_done` |
| `acp/session_handle_class.py` | the class statement: the constructor, the properties, the class default, the member table that binds every owner's members, and the `_MEMBER_OWNERS` set it passes to `own_methods` (`_facade_bindings.py`) |
| `acp/session_handle_dispatch.py` | the per-turn event loop `_dispatch_events`: it reads the session queue, routes each frame and runs the in-band watchdog arm |

The class statement sets `__module__ = "kiro_crew.acp.session_handle"`, so reprs, pickles
and `typing.get_type_hints` on the class name the facade, and `acp/session_handle.py`
re-exports it: `kiro_crew.acp.session_handle.AcpSessionHandle` and
`kiro_crew.acp.runtime.AcpSessionHandle` are the same class object, and a patch of a member on
it reaches every caller. `inspect.getsource(AcpSessionHandle)` does not find the class through
that module name; read a member's source, or `acp/session_handle_class.py`. `own_methods`
renames its members there. `acp/session_handle.py` keeps only the compatibility facade. New
`AcpSessionHandle` behaviour goes to the owner whose row names its responsibility, bound in the
class (`acp/session_handle_class.py`) under that owner's header.

New module-level code goes to the owner whose row above names its
responsibility, not to `acp/client.py` or `acp/runtime.py`: stdio framing and
write bounds to `acp/transport_framing.py`; an `AcpError` subclass, or a
classifier that reads a harness failure into one, to `acp/transport_errors.py`;
model-catalog rules to `acp/runtime_models.py`; process-tree inspection to
`acp/runtime_process_tree.py`; session-start admission to
`acp/runtime_start.py`; a step of the launch both drivers take to `acp/launch.py`;
one host's launch knowledge to that host's adapter under `acp/harness/`. A moved
name a test patches through its facade is
forwarded in `_EXPORTS_BY_OWNER`, which `test_acp_refactor_facade.py` pins. New
`AcpClient` behaviour goes to the client owner whose row names its responsibility,
bound in the class (`acp/client_class.py`) under that owner's header, and a new timeout or turn budget to
`acp/client_timeouts.py`.

`AcpRuntime`'s behaviour moves into `acp/runtime_*.py` owner modules the same way, one
responsibility each. A member's body is a module-level function there whose first parameter
is `self` (annotated `AcpRuntime` for type checkers only), and the class binds it by name in
one block above `__init__`, one comment header per owner. The constructor, the properties and
the class constants stay in the class, and the runtime's state stays on the instance, so a
patch of a method on the class or on an instance, an `AcpRuntime.__new__` build and an unbound
`AcpRuntime.<member>(stub, ...)` call all reach the one function; each owner's docstring names
the instance fields its members write and read. `own_methods` names each bound function after
its class attribute, so its `__qualname__` (what a coroutine's and a task's repr print) and
`inspect.signature` read as before the move, and only its `__module__` names the owner. An
exception class an owner defines keeps `kiro_crew.acp.runtime` as its `__module__`, so error
chains name the path callers import it from; `_MirroredSessionMcp` and `_PendingRequests` keep
their owner's, which nothing reads. The class statement is `acp/runtime_class.py`, which
`acp/runtime.py` re-exports with `kiro_crew.acp.runtime` as its `__module__`, so
`inspect.getsource(AcpRuntime)` does not find the class through that module name: read a
member's source, or `acp/runtime_class.py`. `_RUNTIME_OWNERS` lists the owners lowest layer
first: an owner imports only owners listed before it. Owners log under the
`kiro_crew.acp.runtime` logger and never import
`acp/runtime.py` at run time. The facade keeps binding every name it bound before -- its own
unused imports are exempt from flake8's F401, and an import check stands in for it: each
import is a name the facade bound before, its mirror glue, a name an owner defines, or a name
the facade's or an owner's code reads -- keeps forwarding the names in `_EXPORTS_BY_OWNER` to
their owners, and a write through it reaches every owner binding the same object
(`kiro_crew._facade_mirror`). `test/test_runtime_bindings.py` pins the member inventory, the
binding, the reach and the import check. New `AcpRuntime` behaviour goes to the owner whose row
names its responsibility, bound in the class under that owner's header.

| Runtime owner | Owns |
|---|---|
| `acp/runtime_tree_kill.py` | the runtime's process tree and its teardown: proving the root is still the process it spawned, recording its descendants, signalling the tree, and `kill` with its reap and the release of what the process held |
| `acp/runtime_spawn_launch.py` | launching the child and making it a runtime: the harness spawn plan and the spawn agent's projected spec, the sandboxed launch tail (`_launch_tools`, the retrying spawn factory, the private-state seed), the `initialize` handshake with the client capabilities it declares, and the failed-start guard that reaps what a broken start left |
| `acp/runtime_spawn_entry.py` | `spawn` under the cold-start admission, the one re-derive retry after a stale spawn spec, and the revive of a runtime whose start failed |
| `acp/runtime_session_setup.py` | what `session/new` and `session/load` are built from and checked against: the working directory, the MCP server array (the mirrored projection, the stub session, the pooled and unpooled control planes, the member panel), KAS custom agents, the derived-spec and member-projection checks, the unresolved-reference guard, the session-open mount check, the start budget and the copy of the native launch sources |
| `acp/runtime_session_create.py` | `session/new` end to end: `create_session`, the collector hand-off of a start that outlives its budget and the late-session teardown, the finish every answered start shares, and the entitlement probe session |
| `acp/runtime_session_load.py` | `session/load` end to end: resuming a prior session and waiting for its managed MCP servers |
| `acp/runtime_session_registry.py` | the sessions this runtime holds and how each one leaves: init frames staged before a session id is known, the start-stall MCP diagnostics, turn-active marks, and terminate and unregister |
| `acp/runtime_mode_activation.py` | putting a started session on its agent: the bracketed `session/set_mode` with its skill-view aliases and the one-at-a-time projection generation, the spawn-agent checks, and the model an agent switch selected |
| `acp/runtime_wire.py` | JSON-RPC over the child's stdin: requests, notifications, responses and errors under the one write lock and the no-progress bound, the answer futures, and the awaited control-plane request (`_send_and_await`) |
| `acp/runtime_liveness.py` | whether the runtime is alive, busy, dead or due to recycle: the exit watch, the stderr drain and its latches, death attribution and its summary, reap tracking, session occupancy and the age and RSS staleness checks |
| `acp/runtime_host_requests.py` | requests the agent sends this host that no session turn answers: permission requests for unregistered sessions (bounded off-loop answers and their audit), the host credential callback, MCP sign-in and re-attempt, the consent URL, the engine's MCP secret store (`_kiro/secret/*`), and ownerless requests |
| `acp/runtime_frame_accounting.py` | what the reader drops or truncates, counted instead of logged per frame: the backend subagent roster and its overflow episodes, and the unroutable-frame drop summaries |
| `acp/runtime_reader_loop.py` | the single stdout reader: routing every frame by id and sessionId, the host's notification aliases, oversize-frame recovery, and the error an awaited request fails with |
| `acp/runtime_class.py` | the class statement: the constructor, the properties, the class constants, the field declarations, the binding block and the `_MEMBER_OWNERS` set it passes to `own_methods`; `acp/runtime.py` re-exports the class, which keeps that module as its `__module__` |

`acp/_dispatch.py`, the dispatch parser both transports share, is a mirror facade over
the owners in `acp/_dispatch_parts/`. It keeps the session handshake -- the request shapes
both transports send (`build_session_new_params`, `set_mode_params`, `set_model_params`),
the initialize and `session/new` readers (`agent_version_from_init`,
`parse_session_modes`, `advertised_mode_origin`) and the read loops' yield budget
(`DRAIN_YIELD_AFTER_S`) -- the per-turn metadata, refusal and usage parsers, and
`classify_notification`. Each other responsibility has the owner below, and an owner
imports only the owners above it in this table, never the facade. Every name stays readable and patchable as
`kiro_crew.acp._dispatch.<name>`: a patch through the facade reaches every owner that
binds the name (`kiro_crew._facade_mirror`), and the owners log under the
`kiro_crew.acp._dispatch` logger. `test/test__dispatch_facade.py` pins the owner set,
the layer order and the reach. New frame-shaping code goes to the dispatch owner whose
row names its responsibility, and a new request shape or turn-metadata parser to
`acp/_dispatch.py` itself.

| Dispatch owner | Owns |
|---|---|
| `acp/_dispatch_parts/tool_call_identity.py` | which tool a `tool_call` frame names: the shell-kind test, `classify_tool_call`'s transport-aware `ToolCallIdentity` verdict, the bounded harness tool ids and the harness-authored `_meta` identity channels |
| `acp/_dispatch_parts/surfaced_text.py` | text made safe and readable before it is surfaced: the one redaction policy (`redact_text`, length-safe backend ids), the unified edit diff, the pill title and purpose line, and the JSON encode that degrades instead of raising |
| `acp/_dispatch_parts/tool_call_provenance.py` | the per-turn tool-call provenance caches: their scoped key, caps, overflow record and admission, the release at a terminal status, and the liveness keys (`liveness_key`, `InFlightIdSet`) |
| `acp/_dispatch_parts/adapter_signals.py` | claude-agent-acp and codex-acp signals that ride ordinary frames: compaction notices, adapter-written errors, background-launch records and codex compaction updates |
| `acp/_dispatch_parts/tool_result_events.py` | a `tool_call_update`'s output as `EVENT_TOOL_RESULT`: content and `rawOutput` text, the unrenderable-content diagnostic, the output measure and the `todo_list` snapshot |
| `acp/_dispatch_parts/permission_requests.py` | a `session/request_permission` as `EVENT_PERMISSION_REQUEST`: option kinds and the least-destructive reject, the gate-extension envelope, MCP identity and approval, KAS consent and `build_permission_event` |
| `acp/_dispatch_parts/session_update_parser.py` | the `session/update` parser: chunk text and message ids, the `tool_call` and refinement builders, and `parse_session_update` |

`acp/liveness.py`, the per-session liveness oracle, keeps `LivenessOracle`, the
offloaded-consult guard (`consult_offloaded`, `ConsultFutureHolder`,
`OFFLOADED_CONSULT_TIMEOUT_SECS`) and the module docstring that carries the platform
evidence matrix, and re-exports every name its owners define, so callers keep importing
them as `kiro_crew.acp.liveness.<name>`. It is a plain facade: the names tests patch
through it (`process_start_boot_secs`, `select_darwin_backend`) are read by the oracle as
its own globals and by no owner, and an owner imports only the owners above it in this
table, never the facade. The family logs under the `kiro_crew.acp.liveness` logger.
`test/test_liveness_facade.py` pins the reach, the layer order and the logger. New oracle
logic goes to `acp/liveness.py`, and a new verdict, tag, tunable, process reader, command
reader, interactive rule or tool-call field to the owner whose row names it.

| Liveness owner | Owns |
|---|---|
| `acp/liveness_evidence.py` | the oracle's vocabulary: the `VERDICT_*` verdicts, the `EVIDENCE_*` tags and the contract tunables its probes read |
| `acp/liveness_proc.py` | the process-tree readers: the `/proc` helpers, `steady_now`, and the darwin `LibprocBackend` that `select_darwin_backend` picks on a host without procfs |
| `acp/liveness_command.py` | the cached command text: its retention bound (`MAX_RETAINED_*`, `bounded_command_text`) and the readers that recognise what it started (`match_fragment`, `first_program`, `parse_wait_seconds`, and `launch_matches` over a `LaunchRoster`) |
| `acp/liveness_interactive.py` | interactive-command classification: the program tables, `InteractiveClassification`, `classify_interactive_command` and `non_interactive_hint` |
| `acp/liveness_tool_state.py` | the in-flight tool call: `ToolCallState`, the `InFlightToolTracker` hand-off rule and `wait_tool_verdict` |

## Native skill startup views

`acp/skill_projection.py` is a mirror facade over the owners in
`acp/skill_projection_parts/`: every name stays readable and patchable as
`kiro_crew.acp.skill_projection.<name>`, a patch through the facade reaches every
owner that binds the name (`kiro_crew._facade_mirror`), the owners log under the
`kiro_crew.acp.skill_projection` logger, and an exception class an owner defines keeps
the facade as its `__module__`. An owner imports only the owners above it in this
table, never the facade; `test/test_skill_projection_facade.py` pins the owner set,
the layer order and the reach. New projection code goes to the owner whose row names
its responsibility.

| Projection owner | Owns |
|---|---|
| `acp/skill_projection_parts/side_files.py` | the side files under this data home and the protocol over them: their layout and legacy locations, the alias-name shape, the bounded publication lock and its two timeouts, the per-process leases with their liveness scan, and the identity-checked unlinks |
| `acp/skill_projection_parts/inheritance_overlay.py` | Crew's overlay on `chat.disableInheritingDefaultResources` in a workspace `cli.json`: its keys, the settings read, the recorded preference behind `inherits_default_resources` and the rollback |
| `acp/skill_projection_parts/view_publication.py` | what a view hands kiro-cli beyond its authored spec (the bounded display text, the search tool) and the in-place rewrite that makes kiro-cli reload it (`announce_alias`) |
| `acp/skill_projection_parts/view_ownership.py` | which views are this data home's: ownership records, the home tag, the view identity and digest, sidecar and metadata reads, and whether a published copy is current |
| `acp/skill_projection_parts/view_sources.py` | which agent a skill-view name projects: the in-process map, the view ledger, the sidecar fallback and the resolvers that raise `RetiredSkillView` |
| `acp/skill_projection_parts/native_projection.py` | the prepared `NativeSkillProjection` and the registry of live projections a prune must keep |
| `acp/skill_projection_parts/alias_reclaim.py` | reclaiming unused views and their residue (the per-spawn, end-of-projection and boot prunes, the orphan sweeps, the legacy side-file migration) and the read-only censuses the doctor reports |
| `acp/skill_projection_parts/preparation.py` | `prepare_native_skill_projection` and its view-count ceiling fallback |

Native CLI launches prepare a `skill_projection` after the existing spec freshness
check. The alias preserves the prompt, approval policy and non-skill resources,
while Crew retains the authored skill mapping for scoped discovery. A workspace
CLI overlay suppresses implicit native resource inheritance; explicit steering and
AGENTS.md resources preserve enabled inheritance. Aliases are excluded from Crew's
agent roster, translated on `session/set_mode`, and normalized in incoming mode and
agent-name fields. The original agent name remains the Crew session identity.
Both the direct client and multiplexed runtime apply the same preparation.
KAS takes none of this (no view, no `cli.json`): it holds a session to the same
skill set with `skill` deny rules in the projected agent's `permissions`
([memory-skills-hooks.md](memory-skills-hooks.md), KAS skill scope).
Projection defaults to enabled. Set `agent.native_skill_projection: false` in
`config.json`, or `KIROCREW_NATIVE_SKILL_PROJECTION=0` in the Crew process
environment, and restart Crew's native sessions to roll back to authored native
agents. The environment variable wins over a config value of `true`. A config
file with bad JSON loads with the key at its default; a config load that raises
disables projection, as `mcp_gateway.apps_enabled` does (`_projection_enabled`). Disabled launches restore the Crew-owned inheritance
overlay before spawning and bypass alias translation and projected search
requirements. A spawn-time ceiling takes the same fallback automatically: when
the agents directory already holds `SKILL_VIEW_PROJECTION_CEILING` (128, the
constant the doctor's backlog warning also reads) or more
`kirocrew-skill-view-*.json` files, a fresh preparation stops minting new views
and runs the authored agent, so a regression that reopens the leak cannot deepen
it. The count is the raw total of alias files, including those another Crew home
sharing the agents directory wrote, because kiro-cli reads every one at startup
regardless of owner; the doctor names the foreign-home case and its move remedy.
The ceiling gates only the spawn-time path (preparation that did not specify
`enabled`); an explicit `enabled=True` -- the warm `set_mode` refresh of an
already-projected live session -- is always honored so the ceiling never aborts a
running session, which means a session that began below the ceiling keeps
refreshing its own view. On the ceiling-tripped fallback the existing per-spawn
prune still runs, so this home's own backlog can drain back below the ceiling
without a restart; the disable path (either switch) does not
prune. Existing governance, sandbox and signed-session identity checks
still apply. The switch is latched at spawn; mode changes in a running projected
process keep projection enabled until restart. Rollback restores native skill
metadata enumeration, so the bounded native startup guarantee no longer applies.
The new alias integration has fixture coverage; an end-to-end native CLI probe
remains outstanding. The enabled default is an explicit rollout decision.
Mapped agents and the default `kirocrew` agent expose the single
`@kirocrew-core/skill_search` tool when the authored tool list does not already
include it. Unmapped custom agents gain no tools or servers and have an empty
discovery scope. The added tool uses the managed server declaration without
adding auto-approval. Native `/agent` mutations are refused with a pointer to Crew's
agent selector, which updates both the native mode and Crew's template binding.
A typed `/agent <name>` never reaches that refusal on Crew's own surfaces: the
dashboard composer, the chat runner (split pane and linked channel threads), a
Slack thread, the text-command channels (Webex, Discord, Teams, Feishu, via
`handle_channel_agent_command` / `ChannelAgentPicks`) and Telegram's button picker
(via the same `switch_channel_agent` step) each switch through Crew instead
(`agent_switch_command.py`; the runner and the picker's route share
`chat_handlers.switch_slot_agent`).
Read-only native agent listing/schema commands remain available. Mode activation
refreshes the view inside the existing derived-spec freshness bracket.
The reserved core server's command and environment are pinned to the managed
declaration. Explicit search exclusions disable that agent's projected view with an
actionable error. The reserved server's `disabled` must be a boolean and
`disabledTools` a list of strings; null or malformed values fail only that agent
with an actionable error instead of aborting preparation of healthy agents.
Shared runtimes derive session control-plane elements from the
prepared view, then attach the existing signed session token. An existing broker
element wins. Whether the `kirocrew-core` element can be mounted at all is one
question with one predicate, `session_mcp.native_mount_withholding`, asked over
the source set `session_mcp.native_settings_sources` returns: the agent's own
`kirocrew-core` entry, the global `~/.kiro/settings/mcp.json` the dashboard's
tool toggle writes, and the project's `.kiro/settings/mcp.json`. The two
readers split the sources by what each one is. The projection judges the spec
entry at preparation, as authored and before the managed launch replaces it (the
replacement carries only the keys a per-session element can express, so a `type`
or a key the element cannot carry would otherwise vanish unjudged): a restriction
authored there is static for the view's life and refuses that agent's projected
view, naming the spec and the restriction. The settings files are judged at
every session start, because they outlive a spawn and are shared by every agent
of the process, and the mount is their ONE reader: `kiro_control_plane_servers`
reads them once at `session/new` and `session/load`, judges every granted
identity-bound server -- a name a broker stub already carries included, since the
stub carries a kiro-cli-only restriction no better than the element does -- and
returns the array together with the verdicts it withheld on
(`NativeControlPlaneMount`). For a projected search agent whose element the mount
did not produce, the shared runtime refuses that one session on THAT verdict,
with a sentence naming the file, the restriction and the way back (the
dashboard's MCP tab for a toggle); it reads nothing again, because a second read
is the window in which a toggle undone in between answers "allowed" and the
session falls to the generic guard naming the wrong file. The spawn stands, the
other agents' sessions stand, and a toggle undone restores the next session
without a respawn. A broker stub is not a source: its overlay is written once
from the spec and the global file as they were, so a stub written before a toggle
is exactly what the toggle cannot have reached; the files are judged before it is
accepted. Agents outside the projection take the stub path as the mount leaves
it. The direct client (`AcpClient`) mounts no per-session element at all: one
process serves one session, the identity rides the process environment, and
kiro-cli mounts `kirocrew-core` natively from the view -- the managed launch plus
the `autoApprove`, `disabledTools` and `timeout` the view copies -- so the element
question does not arise there, and the projection it prepares
(`per_session_element=False`) keeps a view whose spec restricts other tools
(`disabledTools: ["learn_add"]`, a `timeout`); only `skill_search` itself
disabled or excluded still refuses it. A restriction is a disabled server, a non-empty
`disabledTools`, a transport a user chose that is not stdio, a key the per-session
element cannot carry -- the table the spec rebuild keeps on a managed entry
(`agent._MANAGED_MCP_ENTRY_KEYS`, the one table both share) less `timeout`, which
`acp_server_element` does not emit, so a `timeout` set on the declaration the
element would replace keeps that declaration native rather than run the server on
the default with nothing to say so -- an entry that is not an object, or a settings
file that cannot be read safely. Kiro Crew's own marks on an entry are not
restrictions: the `registry` transport value the rebuild stamps under
`agent.mcp_registry_mode` -- on the agent spec's managed entry while that mode is
on, the one state in which Crew writes it; outside registry mode kiro-cli drops a
marked entry wherever it is declared, so the same value on the spec with the mode
off (a marker the rebuild has not yet removed, or a line someone wrote) and in a
settings file in any state keeps the declaration native like any other non-stdio
type, and the predicate reads the mode itself rather than trusting the
declaration's place -- the `x-kirocrew` provenance
marker the global-file sync stamps and the `x-kirocrew-derived` record the
rebuild writes -- kiro-cli reads none of them as a setting. The way back the
verdict names is the dashboard's MCP tab only for a mute or a `disabledTools` in
the global settings, the one file that tab writes; a restriction in the spec or
the project's file is offered the edit that removes it there. The message repeats at most eight
user-authored values and at most `_NATIVE_NAMED_CHARS` characters of each -- a
name, a key, a transport, the `repr` of a malformed value -- and at most
`_NATIVE_NAMED_PATH_CHARS` characters of a settings file's path, cut from the
front so the tail that locates the file survives, because the message is
retained per agent for the life of the runtime while the file it quotes is read
up to the reader's 50 MB ceiling. Every value and the path go through the
package's one sink cleaner (`mcp_session_report.sanitize_sink_text`: redact,
drop control characters, then cut) before the message repeats them -- the value
is text from a settings file, the project's checked out with the repository,
and the message reaches the gateway's log, where a newline or an escape sequence
in a tool name would write a line of its own. The cleaner reads the first
`_NATIVE_NAMED_WINDOW` characters of a value whole, so nothing the message
repeats was cut before it was redacted and a 50 MB value costs a window, not
the file; a value the window did not hold whole is a cut value. The restriction itself stands, since the mount
never widens the grant. An agent the spec refused is never in the projection's
search set, so the `session/new` refusal to bind `skill_search` without its
native restrictions is unreachable for that shape: the spawn that would load its
view fails as `AcpRuntimeError` carrying the projection's sentence, the error the
provider's startup paths translate; a session refused for a
settings file names the file instead of that guard, and the guard remains for
what it was written for: an element neither granted nor pre-empted while the
files allow it.

The workspace overlay owns `chat.disableInheritingDefaultResources=true` while
Crew supplies discovery. Only literal JSON `true` in the original native setting
disables inherited steering/AGENTS files; malformed values such as `"false"` or
`1` preserve those instructions. Rollback still restores the original local value
and key presence unchanged. For an inherited global preference, global changes are
re-read at each launch. A pre-existing local preference is preserved in
`kirocrew.skillDiscovery.inheritFiles`; edit that boolean to change explicit
steering/AGENTS inheritance while retaining the native skill metadata bound.
This overlay also affects standalone native custom agents in that workspace.
The [Kiro CLI 2.10 release notes](https://kiro.dev/changelog/cli/2-10/)
document this setting and agent-config hot reload. No documented per-invocation
settings channel was found; changing `KIRO_HOME` would also relocate native
identity and session state, so it is not used for this overlay. Every in-product
workspace `cli.json` writer—projection, effort, Tool Search, and the built-in
review pool—takes the same verified `.kirocrew-cli-settings.lock` sidecar and
reads the file only after acquiring it. Projection holds that lock from the
fresh read through alias publication and settings commit, so a concurrent writer
cannot be replaced by a stale pre-enumeration snapshot. Lock identity changes or
a two-second acquisition timeout fail closed without writing the settings file.
When the workspace is a git work-tree top (it holds `.git` itself), taking that
lock also appends anchored patterns for the overlay and its lock to the
repository's local `info/exclude` (the common git dir, so linked worktrees are
covered), so neither file shows in the user's `git status`. A subfolder or a
Crew run dir inside a repository is left alone, so per-run dirs never grow a
repository's exclude list. Only missing lines are appended, the
user's own lines are never rewritten, and a failed exclude write never blocks
the overlay. A repository that already tracks `.kiro/settings/cli.json` still
sees it modified: git's exclude list does not hide tracked files (known limit).
Crew records the original local inheritance key's presence and value in
`kirocrew.skillDiscovery.previousInheritance`. Rollback restores that snapshot
only while the native key still equals Crew's asserted `true`, removes Crew's
overlay markers and preserves unrelated settings and a native key that the
operator changed or removed. Older overlays use their recorded local/global
source and boolean preference for restoration. Stop projected sessions before
rollback so another active Crew process cannot reassert the shared overlay.
Inactive aliases owned by the same Crew data home are pruned when no projection
in this process holds them and no held lease in any process names them. An
alias is named by a 24-hex digest of the agent name, the owning Crew data home
and the view content, so every spawn from that home that derives the same view
-- any run folder, any session -- publishes the same file, and publication skips
an existing alias that already SAYS the same spec with this home's sidecar: the
two are compared parsed, so a launcher that re-serializes every file in the
agents directory does not force a rewrite of every alias under the shared lock,
while every value, env values included, still counts. Each MCP server env value is in that
digest as a digest of its own, except the values of `volatile_env_keys()`: keys a
launcher re-stamps with a per-launch nonce (default `AIM_CREDS_AGENT_INJECTION`,
extended by `KIROCREW_SKILL_VIEW_VOLATILE_ENV`). Leaving those in would name a new
alias on every spawn, so a changed volatile value reuses the alias and publication
rewrites it with the full view. Every other value counts: a rotated credential
names a NEW alias, one kiro-cli has not loaded, so `set_mode` never activates a
copy still carrying the old credential, and two launch contexts with different
credentials never share an alias. The env keys and the source spec's path are in
the digest too, so two agent files differing only in env values never share an
alias. A launcher nonce under a
key the set does not know shows up as one view per launch; `kirocrew doctor`
names each `<server>.<KEY>` whose value differs across one agent's views
(`census_churning_env_keys`), so the operator can declare it volatile.
The same list governs the worker-spec freshness gate: a change to a volatile value
alone neither ends a worker load (`require_unchanged_derived_spec`) nor re-derives
the `kirocrew-worker.json` mirror. So a key declared volatile must be one the
launcher writes into every agent spec it manages, the worker's included.
Views can still differ per
workspace: a SCOPE_PROJECT agent's prompt path and workspace-local inheritance
shape the view, so those agents get
one alias per workspace. A run's work directory is reclaimed separately (`session_work_dir`),
under ONE invariant: a reclaim decision uses only evidence the deciding process
OWNS -- its session registry for its own runs, and its own data home's pid ledger
(`kiro_session_pids.txt`, reaped at boot by `cleanup_orphaned_sessions`) for a
dead predecessor gateway. It never probes the liveness of a pid another process
wrote to decide a deletion, and it never judges a directory marked by another
data home. The provider factory marks a work directory it DERIVED from an exact
generated one-run key -- `subagent:<16 hex>` from `SubagentManager._mint_agent_id`
(eight hex on the shipped builds whose directories the sweep also recognises),
`cron:<8 hex job>:<8 hex run>` from `build_cron_session_context`,
`memory-consolidation:<store>:<32 hex>` from `llm_helpers.background_turn` (discards
the key when the call ends), `judge:<uuid4 hex>` from `decisions/impl_llm.py`
(destroyed on return), `taskrunner:refine:<epoch ms>` from
`dashboard/handlers/taskrunner.py`, `hook:default:<epoch s>-<token_hex(6)>` from
`dashboard/handlers/hooks.py` (per-request default webhook key), or
`wf-author:<wf_<n>>:a<attempt>` from `workflows/service.py` (destroyed in the
finally block after authoring), or one of the four issue-radar model-call keys
`issue-radar-ai:<owner>/<repo>#<n>:<uuid4 hex>`,
`issue-radar-pr-ai:<owner>/<repo>#<n>:<uuid4 hex>`,
`issue-radar-reco:<owner>/<repo>:<uuid4 hex>` and
`issue-radar-tagging:<owner>/<repo>:<uuid4 hex>` from
`apps/builtins/issue_radar/backend/http_routes/{ai,recommendations,tagging}.py`
(one tool-less call each, released and destroyed on return by
`_run_oneshot_model`) -- never a caller-supplied `cwd`. An issue-radar `<owner>`
may be a nested GitLab `group/sub` path, and each repository segment is limited
to `[A-Za-z0-9._-]`, the set the GitHub and GitLab segment validators admit, so
the key never holds `\`, `:` or a space; a key outside that set (an Azure
segment with a space) stays long-lived and its folder is kept. Unlike the other
shapes, an issue-radar key's folder is not just the key with `:` turned into `_`:
`_safe_dir_name` also turns each `/` into `_` and keeps `#`, so
`issue-radar-ai:o/r#7:<hex>` lives in `issue-radar-ai_o_r#7_<hex>`. Its entry in
the shape table therefore carries a (key pattern, folder pattern) pair for the
repository component instead of one regex shared by both forms. The four
issue-radar shapes are reclaimable on both platforms' walks (the pinned walk and
the by-name Windows walk). The `memory-consolidation` key
counts as one-run only where the walk holds `.kiro` open; on the by-name walk
(Windows) its folder is never marked, so neither the shutdown reclaim nor the sweep
removes it, and its name is no sweep candidate. The doctor's census below still
counts it with the unmarked folders, since counting deletes nothing. The key
predicate and corresponding directory-name regex come from one shared shape table;
stable `cron:<job>:<agent>` keys are not one-run directories.
`AcpProvider.start` writes a `.kirocrew-run-dir` marker before
any other writer: two ASCII lines, the data-home identity (`data_home_id()`, the
digest of the resolved `config_dir()`) and the gateway pid. That marker is the
directory's only provenance: a name under the workspace root is not one (a person
can make `subagent_deadbeef` there), and the content of `cli.json` is not one
either, because the projection merges its keys into a file that already exists.
`mark_run_dir` is idempotent for a marker already naming this (home, pid),
replaces one this home's predecessor wrote (one gateway runs per data home, so
that pid is not running), and never touches another home's or an unreadable one
-- the session then continues without a sweepable mark. Marker reads use the same
bounded discipline as agent scratch: links are refused, the no-follow descriptor
must name a regular file no larger than 4 KiB, the home id must be 24 hex digits
and the pid in the kernel range; any other shape keeps the directory.
`AcpProvider.shutdown` removes a marked-by-flag directory only after its client
positively confirms the root and every tracked child exited (`process_tree_confirmed_dead`),
and when it holds only Crew's own residue (`.kiro/settings/` with `cli.json` and
its lock sidecar, plus the marker) and the `.kiro/agents` folder kiro-cli creates
in the folder it starts in, while that folder is empty. On platforms without
descriptor-relative opens (Windows) the by-name walk keeps a tree that holds that
folder, since it cannot pin `.kiro` against a junction swapped in after the check.
The factory flag permits an absent marker on this path, so a refused marker write
never makes the directory unreclaimable; a
present marker must name this data home AND this process exactly, and anything
else refuses. The flag says the directory was derived from a one-run KEY, not
that this instance is the one the registry kept for it, so registration installs
a fail-closed ownership claim on the provider. At final reclaim that claim holds
the session registry lock from the check through the off-loop filesystem
operation; if cancellation arrives mid-reclaim, shutdown waits for that worker
before releasing the claim and then propagates the cancellation. It refuses
while another registered provider names the same key or
cwd, or while an allocation reservation names the key; a successor that has not
registered yet is therefore fenced too, and one beginning after the claim waits
until the old residue is gone before its own start recreates the tree. No
installed claim, or a claim that raises, keeps the directory. Where the registry
already knows a provider lost ownership -- the loser of a cold-start race, a
recycled session whose successor is already registered, or the bootstrap provider
that borrowed a parent's key -- it still calls `disown_work_dir()` first as the
cheaper one-way early exit. Only Crew's residue names are ever unlinked and every
directory goes through `rmdir`, so a run that wrote anything into its cwd keeps
the whole tree. The gateway sweeps `workspace_root()` for what a dead PREDECESSOR
of its own data home left (`sweep_predecessor_work_dirs`): once at boot, after
`cleanup_orphaned_sessions` has reaped the ledger and past `KIROCREW_READY` but
before any session writer starts, and again on the hourly maintenance wake with
the same rule for whatever the bounds left. It reclaims a marked, residue-only
directory only when the marker names this data home, a gateway pid that is not
this process, and a pid the ledger retains no entry for
(`session_pid.retained_gateway_pids`, read under the ledger's lock and never
modifying it -- an entry left after the reap means something of that gateway may
still be alive); it skips any directory a registered provider names and any whose
newest mtime is inside a grace window (a plain extra brake). A marker naming this
process is never the sweep's business, a marker of another data home is never
touched whatever its pid is doing, and an unreadable ledger sweeps nothing. Each
wake is bounded by entries and wall clock, and streams the listing so a root of
any size costs it counters, not a list. Directories older builds left carry no
marker and are never reclaimed automatically; `kirocrew doctor` prints one line
with three figures from one bounded walk judged by the sweep's own rule --
unmarked directories, marked ones this data home cannot reclaim (another home's,
an unreadable marker, or a gateway the ledger still retains), and marked ones the
rule permits yet the sweep keeps for their contents (counted by a read-only
residue probe that mirrors the sweep's keep rule -- the by-name walk keeping
kiro-cli's `.kiro/agents`, or any marked folder that gained a file beyond Crew's
residue) -- resolving the
workspace root without creating it (`workspace_root(create=False)`) so the
read-only report leaves no tree behind on a host where no gateway ever ran, and
naming the manual remedy for an unmarked backlog. The doctor deletes nothing.
Each run caps reclaims at `_PRUNE_MAX_RECLAIMS_PER_RUN` plus the number
of aliases it publishes. Aliases published by builds that predate this lifecycle carry NO
record of either kind, so an ownership-keyed reclaim alone would leave the entire
accumulated backlog on disk and bound only post-upgrade growth -- which is the
per-turn tool-spec cost this exists to remove. Those are reclaimed on a separate
path that does not read a record: the name must match Crew's own prefix plus the
24-hex digest the projection derives, the file must be a projected view (it
renames itself to that alias and carries no `skill://` resource), AND it must
carry one positive mark the projection itself writes -- Crew's managed
`kirocrew-core` server entry, or the absolute steering resource pointing at this
host's kiro home. A matching NAME alone never authorizes removal, and neither
does shape: an unlink is not undoable and an operator's own agent could in
principle carry that name, so an unrecorded view Crew cannot positively claim is
left alone. That is a smaller reclaim than the name shape would allow and the
right side to err on. That path cannot prove the pair unregenerable, so
its safety rests on the consumer contract instead -- the spawn argv and
`session/set_mode` both re-prepare before they use an alias, and `/agent` is
refused rather than translated -- which makes a removal a cache eviction for a
live pre-upgrade session (its next preparation republishes the same name WITH a
record) and a reclaim for every dead work directory. That contract has exactly
one hole, at the upgrade boundary: a publisher from a build predating the lease
holds no lease, and between its write and kiro-cli reading `--agent` its alias is
indistinguishable from backlog -- and it will NOT re-prepare, having already done
so, making a deletion a failed spawn rather than an eviction. An unrecorded alias
is therefore spared until it is older than a minimum age. That age is NOT a
liveness proxy -- the reason an age cut-off is rejected for the recorded path --
it only has to exceed publish-to-spawn, which is milliseconds, while the backlog
it reclaims is hours to days old; a clock that moved backwards lands on the
sparing side. Because no record exists, a
legacy alias also carries no data-home attribution, so a second Crew home sharing
this agents directory sees the same eviction-then-republish rather than the
home-scoped skip a recorded alias gets. Every other gate still applies to it:
this run's own set, live in-process projections and held leases are all checked
first, and removal is identity-checked against the bytes and inode just read.
The prune runs while the publication lock is held, which is what keeps a
deletion from landing on an alias a publisher that takes the same lock is
writing. What one call bounds is the section it holds, not the pile it works on.
Reclaims, stale-candidate work, and total traversal have separate PER RUN
ceilings. Leases are read by ONE bounded scan per prune, taken before the walk.
A lease is only created under this same lock, so none appears mid-walk; one
released mid-walk (its finalizer takes no lock) still reads as held, which keeps
its aliases one call longer and never frees one early. Candidate discovery
streams the directory under an entry-walk limit: retained aliases do not consume
the stale-work budget, but every directory entry consumes the separately bounded
traversal budget, and skip credit is capped at one work-limit, so padding and a
large live set cannot extend the enumeration. Reaching that limit is logged at
INFO, the same way the lease-scan ceiling is. Classification then runs under
`_PRUNE_MAX_SECONDS_PER_RUN`, a BETWEEN-candidate budget: it limits how many
candidates are walked, not how long any single one takes. It is derived from the
lock's acquisition ceiling -- the ceiling minus the lease scan's budget minus
`_PROJECTION_PUBLICATION_RESERVE_SECS`, the share kept for the publication
writes in the same section -- so retuning the ceiling moves the budget with it.
Reclaims are capped at `_PRUNE_MAX_RECLAIMS_PER_RUN` plus the aliases the call
publishes. That cap is a ceiling and never a floor: a candidate that proves
unreclaimable costs a full classification without counting toward it, so a call
can spend its whole budget and reclaim nothing. Each call starts its walk at an
offset drawn at random into the bounded candidate list, because the workload
spawns a fresh process per run and a remembered cursor would restart at zero
every time; across spawns an entry inside that window is reached with some
probability, never on a schedule, and entries beyond the entry-walk limit stay
deferred while stable padding sits ahead of them, so the ceiling guarantees
bounded entry traversal rather than eventual drain. So one call guarantees a
bounded locked section and at most a capped number of reclaims. No call
guarantees the pile is smaller afterwards. The pile shrinks only while aliases
are reclaimed faster than callers mint new ones, and the mint rate is a property
of the workload -- how often agents are spawned with views that differ -- not of
this code: the budget sets how much work a call may do, not whether that work
outpaces the callers. A gateway-boot drain (`drain_stale_aliases`, reached from
the boot janitor through the `agent_sdk.drivers.acp` seam) runs that same
per-spawn prune under the publication lock in a loop, pausing between batches
for longer than the lock's poll cap so a waiting spawn can take the lock. A
batch that cannot take the lock counts as one that reclaimed nothing. It stops
after `_DRAIN_IDLE_BATCHES` consecutive batches that reclaim nothing, each from
a fresh start offset, or at `_DRAIN_MAX_BATCHES`, and it removes what those
batches reach; it does not promise an empty directory. A reclaim
the filesystem itself refuses -- a read-only mount, an agents directory this
process cannot write -- is reported as a warning naming the directory, the
operation and the errno, at most once per interval with the count of refusals
it stands for; only `EACCES`, `EPERM` and `EROFS` are diagnosed as an unwritable
directory, and any other errno (`ENOENT` from a concurrent prune elsewhere,
`EMFILE`) is reported by name with no such diagnosis. Every other `False`
from the unlink is a deliberate keep and stays at debug.

Threads of one process do not race the publication file lock on their own
clocks. `_projection_alias_lock` first takes an in-process `threading.Lock` per
agents directory (`_in_process_projection_lock`, waited on for at most
`_IN_PROCESS_PROJECTION_WAIT_SECS`, 30 s, then an `OSError`), so the file lock's
two-second `_PROJECTION_LOCK_TIMEOUT_SECS` measures cross-process contention
only, and a burst of session starts in one gateway does not time the later ones
out into starting without their skill view while no other process holds the
lock. Queued starts pay for each other's locked sections in turn, so the
per-spawn prune is throttled: `prepare_native_skill_projection` runs it at most
once per `_PER_SPAWN_PRUNE_MIN_INTERVAL_SECS` (5 s) per agents directory per
process (`_per_spawn_prune_due`, decided under the lock). The boot drain prunes
on its own schedule and is not throttled.

`session/set_mode` never activates a copy of an agent older than the one just
prepared. kiro-cli 2.25.0 and 2.26.0 do not reload their agents directory on the
rename that publishes an alias (fixed upstream in kiro-team/kiro-cli#5026), so a
NEW alias published after the process started can answer `Mode '<alias>' not found`
while the file is on disk. The bracket re-prepares before `set_mode` and always
sends the FRESH alias: an alias the host loaded earlier, the spawn one included,
may hold a generation of the spec an edit has since removed a server or an
auto-approval from, and so may the authored spec kiro-cli cached, which it does
not reload on a rename either. A `Mode ... not found` naming that alias -- and
nothing else -- is retried after `_PROJECTED_MODE_RETRY_DELAYS_SECS` (3 s in
total), each retry preceded by `announce_alias`: a same-bytes in-place rewrite of
that alias, a data write kiro-cli's watcher DOES reload on (a miss triggers no
reload by itself). If the host still has not loaded it, the session start FAILS
with an error naming the agent and saying to restart the gateway; no other copy
is activated in its place. A re-preparation that could not run (the alias lock is
busy) fails the start the same way, since nothing proves any alias still matches
the spec.

Overlapping starts on one runtime are ordered so the newest view always wins. A
preparation reads the agent specs before it waits on the alias file lock, so
without ordering a start that read a spec before an edit revoked a grant could
return last and activate that alias. Preparation and adoption therefore run under
a per-runtime asyncio lock (`_skill_projection_lock`), so specs are read in
adoption order, and each adopted projection is stamped with a generation issued
under that lock; adoption refuses one older than the adopted generation. The lock
covers only the `to_thread` preparation and the in-memory adoption, never
`set_mode` itself, so a slow MCP server boot on one start does not hold up the
others. Instead, a start whose `set_mode` is pending re-checks the generation
before every attempt (it sends the newest adopted view's alias, never its own
older one, and fails if that view does not offer the agent) and after the
answer, after first taking the lock so a preparation still running when the
answer arrived is adopted before the check: if a newer view gave the agent a
different alias while the request was in flight, the host may have activated the
older one, so the session is terminated
and the start fails with "start it again; if it keeps failing, restart the
gateway". A newer view that leaves the agent's alias unchanged refuses nothing.
A preparation whose start is cancelled still runs to completion under the lock
and its view is adopted before the cancellation propagates. A newer preparation
that yields no view (or raises) leaves pending starts unable to prove their view
current, so they fail the same way until a later view is adopted.
Lock order, outermost first: `_skill_projection_lock` -> the alias file lock ->
the workspace `cli.json` settings lock -> the projection lease. The file locks
are taken and released inside one synchronous call on a worker thread, which
never waits on the asyncio lock, so the two cannot deadlock.

The adopted projection keeps translating, in inbound frames, every alias an
earlier projection of the process published (`recognise`: alias-shaped names
mapped to admissible agent names only, spawn aliases first, at most
`_RECOGNISED_ALIASES_MAX`, with one warning per projection past the bound), and
the projection the process spawned with is held for its life as
`_spawn_skill_projection` so those aliases stay out of the prune. In
`availableModes` a projected agent stays listed when the host advertises any of
its aliases OR its authored id, once, so an agent never vanishes from later
session starts because its alias changed. Each outcome logs and counts
`kirocrew.acp.skill_view.fallback`; any other error propagates as before.

An alias view leaves out `welcomeMessage` and caps `description` at
`_ALIAS_DESCRIPTION_MAX_CHARS`. kiro-cli copies every loaded spec's name,
description, source and welcomeMessage into each `session/new` and `session/load`
reply, and an alias is a second copy of an agent the host already lists. So
duplicating that text only doubles the reply, and past the ACP readers' 10 MB
frame limit the reply is dropped: 50 agents carrying 113 KB of release notes each
reached 11.7 MB. Kiro Crew renders an agent's welcomeMessage from the authored
spec, never from an alias. A preparation also warns, once per size, naming each
authored spec whose display text passes `_DISPLAY_TEXT_WARN_BYTES` and the
directory total past `_DISPLAY_TEXT_TOTAL_WARN_BYTES`. That text still reaches
every reply from the authored copy, so the fix for it is at its source.

The strict resolver (`NativeSkillProjection.agent`, used by `session/set_mode`)
refuses any name it never projected, so an agent cannot switch to a mode outside
the scope it launched under. Two seams around it must not open a hole. First, a
spec `list_agents` enumerated and read is projected or, if a validation branch
refuses it, RECORDED AS A REFUSAL (`errors`, under its name), never silently
skipped. An agent in no projected map is the no-view case: `spawn_agent` returns
its own authored name so it spawns rather than aborting a spawn over a skill view
it never asked for -- the issue's fix -- while the strict `agent` still refuses a
mid-session switch to any mode this view never prepared. Second, the launched
agent's OWN
activation must not be refused because the process is already running as it even
with no prepared view -- but this is scoped to session START, not mid-session
switches, and the two spawn paths handle it differently. The shared runtime hosts
MANY sessions and allows activating `self._agent` at EVERY session-start bracket
(`_activate_mode_bracketed`), keyed on `self._agent` (`_resolve_start_alias`); it
does NOT set
`spawn_agent_name`, because that field also makes `request()` -- the general
outbound path a mid-session `set_mode` takes -- tolerate the launch agent
indefinitely. The direct client is one process / one session: it sets
`spawn_agent_name`, its `request()` tolerates the launched agent's FIRST
`set_mode` and then clears it, so a later mid-session switch back to it takes the
strict resolver again and fails closed. The general (mid-session) `set_mode` path
stays strict for both. The create/load availability gate (`_verify_spawn_agent_active`,
Guard A/A2) compares the session reply's reported mode against the launch agent
`self._agent`; every inbound frame runs through `projection.frame`, which maps a
view ALIAS back to the declared name before the gate reads it, so the plain
comparison accepts a projected agent and a no-view launch agent alike. A genuine
substitution (the backend ran its own default after refusing the spec) matches
NEITHER and still fails closed via `_mode_available` (a backend that advertises no
`modes` list is no evidence of substitution and is admitted; an advertised list
that omits the agent fails closed). Because the start reads the session reply's
`availableModes` BEFORE it sends `set_mode`, `frame` keeps the launched agent's
own mode in projected `availableModes` whenever `advertised_launch_name` is set --
otherwise it would advertise no mode the process could activate and fail the
session open during initialization. That advertising field is set by BOTH spawn
paths (direct client and shared runtime)
and is kept DISTINCT from `spawn_agent_name`: advertising the launch mode at
session open is safe for both runtimes (it only decides what the start can see),
whereas `request()`'s mid-session tolerance stays direct-client-only and strict on
the shared runtime. Unlike `spawn_agent_name`, `advertised_launch_name` is never
consumed -- it describes a fixed launch fact -- and `recognise()` carries it onto a
refreshed projection (frame runs on the current projection, which the shared runtime
adopts mid-session), while still refusing to carry `spawn_agent_name`. A projection
no spawn claimed sets neither field, so an unprojected agent stays hidden.

Projected agent JSON contains only fields accepted by Kiro's strict schema. The
one display field a view carries is its own `description` -- "Kiro Crew managed
view of `<agent>` [`<home tag>`]: ... Use `<agent>` instead." -- replacing the
authored one: the Kiro IDE lists every file in `~/.kiro/agents/` as an agent, so
the row at least says what it is and which agent to pick (an edit to the
authored description alone therefore mints no new view). The eight-hex home tag
is a digest of the publishing data home, read by the prune (below). Lifecycle
ownership lives OUTSIDE the agents directory, under the Kiro Crew data home:
`<data_home>/skill-projection/` holds the publication lock (`publication.lock`),
the lease records (`leases/`) and the ownership sidecars with the view ledger
(`metadata/`), which the OS sandbox seals read-only (a forged sidecar or ledger
entry would pick the agent a resumed conversation runs as) and the file-tool
write floor refuses. Only the views themselves go into the agents directory, because
kiro-cli resolves `--agent <name>` there by name and knows no other directory;
everything else there is a row in the IDE's drop-down. Builds before
this layout wrote the same three things INTO the agents directory as
`.kirocrew-skill-projection.lock`, `.kirocrew-skill-projection-leases/` and
`.kirocrew-skill-projection-metadata/`; those legacy locations are still READ
for one release cycle -- the lease scan, the ownership lookups, the sweeps, the
census and `source_agent_name` consult this home's directory first and the legacy
one second -- and never written. The boot drain migrates them once, under the
publication lock, moving only what is provably this module's and this home's:
sidecars whose record names this data home (another home's stay; they are that
home's to move), the legacy ledger's entries merged into this home's ledger (the
legacy file itself is shared by every home that published here and names no
home, so it stays while it holds any entry or cannot be read -- another home's
stored view names resolve through it until that home's own migration copies
them -- and only an empty, readable one is removed; its names are copied in as
this home's OLDEST ledger entries, only where the bound leaves room, so a
re-import never evicts a mapping recovered from a sidecar), the legacy lease
directory once the lease scan has reclaimed its residue and it is empty (a live
lease is never moved), and the legacy lock last, only when both legacy
directories are gone and nobody holds it. The migration is idempotent and
bounded per boot. Each sidecar records the alias's
exact byte digest AND the digest of its identity (the view the alias is named by,
env values out), so a stale or replaced sidecar cannot authorize deletion of a
different spec, while an alias another program merely re-serialized or
re-stamped stays owned. A sidecar written before the identity digest existed,
whose alias bytes moved, is still ownership-checked -- marker, this data home,
the projected-view shape -- and then removed WITH its alias, without the legacy
age wait: every build that writes a sidecar also holds a lease while it uses the
alias; removing the alias alone would strand its sidecar. Two sweeps retire
what outlived an alias, under the same lock, walk limit, random start and time
budget as the alias walk, capped at
`_SIDECAR_SWEEP_MAX_PER_RUN`: this home's sidecars whose alias is gone, and empty
`<alias>.lock` files older than the legacy age with no alias beside them.

A sidecar is also the record a stored view name maps back through
(`source_agent_name`), so no path deletes one before the view ledger
(`view-sources.json`, beside the sidecars) holds its alias -> agent pair. The
orphan sweep records its whole batch in one ledger write, then unlinks, and
the time budget covers both halves; a batch the ledger refuses is kept. The
prune records a window of upcoming candidates the first time a pair needs it;
when the ledger refuses, the alias still goes and the sidecar stays, answering
on its own until a later sweep can record it. The ledger is bounded by
`_VIEW_SOURCES_MAX`. Every write makes its own pairs the newest entries (a pair
already held is rewritten too, so the same write cannot evict it); past the
bound, entries a sidecar still answers for go first, then the oldest retired
names, which stop resolving and are counted in a warning. A full ledger
therefore never stops the sweeps from retiring sidecars. A write the
filesystem refuses keeps every sidecar for the rest of that prune or sweep
without retrying per candidate. This
module never creates the latter; a launcher that rewrites every spec leaves one
per spec it locked. The boot drain counts both sweeps as progress. `kirocrew
doctor` reports the residue and the aliases whose bytes no longer match their
sidecar -- another program is rewriting the directory -- on a `skill-view
residue` line, warning past `_SKILL_VIEW_RESIDUE_WARN` or on any rewrite.

Publication is by rename (`atomic_write`), and kiro-cli -- measured on 2.25.0
and 2.26.0 -- does not reload its agents directory on a rename: its inotify
watch receives `IN_MOVED_TO` but it reloads only on a data write to a `*.json`
name, about 0.6 s debounced. A new alias renamed in after the process started
therefore answers `Mode '<alias>' not found` indefinitely, and an alias renamed
over a known name keeps serving its OLD content. Every publication is
therefore followed by `_announce_publication`, which rewrites the alias's own
bytes in place, unchanged -- a data write the host reloads on, which a racing
reader cannot see torn -- and only while the file still holds exactly those
bytes. That is what lets the `set_mode` retry above land on the published alias
within its budget instead of failing the start.

No released build ever wrote lifecycle fields INTO a spec --
kiro-cli denies unknown fields, so the projection never could -- and an alias
without a sidecar is judged by the unrecorded path above instead, with one
further gate now that each home keeps its sidecars under its own data home: a
view ANOTHER home sharing the agents directory published has no sidecar this
home can see, which is exactly what a pre-sidecar build's view looked like. The
home tag in the view's description tells them apart -- a sidecar-less view
tagged by another home is that home's live view and is kept, like a view with a
foreign sidecar; one tagged by THIS home with its record lost takes the legacy
path with every gate it has; one with no tag at all is the legacy case. The recorded
source paths are never probed, so untrusted metadata cannot
trigger a filesystem or network lookup. Each live projection publishes one bounded lease in
this home's `skill-projection/leases/` directory as TWO files: a `.json` record
naming its aliases, which is never locked, and a `.hold` sidecar that carries the
lock for the projection object's lifetime and is never read. The split is
required, not stylistic: Windows file locks are MANDATORY, so a lock on byte 0 of
the record makes a reader's parse fail with a lock violation from any other
handle, including one in the same process. Pruning always runs while the current
projection holds its own lease, so a single-file lease turned every liveness
probe into the uncertainty answer and reclaimed nothing on Windows while passing
on POSIX, where locks are advisory. Finalization releases the lock and removes
both identity-verified sidecars, then prunes EAGERLY: a projection that ends --
`NativeSkillProjection.release()`, which the direct client calls when its process
is dead, or the finalizer of a projection that was merely dropped -- runs the same
bounded prune, under the same lock and gates (lease scan, time budget), so this
home's now-unleased views leave the agents directory at once instead of waiting
for the next spawn. A per-spawn eager prune honours the `_per_spawn_prune_due`
spacing and never queues behind this process's own in-process lock (a start
holding it runs its own prune). An end has no other party covering it, so it
skips the spacing and queues for the lock -- but ends are COALESCED per agents
directory and home (`_coalesced_end_prune`): while one end's prune runs, a later
end only records a request and returns, and once the running pass finishes one
daemon thread makes the follow-up passes, so the end that ran first is not held
for the length of a wave. Because an end releases its lease before it asks, that later
pass covers it. Without this a subagent fan-out queued one full prune per ended
projection on the lock set_mode re-preparation waits on, and starts ran out
their in-process wait and were refused. When the end runs on an event-loop thread the lease release and
the prune both go to a daemon thread, so no session start is stalled behind a
file-lock wait. A finalizer the cyclic collector runs does none of that work on
the collecting thread: the collector fires on whatever thread allocated, possibly
inside a non-reentrant lock such as a `ThreadPoolExecutor.submit` lock the prune's
own path checks need, so the end is only queued (`SimpleQueue`, safe inside a
collection) for one daemon reaper thread started when the finalizer is armed. A
`gc.callbacks` hook records which thread is collecting. Pruning runs ONE bounded lease scan for the whole
run -- not one scan per candidate, which multiplied the candidate cap by the
scan cap into a quadratic sweep under the held publication lock -- streaming lease
records without materializing the directory and stopping at a fixed scan ceiling.
The scan covers TWO directories under one deadline: this home's own lease
directory and the legacy one in the agents directory, where an older build
sharing it still publishes; a lease held in either keeps the aliases it names,
and uncertainty in either makes the pass uncertain. The legacy directory is
scanned under the older build's own publication lock (the one it keeps in the
agents directory, when that file exists): an older publisher writes its lease
record and then its holder under that lock, so a scan without it could meet the
pair between the two writes and reclaim a live lease. A lock that cannot be taken
in the scan's budget is uncertainty.
That ceiling bounds how many entries are parsed and lock-probed, not how long a
slow filesystem takes over them, so the scan also reads a between-entry deadline
(`_PROJECTION_LEASE_SCAN_MAX_SECONDS`, 0.4 s, sized like the candidate walk's
budget because both share the two-second lock ceiling with the publication
writes). A spent deadline is handled exactly like the cap.
That single scan produces the set of aliases named by every HELD lease plus one
uncertainty bit; the candidate loop then consults the set with O(1) membership. A
capped, interrupted, or failed scan is uncertain, and any uncertainty authorizes
NO candidate deletion for the entire prune -- a candidate whose covering lease the
scan could not fully see must be treated as live. A stale lease is counted as
reclaimed only once neither its record nor its `.hold` remains, and a record is
kept while its `.hold` could not be removed so the pair is retried whole. A
`.hold` whose record is already gone names no alias but still consumes the entry
ceiling, so an unlocked one is reclaimed as residue; otherwise that litter could
keep every scan capped. That reclaim is limited to an EMPTY holder with the
writer's own `<pid>-<uuid4 hex>` stem: the writer publishes every holder empty and
never writes to it (the lock occupies no bytes), and the name alone is no proof
that this module wrote it. A file such as an operator's `notes.hold`, or any
non-empty holder whatever its name, is left in place (and still counts against
the ceiling). A cap, an unreadable
record and an open/iteration failure diverge only in what they do with residue
already validated earlier in that same pass: a failed open or iteration of the
directory trusts nothing it observed and discards its queued residue, while a
capped or expired scan, or one that met an unreadable record (the scan notes it
and keeps walking within its cap and deadline), first reclaims the
crash/finalizer residue it lock-validated -- each unlink is identity-checked on
its own pair, and otherwise a flood of records past the ceiling or one
persistent bad record would make the state absorbing, permanently deferring
cleanup of leases already proven stale -- and only then answers uncertain. The
capped, expired and unreadable-record cases are logged at INFO with the number of
leases reclaimed; the failed open or iteration is not. A held matching lease does not
short-circuit the scan: liveness is carried by the alias union, so the scan runs
to the end, and a clean completion reclaims the crash/finalizer residue queued
earlier in the pass EVEN when held leases exist. Residue is only ever unlinked
after the directory scan closes, never mid-iteration.
Within the bound, pruning reads each record without any lock and tests its `.hold`
with a non-blocking exclusive acquisition: a held lease adds every alias it names
to the live union, while an unlocked one is crash/finalizer residue and both files
are identity-checked and reclaimed. An unreadable, malformed, linked, replaced, or
otherwise uncertain lease marks the pass uncertain, so every candidate is kept;
the scan keeps walking and still drains the residue it validated. A cap or spent
deadline, and an unreadable record, are logged at INFO with the count reclaimed. A
failed open or iteration of the directory itself trusts nothing it observed: it
discards the queued residue and answers uncertain, with no INFO line. OS
lock release makes a crashed process's lease stale without trusting a PID. The
prune performs this scan once and tests a single alias by membership in the
returned live set; there is no separate single-alias predicate.

Alias publication and pruning share one cross-process lock file under this
home's `skill-projection/` directory (it serialises the publishers and pruners
of THIS home, the only writers of this home's views and side files), with a
two-second acquisition ceiling instead of the platform lock's general
five-minute ceiling. An older build sharing the agents directory takes its own
lock there and is not excluded by this one; what keeps the two from pruning each
other's live views is the dual-read lease scan above, the home tag, and the
legacy age floor -- the one window left is an old build reclaiming a new build's
sidecar-less-looking view after `_LEGACY_RECLAIM_MIN_AGE_SECS`, which the
re-preparation contract recovers (the next spawn or `set_mode` republishes it).
A lock file that is a symlink or junction,
changes identity while opened or acquired, or is otherwise unverifiable is
treated as lock failure. Removal revalidates the candidate's identity, bytes and
digest-bound ownership sidecar under that lock immediately before
unlinking it. POSIX uses descriptor-relative identity-checked deletion; Windows
uses the same global publisher lock plus a final no-link identity check before
its by-name unlink. An unknown platform without either contract retains the
unused alias. A changed, unreadable, oversized, or otherwise uncertain candidate
remains on disk. If the lock cannot be opened or acquired, preparation retains
every alias and the current settings file byte-for-byte, then falls back to the
authored native agent rather than risking a stale-snapshot overwrite or blocking
startup. Active, foreign-home, unmarked, malformed, unreadable, oversized or
otherwise uncertain alias files remain on disk.

`kirocrew doctor`'s Agents Directory section reports the census read-only,
through `census_projected_aliases` in the reclaim owner
(`acp/skill_projection_parts/alias_reclaim.py`), reached over the
`agent_sdk.drivers.acp` seam like the doctor's other backend reads, so the
record shapes stay the projection's; the lease record itself is parsed by the one
`_read_lease_record` the liveness probe also uses, and the data-home identity
the `foreign_home` split is judged against is resolved inside the census with
the publisher's own spelling, so no caller can hand it a differently normalised
id. It counts how many `kirocrew-skill-view-*.json` aliases the directory
holds, how many a lease record names, how many -- among the named and the
unnamed separately -- an ownership sidecar attributes to another Kiro Crew data
home (two homes share this directory whenever they share `~/.kiro`; a view with
no record in either directory is attributed by the home tag in its description,
the way the prune keeps it), and how
many lease records are unreadable; a record nested past the interpreter limit
reads as unreadable rather than aborting, for the probe and the census alike.
What the census retains is bounded (`_CENSUS_MAX_ALIASES`, the diagnostic's
own memory budget) and its lease walk has no separate ceiling: it charges every
directory entry, records and `.hold` sidecars alike, against the reclaim scan's
own `_PROJECTION_LEASE_SCAN_LIMIT`, so one constant decides where the census
stops and where every prune defers. A hit bound is reported as `truncated`: the measured counts are then floors, the
derived ones (not-named, this home's share) are not printed. An unscanned
record could be the unreadable one that makes a reclaim pass fail closed, so
the report says reclaimability is unknown rather than promising a drain. A
backlog left by a build that predates the reclaim is thereby visible without
`ls`, and its drain can be watched. Above
`_SKILL_VIEW_BACKLOG_WARN` (128, the same number the projection enforces as
`SKILL_VIEW_PROJECTION_CEILING`, so a host the doctor warns about is a host whose
next spawn has already fallen back to its authored agent; the number is sized
for the IDE's drop-down over the measured steady state -- 35 views for 34
authored agents on a heavily used host, since global agents share one view
across workspaces and only a project agent or a workspace with its own
inheritance setting mints more -- not for kiro-cli's start cost alone, which
the old 2,000 net was) it warns and says exactly which share the
reclaim covers: this home's unreferenced aliases, a bounded number per spawn;
this home's lease-named aliases are described as kept while their lease is
held (the census probes no lock, so a crash-stale record is indistinguishable
from a held one and is reclaimed on the next spawn's probe); aliases another
data home owns, leased or not, never drain here; and while a lease record is
unreadable nothing is reclaimed, which the report states instead of promising a
drain. The manual fallback -- moving the aliases out of the agents directory
and their metadata directory (named by its real path under the data home) out
of the way with the gateway stopped, or with every gateway that uses the
directory stopped once another home's aliases are present -- is named without being
performed and without suggesting a delete: the doctor
cannot prove who authored a file that merely carries the prefix, and a move is
undoable.

Windows runtime teardown records the reaped return code after the owned-handle
drain, before dropping the process reference, just as POSIX teardown does. The
existing death summary is amended without changing its reason or stderr tail.

## Backend Selection

`AcpSessionHandle.active_agent` records the mode named by session configuration,
a completed mode handshake or an observed agent-switch event. A queued mode
request clears that observation until confirmation. `AcpSessionProvider` exposes
`loaded_capability_template` only for a live dedicated Kiro runtime whose active
mode matches its launch template; shared handles provide no full-spec loading
claim. Member generation and MCP-readiness checks belong to
[session](session.md#member-capability-generations).

The `member_context` flag captures native instruction sources so essential
context delivery can deduplicate them; it does not change sandboxing or MCP
transport. Member calls use the ordinary authenticated session identity and the
gateway's canonical execution record. No member-specific ancestry proof or
filesystem-isolation capability is required.

The shared Kiro runtime carries its ordinary signed session token on eligible
unpooled `kirocrew-core` and `kirocrew-cron` stdio elements, on both `session/new`
and `session/load`. An empty broker stub list does not remove this identity
channel. Only existing, referenced managed declarations are projected; an
existing broker element wins. Disabled servers, native-only restrictions,
custom commands and registry-governed entries remain with Kiro's native loader
rather than losing their restrictions in the ACP array. A native entry without
another verifiable identity channel retains the strict caller refusal.

`memory_mode` is fixed before provider startup and before `session/new`, including
late-start adoption. Incognito and Temporary suppress Crew raw-frame recording
and payload diagnostics, do not resume retained native context, and cannot retain
native transcripts for continuation. Shared-runtime recording latches off before
a restricted session can emit its first frame. Normal shutdown and abandoned
late starts clean the native transcript files supported by the provider. This
does not add a sandbox or promise control over every external provider's own
on-disk session format or crash recovery.

`AcpClient(acp_backend=...)` stores the id without validating it; the id is
validated at config load (`_normalize_acp_backend`, harness-parity H3), and an
unknown id fails later at the harness or launch lookup. The launch record comes
from `agent_sdk/backends_launch.py`. Current ids are `""`
(kiro-cli), `"kas"`, `"claude"`, `"codex"`, `"opencode"`, `"pi"`, `"goose"`,
and `"deepseek"`; public-build selectability is a separate registry. The complete
launch and host-capability table is in
[agent-host-contract.md](agent-host-contract.md#column-meaning). Two launch paths
need additional detail here:

- `""` (Kiro CLI): `kiro-cli acp --agent <name>` (resolved by `_resolve_kiro_bin`). A stored Kiro CLI pick leaves this launch only once this process confirmed the host's kiro-cli offers its v3 engine (`confirm_kiro_cli_offers_kas`) and while KAS is selectable — the gateway's KAS floor at boot, or a non-gateway spawn entry's `ensure_kiro_cli_kas_verdict` before its first spawn; then config load reads it as `"kas"` and it takes the launch below, because kiro-cli now serves a bare `acp` on v3 and this launch's v2 projections (skill-view aliases, the `cli.json` overlay, re-filtered specs) are not read there ([harness-parity](harness-parity.md) H1). Per-session kiro settings are layered in via the workspace overlay `<work_dir>/.kiro/settings/cli.json` (written by `AcpProvider`, not the client): reasoning **effort** (`chat.modelDefaults`) and **MCP Tool Search** (`toolSearch.enabled` + activation thresholds from `agent.tool_search_min_pct` / `tool_search_min_tokens`, gated by `agent.tool_search`, default on) — see providers.md.
- `"claude"` (`ACP_BACKEND_CLAUDE`): `claude-agent-acp` (resolved by `_resolve_claude_acp_bin` → `(list[str] | None, str)` (argv plus the augmented PATH actually searched)). Resolution order: `CLAUDE_AGENT_ACP_BIN` env var, then a **project-local copy** (`_resolve_vendored_claude_acp` — `<node_modules>/@agentclientprotocol/claude-agent-acp/dist/index.js` found under the roots `_vendored_acp_roots` returns: the package's optional `_vendor/node_modules` copy and `$KIROCREW_PROJECT_DIR/node_modules`; needs no global npm install or network), then `mise which claude-agent-acp` (respects MISE_DATA_DIR and all mise config), then a direct glob under mise's Node installs dir (`_mise_node_installs_dir` — `<mise-data>/installs/node`, root from `env.mise_data_dir` so MISE_DATA_DIR / XDG_DATA_HOME are honoured), then augmented PATH (`env.augmented_path` — mise shims, `~/.npm-packages/bin`, `~/.volta/bin`, n's `~/n/bin` and `~/.n/bin` (every `env._NODE_MANAGER_DIRS` shim dir, where `npm i -g` under that manager lands), `/opt/homebrew/bin`, plus EVERY per-version manager bin dir via `env.node_all_bin_dirs` (mise/asdf/nvm/fnm, all installed versions — a global npm binary can live under any of them), so a non-login launchd/systemd gateway also finds globally-installed binaries). Nothing vendors the adapter into a build; `install.sh` provisions it with `npm install -g @agentclientprotocol/claude-agent-acp`. `_resolve_vendored_claude_acp` is the claude spelling of the ONE project-local check shared by the three Node adapter resolvers (`_vendored_adapter_entry`; codex-acp and pi-acp call it with their own entry and marker): a copy is accepted only when the adapter's dependency marker `@agentclientprotocol/sdk` is reachable the way Node resolves a bare import from the entry script's **real path** (`os.path.realpath`, then `node_modules` directories walked upward from there -- `_node_module_search_dirs`). An ordinary `npm install` satisfies that at the hoisted root; a `file:` / `npm link` install is a symlink whose dependencies sit under the link target's own `node_modules`, which the real-path walk reaches, so a linked adapter is accepted rather than falling through to the global copy on PATH. An entry whose dependency is reachable nowhere is still skipped in favour of the next candidate instead of being spawned and crashed, and the skip is logged once with the entry, the dependency and the real location, so the fall-through is never silent. For scripts under mise installs, returns `[node_binary, script_path]` to bypass `#!/usr/bin/env node` shebang resolution which fails in non-interactive daemon contexts. For standalone binaries, returns `[binary_path]`. Pre-spawn the client writes `<work_dir>/.claude/settings.local.json` with `defaultMode: default` so the adapter routes every tool decision back to Kiro Crew via `session/request_permission`. This makes claude-agent-acp participate in the same approve / trust_reads / trust / yolo protocol as kiro-cli — dashboard, subagents, channel agents, cron, and heartbeat all share the path. Kiro Crew still enforces per-tool security via `HooksConfig.auto_deny_tools` (evaluated by `HookManager.on_tool_call` in `hooks.py`) on every `session/request_permission` event. `CLAUDE_CONFIG_DIR` (an isolated config root, distinct from the project-scope `<work_dir>/.claude/settings.local.json` the client writes itself) is **not** set by this core: the launch tail (`launch.launch`) merges a caller's `extra_env` into the child environment, so an edition can point the adapter's `SettingsManager` and the SDK at a seeded root, but with nothing supplied they read the user's global `~/.claude` — which is a live gate-bypass hazard for inherited `permissions.allow` entries, recorded as a known gap in claude-code-provider.md. The env also carries `CLAUDE_CODE_EXECUTABLE` (claude backend only, set by the Claude adapter's `apply_spawn_env` when unset): the adapter delegates the model turn to `@anthropic-ai/claude-agent-sdk`, which needs a per-platform native Claude binary (~250 MB each) shipped as npm `optionalDependencies` that the website install omits — so the vendored closure does **not** include it and the SDK fails `session/new` with `Claude native binary not found for <platform>`. The SDK does **not** search PATH for `claude` itself (so the host merely having the external agent CLI installed is not enough), and bundling a quarter-GB binary per platform is not viable; instead `_resolve_claude_code_executable` finds an existing `claude` (`CLAUDE_CODE_EXECUTABLE` override → `mise which claude` → augmented PATH incl. `~/.toolbox/bin`, where a managed distribution may ship the external agent CLI) and the adapter forwards it to the SDK as `pathToClaudeCodeExecutable` (no version check). If none is found the var is left unset (with a warning) so the adapter's native-binary error surfaces rather than a guessed bad path; an explicit operator-set value always wins.

When the Claude adapter is not found, the spawn error reports the augmented PATH
captured by that resolution attempt. The failed result and its PATH are cached
together so a later environment change cannot make the diagnostic claim it
searched elsewhere.

**Kiro executable resolution at spawn.** Trust is "the CLI runs": any resolvable
executable Kiro CLI launches for ACP, regardless of install source, owner, or
fixed path — Kiro Crew is not the authority on where Kiro CLI is installed, and
Kiro CLI's own self-updater legitimately rewrites its bytes as the user, so an
install-source/owner/path/codesign gate would strand real installs (toolbox,
Homebrew, winget, a self-updated `/Applications` bundle) with no recovery path.
On Windows the fixed candidates include the native per-user install at
`%LOCALAPPDATA%\Kiro-Cli` before the machine-wide `Program Files\Kiro-Cli`
location. After the inherited `PATH`, discovery also checks the shared set of
standard user tool directories, preserving managed installations without
hardcoding package-manager-specific paths. Discovery therefore sees a CLI
installed after the desktop gateway started even though that process retains
its old `PATH`. When resolution fails, the spawn error names this same bounded
set of searched directories rather than claiming only the inherited `PATH` was
searched.
`snapshot_trusted_acp_executable` refuses only a non-runnable candidate and
returns the resolved path; `TrustedAcpExecutableSnapshot` carries only
`launch_path`.

**The CLI is always launched IN PLACE — never from a copy.** Kiro Crew execs the
binary at the path it resolved, on every platform. This is a hard requirement,
not a preference:

- **Kiro CLI 2.15+ is a multi-call binary.** It dispatches subcommands by
  exec'ing a SIBLING executable (e.g. `kiro-cli-chat`) that it locates relative
  to its own executable path — on macOS by finding `.app/Contents/MacOS/` in that
  path. Copying the binary into a flat private directory strands the sibling, so
  every dispatch fails with `No such file or directory (os error 2)` and ACP dies
  at the handshake with `process exited (rc=None)`.
- The same breaks any launcher that resolves adjacent resources: a multiplexer
  dispatching on `argv[0]` (`~/.toolbox/bin/kiro-cli` → `toolbox-exec`), a
  wrapper reading a sibling registry, or a self-updating install whose real
  payload lives beside it. The launch path is therefore the path the caller
  resolved, **not** its realpath. One exception, a pod child only: its remapped
  `$HOME` puts the sibling nowhere, so `apply_pod_bundle_spawn` resolves a
  symlinked `argv[0]` **onto a verified `<name>.app/Contents/MacOS/` target only** —
  same basename, executable, with a `<basename>-` sibling beside it. Verifying the
  whole layout, not just that the link resolves, is what keeps the exception off
  the `argv[0]`-dispatching multiplexer above and off any wrapper that finds its
  resources through the path it was invoked by. Crew's launcher still takes the
  sandbox, though not for the bundle swap's reason: no shim is in this chain, and
  delegating would skip Crew's seatbelt for an internal sandbox whose behaviour
  under the pod's remapped `$HOME` Crew cannot verify.

No copy-then-exec strategy (a sealed memfd, a verified private copy) is used for
the Kiro CLI. The threat such a copy would close is an attacker who already has
write access to the user's own machine, which the rest of the product does not
defend against either, and its cost is breaking every multi-call and multiplexer
install outright. The
spawn still passes an explicit `is_kiro_cli` classification to `wrap_argv`, so
macOS internal-sandbox delegation never depended on a private launch-path
basename, and Windows can grant its Kiro-only delegation without trusting a
filename heuristic. Resolution runs off the event loop (`asyncio.to_thread`, shielded so a
cancelled caller still lets the worker settle).

## Tool Permission Protocol

`session/request_permission` is the single inbound channel. The agent sends:

```jsonc
{ "method": "session/request_permission",
  "params": { "sessionId": "...", "options": [PermissionOption], "toolCall": ToolCallUpdate } }
```

**Unknown server→client requests are answered, never dropped.** `session/request_permission` and the elicitation request (`elicitation/create`, and on KAS `_kiro/mcp/elicitation`; see *MCP elicitation*) are the inbound *requests* `AcpClient` implements. `AcpSessionHandle` serves three more, all KAS-specific: `_kiro/hooks/list`, `_kiro/hooks/sessionStart` and `_kiro/hooks/executeHook`, answered from `acp/kas_wire.py` (see agent-host-contract.md, which states the four gates `executeHook` passes before anything spawns). `_kiro/hooks/executeHook` is routed there only while hooks are announced in the handshake (`kas_wire.hooks_announced()`); `KAS_CLIENT_CAPABILITIES` announces none today, so it is answered `-32601`. They are matched in that handle's own dispatch loop, ahead of the shared classifier, precisely so the classifier keeps reporting them as unknown on the `AcpClient` path — which serves no hooks surface, and where naming an action no branch handles would leave the request unanswered instead of refused. Any other server→client request (method **and** id — e.g. `fs/read_text_file`, `terminal/create`) is classified by `_process_message` as `"server_request_unknown"`. Every prompt dispatch site (`send_message_stream`, `_dispatch_events`, `_read_prompt_response`) handles that action by calling `_reject_unknown_server_request`, which replies with a JSON-RPC `-32601` (`JSONRPC_METHOD_NOT_FOUND`, "Method not found") error via `_send_error`. Without this, JSON-RPC semantics leave the agent blocked forever on an unanswered request — the turn hangs. Notifications (method, no id) are unaffected and still classified `"skip"`.

`PermissionOption` field names differ between backends — kiro-cli uses `id`/`label`, claude-agent-acp uses `optionId`/`name` (per the public ACP spec). `_build_permission_event` reads both and remembers the optionIds keyed by `kind` (`allow_once`/`allow_always`/`reject_once`/`reject_always`) on the request id — recording an entry when **either** an allow option (for `approve_tool`) **or** a reject option (for a clean `reject_tool`) was advertised. `approve_tool(request_id, *, always=False)` echoes the matching allow id back, so the host doesn't need to know whether it's talking to kiro (`"allow_once"`/`"allow_always"`) or claude-agent-acp (`"allow"`/`"allow_always"`). `reject_tool` prefers a **clean reject**: if a reject optionId was advertised it sends `outcome: "selected"` with that id. Both backends advertise one — claude-agent-acp as `{kind:"reject_once", optionId:"reject"}` (→ `behavior:"deny"`), kiro-cli as `{kind:"reject_once", optionId:"reject_once"}` — and the fallback to `outcome: "cancelled"` therefore only applies to a backend that advertises no reject option at all. The distinction is load-bearing, not cosmetic: a clean reject resolves the tool call to `status:"failed"` with kiro-cli's fixed content `"User denied tool execution"` and the turn continues to the next model-inference boundary (`stopReason: "end_turn"`), whereas `cancelled` ends the turn immediately with `stopReason: "refusal"` and no text — and drops any queued `_session/steer` as `AgentExecutionUserMessageCleared`. That is why the host's in-band deny notice (`_steer_policy_notice`) can only be folded in on the clean-reject path, and why `stopReason: "refusal"` is NOT by itself evidence of a model-side content refusal.

Both the shared runtime and the legacy direct `AcpClient` route permission
frames through `_dispatch.build_permission_event`, including the same provenance
flags. The builder accepts string, integer, and float request ids, with booleans
excluded. A float id (`5.0`) stays a float in the pending-request maps and is
echoed unchanged by either an approval or rejection. A boolean, list, dict, or
null id produces no event. A non-null invalid id is answered exactly once with
JSON-RPC `-32600` (`"invalid request id"`) and touches no pending-request map; a
null id remains a notification and is skipped without a reply or audit row.
String-valued `toolCall` fields (`title`, `kind`, and `toolCallId`) normalize a non-string value
to an empty string before redaction, cache lookup, or event construction. A
shell-cache hit whose value is `False` sets `shell_classified=True` — it is a
resolved non-shell call, not a cache miss — and a structured-params cache hit
sets `raw_params_trusted=True`.

Both transports use `_dispatch.select_tool_title`; the direct client imports
it as `_select_tool_title`. A confirmed shell call prefers `rawInput.description`,
then its command, then the SDK title. A sub-agent call identified by a non-blank
string `rawInput.subagent_type` also prefers its description (for example Claude's
`Task` tool). Every other non-shell or unclassified call keeps the SDK title; its
`description` can be a functional argument such as an issue body and is not a
label. Refinements use cached parameters and `is_shell` when fields are absent,
including an explicit cached `False`. This display rule does not alter tool
identity, permission checks, reserved purpose arguments, or the delivered tool input.

The shell cache is written **only** from a usable backend `kind` string. A
`tool_call` frame that omits `kind` writes nothing — even when its
`_meta.kiro.mcpServerName` proves the call MCP-served — because a cached `False`
reads back as a RESOLVED non-shell classification (`shell_classified=True`),
which flips `AcpEvent.child_low_fidelity` to `False` and would un-gate the
content-matching auto-approve paths (title-keyed `auto_approve_tools`) for a
call whose title is agent-authored and whose mutating/read nature nothing
verified. The transport signal instead feeds the identity-only lane: the
`_meta.kiro` identity caches (below) carry it to the permission event, where
`AcpEvent.child_mcp_identity_trusted` and the CLI consumer's
`_unverifiable_shell` escape consume it without ever minting a classification.
The same identity is what an identity-keyed grant matches for such a child —
the hook gate's `auto_approve_tools` pattern (matched against
`@server/tool` rendered from the identity, never the title, for an
MCP-identified call) and app-own-server grant, reported as
`ToolHookResult.identity_grant`, and the TrustDropdown's `approval_command`
key — so the user's narrow allowance covers the child's call to that tool
without a session-wide trust grant (`security.md` § Child-fidelity split).
A miss keeps reading as an absent classification. Classification reads the
whole frame through `_dispatch.classify_tool_call`, not the `kind` alone: a
kiro-cli frame reporting `kind: "execute"` caches `True` whatever its
`_meta.kiro` says — that identity never waives a shell check — while a
codex-acp frame carrying the adapter-authored `_meta.is_mcp_tool_call` marker
is an MCP call the adapter happened to build with its shell builder, so it
caches `False` and takes its trusted identity from the adapter-resolved
`rawInput.server`/`rawInput.tool` pair. A marker with an unreadable pair
resolves nothing (the shell cache stays unwritten), so the permission event
stays low-fidelity rather than earning a minted non-shell verdict.

One exception fills a shell-cache MISS from the permission request itself, and
only on KAS (`build_permission_event(kas_consent_meta=True)`, set by both
transports when the backend is KAS). A KAS sub-agent spawn's `tool_call` frame
(`_meta.kiro.kind: "agent-subtask"`) is taken by
`AcpSessionHandle._handle_kas_subagent` for the sub-agent roster and never
reaches the shared parser, and its toolCallId is synthetic
(`invoke_subagent_<id>`), so every cache misses and the request would reach
`_unverifiable_shell` unclassified and be refused. The request carries the
engine-written `_meta.kiro` block, and `_dispatch.kas_consent_tool` reads it:
when the toolId is `invoke_sub_agent`, `consent.capability` is `subagent`,
`consent.resource` names the target agent and no `command` field is present,
the miss resolves to a non-shell verdict (`shell_classified=True`,
`is_shell=False`), `tool_name` is set to the Crew name `use_subagent`, and
`spawn_target` carries the target. The deny floor, `auto_deny_tools` and the
`tools` ceiling are then asked about `use_subagent` rather than the title
alone, and `HookManager.on_tool_call` judges `spawn_target` against
`capabilities.spawn` (the gate on, the target in its `agents` scope: the two
questions `subagent._vet_spawn_governance` asks on Crew's own spawn path)
inside `_governance_denial`, on the same ceiling and profile it resolved for
the `tools` question, before any grant or prompt. That profile is resolved for
the calling agent, so a profile bound to the spawning agent's name applies, and
an evaluation error refuses the spawn. Because an auto-approved spawn raises no request at all,
the shared `governance.may_skip_gate` withholds `use_subagent` from every
`allowedTools` writer, and so from the `subagent` rule on the wire and on disk,
while the ceiling or any configured profile restricts `capabilities.spawn`
(governance.md § `allowedTools`). Any other
shape stays unclassified.

**Crew-answered built-ins on KAS.** `kas_agents.to_client_custom_agent` records the
shell/filesystem built-ins the spec grants (which the KAS policy keeps at `ask`) under
`kas_permissions.CREW_ANSWERED_BUILTINS_KEY` on the projected entry.
`_dispatch.attach_kas_custom_agents` drops every key in `CREW_ONLY_ENTRY_KEYS` from
the wire copy, so the engine never sees it. `KasHarness.record_session_projection`
stores the active agent's set on `AcpSessionHandle.kas_crew_answered`
(`kas_agents.projected_crew_answered`, exact id, no first-entry fallback), and a KAS
mode switch replaces it by wire id (`switched_crew_answered`; a mode the batch does
not carry answers empty). `session_handle_permissions._build_permission_event` copies
it onto `AcpEvent.spec_granted_builtins` for a request on the handle's own session
only; a child-routed request (`sub_session_id` set), a native KAS sub-agent's tool
call (its scoped id is in `_native_child_tool_call_ids`, recorded from the
`agentSubtaskId` frame, though it carries the parent's sessionId), a request naming
no tool call, and every other backend carry the empty set. The legacy per-session `AcpClient` sets nothing, so a KAS call there
still prompts. The gate's `spec-builtin-grant` row is what reads it (security.md
§ KAS spec built-in grant). It never sets `is_shell` True, never overrides a
cache hit, and grants no provenance: `raw_params_trusted` and
`mcp_identity_trusted` stay `False`, so a child spawn stays
`child_low_fidelity` and title-keyed auto-approve stays gated for it exactly
as for any other child request with no trusted params.

For a CHILD event, the identity lane only helps a consumer the handle actually
delivers to: the session handle fail-closes every low-fidelity child permission
request whose consumer never set `child_fidelity_aware`
(`child_low_fidelity_unaware_consumer`). The dashboard runner and `kirocrew
chat` both opt in — the CLI qualifies because its approval path runs no
content-matching auto-approve: hook gate, then the `_unverifiable_shell`
fail-close, then an interactive prompt that shows only non-model-authored
context (the cached command, the `_meta.kiro` identity, the target path). The
opt-in admits every low-fidelity child event, not just MCP-served ones, so the
CLI's own first check re-applies the boundary: a low-fidelity child event is
rejected (`child_unverified_context`) unless its identity is verified, consumed
as `not child_unconditional_grant_eligible` — the same hoisted expression the
other grant-path consumers use, never a re-spelling — the
trusted transport identity is the one context that survives an empty params
cache and can be shown to the human, while a child edit with no cached
parameters would prompt without a Path line, an undisclosed write.

The raw-params cache is read without consuming it, so a repeated permission frame
for the same `toolCallId` keeps the original tool-call arguments authoritative
instead of falling back to the permission frame's agent-authored inline input. A
genuine miss may carry inline data for display, but both provenance flags remain
false and consumers that need trusted arguments fail closed.

OpenCode can send a shell permission request before the `tool_call_update` that
contains its command. If the trusted cache identifies a shell call but has no
command, the prompt loop selects OpenCode explicitly before calling
`_defer_opencode_shell_permission` to wait at most two seconds for the next frame.
The method reorders only a same-session, same-call,
in-progress `tool_call_update` with a nonempty command and no output, so the
normal update handler fills the trusted cache before the permission is built.
An unrelated or missing frame leaves the permission unchanged; the shell gate
then denies it. The permission frame's inline `rawInput` never fills the cache.

The host always sends one-shot approvals (`always=False`, the default). Kiro Crew — not the agent — owns the trust scope (`slot._trust`, `slot._trust_reads`, `slot._trusted_patterns`, `safety_override`, `channel.trusted`, parent session `approval_policy`). Per-call `session/request_permission` is required so Kiro Crew's PreToolUse hooks (`auto_deny_tools`, sensitive-path checks, credential redaction) fire on every tool invocation. The `always=True` path is reserved for a future "skip Kiro Crew hooks for this exact tool" feature; no caller passes it today.

### Approval floor in `approve_tool` (`permission_floor.py`)

Both transports' `approve_tool` methods (`AcpClient`, `AcpSessionHandle`) run the
tool gate's security tiers before sending an `allow`: `_build_permission_event`
records each event by request id in `_permission_gate_events`, and `approve_tool`
pops it and calls `permission_floor.refusal_for(event)` off the loop. A SECURITY
deny (the denied-command floor, the sensitive-path read and write checks, the
unverifiable-shell refusal) or an id with no recorded event is answered with
`reject_tool` and a `rejected_transport_floor` SEL row; a gate that cannot be
built or consulted refuses (fail closed). Refusal reasons keep their original text
for the in-band result, while operator logs and SEL metadata use the shared
fail-closed redaction helper. Both methods return `True` only after they send an
allow answer and `False` when this floor sends a rejection instead.
Every production caller consumes that result before it records approved state or
activity. This transport consultation is uncounted and writes no
`governance_decision` row (`hooks.uncounted_gate`) because the consumer's
identity-bearing consultation owns the gate decision and its audit.
Governance (identity-scoped policy) stays with the caller identity: approval
consumers call `HookManager.on_tool_call` or `refusal_for(event,
session_key=..., agent=..., security_only=False)` before their approval tiers.
This includes `stream_and_collect` under `AUTO_APPROVE`, the `AcpClient` internal
auto-approve path, the evaluation runner, and the code-review pool. When an
automatic helper resolves a request, it calls the shared identity-aware
`refusal_for` body off the event loop. Provider adapters only forward an approval
from a consumer that has completed that consultation.

A caller may also pass `withdraw_if`, a predicate both `approve_tool` methods read
after the floor's thread hop and again under the stdin write lock right before the
allow frame is written. `AcpClient._send_response` takes it in its `before_write`
check, ahead of a timed grant's deadline. `AcpRuntime.send_response` takes it as
`withdrawal_check`, which runs ahead of the runtime's own liveness check
(`_refuse_write_if_dead`), while the deadline stays in `before_write`, which runs
after that check: a runtime that died during the lock wait raises `AcpRuntimeDead`,
so the caller takes the session-reset recovery instead of asking a person about an
ended grant. Once `withdraw_if` holds, no byte of the allow is written, the request's
records (`_permission_gate_events`, `_permission_options`, and the client's
gate-extension mapping) are put back, and `permission_floor.ApprovalWithdrawn` is
raised: the request is still pending, and the caller answers it once. Both
providers declare the keyword with the class attribute `approval_withdrawable`;
`tool_permission.AcpWire` passes a sub-agent's stop predicate (`Policy.stopping`)
only to a provider type that declares it.

The rendered tool-input cache is consumed by the first permission event, but
structured raw params remain keyed by `toolCallId` for the whole turn. A repeated
permission for the same call therefore retains the fact that a non-shell MCP tool
had arguments; it cannot be reclassified as an inputless canonical tool and match
session durable trust merely because the display cache was already consumed.

A remote (HTTP) MCP server's initial `tool_call` legitimately streams an empty or
absent `rawInput`, so the params cache stays empty and every child permission
request for such a tool is low-fidelity (`AcpEvent.child_low_fidelity`) on the
arguments half. The `_meta.kiro` identity caches are written unconditionally from
the same frame, so the permission event still carries the verified
`mcp_server_name`/`tool_name` pair plus the explicit `mcp_identity_trusted`
provenance flag (set only when BOTH cache reads hit — mirroring
`raw_params_trusted`, so an inline fallback can never count as verified);
`AcpEvent.child_mcp_identity_trusted` exposes
that verified-identity half (arguments unverified) and
`AcpEvent.child_unconditional_grant_eligible` hoists the grant-eligibility
expression for the unconditional grant paths documented in
`security.md` § Child-fidelity split.

The handshake also branches on the backend:

- `protocolVersion` in the `initialize` request: kiro-cli expects the date string `"2025-08-22"`; claude-agent-acp expects an integer (`1`, per the upstream ACP SDK schema).
- claude skips `session/set_mode` and uses `session/set_config_option` (configId `model`) instead of `session/set_model`.

Sending the wrong shape yields `-32602 Invalid params` or `-32601 Method not found`.

**`clientCapabilities` in the `initialize` request.** Both transports (`AcpClient._initialize_session` and `AcpRuntime`) send the shared `ACP_CLIENT_CAPABILITIES` dict from `acp/types.py`. Without the key the agent would assume the all-false default. `AcpClient` reads its dict from the per-backend table `client_native_children.CLIENT_CAPABILITIES_BY_BACKEND`; a backend with no row sends the shared dict unchanged, and the claude backend's row adds `subagents` (below).

**`agentInfo.version` from the `initialize` response.** Both transports retain it (`AcpClient.agent_version`, `AcpRuntime.agent_version`, surfaced through `AcpSessionHandle` → `AcpSessionProvider` → `AcpProvider.agent_version`; `""` until the handshake completes). It is the version the spawned process RUNS, which after an in-place kiro-cli upgrade differs from the binary on disk — the MCP hot-reload gate reads it for that reason. Parsed with the shared `agent_version_from_init` in `acp/_dispatch.py`; a missing or non-string value reads as unknown rather than failing the handshake.

| Key | Value | Why |
|---|---|---|
| `fs.readTextFile` / `fs.writeTextFile` | `false` | We serve no `fs/*` handler; advertising them would invite requests that hit `_reject_unknown_server_request`. |
| `terminal` | `false` | Same — the agent uses its own tools. |
| `subagents` | `{}` on the claude backend only (`CLAUDE_CLIENT_CAPABILITIES`) | claude-agent-acp then announces each subagent it starts and sends the child's frames under the child's own sessionId. `client_native_children` reads that form, so the declaration ships with its handler. Every other backend leaves it out: kiro-cli has its own roster, and an adapter that has not been measured gets nothing it might route around. JetBrains AIR's `_meta` opt-in is never sent. |
| `elicitation` | `{"form": {}, "url": {}}` | **Declared together with its handler, never ahead of it.** The ACP schema gates `elicitation/create` on this capability, so a standard harness forwards an MCP server's elicitation only when the client declares it; an advertised-but-unserved key would turn that ask into a `-32601` the harness reports as a cancelled tool call. Both drivers classify and answer the method (*MCP elicitation* below); `test_acp_client_capabilities.py` pins the declaration to that classification so the two cannot drift apart. The key does not move tool approvals: codex-acp (`@agentclientprotocol/codex-acp`, its `CodexElicitationHandler.ts` module: `createMcpElicitationContext`, `shouldUseAcpElicitation`, `canUsePermissionFallback`) recognises a tool approval as a message-only form and routes it through `session/request_permission` whatever the client declared; only a form WITH fields and a URL ask consult `clientCapabilities.elicitation.form` / `.url`, and the same gate decides whether codex's own `tool/requestUserInput` reaches the client at all. Measured live, not only read off the source: `test_codex_session_mcp_approval.py` drives the pinned codex-acp with this dict declared and the command approval arrives as `session/request_permission` (fixture `codex/command-approval-elicitation-declared-live.jsonl`); codex's own command approvals never pass through its elicitation handler (`handleCommandExecution` → `requestPermission`). Without the key codex answers a URL ask through `session/request_permission` (where a trust tier could approve it) and cancels a form with fields — with it, a URL ask on a consumer that cannot put it to a person (a channel, a cron) is declined, which is the intended answer: no trust tier answers an elicitation. opencode's ACP server routes tool permissions through `requestPermission` and never calls the SDK's `unstable_createElicitation`; pi's gated `bash` call is measured in `test_pi_real_adapter.py` under Crew's own handshake and arrives as `session/request_permission`; kiro-cli, KAS, claude-agent-acp and goose never send `elicitation/create`. Both modes are declared because both are rendered: a form from the requested schema, and a URL the person is sent to. |

**Request-id namespaces are independent.** Our outbound requests (prompt, initialize, set_model, ...) use `_next_req_id()`; the agent's inbound server→client requests (`session/request_permission`) carry their own id counter. The two collide on small integers, so `JsonRpcMessage.is_response_for(req_id)` requires both `id == req_id` **and** `method is None` — a response never has a `method`. Without the `method is None` guard, a permission request whose id equals the in-flight prompt's `req_id` was misclassified as that prompt's completion in `_process_message`, ending the turn early and leaving the tool's permission unanswered → the agent turn hangs on follow-up messages (the agent waits forever for a `session/request_permission` response that never comes).

This same method-aware discipline is enforced in `_wait_for_response()`. While it awaits a specific `req_id`, an inbound server→client **request** (method + id — e.g. a colliding `session/request_permission`) or a **foreign-id response** (id ≠ req_id, no method) must not be misread as the awaited response, must not be dropped, and must not be re-appended to `self._buffer` and `continue`-d. The last is the critical hazard: `_read_message()` pops `self._buffer` first, so re-buffering + looping immediately re-reads the same frame and **spins until the deadline** (the original bug — stuck `init`/`load`/`set_config_option` ending in `AcpTimeoutError`). Instead, non-matching survivable frames are collected into a **local `deferred` list** and re-injected at the **front** of `self._buffer` *in arrival order* once the matching response arrives (or on timeout/shutdown), so a later `_prompt_loop`/`_process_message` can still answer a deferred permission request. Notifications (method, no id) continue to go to `_mcp_notifications` for `_drain_notifications`.

### MCP elicitation (`acp/elicitation.py`)

An MCP server that needs the person to confirm or fill something in before a tool
call may go on sends its agent an `elicitation/create`. The agent cannot answer it;
the harness turns it around as a **server→client request** that Crew answers:

| Harness | Method | Shape |
|---|---|---|
| every ACP harness that honours `clientCapabilities.elicitation` | `elicitation/create` (`METHOD_ELICITATION_CREATE`) | the elicitation fields FLAT on `params` — `mode` (`form` or `url`), `message`, and `requestedSchema` or `elicitationId` + `url` — next to `sessionId` and an optional `toolCallId` |
| KAS (the engine, relayed by `kiro-cli acp --agent-engine v3`) | `_kiro/mcp/elicitation` (`METHOD_KAS_MCP_ELICITATION`) | the same fields NESTED under `params.elicitation`, next to `sessionId` and `toolCallId`; `mode` is filled in even when the MCP server omitted it |

Both expect the same answer: `{"action": "accept" | "decline" | "cancel"}` plus
`"content"` (the person's answers) for an accepted form. kiro-cli's own engine
(`--agent-engine v1`/`v2`) does not route an elicitation at all — it answers the MCP
server `-32601` itself — so on that harness nothing reaches Crew. The standard
`elicitation/complete` NOTIFICATION (a URL ask finished out of band) shares the
prefix and carries no id; the classifiers leave it to `skip`.

`acp/elicitation.py` owns the shape on both wires. `parse_elicitation_request`
reads either into one `ElicitationRequest` **fail-closed**: a shape this client
cannot render (a non-primitive property, an enum of non-strings, more than
`PROPERTIES_MAX` fields, a URL that is not http(s), no session, a numeric bound past the
float range, an offered choice longer than `STRING_VALUE_MAX_CHARS` — a choice is sent back
verbatim as the answer, so it is refused rather than cut) raises
`ElicitationParseError`. `validate_form_content` checks a person's answers against
the requested fields — type, `required`, `enum` membership, and `minItems` /
`maxItems` on a multi-select — and raises `ElicitationContentError` on a value the
schema refuses, so a server never receives content its own schema would reject.
`elicitation_response` builds the result; `content` rides only with `accept`.

**Every decision is SEL-audited** as a tool invocation named `ELICITATION_AUDIT_TOOL`
(`mcp_elicitation`), outcome the action sent, metadata the reason (`consumer` for a
person's answer, else the driver's own ground: unrenderable, no consumer, cancelled,
stranded; the dashboard runner tracks its exit path explicitly — `resolved`,
`window_expired`, `turn_cancelled`, `no_turn_budget`, `stopped` — because `wait_for`
cancels the future on timeout and `fut.done()` is true on every exit; a Stop's mark in
`slot._approval_stopped` is read and discarded by this path as the permission path
discards its own, since ACP request ids are reused and a stale mark would brand a later
person's permission answer as the host's) — both drivers (`_audit_elicitation_decision`) and the dashboard runner
(`_audit_runner_decline`), off-loop and before the wire write like
`_audit_handle_reject`, so the record lands whether or not the answer reaches the
engine.

**Three outcomes, none of which leaves the request unanswered.** Both drivers
(`client_elicitation.py`, `session_handle_elicitation.py`) take the request off the
`elicitation` action the two classifiers return (`classify_notification`,
`_process_message`) and:

1. a request that cannot be rendered is answered `decline` — never a JSON-RPC
   error, which the KAS engine reports to the MCP server as `cancel`;
2. a request on a driver whose consumer never set `accepts_elicitation` is answered
   `decline` before it would be yielded, so a consumer that predates
   `EVENT_ELICITATION_REQUEST` (a cron, a subagent, a channel bridge, the text-only
   prompt APIs) can never leave the tool call hanging on an event kind it does not
   read;
3. otherwise the request is recorded in `_pending_elicitations` and yielded as
   `EVENT_ELICITATION_REQUEST` (`AcpEvent.elicitation` carries
   `ElicitationRequest.as_dict()`; `request_id` and `tool_call_id` the ids). The
   consumer answers through `answer_elicitation(request_id, action, content)`,
   which validates form content first — a refused value leaves the ask pending for
   a corrected answer. Whatever is still pending at the per-turn reset, on
   `session/cancel` (`AcpClient.cancel_session`, `AcpSessionHandle.cancel`: the
   notification first, then the open request it leaves behind, the order the ACP
   spec gives for permission requests) and at teardown (`destroy`) is answered
   `cancel` (`cancel_pending_elicitations`): the engine holds the tool call open
   until then. `test_acp_elicitation.py` pins each exit on both drivers.
   The pre-turn drain in `session_handle_turn.py` treats a stranded elicitation
   request (one that arrived while no turn was reading, from an abandoned turn's
   still-running tool) like a stranded permission: answered `cancel`, never dropped
   with the frames around it.

The yield is parked like a permission (`_awaiting_permission` / `_parked_on_permission`):
the human's time is not a stall. `AcpProvider._to_llm_event` forwards the `elicitation`
field (pinned by `test_acp_provider.py`'s field-parity test); the replay corpus carries
one synthesized fixture per wire (`kas/mcp-elicitation.jsonl`, `codex/mcp-elicitation.jsonl`)
so a change to either parser shows up as an event diff.

**No trust tier answers an elicitation, and never will.** Trust, hooks and grants
answer Crew's OWN tool gate. An elicitation is the SERVER's statement that a person
must decide; a `yolo` session auto-accepting it would defeat the compliance gate the
server declared. With no person reachable the answer is `decline`.

The dashboard chat runner is the one consumer today (`chat_turn/elicitation_prompt.py`,
opted in at turn admission beside `child_fidelity_aware` and reset with it): it appends an
`elicitation` row (transient, like `permission`), registers the request in the slot's
approval registry so the Board's Blocked lane, Continue's 409 and a Stop treat it as the
open question it is, and waits the same window as a permission. `POST
/api/chat/slots/{slot}/elicitation` (`dashboard/handlers/chat_elicitation.py`) answers
it — the dashboard owner's browser only; an internal-secret or app caller is refused
403, because an agent answering is exactly the automation the server asked to exclude,
and so is a non-owner dashboard session (a Telegram, Teams or Webex allowlist link with
the same empty app claim), through `require_owner_dashboard_request` (403 `owner_only`),
because an answer is the owner's consent to the server's operation.
The route validates a form's content against the row's fields BEFORE resolving the
future (a refused value is a 400 the card shows inline, the ask stays open) and writes
the validated answer onto the row, which the runner reads back and sends; its body cap
(`ELICITATION_BODY_MAX_BYTES`) is sized from `PROPERTIES_MAX` × `STRING_VALUE_MAX_CHARS`,
so every form the card can draw is answerable. Every other
resolver of that registry speaks the permission vocabulary, and `read_elicitation_answer`
lets none of its words mean `accept`: a reject button declines, and an approval CANCELS,
URL ask or form, because it is not a person answering this question. A Stop rejects every
pending future on the slot, but the runner reads its `_approval_stopped` mark and sends
`cancel` too: ending the turn is nobody refusing the server.
The app token's `/api/approvals/{id}/{action}` route leaves an elicitation's future out of
its candidates (the id reads as not found, like every other refusal there); the tool approve
route refuses an elicitation's id outright (409
`elicitation_not_an_approval`) — checked on the RESOLVED `(owner, request_id)` after the
owner scan, so a shared slot's open question is refused too — its bare-approve fallback
(no `request_id`, resolve the slot's sole pending future) leaves elicitation futures out
of the pick, and the trust/yolo sweep skips those futures, so neither an app holding the
session-approval grant nor a mode switch can stand in for the person. The elicitation
route itself binds the answer to the row the person READ: the card posts the row's
`request_mid` beside `request_id`, and the handler resolves only when
`slot.approval_instance(request_id, row)` equals it — an ACP request id is
connection-scoped and a backend may reuse one once the earlier ask expired, so a delayed
POST from the old card is a 404, never an `accept` for a question nobody saw.
What the person reads is server-authored and takes display redaction: the message, the
URL, field titles and descriptions, option labels. The server's opaque URL-ask token
(`elicitation_id`) is dropped before the row is appended: the card never reads it, the
driver answers by request id, and it is the one server string no redaction would touch. A string
field's `format` is kept only when it is one the MCP schema names (`STRING_FORMATS`: `email`,
`uri`, `date`, `date-time`) and dropped otherwise, so that unredacted type hint cannot carry
free text. A value the answer is validated
against or keyed by (a field `name`, an option value, a default) cannot be rewritten
and still be sent back, so a request whose redaction would change one of those is
declined, as is one whose URL it would change. The runner stamps the row it appended
(not the oldest row carrying the request id, which a backend may reuse) and the
`elicitation_resolved` frame carries that row's `mid` and, for an accept, the
display-redacted answer, so the live card's summary fills in without a transcript reload; the frontend resolves by that
`mid` alone and never falls back to another row. The row keeps its stored `mid`
through display redaction (`chat_utils_display.py`) for the same reason. An
`elicitation` row is transient like `permission` (`_TRANSIENT_ROLES`, which the
persistence builder `_build_message_entry_uncached` reads directly, so the row is never
written to the transcript) but, like it, is carved out of `_UNOWED_WINDOW_ROLES` and counted by `_is_answered_permission`:
a bounded transcript read hands the open question back, and an answered one reads
as answered; the dashboard's refresh merge re-seats the client's own copy of an answered
card (keyed by its `mid`) into a refreshed page that omits it, as it does a permission
card, and retires a still-PENDING local card the page no longer lists as `stale` (the
gateway keeps an open ask in its tail, so absence means answered or expired while this
tab was away), and the unread-on-attention predicates count a pending card as the wait it is. The runner also stamps WHO is asking — `mcp_server` and `tool_name` read from the turn's
trusted identity for the ask's `tool_call_id` (`_seen_tool_identity`, the same `_meta.kiro`
pair a permission row carries), never from the server's own text — and the card names
them in its title ("probe · delete_file is asking you") and shows the link's host before a
URL, because a person sent off the app rightly refuses a link whose sender it cannot name.
The frontend card (`pages/chat/ElicitationCard.tsx`) is drawn by the shared
`createTranscriptRenderers` entry, which hands it the surface's slot (the SDK default
registry draws it read-only with no slot, and a peer's crew window keeps that default
because the ask lives on the peer, not behind this hub's slot route); it draws the schema's primitive fields, or the
URL, on the `bg-card ring-1 ring-border` notice surface — an unset enum is handed a value
no option carries so `''` stays a real choice, a fraction in an integer field and an
integer past the safe range are each refused before submission with their own hint rather
than sent changed, and every field input carries a per-instance `useId` so two cards on one
page cannot share a label target;
once resolved it retitles ("A tool asked you") and shows the answer that was sent;
a refused answer renders through the shared `ErrorNotice`, a required-field hint as
status text.

## Agent Configuration

Data-driven — no code changes needed:
- `config/defaults.json` — base config (tools, model, permissions), resolved via `_BUNDLED_CFG_DIR` in `agent.py`
- `config/prompt.md` — system prompt, resolved via `_BUNDLED_CFG_DIR` in `agent.py`
- `~/.kiro/crew/agent.json` — user overrides (optional)
- Run `kirocrew setup --agent-only` after editing

There is no checked-in `agents/` directory. `agent_parts/spec_build.py` reads
`<KIROCREW_PROJECT_DIR>/agents/defaults.json` and `prompt.md` only when that variable
points at a project holding them; otherwise it uses the bundled `src/kiro_crew/config/`.

The shipped prompt is a self-contained input; custom/app agents can supply
their own prompts. Its compact tool directory retain exact callable syntax, session ownership,
privacy boundaries, stop conditions and output formats. Shared wording is not
moved into a common include: source deduplication alone would not reduce the
selected prompt sent to the model. `test/test_prompt_compact_contract.py` checks
each file's UTF-8 size budget, template slots and critical operational clauses. The size budget is an
absolute UTF-8 byte ceiling per selectable prompt: a context-cost guard, not a
token count and not a permanent ban on new rules. A maintainer may raise a
ceiling in a reviewed change when a rule earns its bytes; the clause tests, not
the ceiling, decide whether a contract survived. Prompt tests guard text
contracts, not a guarantee that a model follows them; runtime controls remain
authoritative. The normal prompt's browser instructions keep approval groups,
borrowed-browser ownership and subagent session isolation inline rather than
relying on a skill pointer for those controls.

Default model: `auto`. Default tools: `execute_bash`, `fs_read`, `fs_write`, `code`, `grep`, `glob`, `web_fetch`, `web_search`, `introspect`, `session`, `report`, `tool_search`, `@kirocrew-cron`, `@kirocrew-core`, `@kirocrew-computer`, `@kirocrew-guide`.

**Agent compatibility repair** (`agent_parts/spec_upgrades.py`): `repair_agent_configs()` is the single
entry point. It runs as the last step of every `rebuild_agent_config()`, which is
event-driven (setup and install, a provider or model change, the MCP and connection
handlers, a governance ceiling refresh); there is no timer. Its
`_sanitize_agent_hooks()` pass repairs only the exact host-managed filenames in
`agent_files.OWNED_KIRO_AGENT_FILES`. Kiro-cli rejects the legacy
`auto_approve_tools` variant in an agent spec's `hooks` field, causing silent
fallback to the default agent which loses the internal MCP servers. The repair
therefore removes that one Kiro Crew-authored legacy key and preserves every
unknown key; an unfamiliar key may belong to a newer kiro-cli schema or to the
user. Foreign specs, prefix lookalikes such as `kirocrew-custom.json`, and app
materialized specs are never scanned or rewritten. Mtime-based caching skips
unchanged owned files. Bundled `auto_approve_tools` patterns are applied at
runtime in the hooks layer (`_BUNDLED_AUTO_APPROVE_TOOLS` in `hooks.py`) rather
than being serialized to the config file. `_kiro_hooks_only()` remains the
strict filter for newly generated Kiro Crew specs, where Kiro Crew owns the whole
output schema.

**A checkout may not claim an agent name Kiro Crew owns** (`acp/managed_agent_shadow.py`).
kiro-cli loads `<cwd>/.kiro/agents`, nested and symlinked directories included, ahead of
`~/.kiro/agents`, and both kiro spawn paths (`AcpClient._spawn` and
`KiroHarness.resolve_spawn`) pass only a name. So a checkout that claims a name in
`agent_files.OWNED_KIRO_AGENT_FILES` would run its own file instead of the installed
spec. A file claims a name by declaring it, in JSON or Markdown and under any
filename, or by having it as its filename stem. Names are compared
case-insensitively. That file's `allowedTools` would skip the approval gate, and
any restriction the installed spec carries would be lost. Skill-view alias names
(`kirocrew-skill-view-*`) are reserved the same way, because with native skill
projection on, the name a spawn finally hands to `--agent` is such an alias. Both
paths refuse the spawn, naming the project file, when the checkout claims ANY
reserved name, whichever agent the spawn names: kiro-cli loads every project agent
at startup, and a shared runtime can later switch to one with `session/set_mode`.
The scan lists the directory itself, because the discovery roster leaves aliases
out. It follows a linked directory once, keyed by device and inode, so a link loop
ends. Every link, the agents directory's own path included, passes
`hooks.validate_file_path` before anything follows it, so a link to an untrusted
Windows share is refused without the outbound probe. It skips a protected
checkout, but refuses a cwd the sensitive-path resolver could not check (a stall is
not a confirmed match), and it keeps an entry that cannot be stat'ed
rather than letting it hide the others. A file whose bytes cannot be read (a
hardlinked spec, which kiro-cli loads although the hardened reader refuses it, a
link to a protected target, a file past the size cap) refuses the spawn unless its
filename already claims a reserved name, because a filename is not evidence of what
the file declares. A file whose bytes were read but do not parse as a spec object
claims nothing: broken JSON, a non-UTF-8 byte, a Markdown file with no
frontmatter, a document nested past the parser's depth. kiro-cli's default engine,
which both paths run, offers no mode for any of them; its v3 engine would load some,
so a move to that engine must revisit this rule. An AppleDouble sidecar claims
nothing too. It is recognised by its magic bytes, not its `._` name, because
kiro-cli loads a `._` file that parses, so any other `._` file refuses. A
tree past the file or directory cap is refused, and so is a link that leads to an
untrusted share or a protected location, without following it.
A cwd whose
`.kiro/agents` is the installed directory itself, the home directory for example,
is not a shadow. The fork gate refuses a shadowed fork the same way. `kirocrew-worker` and `kirocrew-heartbeat`
are reserved here too: the derived-spec gate refuses a project copy of either on
that agent's own spawn (`_DERIVED_AGENTS`; the heartbeat spec is checked by
`_require_fresh_heartbeat_spec`), but it scans the top level only and matches the
name exactly. A project spec
under any other name is still the documented discovery feature.

App command bindings scan no checkout: an app-owned turn runs the app's registered
file on both backends that allow it (see app-kit-platform). `agents_dir_competing_claim`
checks that the registered file is the only user-level spec declaring the agent, with
the same screened walk and strict reader, excluding only the app's own registered
file by its entry path. It refuses unreadable specs (including hardlinks),
unlistable directories, rejected links and scan limits rather than dropping them as
discovery does. A confirmed competitor gets the user-level ambiguity message.

**Which start paths reach an admission gate.** Two gates decide whether a session
may start on an agent spec, `agent.require_fork_governance` and
`agent.require_fresh_derived_spec`, and their reach is disjoint.
`test/test_agent_admission_paths_ratchet.py` pins several things: every function
that references either gate or the refusal above; the gate each function does not
reach, and whether that gap is covered elsewhere; and every function that writes
`--agent` into a child's argv, together with the admission sites that govern it. A
governed `--agent` site must count a site that refuses a shadowed managed name among
its governors. A new start path that references a gate, or that names its spec with
`--agent`, fails that test until it is classified there. A spawn through a harness
that selects its spec some other way and references neither gate creates no site,
so that test does not see it. Mode activation on a shared runtime is the recorded
gap for fork governance: it runs no fork-governance check for the agent it switches
to.

## Custom Agent Support

Custom-agent support is backend-specific. Kiro CLI consumes the selected agent
through `--agent`; KAS projects it on the wire; foreign hosts translate, withhold,
or decline fields through their provider-mirror contracts. The authoritative
per-backend matrix is [agent-host-contract.md](agent-host-contract.md#1-agent-definition-and-layout).
The details below describe the Kiro and Claude paths implemented directly here:

- **Model (kiro-cli)**: `set_model` is skipped for custom agents — kiro-cli uses the
  agent's own `model` field. Only the default kirocrew agent gets Kiro Crew's
  configured model override.
- **MCP servers**: backend-dependent.
  - **kiro-cli**: kiro-cli loads ordinary servers from the agent config. The
    shared runtime adds eligible managed control-plane elements carrying the
    per-session token, plus configured broker stubs, to `mcpServers`; it does not
    project third-party declarations. Override eligibility reads project and
    global MCP settings through the bounded sensitive-path reader. A refused,
    unreadable or malformed settings file withholds these overrides, preserving
    native restrictions; only an absent settings file, or one that is empty or
    holds only JSON whitespace (space, tab, CR, LF), with or without a single
    leading UTF-8 byte-order mark, contributes no restrictions. A document behind
    that same BOM is parsed normally. A UTF-16 or UTF-32 mark, or bytes that are
    not UTF-8, are refused before parsing (kiro-cli cannot read such a file
    either). Each refusal carries a fixed reason
    (`acp/session_mcp_settings_read.py`, re-exported by `session_mcp`) that the
    WARNING and the user-facing message repeat; the reason never quotes the
    file's content. Off the event loop a read may pause for a
    retry, so `session_mcp.native_settings_sources` brackets each pass with every
    file's identity and keeps a pass only when no file was replaced or rewritten
    while it ran; after `_NATIVE_SOURCES_BRACKETED_PASSES` (4) voided passes the
    set is treated as unreadable, the same as a refused read. When the settings
    are unreadable every identity element is withheld and a WARNING names the
    agent: its unpooled Kiro Crew servers mounted from the spec then refuse calls
    with `identity_unattested`, while pooled broker stubs keep their token.
    kiro-cli loads
    servers from the agent config (respects `mcpServers` in the agent's config
    file). Non-kirocrew agents (e.g. AIM-installed) load only their own
    `mcpServers`. The kirocrew agent loads from global `~/.kiro/settings/mcp.json`
    where `disabled` and `disabledTools` flags are respected. Kiro Crew's dashboard
    MCP tab writes directly to the global config. Loading is not one-shot:
    kiro-cli 2.10.0+ watches the agent file and reconciles a RUNNING session
    against an edit (only the changed servers restart, conversation kept, applied
    at the next turn boundary), which is why the dashboard's MCP sync skips its
    session reset on that harness — gate and semantics in
    [mcp.md](../../architecture/mcp.md#live-reconcile-when-no-restart-is-needed-at-all).
  - **claude-agent-acp**: does NOT read any config file or `--agent` flag, so
    `session/new` (and `session/load`) must carry the servers in the
    `mcpServers` param. `_session_mcp_servers()` — gated on
    `backend in ACP_BACKENDS_SESSION_MCP_ARRAY` (`acp_backends.py`) rather than on
    the harness's identity, so the next adapter that reads no agent spec joins the
    set instead of adding a branch — delegates to
    `acp/session_mcp.py:session_mcp_servers`, which reads the SAME
    materialized kiro agent spec (there is no CC-shaped second registry to keep in
    sync) and reshapes it to the ACP array (stdio →
    `{name,command,args,env:[{name,value}],type:"stdio"}`; url →
    `{name,type:"http"|"sse",url,headers:[{name,value}]}` — `env`/`headers` are
    required arrays, emitted even when empty, and the transport `type` is always
    explicit). The spec's `tools` references gate what mounts, so an entry kiro-cli
    declares but does not mount stays unmounted here too; `type:"registry"`
    catalog pointers are withheld; `timeout`, `disabledTools` and `autoApprove` are
    kiro-only and dropped (`autoApprove` deliberately — its CC equivalent would
    stop the call reaching Crew's gate). kirocrew-core/cron are re-derived from
    `agent.managed_mcp_spec_entry`, overriding a stale spec entry, and are present
    even when no spec exists. Read per spawn so MCP installs/toggles apply on the
    next session without a gateway restart.
- **Tools/allowedTools/toolsSettings**: Applied by kiro-cli via `set_mode`.
- **Prompt/resources/hooks**: Applied by kiro-cli via `set_mode`.
- **Denied commands**: Enforced at Kiro Crew's `hooks.py` PreToolUse gate;
  see [security](security.md).

Kiro CLI custom agents use a cold start with `--agent <name>` at spawn time.
Other backends follow the selection and projection contracts in
[agent-host-contract.md](agent-host-contract.md#1-agent-definition-and-layout).

## Protocol Flow

`initialize` → `session/load` or `session/new` → `set_mode` (conditional) → `set_model` (conditional) → drain notifications → `session/prompt`

A provider error is classified as `AcpConversationBindingMismatch` only when one
raw `message` or `data` field contains both the invalid preserved-thinking
signature clause and the statement that the block is bound to another
conversation. Splitting the clauses across fields does not qualify, and a merely
invalid signature remains a terminal `AcpError`. The mismatch is non-transient:
retrying the same native session resends the same rejected prefix. The dashboard
may replace that native conversation once, rebuild from visible history, and
retry only before any output or tool activity; attached sub-agents, Stop, a newer
message, a steer, or a session rebind refuse the destructive recovery.

`ensure_ready()` creates `_work_dir` once per instance (off-loop `mkdir -p`,
remembered via a flag) so the per-prompt warm path pays no filesystem syscall;
`_spawn()` re-creates it (also off-loop) on every spawn, and `_reset_state()`
clears the process and session id together, so every session-init path re-enters
`_spawn` first. A per-prompt re-check could not repair external deletion for a
live child anyway: kiro-cli's spawned shell inherits the client's cwd by inode,
not by path, so re-creating the directory does not restore it.

Steps 1–2 (`initialize`, `session/load` or `session/new`) block until a JSON-RPC
response arrives (base 240s) because the session ID is required before proceeding.
If the first attempt times out, `ensure_ready()` kills the process and retries once
with a fresh spawn — this handles slow kiro-cli first launches where MCP servers are
still initializing.  `_wait_for_response()` checks `shutdown_event` each iteration
so init aborts promptly on Ctrl+C instead of blocking for the full timeout.

**Activity-based deadline.** `_wait_for_response()`'s deadline is *not* a fixed
wall-clock. Every received frame (notification, deferred server request, or
foreign response) resets the deadline to `now + timeout`, bounded by an absolute
`_WAIT_RESPONSE_MAX_TIMEOUT` (600s) safety cap. This matters for `session/load`:
the adapter streams the ENTIRE prior transcript as `session/update`
**notifications** before resolving the load response, so a fixed deadline would
kill a long replay and silently fall back to `session/new`. Extending only while
the agent is actively sending data is safe for the init/handshake callers — the
hard cap still bounds a truly stuck handshake.

### Prompt timeout ownership

The prompt transport is the outer budget shared by dashboard turns, subagents,
and review callers. `resolve_prompt_timeout()` never returns less than the 4-hour
historical floor. Above that floor it follows the larger of the configured
chat-turn ceiling and `agent.subagent_timeout_secs`, adding 60 seconds so the
owning outer layer reports its specific timeout before ACP can return a raw
transport failure. At or below the floor it returns the floor unchanged, with no
margin.

A subagent run resolves one explicit transport budget when execution begins:
the larger of the live configured transport budget and its already-captured
manager deadline plus the 60-second outer margin. Every original, transient,
model-fallback, stop-recovery, and dependency-recovery prompt in that run reuses
the same value through either `AcpSessionProvider` or `AcpProvider`/`AcpClient`.
Those providers declare `prompt_timeout_for_deadline`; the generic agent-SDK
resolver returns `None` for an undeclared legacy provider, and the run preserves
its exact one-argument `stream(message)` call rather than speculatively sending a
keyword or retrying after `TypeError`. A config reload can change future runs but
cannot move ACP inside an in-flight run's immutable manager deadline. The
ordinary `timeout=None` provider path continues to resolve live config, so
dashboard turns and non-subagent callers retain their existing behavior.

### Session Resume via `session/load`

When `set_resume_session_id(sid)` is called before `ensure_ready()`, the client
attempts `session/load` instead of `session/new`:

1. Check `agentCapabilities.loadSession` from `initialize` response
2. Verify `~/.kiro/sessions/cli/{sid}.json` exists on disk
3. Send `session/load` with `sessionId`, `cwd`, `mcpServers` (the pooled
   broker stubs, re-declared so the resumed session keeps talking to the
   shared gateway — `session/load` re-initializes the session's MCP servers,
   so an empty list would un-pool the session), plus eligible unpooled managed
   elements carrying their session token, and
   `_meta: {"_kiro.dev/session_file": "<path>"}` (required —
   without it kiro-cli silently ignores the request). `AcpRuntime.load_session`
   builds the same params for the multiplexed runtime.
4. On success (response contains `modes`): set `_session_id`, `_resumed = True`
5. On failure (JSON-RPC error, timeout, file missing): fall through to `session/new`

The resume ID is consumed on attempt (no retry loop). After successful load,
`client.resumed` returns `True` — callers use this to skip thread history injection.

**A KAS resume must prove history came back.** KAS has no transcript Crew can
stat, and it answers `session/load` for an id whose stored record is gone by
creating a new, empty record under that id, with a normal response that carries
`modes`. Taken as a resume, the turn gets the short `[SESSION RESUMED ...]` block
and no history, and the agent denies its own earlier work. So the provider's
resume on KAS passes `require_replay=True` to `AcpRuntime.load_session`. While
that load is in flight, the reader loop counts the `session/update` frames for
its id that carry `_meta.kiro.replay: true`, which KAS puts on every turn it
replays (`test/fixtures/acp_frames/kas/mcp-live-apply-live.jsonl`). A load with
none evicts the session (`evict_session`, which keeps the stored record, so a
wrong refusal cannot delete a real conversation) and raises, and
the provider then takes the fresh-session path with `_history_replay_needed`, the
same one a failed load takes. The MCP re-injection load and every other backend
pass `False`.

`AcpRuntime.load_session` has a second caller on KAS: applying a dashboard MCP
config change to a LIVE session (`acp/session_provider_mcp_reinject.py`,
membership `ACP_BACKENDS_MCP_WIRE_REINJECT`). The engine registers a session's
`_meta.kiro.customAgents` batch once and ignores a re-sent `set_mode` or a
`session/load` of a session it still holds, so the provider first sends
`AcpRuntime.evict_session` -- the standard `session/close` as a bounded request
plus the local unregister, which frees the resident copy and KEEPS the stored
record (KAS's teardown verb, `_kiro/session/delete`, removes it) -- and then
`load_session` on the same id with no transcript path, inside
`AcpRuntime.keep_record_on_teardown(sid)`: the load's failure arms end the
session through `terminate_session`, whose KAS verb deletes the stored record,
and inside that scope the call evicts instead. The engine replays the
conversation, the load re-projects the batch from the spec as it now is, and the
returned handle replaces the provider's. Measured:
`test/fixtures/acp_frames/kas/mcp-live-apply-live.jsonl`,
`mcp-live-remove-live.jsonl`. The decision and the mark are the dashboard's
(`docs/architecture/mcp.md`, "Live reconcile"); the apply runs at the session's
next turn boundary under the shared-turn claim, never mid-turn.

Step 3 (`set_mode`) is **conditional**: sent for all kiro-cli backend agents.
Skipped for claude-agent-acp, whose `session/set_mode` takes permission modes
(`default`, `acceptEdits`, `plan`, ...) rather than agent names. The claude
session that leaves a project-owned settings file out uses that verb to pin
`default` (see claude-code-provider.md).
Two of the guards in front of it, on `session/new` and `session/load` alike,
end the session rather than let it run as a different agent. Guard (A): when
the response advertises a `modes` list, the requested agent must be in it
(`AcpRuntime._mode_available`); an absent id is refused naming the spec file
and `kirocrew setup --agent-only`, never silently left on the host's default
mode. A published `<agent>--readonly` side spec is the exception: its refusal
explains the derived spec and names the base agent
(`side_readonly_spec.unavailable_mode_explanation`; see [side.md](side.md)). Guard (C): the shared runtime asks its harness
`activation_refusal(agent, resp)` (harness-parity H13: a seam, not a backend
test); the spawn-time hosts answer `None`, and the KAS harness reads the
advertised entry's `_meta.kiro.resource.source.origin`
(`_dispatch.advertised_mode_origin`). The value `bundled` -- the one stamp
MEASURED on kiro-cli 2.23.0 for the engine's own agents -- means the engine kept
its OWN built-in under that id and discarded the `customAgents` definition; a
`set_mode` would succeed and run the built-in under the mate's name, so the
harness answers the refusal text -- the id is reserved for a built-in agent,
plus the mate-side remedy in the dashboard's own labels (the Agent Template
tab; on the mate's own copy 'Save as new template…' under another name or
'Reset my changes'; on a shared template -- one created under a reserved id
before the refusal existed, where neither control renders -- the template
picker at the top of the tab), in plain words with no wire vocabulary or
session id, the id in curly quotes as the dialogs render it. Only that measured stamp refuses: an absent stamp (kiro-cli,
the offline fake, an older engine) is not evidence and never refuses, and a
positive stamp this code has never measured (an engine that renames `client`)
is logged at WARNING and let through, because kiro-cli is the operator's own
install, not pinned by Crew, and an unmeasured value must not become a
session-start denial of every mate with a rename remedy that cannot help.
The ids measured to trip (C) on kiro-cli 2.23.0
(`agent_files.KAS_RESERVED_AGENT_IDS`) never reach the wire under their own name:
`attach_kas_custom_agents` sends the batch entry as
`agent_files.kas_wire_agent_id(id)` (`<id>-crewmate`), and both session-start
paths read the advertised modes, ask (C) and send `set_mode` with that same wire
id, so a `default` mate carried over from kiro-cli runs unchanged. Nothing
else sees the alias: the batch Crew records, `handle.active_agent` and
`kas_projected_agent` keep the mate's id, and a `current_mode_update` naming
the alias is mapped back through the registered batch
(`_dispatch.kas_crew_agent_id`). (C) is the read of the wire itself, so a
built-in a later engine adds under a new id fails loudly instead of resurrecting
the silent substitution. (Guard (B) is the spawn-flag check for a markdown-only spec on a
JSON-only host, `_spawn_agent_not_loaded_reason`.)

Step 4 (`set_model`) is **conditional**: only sent when `model` is explicitly
set (i.e., for the default kirocrew agent).  Custom agents skip this so
kiro-cli uses the model from their own agent config file.

**Advertised models and read-path revalidation.** Each `AcpSessionHandle` keeps
the `availableModels` its own `session/new` answered as `available_models()`.
That answer is captured once, and a lookup racing a token refresh can return the
free tier. Two paths heal it through the runtime's `probe_advertised_models`,
which opens a throwaway minimal session (no MCP servers, no mode) on the same
process and ends it before returning: `refresh_available_models` before an
explicit `set_model` pick is refused, and `maybe_refresh_available_models(catalog_ids)`
on the model-picker read path. The read-path call probes only when the snapshot
would drop a catalog row by `catalog_row_would_drop` (the dashboard filter's own
per-row verdict, skipping the case where that filter fails open to the full
catalog) and the snapshot is suspect -- never probe-confirmed, captured within
`_READ_PATH_SPAWN_RACE_SECS` of spawn, or `auto`-only -- and a confirmed snapshot
probed within `_READ_PATH_REPROBE_MIN_INTERVAL_SECS` is trusted. The probe is one
shielded task per handle under `_READ_PATH_PROBE_DEADLINE_SECS`; a deadline miss
raises `EntitlementRevalidating` (the endpoint answers `503
model_list_revalidating`) while the task keeps running, a later read awaits the
same task, and a probe failure returns the current snapshot unchanged.

`probe_advertised_models` is single-flight with two independent clocks, both
`_ENTITLEMENT_PROBE_TTL_SECS`: a non-empty success replays on its result clock,
and an empty or failed attempt replays as `[]` (no evidence) on its attempt
clock, so a failure never revives an expired success and a burst of reads costs
one round-trip. `force=True` -- passed by an explicit `set_model` pick and the
spawn-time pin check -- skips only the attempt-clock replay, so a user action
always earns a fresh probe; the read path leaves it `False`. Either replay is
served only if its clock is at least as new as the snapshot the caller holds
(`not_before`): a cached broader answer can never replace a session's newer
narrower one, and a failed attempt that predates the snapshot never stands in
for the probe it has yet to receive. The handle dates the snapshot it stores by
the answer's own clock (`entitlement_probe_result_at`), not by its call time, so
its floor never rises above the data it holds and a replayed answer is never
re-dated out of the spawn-race window it was captured in.

**Direct-spawn revalidation.** `AcpClient` owns one process and one session, so
it has no runtime probe to borrow. It stamps its own `session/new` capture
(`_available_models_captured_at`, unconfirmed) and, before an explicit
`set_model` pick is refused or a startup pin is withheld on kiro,
`refresh_available_models` asks again: a snapshot a probe confirmed within
`_ENTITLEMENT_PROBE_TTL_SECS` is returned as is (fresh -- no round-trip); otherwise
`_probe_advertised_models` asks on a DEDICATED short-lived transport: a throwaway
`AcpClient` built by `_entitlement_probe_client` from this client's own launch
inputs (work dir, agent, env, backend, sandbox mode, gateway overlay, session key),
so `_spawn` starts it exactly as it started this session. On its own process it
sends `initialize` (the same `_initialize_params` this session's handshake sends)
and, before `session/new`, re-verifies the derived spec with
`require_unchanged_derived_spec` -- the same verify→create bracket every other
spawn path closes between initialize and session creation, so a spec revoked
during probe init aborts (raising `DerivedSpecStale`, absorbed as an empty answer)
rather than creating a session that would start that revoked spec's MCP server
commands. It then sends one minimal `session/new` (the pooled broker stubs, for
cost only), reads the answer through `advertised_models_from_session`, ends it
with the harness's teardown verb and shuts the process down, bounded by
`_INIT_TIMEOUT + _ENTITLEMENT_PROBE_TIMEOUT`. A non-empty answer replaces the
snapshot, dated by its arrival and marked confirmed; an empty or failed one is no
evidence and the refusal stands. There is no failure replay: the callers are user
actions and the picker read, the direct-client form of `force=True`.
`refresh_available_models` owns single-flight: a caller arriving while a probe is
in flight awaits that same (shielded) probe, so an overlapping picker read and
explicit pick start one probe process, never two. Because the probe never touches
this session's stream, nothing it emits -- session updates, substitution
advisories, and the MCP OAuth and init frames (keyed by `serverName`, no
`sessionId`) of whatever servers kiro-cli starts from the spec for it -- can enter
this session's notification buffer, its `_oauth_emitted_servers` dedupe or its
event stream; no frame filter, prompt hold or turn guard is needed, and isolation
does not depend on which servers the probe process happens to start.

Step 5 drains MCP server init notifications (both after `session/load` and
`session/new` — loading a session triggers MCP re-initialization).

### Notification Buffering

`AcpClient._wait_for_response()` buffers all JSON-RPC notifications in
`_mcp_notifications` instead of discarding them. `_drain_notifications()`
processes buffered notifications first, then reads any remaining from stdout.

The multiplexed `AcpRuntime` has the same guarantee for session-scoped init
frames even though it cannot register the session queue until `session/new`
or `session/load` returns the session id. While either request is in flight, the
runtime stages matching `_kiro.dev/mcp/oauth_request`,
`_kiro.dev/mcp/server_initialized`, and `_kiro.dev/mcp/server_init_failure`
notifications in a bounded buffer and transfers them into the new handle's
queue once the id is known. `AcpSessionHandle.drain_init()` retains OAuth
requests for `pop_pending_oauth_requests()`; the registration frames are what
arm its idle shortcut (below). Staging is cleared when the last concurrent init
finishes, including failure paths, so a stale approval URL cannot leak into a
later session. A start whose caller already timed out keeps its own copy of the
frames on its `StartCollector` instead of in that buffer — the frames outlive the
caller's budget, and the timeout diagnostic reads the buffer un-keyed (below).

`drain_init()`'s idle shortcut means "quiet **after** the servers reported",
not "quiet, therefore done": until the first MCP registration frame
(`server_initialized` / `server_init_failure` / `oauth_request`) is observed,
queue silence is treated as a server still booting — an npx-based stdio server
spends seconds on npm resolution plus a Node boot before emitting anything —
and the drain keeps waiting, bounded by `_MCP_DRAIN_NO_REPORT_CEILING`. Once a
report has been seen it allows up to `_MCP_DRAIN_DURATION` more and exits
after `_MCP_DRAIN_IDLE_EXIT` of silence, so warm sessions (whose registration
frames were staged during `session/new`) arm immediately and pay no extra
latency. A session with no MCP servers at all is the one case that pays the
full no-report ceiling; a runtime whose agent is KNOWN to be MCP-free — the
`kirocrew-lite` background runtime, whose config Kiro Crew itself writes with
an empty `mcpServers` map — opts out via
`AcpRuntime(expect_mcp_reports=False)`, which passes a zero ceiling and keeps
the idle shortcut active from the start (the pre-ceiling behavior).

**An unattended run waits longer, for named servers.** The drain above gives a
server about a second after the first report, which a slow `node` server can
miss; the session then prompts without its tools. Chat lives with that, because
a person sees it. A webhook or cron run has nobody, so before the first prompt
of a NEW session it calls `wait_for_declared_mcp(UNATTENDED_MCP_WAIT_SECS)`
(30 s, `agent_sdk/mcp_problems.py`) on the provider. Both transports implement
it (`AcpSessionHandle.wait_for_declared_mcp`, and `AcpClient` through
`_drain_notifications(until_declared=True)`): it keeps draining init frames,
handled exactly as the drain handles them, until every enabled `mcpServers`
name of the agent spec has reported initialized, failed or awaiting
authorization, or the bound passes. The names come from the spec snapshot the
unresolved-ref guard already reads (`McpSessionReport.record_declared`, which
skips a muted entry by the launch's own `mcp_entry_is_muted` rule), and only on
kiro-cli (and, on the runtime, with `expect_mcp_reports`): kiro-cli launches
the spec's own servers via `--agent` and reports each one. On another host a
spec server may be held back or never report, and every run would pay the
whole bound. `declared_not_ready()` decides the outcome: a webhook run records
`degraded` instead of `completed`. `declared_never_reported()` is what its
notice names, because `problem_summary()` already names a failed or
awaiting-authorization server with its reason. A cron run, which prints no
problem summary, names every not-ready declared server at the top of its
result. The other headless callers (sub-agents, workflow agent steps, task-runner
steps, heartbeat turns) do not wait yet.

**`problem_summary()` renders the report's bad news, and nothing else.** The
report answers two different questions, so it has two renderers: `payload()` is
the whole picture a dashboard draws (including `ready` and `configured`), and
`problem_summary()` is one line naming only what this session cannot use --
`failed to start`, `awaiting authorization`, `declared by the agent spec but not
configured` -- and the EMPTY string when it can use everything. Empty-on-clean is
the contract, not a caller's convention: a consumer prints the line
unconditionally and a healthy session stays silent everywhere. It is declared on
`providers.base.SessionMcpReport` beside `payload()` because the consumers that
need it most sit outside this layer, where the agent-SDK boundary gate refuses a
new ACP import -- a sub-agent spawn reaches it through the report the provider
already hands it (`subagent_manager/run.py`).

`include_reasons=False` keeps the server names and drops the failure text. A
reason is the failing server's own startup output, so in the OAuth and
remote-contacting cases it can carry content nobody here authored, and
`sanitize_sink_text` bounds credentials, URLs, control characters and length --
none of which disarms a plain-English instruction. A log a person reads takes the
reasons; a sink that feeds a MODEL asks without them, and fences the names it does
pass.

### KAS managed MCP readiness

KAS opts into a readiness barrier through its harness notification declaration.
Its `_kiro/mcp/status` and `_kiro/tools/didChange` notifications carry an explicit
`params.sessionId` and full `servers` / `tags` snapshots. The reader stages both
methods before the create/load response and transfers only that session's frames.
Status entries carry `name`, `status`, `failedAuthorization`, `errorMessage`,
`tools` (the connected server's catalog) and `_meta.kiro.resource.source.origin`;
the required Crew declarations have origin `client`. Exposure -- the model can
reach the connected server's tools -- is established by EITHER a non-empty
`tools` catalog on the server's own `connected` entry OR a tool tag with
`source: "mcp"` and `tag: "@server/tool"`; neither is the native callable
identifier. Both are accepted because released kiro-cli versions differ: captured
2.18.0 and 2.20.0 send the catalog and the tags; captured 2.22.0 (KAS 0.66.0)
sends the full catalog on the connected entry but its `didChange` snapshots list
only `builtin` tags (`read`, `write`, `shell`, `web`), with or without
`tool_search` in the agent's tools -- no MCP tag ever arrives. The two kinds of
evidence are kept apart: a full tag snapshot replaces the tag evidence (a tag
that disappears is retracted) but never the catalog evidence, and a reconnect
(`connecting` after `connected`) clears both, so the next connected snapshot's
catalog or a later tag frame must re-establish it.

Provenance is read off the whole snapshot, not one entry. A backend that stamps
any entry stamps them all (released 2.20.0 stamps `connecting` and `connected`
alike), so on such a backend an unstamped or non-`client` required entry is a
same-named foreign server and is skipped: the private declaration may still
report, so the requirement stays pending. A backend that stamps NO entry predates
the field. Released kiro-cli 2.18.0 is one: its captured wire emits both
notifications under the session's own id with a connected catalog and tool tag,
and no `_meta` on any status entry. On that release the two declaration sites
behave differently against a same-named `~/.kiro/settings/mcp.json` server: an
explicit session-level `mcpServers` injection wins on `session/new` and
`session/load` alike, while the active agent's own `mcpServers` declaration is
shadowed by the global server on `session/new` and coexists with it under one
name on `session/load` — all reporting under the session's id, so the wire cannot
say which server a bare name is. The barrier therefore takes the request's own
`mcpServers` array (`injected`, intersected with the required roster) as
positive evidence: on a provenance-less snapshot an injected required server is
read as reported (connection and exposure still required; failure states still
terminal), and a required server the active agent alone declared that such a
snapshot reports as `connected` is read as `connected without provenance`, a
terminal state that ends startup with `AcpRuntimeError` naming the server and
the limit. With the carriage below, the ordinary install's managed servers are
injected and therefore start on such a release; the refusal remains only for a
managed entry the carriage leaves in the block (a `disabled`, `disabledTools`,
`timeout` or registry customization), and it is a stated compatibility limit,
not restored support: accepting the entry could hand the session a server
carrying another identity's session key and memory. Missing metadata alone
never implies `client`; the connecting state on such a backend stays pending as
usual.

After activation, create and load wait for every required managed server to be
`connected` and exposed (a connected catalog or a tool tag, as above) when
exposure is permitted.
The required roster is the union of the ACTIVE custom agent's `mcpServers`
declarations and the actual session-level injection, intersected with Crew's
managed server catalog. Inactive agents and inherited global/external servers do
not contribute requirements or satisfy them. Other-session and sessionless reports
are ignored. Reports queued
before a mode change cannot satisfy the new activation; when the prior mode is
unknown, KAS conservatively treats activation as a change. A reconnect invalidates
that server's prior tool exposure.

Before either request goes out, the runtime carries the ACTIVE agent's managed
declarations in the session-level array (`kas_agents.hoist_servers_for_session`).
Captured released 2.18.0 honours that array over a same-named global or
workspace `mcp.json` server on new and load alike (the member dispatch server
already travels this way); the retained 2.20.0 capture proves a session-level
injection reports `origin: client` and reaches readiness, and its same-name
collision behaviour was not probed. The entries moved are the
already projected ones (credential fields withheld, `autoApprove` dropped,
`KIROCREW_PORT` / `KIROCREW_SESSION_KEY` applied), converted with
`session_mcp.acp_server_element`; no spec is re-read and the derived-spec
snapshot is unchanged. Each hoisted entry also receives this session's
`KIROCREW_STUB_SESSION_TOKEN`: the runtime stamps the token onto the
session-level array before the hoist runs, so without it the hoisted
`kirocrew-core` would be the one managed server launched unattested and refuse
every call as `identity_unattested`. Caller entries keep their own identity and
third-party servers never receive the token. A tokened entry launches the
managed invocation (`agent.managed_mcp_spec_entry(name, include_opt_in=True)`),
never the spec's `command` / `args`: KAS spawns it directly, so
`daemon/control_plane.py::_spawns_own_control_plane` never vets it, and a hand-edited spec
must not hand the token to another binary. Its env keeps only the projected
`KIROCREW_PORT` / `KIROCREW_SESSION_KEY` plus the managed env; a name whose
managed invocation does not resolve is hoisted as projected with no token.
Resolving that invocation probes the filesystem and `PATH` for the `kirocrew`
launcher, so the KAS harness does it in its `session_extras` thread
(`kas_agents.resolve_managed_entries`) and carries the result on
`SessionExtras.managed_entries`; the hoist itself does no I/O on the event loop
(#15282). On `session/new` the move covers the active agent's managed names only; on
`session/load` it additionally hoists remaining user-declared names
(`include_user_servers=True` in `hoist_servers_for_session`): kiro-cli
re-initialises MCP servers from the session-level array only on load — not from
agent-block declarations —
so a spec edit adding a new MCP server is silently ignored on resume without the
wider hoist. That load-only launch set was probed on released kiro-cli 2.31.0
(the #17781 repro: `session/load` with a spec adding `@probe/beta` exposed no
`beta` tool, `session/new` with the same spec did); the 2.18.0 and 2.20.0
captures above did not probe which declaration site `session/load` launches
from, so the premise is pinned to 2.31.0 alone. Exception: a user-declared server whose name a settings file
kiro-cli reads also declares — the user's global `~/.kiro/settings/mcp.json` or
the session cwd's `.kiro/settings/mcp.json`
(`reserved_names=settings_server_names(session_work_dir)` at the `load_session`
call site in `runtime_session_load.py`) — stays in the agent block, so the
resumed session resolves that name by kiro-cli's own precedence, exactly as
`session/new` did; a session-level injection would outrank both files.
`session/new` (`create_session`) is unchanged. Non-managed entries hoisted on
load travel as projected with no session token. On both paths: a name
the caller's array already carries is skipped (a broker stub or member dispatch
entry stays authoritative and a name appears once), and only a stdio entry whose
keys are drawn only from `command`, `args`, `env`, `type` — a `disabled`,
`disabledTools` or `timeout` customization, a registry marker, a remote URL or a
command-less entry keeps the agent-block path where that field is honoured. The agent's `tools`, `excludedTools` and `permissions` are
untouched: refs resolve wherever the server was declared, and on 2.18.0 an
allowlist-only or `excludedTools` restriction on the hoisted server still
yields readiness with only the permitted tag advertised. Exact legacy
limitation: on a release that stamps no provenance, a managed entry left in the
block by one of those extra fields is still refused as
`connected without provenance`; it works on a release that stamps `origin:
client`.

Every managed stdio element in the returned array, caller entries included,
carries `_meta.kiro.versionNegotiation: "legacy"`, so KAS starts each of Crew's
own servers once instead of twice. A non-managed or command-less entry keeps the
host default, and the agent-block path carries no pin.

**v2 parity of the projected entry (#25320).** The projection answers the same
spec the same way kiro-cli v2 does where the wire allows it:

- A spec with no `prompt` (missing or blank) is sent as `"prompt": ""`
  (`kas_agents.resolve_prompt`), so the agent runs on KAS's own default prompt
  alone, as it runs on kiro-cli's. The key itself must be present: KAS answers an
  entry without it with `_kiro/customAgent/not_found` and runs the session in its
  `vibe` mode. A non-empty prompt is layered on KAS's base prompt, not put in its
  place. Only Crew's background agent `kirocrew-lite` gets the inline
  `_KAS_FALLBACK_PROMPT` brief instead. Measured live on kiro-cli 2.35.1: `""`
  registers the agent (its empty `tools` held), a missing key falls back to `vibe`.
- A `file://` prompt is read by Crew and its text shipped over the wire, so the
  path is checked first. A relative ref is anchored to the agents directory and
  may use `..` (`file://../prompts/x.md` loads, as on kiro-cli); relative or
  absolute, the resolved path (symlinks followed) is refused when it is a
  sensitive location, `~/.config/gh`, the data home's `app_receipt_secret` or `session_token_*.sig`,
  or a pseudo-filesystem (`acp/kas_prompt_paths.py`). A spec from
  a project checkout is untrusted, and its relative ref must stay inside that
  checkout's `.kiro/agents` (`acp.harness.kas.resolve_projected_spec`).
- `toolsSettings.subagent` becomes `subagent` rules
  (`kas_permissions.tools_settings_to_permissions`), the capability whose
  resource is the sub-agent's name (a KAS consent names it as such, and KAS writes
  its own trusted sub-agents as `{capability: "subagent", match: [ids], effect:
  "allow"}`). `availableAgents` is a `deny` over `"*"` with the listed names in
  `exclude` (`[]`, or a value it cannot read, denies every sub-agent; a list
  holding `"*"` emits nothing); `trustedAgents` is a name-scoped `allow` that the
  merge puts to the governance ceiling like any authored allow. A pattern is
  carried when it is an agent id with `*` as its only wildcard; any other is named
  in the session-start notice, and dropping it only narrows. Measured live on
  kiro-cli 2.35.1: the deny with the target excluded left the call to its
  permission request, with another name excluded it was refused by the rule, and
  the allow ran it with no request.
- `toolsSettings.shell.allowedCommands` travels as no rule: a shell allow would
  skip the permission request Crew's deny floor runs on. Crew answers a matching
  command at that request itself, through the gate's `spec-builtin-grant` row
  ([security](security.md) § KAS spec built-in grant).
- `toolsSettings.shell.denyByDefault` travels as no rule either: Crew's gate
  refuses every shell call of that agent that no usable `allowedCommands` entry
  matches whole, on every approval policy, through its `spec-shell-allowlist` row
  ([security](security.md) § KAS spec shell allowlist).
- `toolsSettings.fs_write.fallbackAction: "deny"` travels as no rule too: Crew's
  gate refuses every file write of that agent whose target no usable
  `allowedPaths` entry covers, anchored at the session's directory, on every
  approval policy, through its `spec-write-allowlist` row
  ([security](security.md) § KAS spec write allowlist).
- The spec's `hooks` run from Crew's turn loop (`agent_sdk/spec_hooks.py`); a
  PostToolUse spec hook's tool matcher is matched on the id the call's permission
  request stated, or for a call the frame classified as a shell command, KAS's
  shell tool (`spec_hooks.post_tool_hook_tool_id`), with the call's trusted MCP server and tool name kept beside it, because KAS's `tool_call`
  frame names no built-in tool and titles the call in prose. Pinned through a real
  `chat_runner._run_chat` turn by `test_kas_spec_hooks_turn_loop.py`.

For a derived worker, the projected payload carries both its checked specification
snapshot and its runtime session key through create and load. Activation checks
that same snapshot before readiness: connected tools cannot admit a revoked
template, and an unchanged template still waits for its managed tools.

The exposure check reads the active agent's projected `tools` and `excludedTools`,
plus the connected status's `tools[].disabled` flags. If these deliberately hide
every tool from a declared server, connection is sufficient: an absent tag must
not make a restricted agent unusable. An empty or missing catalog alone does not
prove that restriction. `allowedTools`/`permissions` governs approval, not
exposure; readiness never changes grants or declarations to obtain a tag.

The barrier retains the ordinary drain's config updates, pending OAuth requests,
and initialization-failure diagnostics, including for unrelated external servers.
These side effects do not satisfy or extend managed readiness. An agent with no
required managed servers keeps the ordinary drain. Readiness extends the existing
configured roster rather than resetting the report to its required subset.
Session-owned KAS status frames also update the report for external servers,
including failure, authorization and reconnect states. This display report is not
the managed-server provenance and tool-exposure gate.

The wait uses the existing `agent.session_start_timeout_secs` budget, with no
idle-success shortcut or extra sleep. `failed`, `disabled`, failed
authorization, and `connected without provenance` terminate startup with
`AcpRuntimeError`; connecting, missing
reports, or missing tool exposure remain pending until `AcpRequestTimeout`.

While `agent.mcp_registry_mode` is on, the wait has a settle checkpoint (6s,
the init drain's first-report bound) inside the full start budget. The KAS
customAgents wire has no slot for the registry marker, so a governed host's
catalog filter drops Crew's managed servers and never reports them; the full
wait would only be spent, fail, and meet the same filter on the retry, so the
chat would never start. At the checkpoint, and again at the deadline, when
every pending server is one the host never named for this session in any
status entry (any state or origin) or `@name/` tag, the session starts, as the
kiro-cli backend does on such an account: each such server is recorded in the MCP report as failed with
the reason and ready ones as initialized (the spec projection already warns of
the drop, so no second warning is logged); a later frame still moves a failed
name to ready. Every other guard is unchanged on this
path: a server the host did name keeps the full budget, a failed, disabled,
auth-failed or provenance-less server still raises, and a foreign same-named
entry or a server still connecting still times out at the deadline.
An unreadable or half-read config reads as off here, the opposite of the
spec-server ceiling, because this reading loosens readiness rather than
withholding.

With registry mode off the wait and its timeout stand. Silence alone is not
proof of a governed drop (a slow or quiet ungoverned host looks the same), so
when every pending server is one the host never named for this session in any
status entry (any state or origin) or `@name/` tag, the timeout message only
adds a pointer to `kirocrew doctor`, whose MCP Governance section checks the
account's identity before advising the setting. A server the host did report
(still connecting, or a same-named entry that is not `client` origin) gets the
plain timeout.
Errors name the required server and sanitize backend failure text. On failure,
runtime death, or cancellation, no handle escapes. A failed fresh `session/new`
is terminated through the bounded per-session teardown, freeing its resident state
and MCP children without touching siblings. A failed `session/load` is only
unregistered locally because KAS's delete verb would destroy the existing native
history; that history remains available to a later resume. No first prompt is
sent on these paths. Harnesses without this opt-in retain their existing
initialization drain.

### KAS MCP OAuth sign-in

A remote OAuth MCP server that KAS cannot reach without a token reports a `failed` status entry with `failedAuthorization: true`. Its `authorizationUrl` comes from a passive connect whose loopback callback listener is already closed, so Crew never renders it. A sign-in that can complete is started by the client: `_kiro/mcp/resetServer` with `{sessionId, serverName, startOAuth: true}` makes the engine keep its callback listener open for the whole connect attempt (the server's connect timeout, 60 s by default) and send the consent URL as a `_kiro/openExternalUrl` request. The engine sends that request only because `KAS_CLIENT_CAPABILITIES` declares `_meta.kiro.openExternalUrl`.

The session handle tracks, from its own status snapshots, which servers need a sign-in: added on `failedAuthorization`, removed once the server connects, is disabled, or leaves the snapshot. A timed-out attempt reports `failedAuthorization: false` with a timeout error, so the server stays tracked. The tracker reads at most the report's `BUCKET_CAP` entries and skips a name over `NAME_CAP`.

Needing a sign-in never starts one. An unsigned KAS server's tools are invisible to the model (exposure needs the catalog of a `connected` entry, above), so the agent can never call one, and a tool call cannot be the trigger. The trigger is the user's own message: before each user turn, the dashboard runner hands what the person asked (`user_typed_message`: captured before `@prompt`/`$skill` expansion, with no cancelled-turn preamble, sub-agent failure text or app context, any of which could name a server the message did not; only a turn with `_directive_user_origin` from a person's dashboard/channel send or human app-chat hands text over; a generated app dispatch, an OpenAI-compatible request (its prompt is the flattened conversation), a Spec Builder decision answer (composed from the agent's title and options), or a Regenerate replay of a stored row, all `_message_is_typed=False`, a turn restored from the on-disk queue (`_turn_provenance_restored`), synthetic turn, self-wake, slash command, `/prompts get` re-entry, or turn with a `_turn_actor` hands nothing) to the provider's declared `LLMProvider.note_user_message` (a no-op by default; the direct kiro-cli client has no trigger). `AcpProvider.note_user_message` holds the text only until that provider's next `stream()` or `stream_command()`, which takes and clears it at entry before anything can fail; `stream_command()` discards it. For `stream()`, the text then travels as an argument into that one attempt's handle `prompt`/`_run_turn`, after any pending MCP re-injection, so it never outlives the attempt or reaches a later automated turn. After the pre-turn drain, `_run_turn` calls `want_mcp_sign_in_named` with that attempt's text to mark each tracked server it names as wanted. The handle holds the text for that one turn and matches it on each mid-turn status snapshot, each server at most once, so a server whose `failedAuthorization` status was still queued or arrives mid-turn is found; the turn's end clears it, so a later cron or sub-agent turn never matches an old message. A server is named by its configured name or an obvious alias (`mcp_server_aliases`: the words split at separators, the part after a `namespace:` prefix (a one-word part only when it repeats the namespace), a Kiro Power's name without its `power-` prefix with repeated words collapsed (`power-figma-figma` gives `figma`, but a multi-word name's last word alone, such as `search` from `power-aws-docs-search`, never counts), each without a trailing `mcp` or `server` word, repeated words collapsed, derived aliases under three characters or only `mcp`/`server` dropped, while the configured name itself always counts, so a server named `gh` or `mcp` can still be named), case-insensitive and as whole words only. So a chat that never names an unsigned server shows no Authorize card for it, at session start or later. An accepted start removes the server from the wanted set, so one ask starts at most one sign-in. A message that names a server whose sign-in this session already holds in the runtime asks for nothing, which keeps one live card per server. Once an attempt ends (signed in, timed out, or left by the user), only a later message naming the server offers it again; unrelated messages and status snapshots never do.

A task that needs an unsigned server without naming it ("check my design files") gets no card: the user names the server to be offered its sign-in. A start the runtime refuses (its one slot is held) leaves the server wanted for the next offer point, in name order. The wanted set holds at most `BUCKET_CAP` names, and a refused name is logged. Settings > MCP sign-in does not reach KAS yet: it runs a dedicated kiro-cli mint (`connections/mint.py`) whose grant lands in kiro-cli's own OAuth cache, while a KAS engine keeps its grants in its process memory or, with `agent.kas_persist_mcp_sign_in` on, in the `kas/mcp-grants` vault below, and nothing carries one to the other. On KAS the chat card, offered when a message names the server, is therefore the only sign-in path, and its grant lasts as long as that chat's KAS process unless persistence is on. On kiro-cli, `begin_mcp_sign_in` refuses every start (the host opens no external URLs) and the engine sends its own `_kiro.dev/mcp/oauth_request` frames, so none of this changes it.

A sign-in is offered only where a turn is reading the queue, because the consent URL comes back on that queue: at turn start, after the pre-turn stale drain, and mid-turn when the dispatch loop reads a status snapshot. Session start (the managed-readiness barrier and the init drain) reads status without offering: a start that fails there is torn down, and a link it started would land between turns. The pre-turn drain reads every between-turns status snapshot before dropping it, so a server that connected while no turn was reading leaves the set and the turn-start offer never resets a connected server (a reset with `startOAuth` invalidates its credentials). The pre-turn drain keeps an owned `_kiro.dev/mcp/oauth_request` frame only while the runtime holds the sign-in slot for that session and `serverName`; otherwise it drops the stale link.

`_kiro/openExternalUrl` names neither the session nor the server, so the runtime runs at most one sign-in per process (`AcpRuntime.begin_mcp_sign_in`), gated on `ACP_BACKENDS_OPEN_EXTERNAL_URL` and refused while a credential re-attempt (below) is unanswered. The slot is held until the reset is answered, even after its session unregisters, so a departed session's late link is refused rather than handed to a newer sign-in; the slot's own timeout sits past the engine's 600 s connect ceiling. The URL is put on the owning session's queue as an ordinary `_kiro.dev/mcp/oauth_request` frame and answered `{success: true}`; it therefore takes the same URL checks and the same Authorize banner as the kiro-cli engine's own request. It is refused with a JSON-RPC error when no sign-in is in flight, when the owning session is gone, or when the URL is not http(s); the engine raises on that error, so the attempt ends at once instead of waiting for a callback no user was shown. By default `secretStorage` is not declared, so the engine keeps the resulting grant in its own memory: Crew stores no MCP token, and a new KAS process needs a new sign-in.

With `agent.kas_persist_mcp_sign_in` on, the spawn declares `_meta.kiro.secretStorage` on a copy of `KAS_CLIENT_CAPABILITIES` (`runtime_spawn_launch._with_mcp_sign_in_storage`, only for a harness that `opens_external_urls`) and holds a per-process `kas_secret_storage.SecretStorageServer` on the runtime. The engine then sends `_kiro/secret/get {key}`, `_kiro/secret/store {key, value}` and `_kiro/secret/delete {key}`, which name no session; the reader loop answers them off-loop only on a process whose handshake declared the capability, and `-32601` otherwise. Only keys `kiro.mcp.<sha256 hex>.(client|tokens|verifier|discovery)` and values up to 64 KiB are accepted; anything else gets a JSON-RPC error. `client` and `tokens` persist in a dedicated vault at `<data_home>/kas/mcp-grants/.vault/` (own key; not the Secrets vault, so not listed there and not grantable to a cron; under the `kas` keystone leaf), each written with `set_with_revision`. `verifier` and `discovery` stay in the server's memory and die with the process. These frames are never handed to the raw-frame recorder, and no value is logged or put in an error message. Concurrent sessions share one grant; each write is atomic under the vault's cross-process lock and the last writer wins. A delete removes only the entry revision this process last read or wrote (`delete_if_revision`), so a process whose refresh fails cannot erase a grant another process stored since; a key the process never read or wrote is deleted outright.

KAS sends no `_kiro.dev/mcp/server_initialized` frame when a sign-in completes; the server's next status entry reports `connected`, and status snapshots are classified `skip`. The tracker therefore records a tracked server that connects as a completed sign-in, and the dispatch loop yields one `EVENT_MCP_SERVER_INITIALIZED` (`runtime_global: false`) for each such server right after it reads a status snapshot, clearing the server's banner dedupe as the frame-driven branch does. A completion read by the session-start or pre-turn drain is yielded with the next turn's first frame. Only a server that was being tracked for a sign-in qualifies; an ordinary connected server yields nothing. The dashboard handles the event as it handles the engine's own frame: the Authorize banner is marked completed and its dead consent URL is withdrawn.

### KAS MCP credential re-attempt

A stdio server whose signer resolves credentials lazily fails its first `initialize` when the session starts before a credential exists, and the engine never initializes it again on its own. The session handle tracks, from the same status snapshots, servers whose entry is `failed` without `failedAuthorization` and whose `errorMessage` reads as a missing or rejected credential (`acp/mcp_reattempt.is_recoverable_auth_failure`). The word "credential" counts only next to wording that says it is absent or bad ("No AWS credentials available", "Unable to locate credentials", "credentials have expired"); a refused or expired login is read with the shared auth vocabulary (`acp/transport_errors.is_auth_failure_output`), not a second one. A start failure that merely mentions a credential or a number is not re-attempted. At turn start, after the sign-in offer, each such server is re-attempted with `_kiro/mcp/resetServer` and `{startOAuth: false}` (`AcpRuntime.begin_mcp_reattempt`), at most `MAX_ATTEMPTS` (3) times per server per session and for at most `BUCKET_CAP` distinct servers. A refused start spends no attempt.

A re-attempt opens no consent URL, but the engine might still send one, so a re-attempt and a sign-in never overlap: `begin_mcp_sign_in` refuses while any re-attempt is unanswered, and `begin_mcp_reattempt` refuses while the sign-in slot is held. Re-attempts of different servers may run together. A re-attempt holds off sign-ins for at most `_MCP_REATTEMPT_TIMEOUT` (75 s, past the engine's default 60 s connect timeout); a URL that still arrives later is refused because no sign-in is in flight, or reaches a sign-in started since. A re-attempted server that connects is yielded as `EVENT_MCP_SERVER_INITIALIZED`, like a completed sign-in.

The session MCP report carries `restart_to_load`: failed servers whose reason reads as a missing or rejected credential and that the session does not re-attempt itself (kiro-cli, which has no per-server reset, or KAS once the budget is spent). The handle decides which applies from `AcpRuntime.reattempts_mcp_servers`, the same capability `begin_mcp_reattempt` checks, not from whether the method exists. The MCP panel shows a "start a new session" hint under each, and `problem_summary` names them without their reason.

### Idle reader and agent-started turns (`AcpClient`)

On a backend in `ACP_BACKENDS_AGENT_STARTED_TURNS` (claude), Claude Code can
start a turn by itself after a prompt's turn has ended, for example when a
`run_in_background` command exits, and claude-agent-acp writes that turn to
stdout while no request waits. Design record:
`docs/request-for-change/rfc-agent-started-turns.md`.

- **Bound by the claude adapter.** `ClaudeLaunch.resolve_spawn`
  (`acp/harness/claude.py`) calls `_install_idle_reader()` on the session it
  launches when its backend is in `ACP_BACKENDS_AGENT_STARTED_TURNS`; a respawn
  calls it again and it binds nothing twice. On that instance it wraps
  `ensure_ready`, `_initialize_session`, `_wait_for_response`,
  `_drain_notifications`, `wait_for_compaction`,
  `_drain_post_compaction_metadata`, `_read_steer_answers` and `_prompt_loop`
  in a stdout claim, and
  makes `_kill_process` and `_reset_state` drop the reader first. The first
  call runs inside the first `ensure_ready`, so only the `_initialize_session`
  claim covers that handshake; the `ensure_ready` claim stops the reader when a
  later prompt starts. It also wraps `send_message`, `send_message_stream`,
  `stream_events` and `stream_command` so they queue behind an agent-started
  turn (below). The kiro-cli
  arm of `_spawn` reaches no adapter, so a Kiro client's constructor and read
  methods behave as on main and it never reads between turns (harness-parity
  H13). The one shared change on that path is `_read_message`'s keyword-only
  `check_cancel`, which defaults to main's check and only the idle reader
  passes.
- **Instance bindings hide class patches.** The wrappers are set on the
  instance with `setattr`, and each holds the method it wraps, so after the
  spawn a patch of `AcpClient.<method>` on the class does not reach a claude
  client for any method named above. Patch the instance, or patch the class
  before `_install_idle_reader` runs.
- **One reader at a time.** An `asyncio.StreamReader` allows one waiting
  `readline()`, so a claim cancels the idle reader and waits for it to exit;
  the last release restarts it. `_prompt_loop` is claimed before it takes
  `_turn_lock`. `test/test_acp_client_agent_started_turns.py` fails when a new
  `_read_message` caller in `client.py` or one of its owner modules is not
  wrapped.
- **The idle reader.** It runs while no claim is held, the turn is done, the
  process is alive, no hand-off is pending and `on_agent_turn` is set. It holds
  what it reads locally, because `_read_message` pops `_buffer` first, and puts
  it back at the front of `_buffer` in arrival order when it stops, unless
  `_cancel_idle_reader` dropped it on a kill or reset. It reads with the
  cancel-grace check off, since `_cancelled` belongs to the turn that ended,
  and it stops once `_buffer` is full (its `maxlen`, 100), leaving the rest in
  the pipe.
- **Only the latest snapshot is held.** A frame that only reports state waits
  for the next turn, so when the reader reads a frame that supersedes one it
  holds, it drops the held one; the newer frame keeps its arrival position and
  no other frame moves. A frame supersedes a held one when both are `session/update`
  frames of a kind in `_SNAPSHOT_UPDATE_FIELDS` with the same keys, and they
  differ only in that kind's snapshot fields: `used` and `size` for
  `usage_update`, `availableCommands` for `available_commands_update`. The
  session, `_meta` and a `usage_update`'s `cost` must match, because cost is a
  running total billed as the change between readings, so a new reading is
  kept. `config_option_update` is not in the table: `_sync_effort_levels` adds
  the effort levels of every config frame to the process-wide validation set,
  and the session-config tripwire of a backend in `ACP_BACKEND_FORBIDDEN_MODES`
  checks each frame. A kind a reader starts to treat as incremental leaves the
  table. A restarted reader takes its earlier frames from `_buffer` through the
  same rule, and no other path puts a state frame back into `_buffer`, so
  snapshot frames alone never fill it, across restarts too. A kept end marker
  still precedes the turn's first activity frame, so it ends nothing (below).
  Every other frame (`session_info_update`, `config_option_update`,
  `current_mode_update`, a `usage_update` with a new cost or `_meta`, an
  unknown kind, other notifications, responses) still counts toward the
  bound.
- **Hand-off.** A request from the agent, or a `session/update` of kind
  `agent_message_chunk`, `agent_thought_chunk`, `tool_call`, `tool_call_update`
  or `plan`, ends the reader: it records that frame as the turn's start and
  calls `on_agent_turn(client)`. A frame that only reports state
  (`usage_update`, `session_info_update`) starts nothing. The hand-off stays
  pending while the start frame is still in `_buffer`, so whichever reader
  takes it ends the hand-off. A request wait files a start `session/update` in
  `_mcp_notifications`, as it files every agent frame it reads, and the
  restarted reader hands off the next activity frame; a start request (a
  permission request) goes back to `_buffer`, and the hand-off stands.
- **Reading the turn.** `stream_unsolicited()` reads the pending turn through
  `_dispatch_events` under request id `-1` and sends nothing. After the turn's
  first activity frame, the bound `_prompt_loop` ends it on the `usage_update`
  whose `_meta["_claude/origin"].kind` is `task-notification`, `peer`,
  `coordinator`, `observer` or `observer-activity` (a user turn's is `human`,
  which ends nothing), or on a response to a request sent before the turn
  began: no request is sent under `-1`, so such a response answers a wait that
  already ended, usually a prompt whose turn ended here, and the frames were
  that prompt's tail; it keeps its own `stopReason`. A response to a request
  sent after the turn began (an id at or above the `_next_id` the turn started
  with) also ends the turn, but stays at the front of `_buffer` for that
  request's reader. The answer to a `_session/steering` request ends no turn,
  whenever its steer was sent: the client keeps the id of each steering
  request until its answer is read, a steer that still waits is handed its
  answer before this rule runs, and an answer that arrives after its steer
  stopped waiting passes as a stray response, as in a user turn. Process
  death, the stale-turn gate and the prompt timeout end it too. A prompt that
  starts first reads the frames as its own, which ends the hand-off, and
  `stream_unsolicited()` then yields nothing, as it does while any prompt is in
  flight.
- **The turn takes no steers.** `_dispatch_events` leaves `_steering_turn_req`
  unset under `-1`, so `steer()` returns False while the turn reads and the
  caller queues the text as the next prompt. The dashboard's busy send button
  defaults to Steer, and a Steer-mode send falls back to its queue on that
  False, so a message typed during the turn runs after it.
- **A prompt queues behind the turn.** `stream_unsolicited()` holds
  `_agent_turn_gate` while it reads, and keeps it until `_prompt_loop` has
  released `_turn_lock`, since `_dispatch_events` leaves that loop to the
  async-gen finalizer. A wrapped prompt entry point passes the gate before it
  resets `_cancelled` and `_turn_done` or writes anything, and counts itself in
  `_prompts_in_flight` until it returns, so a Stop pressed on the agent-started
  turn ends that turn, and `has_active_turn()` reads the prompt's own state.
  `stream_unsolicited()` checks that count under the gate and yields nothing
  while it is above zero. A consumer closes the stream with `aclosing()`, as
  for `stream_events`: one left open holds the gate. This is the queue of
  `docs/request-for-change/rfc-agent-started-turns.md` decision 3. Do not await
  a prompt inside an open `stream_unsolicited()` loop: the prompt waits for that
  stream to close, so the two wait on each other.

No production caller registers `on_agent_turn`; until one does, the reader
never starts.

## Key APIs

| Method | Purpose |
|--------|---------|
| `ensure_ready()` | Spawn kiro-cli + init handshake (steps 1-5) |
| `send_message(msg)` | Full response text, auto-approves tools |
| `send_message_stream(msg)` | Yields text chunks, auto-approves (CLI) |
| `stream_events(msg)` | Yields `AcpEvent` objects, caller handles permissions (dashboard) |
| `approve_tool(id)` / `reject_tool(id)` | Tool permission responses |
| `send_command(cmd)` | Slash commands (e.g. `/compact`), returns response text |
| `command_result(cmd)` | Kiro-only native command result including structured `data`; internal callers must reduce it before external use |
| `cancel_session()` | Cancel in-flight operation |
| `wait_turn_done(timeout)` | Wait for the current prompt to finish; returns `stop_reason` or raises `asyncio.TimeoutError` |
| `has_active_turn()` | Returns `True` while a prompt is in flight and not yet complete |
| `shutdown()` | Kill kiro-cli process |

The Connections authenticated Test action is the only application consumer of
`command_result`. Its agent-SDK driver resolves the operator's configured
`agent.sandbox` tier off the event loop, gives readiness plus the ordered command
batch one total timeout, and calls `shutdown()` in a `finally` on success, failure,
timeout, or caller cancellation. Only the bounded verdict and tool count leave
the Connections layer; raw command data and tool descriptions do not reach the
HTTP response.

### Extension Notifications

`stream_events()` yields events for kiro-cli extension notifications:

| Notification | Event Kind | Fields |
|-------------|-----------|--------|
| `_kiro.dev/compaction/status` | `compaction_status` | `text` = started/completed/failed, `title` = summary |
| `_kiro.dev/clear/status` | `clear_status` | (none); also invalidates the essential-context receipt (`providers.md`) |
| `_kiro.dev/agent/switched` | `agent_switched` | `text` = new agent name |
| `_kiro.dev/mcp/oauth_request` | `mcp_oauth_request` | `server_name`, `oauth_url` |
| `_kiro.dev/mcp/server_initialized` | `mcp_server_initialized` | `server_name` |
| `_kiro.dev/mcp/server_init_failure` | `mcp_server_init_failure` | `server_name`, `text` = error |

`_process_message()` classifies these as `"compaction"`, `"clear"`, `"agent_switched"`, `"mcp_oauth_request"`, `"mcp_server_initialized"`, `"mcp_server_init_failure"` actions.
Other methods (`send_message_stream`, `send_message`) log compaction but do not yield
clear/agent events (CLI/Slack paths handle these differently).

### MCP OAuth Inline Banner

When kiro-cli needs OAuth authentication for an MCP server, `AcpClient` surfaces the flow inline:

1. `_kiro.dev/mcp/oauth_request` — captured during `_drain_notifications()` (init) and `_prompt_loop()` (mid-session). Yields `EVENT_MCP_OAUTH_REQUEST` with `serverName` + `oauthUrl`. Frontend renders an Authorize banner; kiro-cli's local callback handles the OAuth redirect.
2. `_kiro.dev/mcp/server_initialized` — flips the banner to authenticated state. Clears the per-server dedupe entry so a future token expiry can re-prompt.
3. `_kiro.dev/mcp/server_init_failure` — flips the banner to failed state with the error string. Also clears dedupe so a retry surfaces a fresh banner.

**Dedupe**: Per-server dedupe via `_oauth_emitted_servers: set[str]` prevents kiro-cli's per-probe retries from spamming the user. Works across both buffered (init drain) and live (mid-session) paths. Cleared on new session.

**Card-owned requests**: a request for a server that a rendered Connections card owns (`_connections_managed_mcp_names`) is tagged `card_owned` on both paths, the init drain and the mid-session `_on_mcp_oauth_request`. The tag rides through every later state of the banner (completed, failed, superseded, expired), and the chat hides tagged banners while `connections_ui` is on, because the card shows the same Authorize action. The KAS sign-in offer re-requests every unauthorized server at each turn start, so an untagged mid-session request would repeat the prompt on every turn of every session.

**URL validation**: `_is_safe_oauth_url()` rejects non-http(s) schemes before dedupe — an unsafe URL doesn't consume the dedupe slot.

**Persistence**: Role-aware redaction (`_redact_meta_for_role`) preserves `oauth_url` for `mcp_oauth` messages so the Authorize link survives history rehydrate, while still scrubbing unsafe schemes on the read path.

**API**: `pop_pending_oauth_requests()` drains requests captured during init on
both `AcpClient` and `AcpSessionProvider` (called after `ensure_ready()`).

**Remote-gateway callback relay**: The Connections waiting card and the chat `mcp_oauth` banner both accept the failed browser return address when the browser and gateway run on different machines (the banner surfaces it behind a one-line disclosure, so any server the banner names — including user-added / self-hosted ones — can recover). `POST /api/mcp/oauth/relay` sends that address from the gateway host to kiro-cli's local callback listener. The `server` field is validated with the same `_is_valid_mcp_name` rule that governs which servers can be added at all (128-char bound); it is a bounded audit label, not a registry-membership gate. The handler is intentionally not a generic proxy: it accepts only plain-HTTP URLs whose host is in the fixed loopback set the runtime callback can produce — `127.0.0.1`, `::1`, or `localhost` (the network host is later selected from fixed literals, never from request data) — with an explicit port ≥1024 and exactly one non-empty `code` value; it rejects userinfo, fragments, other hostnames, non-loopback addresses, oversized input, and does not follow redirects. The callback URL and authorization code are never logged or returned; SEL records only the validated server name and completed/failed outcome. Minting an approval URL is bounded by configuration, never by request data: a registry provider mints against its registry URL, and an owner-added remote server (`POST /api/connections/mint` with `server`) mints against the `url` of its entry in the owner's main agent spec, and only while that URL equals the one recorded for the name under `connections.owner_mcp_servers` in the sealed `config.json` (`connections/owner_servers.py`). Only the owner-only custom add and edit routes write that record, and removal drops it, so a server an agent or a session added, or an owner entry re-pointed outside the dashboard, is refused. That second path is owner-only, refuses a name that is a registry slug, a name `mcp_server_alias` rewrites, and any entry without an http(s) `url`, and skips the registry-only OAuth-app check and premint adoption.

## Cancellation

`cancel_session()` sends a `session/cancel` JSON-RPC notification to the agent's stdin. It is fire-and-forget — no response ID is awaited.

After the notification it answers every permission request still open (`_permission_options`) with the `cancelled` outcome, as ACP requires. goose and pi do not acknowledge a cancel while a permission request is unanswered, so without this a Stop from a surface that had not rejected the open approval first waited out the ack budget and hard-killed the harness. Each answered request gets one SEL record (`tool_name=approval_cancel`, `outcome=rejected_on_cancel`) before the answers go out: one off-loop hop for all of them, bounded by `_SEL_AUDIT_TIMEOUT_SECONDS`. The answers are ordinary response frames through `_send_response`, so each carries the response-write no-progress bound. Both costs fall inside the cancel grace window. A cancelled pi gate dialog is recorded as denied, the same as a rejection.

### A session the harness disposed (`acp/disposed_session.py`)

A harness in `ACP_BACKENDS_RELOAD_DISPOSED_SESSION` can stop serving one session while its process keeps running, and then answers every `session/prompt` on that id with the same error. The Strands CLI does this when a cancel does not stop its Python worker within 3s: it kills the worker and answers `-32603` with `data.details` "The Python agent did not stop after cancellation. Reload it to continue." (`test/fixtures/acp_frames/strands/cancel-dispose-live.jsonl`). A second process loads the saved session and serves the next prompt (`cancel-dispose-reload-live.jsonl`).

For a member, `_prompt_loop` asks the module about every prompt error and every approval request, so all three prompt APIs behave the same. A harness outside the set never reaches it:

| Read in the turn | What the client does |
|---|---|
| A prompt error whose `data.details` is one of the member's disposal texts (`DISPOSED_SESSION_DETAILS`), on a turn Crew cancelled | Ends the turn as `cancelled`, marks the client stalled (`_stdin_stalled`) and keeps this session as `_resume_session_id` |
| The same error on a turn nobody cancelled | Marks the client the same way and raises a non-transient `AcpError` with a plain message: send the message again |
| A `session/request_permission` read after `cancel_session` | Answers `cancelled` at once, with the same SEL record as the cancel's own answers (`cause=asked_after_cancel`), and never puts it to a person. The CLI holds its turn open on that answer, so the cancel could not finish while a card waited |

The next `ensure_ready` replaces it through the stalled-child path it already has: it kills the process (force), refuses to spawn beside one it could not confirm dead, and resets state, which clears the flag. The start path itself is unchanged. `_resume_session_id` survives the reset, so the spawn that follows sends `session/load` for the same id and the conversation comes back before the next prompt goes out. Any other error text on the same code stays an ordinary turn failure.

An open approval is a different case, and the client does not handle it. A cancel that finds a Strands approval OPEN ends cleanly as `cancelled`. But Strands CLI 0.2.3 leaves the agent in its interrupt state and saves it, so a bare CLI then fails every later prompt with "must resume from interrupt", and so does a reload (`cancel-approval-open-live.jsonl`). That text is not a disposal, because a fresh process loads the same saved state. Crew's gate module clears it instead: before any prompt that is not itself an interrupt answer, it resumes each open interrupt with a deny (`agent_sdk/gate_extensions/strands/kiro_crew_strands_gate.py`, `_gated_stream_async`).

### An MCP change applied by relaunch (`acp/relaunch_reload.py`)

A harness in `ACP_BACKENDS_MCP_RELAUNCH_RELOAD` reads its MCP servers once, at launch, and keeps its own session records. The Strands CLI is one: its servers come from the `--mcp-config` file the adapter writes at each spawn, and project mode refuses a non-empty `session/new` array. So a running process cannot take a new server. A second process launched with a changed file can: it `session/load`s the same id, replays the saved conversation, answers `{}`, and offers the new set on the next turn (`test/fixtures/acp_frames/strands/mcp-relaunch-add-live.jsonl`, an added server; `mcp-relaunch-remove-live.jsonl`, a withdrawn one).

| Step | Where | What happens |
|---|---|---|
| Decide | dashboard MCP writer (`mcp_hot_reload.live_sessions_apply_mcp_live`) | `AcpProvider.mcp_config_relaunch_reload` is True for a direct `AcpClient` on a member, so the all-sessions reset is skipped |
| Mark | `mark_reinjecting_providers` -> `AcpProvider.mark_mcp_config_changed` | sets `_mcp_relaunch_pending` on the client; a client outside the set keeps no mark |
| Clear | the Strands adapter's `resolve_spawn` | drops the mark right before `_prepare_session_mcp` reads the spec: this launch already writes the new set. A mark set after that read survives |
| Apply | `providers/acp_turns._ready_confirmed_session`, after `ensure_ready` and before `confirm_member_projection` | `apply_pending_mcp_relaunch`: skipped while `_turn_lock` is held (never mid-turn) or with no live process holding a session. Otherwise the mark is consumed and `relaunch_and_reload` runs |
| Relaunch | `relaunch_and_reload` | the disposed-session path: marks the client stalled, keeps the session id as `_resume_session_id`, and calls `ensure_ready`, which kills the process, spawns a new one with a fresh `--mcp-config` and sends `session/load`. Once the same id loads, the stub token is re-claimed for the new process (`reclaim`), as the KAS re-injection does |

A replacement that opened a different session (`ensure_ready` falls back to `session/new` when a load fails) is not this conversation, so it counts as a failure. A spawn refusal that names its own remedy (an `AcpError` such as a wrong CLI version or a missing vault secret) reaches the person unchanged, and a dead process stays transient. A replacement that opened another session, or a failure that is not an `AcpError`, refuses the turn with a plain message (`RELAUNCH_FAILED_TEXT`). Either way the load stays owed (`_relaunch_owed_session`): the client is stalled with the session id as its resume id and as `_session_id` (so the shutdown sweep never saves a fallback session's id as the chat's), and every later turn is refused until a replacement serves that id, so no turn is prompted into another conversation. A chat whose saved session is gone keeps refusing, and the message tells the person to start a new chat. The relaunch keeps the chat's model (`session._model` reaches `--model` again). `relaunch_and_reload` is the shared step for any launch-time setting a live chat must take.

### stopReason Parsing

When the ACP agent acknowledges a cancel, the `session/prompt` response carries `result.stopReason`. `_dispatch_events` reads this field on `action == "complete"` and populates `AcpEvent.stop_reason`:

- `"cancelled"` — agent honored the cancel request (`STOP_REASON_CANCELLED`)
- `"end_turn"` — normal turn completion (`STOP_REASON_END_TURN`)
- `""` — field absent or not a dict result

### Cancel Grace Window

Setting `_cancelled = True` does not short-circuit `_read_message`. Instead, a 10-second grace window (`_CANCEL_GRACE_SECS = 10.0`) allows the agent to deliver its `stopReason` acknowledgement. If no response arrives within the window, `_read_message` raises `AcpError("Cancel grace window exceeded; agent unresponsive")`. This preserves the escape hatch for broken agents without sabotaging cooperative cancels. The idle reader reads with this check off (`check_cancel=False`): it starts only after the cancelled turn has ended.

`_cancel_ts` is set to `time.monotonic()` inside `cancel_session()`.

### Tool-Interruption Auto-Complete

kiro-cli's built-in security filter can cancel tool calls before they execute (e.g.
when a bash command contains sensitive keywords).  When this happens kiro-cli emits an
`agent_message_chunk` with the exact text
`Tool uses were interrupted, waiting for the next user prompt` **and then goes idle
without sending a `session/prompt` response**.  Without special handling the caller
would wait for the full 2-hour prompt timeout.

All three prompt paths (`send_message_stream`, `_dispatch_events`, `_read_prompt_response`)
detect this marker (exact stripped match, not substring, to avoid false positives when
the model quotes the text in prose) and complete the turn immediately — `_dispatch_events`
also synthesizes a final `EVENT_COMPLETE` so dashboard and CLI callers using
`stream_events` exit cleanly.  The text itself is still yielded so the user sees what
happened, and a `tool_interrupted`-tagged SEL audit event is written for the security
log since kiro-cli's cancellation is a permission decision outside Kiro Crew's control.

### Stale-turn gate (`AcpClient`)

After text has streamed (`_stale_eligible`), a turn whose stdout+stderr fall silent for `_STALE_TURN_TIMEOUT` (90s) is a candidate for "treat as complete". A bare wall-clock reap would false-positive on a genuinely-working-but-quiet backend (a long model generation, or a spawned build emitting nothing to the pipe), ending the turn and losing all subsequent output — the *capture*-side analogue of the same blunt-timeout defect the runtime path guards against for tool-stall. `AcpClient` **oracle-gates** the reap, converging onto the same `LivenessOracle` (`acp/liveness.py`) contract the shared-runtime path uses: on every silent read while `_stale_eligible`, `_consult_liveness_model_wait()` calls `oracle.check_model_wait(self._pid)` (offloaded to `subprocess_executor()` under a 10s `wait_for`; degrades to `VERDICT_UNKNOWN` on any error — fail toward reaping). Consulting on **every** silent read, not only at the 90s mark, is required: the oracle needs a prior sample to compute a CPU/IO movement delta, so with readable counters a fresh oracle's first *submitted* consult returns `UNKNOWN`/`"sampling"` and a single consult at the cutoff would always reap. A missing runtime PID or unreadable counters also return `UNKNOWN`, each with its own evidence string.

The submitted future is tracked on the client, and polls while it is unfinished return `UNKNOWN` without submitting another job — so a wedged walk does not submit a fresh worker on every silent read. It stays tracked until it finishes **or the next liveness-state boundary retires it**, whichever comes first; a still-pending walk is deliberately detached at a boundary rather than waited on. The residual executor-occupancy bound is therefore at most one abandoned worker per boundary — turn start or process reset — rather than one per silent read: a pathological loop of turns against a permanently wedged `/proc` read can still occupy `subprocess_executor()` workers, which teardown (`_get_child_pids`) also uses. Eliminating that entirely needs a killable per-walk process or a dedicated liveness bulkhead, neither of which this gate attempts.

Both boundaries that drop a movement baseline — turn start in `_prompt_loop()` and `_reset_state()` — **retire** the liveness state through `_retire_liveness_state()`, which releases the tracked consult future AND swaps in a fresh oracle via `LivenessOracle.fresh()` (`fresh()` rather than a default construction, so an injected `/proc` root or sampling interval survives the swap). The two must retire together: replacing only the oracle would leave a walk wedged during the previous turn answering every later poll with `"prior consult still in flight"`, so the new turn would never sample its own process and the 90s cutoff would complete it early. Clearing in place is not sufficient either — a consult detached by a timeout keeps a bound reference to the instance it was submitted with, and samples are keyed without a PID, so a late write would repopulate the live baseline after that baseline was taken; since any nonzero delta counts as movement, that reads `WORKING` for a flat turn and defers its reap. Retiring confines a late writer to an instance nobody reads, which is what makes the `"sampling"` behaviour above hold. Retirement sits inside `_prompt_loop()` immediately after `_turn_lock` is acquired, which is load-bearing twice over: it is the single point every prompt path funnels through (`send_message` via `_read_prompt_response`, `send_message_stream`, and `_dispatch_events`), so no public prompt API is left carrying the previous turn's walk; and doing it under the lock stops a queued turn from clearing the *active* turn's tracked consult and thereby allowing a second walk while the first is still pending.

A retired walk that fails afterwards has its exception consumed via a done-callback attached at submission, so an ordinary probe failure is not reported as an unhandled-asyncio crash. Past the cutoff, **only `VERDICT_WORKING`** (moving CPU/IO in the backend subprocess subtree) defers the turn (loop continues); every other verdict (`DEAD`/`UNKNOWN`/`STUCK_INPUT`) preserves the prior end-the-turn behavior, so hang recovery is never weakened — a genuinely dead turn still ends, bounded by the resolved prompt timeout (`_DEFAULT_PROMPT_TIMEOUT`, 4h — raised alongside `agent.chat_turn_timeout_secs` via `resolve_prompt_timeout`) and the tool-stall watchdog below. Unlike the runtime path's `session/cancel` probe, the `AcpClient` reap is a plain `return` (process-per-session: the turn simply completes; no shared runtime to protect).

The compatibility reap emits `EVENT_COMPLETE` with `stop_reason=end_turn` so
existing consumers finalize normally, but also sets `synthetic_completion=true`.
The provenance distinguishes it from the provider's genuine `end_turn` result;
accounting consumers must reject the synthetic form.

### Tool-stall watchdog

A JSONL tool result retires its matching active call before dispatch yields the
result, even though it carries no `tool_status`. The scan
(`_read_new_tool_results_sync`) reads the file through
`jsonl_util.bounded_raw_records_with_offsets`, so one record's memory is capped
at `RECORD_CAP` and the scan resumes from a byte offset. It saves its offset past
each complete line before parsing it, and decodes each line on its own with
`errors="replace"`, so a line it cannot use costs only that line and one bad byte
cannot hold the offset in place for the rest of the session. Skipped: a record
over the cap (the offset still moves past it), a line that is not a JSON object
(malformed, nested past the decoder's ceiling), a `data`, `content` or item of
the wrong type, a result without a non-empty string `toolUseId`, a `stdout` that
is not a string, and any line whose handling raises. An unterminated last record
is left for the next scan. All JSONL flush paths update the
watchdog state on the event loop after the file read: remaining calls keep the
tool watchdog armed; the final result disarms it and re-arms stale recovery.

While a turn is dispatching, both ACP transports run a watchdog over a turn gone silent after a tool was dispatched — and both **recover** rather than just `return` on a dead turn (`AcpClient` keeps the blanket `_TOOL_STALL_TIMEOUT` window; the session handle is verdict-driven, below):

- **`AcpClient`** (process-per-session, `_TOOL_STALL_TIMEOUT = 600s`): the stall clock is measured against `_tool_last_seen = max(last_data_ts, self._last_activity)`, so tools that keepalive-ping without emitting stdout frames (`wait`, `spawn_sub_agents`) don't trip a false stall (`_last_activity` is refreshed out of band by the stderr drain / keepalive). On a real stall it `_kill_process(force=True)` and raises `AcpProcessDied`, routing through the existing pipe-death recovery (dashboard resets the session + re-queues, bounded by `_acp_pipe_death_retries`; cron/other callers get a clean error instead of a wedged slot). `_kill_process` only touches the subprocess/pipes (never `_turn_lock`), and blast radius is one session — each `AcpClient` owns exactly one process.
- **`runtime.py` / `AcpSessionHandle`**: watchdogs are **verdict-driven, not timeout-driven**; see "Session-handle watchdog" below.

#### Session-handle watchdog

| Setting | Default | Governs |
|---|---|---|
| `watchdog.check_after_secs` | 60s | idle time before the oracle is consulted |
| `watchdog.stale_window_secs` | 600s | stale probe for an UNKNOWN verdict |
| `watchdog.model_silent_probe_secs` | 1800s | stale window when the evidence is `established_flat` |
| `watchdog.tool_stall_suspect_secs` | 5400s (90 min) | tool cancel for an UNKNOWN tool verdict |
| `watchdog.tool_stall_hard_cap_secs` | 7200s (2h) | hard cap, UNKNOWN only; also bounds per-agent overrides |

watchdogs are **verdict-driven, not timeout-driven**, which avoids the failure mode of a pure timeout death detector: killing healthy-but-slow work (a silent 30-min redirected build `long-build > build.log 2>&1` at exactly the blanket window; healthy long non-streamed reasoning at 90s, where the destructive `session/cancel` probe would be acked by the LIVE turn and surfaced as "Turn cancelled by user").

Once a turn is idle past `watchdog.check_after_secs` (60s), the per-session `LivenessOracle` (`acp/liveness.py`) returns a verdict with evidence: **WORKING** (a live cmdline-matched shell child, a kirocrew-core `wait` call inside its declared duration (at most `WAIT_TOOL_MAX_SECS`, the tool's own 1800s maximum) + slack, selected by the provenance-verified adapter identity (`ToolCallState.is_trusted_wait`) and never by the model-authored title, moving CPU/IO counters, backend socket bytes flowing) is never acted on at any elapsed time, with one exception: an opaque MCP tool's `mcp subtree active` reading (any CPU/IO movement in the runtime's tree, which a warm sibling MCP server also produces, so it cannot tell a live call from one whose result frame was lost) is bounded by `watchdog.tool_stall_hard_cap_secs` on the session's own frame clock and then gets the same session-scoped tool-stall cancel as an UNKNOWN past the cap; its metric point carries `window=working_cap`; the kirocrew-core `spawn_sub_agents`, which pings `/api/session-keepalive` for its whole run (matched by `ToolCallState.is_trusted_keepalive_tool` on the adapter identity) keeps the unbounded deferral (logged at most once per 10 min — at INFO below the escalation mark, which is the lower of 30 min and a quarter of this turn's deadline, and at WARNING past it so a deferral able to hold the turn to its ceiling is visible at the default `agent.log_level`); **DEAD** (tracked shell child exited without a result frame past a 15s grace; model-wait with flat counters and NO established backend socket — the done-but-lost-frame wedge signature) acts immediately, so recovery lands seconds after actual death instead of at a blanket window; **STUCK_INPUT** (matched subtree flat across samples with a process blocked reading a tty/stdin pipe) acts immediately with a cause the recovery nudge names; **UNKNOWN** is the only timeout-governed class — stale probe at `watchdog.stale_window_secs` (600s; extended to `watchdog.model_silent_probe_secs` = 1800s when the evidence is `established_flat`, i.e. probably a non-streamed server-side think), tool cancel at `watchdog.tool_stall_suspect_secs` (5400s / 90 min — clears every shipped budget a single tool call can legitimately spend silent, such as the task runner's 90-minute test command), hard-capped at `watchdog.tool_stall_hard_cap_secs` (7200s / 2h, UNKNOWN and opaque-MCP WORKING; also bounds the per-agent overrides).

The oracle's evidence is Linux `/proc` where it exists; on macOS (no procfs) it selects an in-process **libproc backend** once per oracle instance (`select_darwin_backend`, injectable for tests): `proc_listchildpids` enumerates the runtime's descendants, `PROC_PIDTBSDINFO` supplies ppid / zombie state / start time, `proc_pidpath` and `sysctl KERN_PROCARGS2` supply the executable and argv for the same cmdline match the `/proc` walk performs, and `PROC_PIDTASKINFO` supplies per-process CPU time summed over the subtree (evidence labelled `darwin cpu-only`, since IO bytes are not readable there). That counter is in Mach ticks, scaled to nanoseconds by `hw.tbfrequency` (the two differ on Apple Silicon; the sysctl is read rather than `mach_timebase_info`, which Rosetta 2 answers 1/1 for an x86_64 process), and a growing sum counts as movement only from one 10 ms Linux tick per sample (`_DARWIN_CPU_FLOOR_NANOS`), so the idle timers of the runtime's MCP servers read flat, as `/proc`'s whole ticks read them. So a shell command is WORKING/DEAD/`shell_child_absent` on macOS by the same rules as Linux, and an active MCP subtree reads WORKING instead of running out the suspect window; evidence only `/proc` carries — the `established_flat` socket tag, the `blocked_read_fd` STUCK_INPUT check, `wchan` — is never invented on macOS: a flat model wait is UNKNOWN tagged `platform_limited` (never `DEAD`), which the dispatch loop gives the same `model_silent_probe_secs` window as `established_flat`, since without socket evidence a non-streamed think and a wedge are one state, and a live tracked shell child whose subtree is flat is UNKNOWN tagged `platform_limited` — bounded by the standard no-progress budget — rather than WORKING, because without stdin-block evidence a stuck child and a quiet one are one state and "alive" must not buy an indefinite deferral (see "Platform evidence matrix" below).

Dispatch stamps on darwin are wall-clock (`time.time()`), the clock libproc dates processes on, and a wall clock can step: a backward NTP or VM-resume correction between the dispatch stamp and the runtime's fork dates a live child before its own dispatch. The stamp is therefore paired with a steady one (`steady_now()`, darwin `CLOCK_MONOTONIC`, which counts sleep), and when wall elapsed and steady elapsed disagree by more than the attribution tolerance the oracle declines to attribute by start time at all — every row reads as possibly this tool's, so a matched child stays WORKING and no `shell_child_absent` claim is made. A missing steady stamp gets the same fail-open answer.

**Windows has no tree backend yet**: the oracle there reads only the runtime's own CPU time (`proc_cpu_nanos_for_pid` via `GetProcessTimes`, root pid only), so every shell and MCP tool call stays UNKNOWN — tagged `platform_limited` so the degradation is visible in the evidence and the metric bucket — and the 90-minute suspect window is the effective tool timeout on that platform (narrowed to the ordinary silence window when the command was classified prompt-shaped, see "Interactive-command policy" below) — a genuinely hung non-interactive tool holds its slot for that long. The trade is accepted rather than sized around: a Toolhelp-based descendant walk is the follow-up that closes it, the same way the darwin backend did for macOS.

Four refinements keep the build-scale tool forbearance from sheltering an **LLM-shaped** stall (a model turn riding inside a tool, e.g. kiro-cli `use_subagent`, whose longest legitimate silent gap is minutes) or an **already-finished** one: (1) the oracle tags an UNKNOWN tool verdict with `established_flat` when the subtree's counters are genuinely flat (a real two-sample delta, not the baseline tick) AND the **runtime process itself** holds an established backend socket — deliberately narrower than the model-wait branch's whole-tree socket scan, so an MCP server's own remote call is not read as a model wait (refinement (4) handles that shape separately) — and the tool branch then uses `min(model_silent_probe_secs, tool_stall_suspect_secs)` as the effective suspect window; plain flat-subtree evidence keeps the full window, and under the OS sandbox (pid = launcher parent, no sockets on it) the tag never fires, failing toward the long build-safe window.

(2) the never-matched SHELL fork is split instead of uniformly forgiven: `no matching shell child` conflated a command that already exited — a sub-second `ls | grep | wc` whose result frame was lost is never observed alive, so the DEAD branch's 15s exit grace can never fire for it — with one running unrecognized; undifferentiated, both would get the same `tool_stall_suspect_secs` window (5400s / 90 min). The oracle tags the first case `shell_child_absent` when the runtime's descendant tree is OBSERVABLE (a readable `/proc/<pid>/task/<tid>/children`, empty or not) and holds no live descendant attributable to this dispatch, and the tool branch then uses `min(stale_window_secs, tool_stall_suspect_secs)` — the ordinary silence budget — instead of the build-scale one. Attribution compares a descendant's `starttime` against a `CLOCK_BOOTTIME` stamp taken at the tool_call frame and widened by the turn's banked consumer parking (`_parked_total`), because `/proc` dates processes on a clock that counts suspended time while `time.monotonic()` does not, and the stamp is taken when the frame is PROCESSED rather than when the runtime spawned (a frame queued behind an approval is stamped that late). Four states each keep the full window, so every unattributable one fails toward build-scale patience: a descendant young enough to be this dispatch's, one whose cmdline matches while predating the stamp (indistinguishable from a coincidental lookalike), an unreadable child list, and a missing stamp or tick rate (no `os.sysconf` off Linux). The verdict stays UNKNOWN, never DEAD — absence is inferred, so it only shortens the non-lethal cancel.

(3) An agent definition can override the windows per agent (`agents.<name>.watchdog_tool_stall_suspect_secs` / `watchdog_tool_stall_hard_cap_secs`, 0 = inherit the global — the same empty-inherits convention as the agent's `model`), applied in the `WatchdogSettings` snapshot at handle construction (`_load_watchdog_settings(crew_agent)` — a direct lookup on the CANONICAL crew name, resolved by the surface that owns the identity: the dashboard passes the slot member explicitly through `get_or_create(crew_agent=...)`, and crew-name-passing surfaces (Slack threads, cron, spawned agents) are covered by the provider factory's crew-namespace membership fallback; the identity is plumbed provider → runtime → handle, and a warm-pool claim rebinds the live handle via `rebind_watchdog()` so it travels with the SESSION, not the pool key — a name that is not a crew key simply inherits the global) so a pure-LLM agent like a PR reviewer can declare minutes-scale windows without touching the global build budget; an override is bounded by the same load-time ceiling clamp as the global windows, so it cannot smuggle a window past the prompt timeout.

(4) An opaque MCP tool whose subtree is genuinely flat while the in-flight tool's OWN MCP server holds an established TCP connection to a non-loopback peer is tagged `remote_flat` — the shape of a tool blocked on its own remote call, which with no client timeout holds the call until the build-scale window. "Own server" is the server the call names in its trusted `_meta.kiro.mcpServerName` (`ToolCallState.mcp_server_name`, empty and therefore never tagged when the identity is not provenance-verified), and only that server's process subtree is scanned, so another server's persistent connection (a keepalive, `mcp-remote`) or a shell tool's background tunnel elsewhere in the tree cannot tag a quiet tool of a different server. The server process is found from how the session launched it: the runtime hands the handle the agent spec and the final `mcpServers` array it already read for the session (`AcpSessionHandle.record_mcp_launches`, beside the unresolved-ref guard on both `create_session` and `load_session`, in memory, so no scheduling point), `session_handle.mcp_launch_roster` keeps each stdio server's `(command, *args)` from both sources (bounded where it is retained: the session report's `BUCKET_CAP` servers and `NAME_CAP` name length, `_LAUNCH_MAX_ARGS` arguments, `_LAUNCH_MAX_TOKEN` characters per string and `_LAUNCHES_PER_SERVER` launches per name, an entry past any bound refused whole and the refusals logged once per recording), and `liveness.launch_matches` recognizes a process whose argv is that launch exactly, the same program reached by another path, or an interpreter running it as a `#!` script, each with exactly the declared args. The topmost match below the runtime is the server (a launcher that starts a versioned copy of itself as its child matches twice, and the child sits in its subtree), and the oracle declines unless there is exactly ONE such match that fits no other declared server: a second copy is another session's or a subagent's server on a shared runtime, and which copy serves this call cannot be read from the tree. A launcher that re-executes under a different argv, a server the host mounted from a source the roster does not read, and a remote (`url`) server are never found, which keeps the full window. The handle declares the roster (`server_launches=AcpSessionHandle._remote_flat_launches`) only while `watchdog.remote_flat_probe_secs` is above 0, so with the key off the tag, its metric bucket and the window never appear; the roster is read at verdict time, so a resumed session's re-declared servers govern the next probe. The runtime pid is never a server and, when that pid holds no socket at all (the Linux namespace sandbox's launcher parent), neither are its direct children, so kiro-cli's own model connection never counts, and a loopback peer (Kiro Crew's gateway, which in-tree MCP servers such as `spawn_sub_agents` block on for their whole run) never counts either; on macOS the process view is the libproc backend's argv rows and the socket view is `proc_pidinfo(PROC_PIDLISTFDS)` + `proc_pidfdinfo(PROC_PIDFDSOCKETINFO)` (`darwin_established_tcp_count`, refused on any fill size other than the 792-byte `socket_fdinfo`), and Windows has no socket view so the tag never fires there. The tool branch then uses `min(watchdog.remote_flat_probe_secs, tool_stall_suspect_secs)` when the key is above 0. It is **off by default** (0) and 900s is the suggested opt-in value; turning it on by default is a separate decision, and ships as a new default value so the key keeps meaning the same number of seconds for an operator who already set it. That value is shorter than `model_silent_probe_secs` (1800s) on purpose: that window is sized for a documented shape, kiro-cli's own non-streamed extended-effort think, while a flat tool waiting on a remote peer has no such contract and is most often a call with no client timeout; a deployment whose MCP tools legitimately block longer (a long-poll API, a non-streaming model behind a third-party server) raises the key, and the recovery on expiry is the non-lethal continue-nudge, not a lost turn. The window is measured from the later of the last own frame and the last probe that read the tree as WORKING, so a slow stream that moves bytes between flat samples keeps the full window; the full suspect window still applies as an upper bound. Every idle window is bounded at load by the resolved prompt timeout (`resolve_prompt_timeout` — the one deadline every caller shares; 14400s default, following a raised `agent.chat_turn_timeout_secs`) minus 10% headroom for the cancel + ack grace, and an over-ceiling on-disk value is clamped with a warning: a window at or past the deadline makes the UNKNOWN class unreachable, because the turn's timeout fires first and the user gets the generic turn-limit card instead of the tool-stall recovery below. A window above the DASHBOARD ceiling (`agent.chat_turn_timeout_secs`) is reported but **not** clamped — the same handle serves callers that pass their own larger prompt timeout, and shrinking their windows would cancel live work. **Every watchdog action is non-lethal:** a stale probe's cancel-ack is reclassified in the turn-complete branch (`_stale_probe` + `stopReason==cancelled` → `STOP_REASON_STALE_RECOVER`; the flag is single-shot — consumed on reclassification and superseded by a genuine `cancel()`, so a user cancel arriving after a probe is never misattributed to auto-recovery) so the dashboard auto-recovers instead of logging a user cancellation — an oracle mistake costs a regeneration, never a session. A tool stall ends the turn with `STOP_REASON_TOOL_STALL` (`"error: tool stall"`, in the `error:` family so branch-less callers degrade to generic handling) carrying the tool title / redacted command / evidence on the terminal `AcpEvent`; `_turn_stop_recovery`'s (`chat_turn/stop_recovery.py`) dedicated branch queues a **continue-nudge** (`build_tool_stall_recovery_prompt` — check partial results, tail any `> file` redirect target, re-run non-interactively on STUCK_INPUT) instead of the legacy verbatim re-queue of the original user message (which restarted the whole task and re-ran the very command that stalled), charged against a separate `slot._tool_stall_retries` budget (3) so a stall never burns the pipe-death reconnect budget. The runtime is **shared** (multiple sessions multiplexed on one process), so recovery is always `session/cancel` for **this `sessionId` only** (bounded by `asyncio.wait_for(..., 5s)`); siblings keep running. `watchdog.*` config is snapshotted at handle construction (`WatchdogSettings`); the dispatch loop never reads config. The snapshot is **re-bound on a config reload**, so a live handle does not keep boot's windows until its turn ends: `SessionManager._rebind_live_watchdogs(cfg)` fires when a reload touches `watchdog.*`, `agent.chat_turn_timeout_secs`, or any `agents.<name>.watchdog_*` key, walks every registered session's provider (`_watchdog_handle_of` resolves both shapes — `AcpSessionProvider._handle` and `AcpProvider._client._handle`) and calls `handle.rebind_watchdog(crew, _load_watchdog_settings(crew, cfg=cfg))`. It re-runs the loader for the handle's OWN crew identity rather than copying raw seconds across, so the per-agent override overlay and the prompt-timeout ceiling clamp above are re-applied per handle — a raised `agent.chat_turn_timeout_secs` lifts the ceiling that was clamping a window, and a lowered one re-clamps it. The config is the one the watcher already loaded, so the fan-out touches no disk on the loop, and the dispatch loop reads the snapshot every tick, so the new windows govern the next check. A handle that raises is skipped and logged at DEBUG; the rest still rebind.

The tool watchdog remains armed while any dispatched tool-call ID is in flight.
Only a terminal result removes that ID; partial output and interleaved model text
do not arm model-wait recovery. After the last call finishes, model-wait recovery
is armed even if the model never sends another text chunk. The shared handle
retains each live call's attribution so either completion order leaves the
watchdog inspecting a surviving call.

**Both idle clocks measure BACKEND silence, so consumer time is subtracted from them.** `_dispatch_events` is an async generator: it is suspended at its `yield` for the whole of a consumer-side await (a tool approval, an IM send, a hook), and `last_data_ts` does not advance while suspended. Charging that interval to the runtime lets the arm cancel a turn moments *after* a human approves a tool — and at that instant the tool has not started, so the oracle draws `UNKNOWN` or `DEAD`, and `DEAD` acts immediately regardless of the window. `prompt()` therefore times each park around its single re-yield (`_parked_since` → `_parked_total`, cleared in a `finally` so an abandoned generator does not read as parked forever), and the timeout arm subtracts the park accumulated since `last_data_ts` was taken. The tool clock is exact; the stale clock can key off the newer stderr/keepalive activity, in which case part of the correction predates its reference point and is subtracted twice — which only makes that branch more patient, never quicker to probe.

**On the shared runtime the tool clock is also SESSION-SCOPED.** `AcpRuntime._reader_loop` marks a frame `fanout_no_owner` when it fans an ownerless frame (no `sessionId`) out to more than one registered session — a lone session is the sole owner and stays unmarked, so a single-session runtime behaves exactly as before. The dispatch loop advances a session-attributable twin of the pair, `last_own_data_ts` / `parked_at_own_data`, only for an unmarked frame, and both the **tool-idle clock** and the **post-compaction-failure budget** read that twin: a co-tenant's roster broadcast (`_kiro.dev/subagent/list_update`) does not defer either on traffic this session never produced. The tool-idle clock also takes the session's own keepalive stamp, `AcpSessionHandle.note_keepalive()`, which `AcpSessionProvider.touch_activity()` sets from `/api/session-keepalive` — the ping `wait` and `spawn_sub_agents` send while they block, since a blocking tool emits no frame. A co-tenant's keepalive stamps only its own handle, so it never defers this session. The stamp records the consumer park accumulated up to the touch, an open park included, so the clock subtracts only park time after it and stays exact. The **stale** clock deliberately keeps reading `last_data_ts`, because it already folds in the runtime-wide `_last_activity` (bumped on every stdout line), so runtime-global traffic is inside its contract by construction. Provenance, not the frame's method, is the discriminator — the same notification kind can arrive routed (this session's own progress) or fanned out (a co-tenant's), and only the runtime knows which. The events a marked frame yields carry that provenance to the consumer as `AcpEvent.runtime_global` — the roster, the compaction, clear and agent-switch notices, the steer echoes, and (by the stricter must-name-this-session test) the MCP registration notifications — so a consumer that measures this session's own activity or its startup can tell a co-tenant's frame from its own. A `session/update` names its session by protocol and is routed, so its events leave the flag clear. The one remaining over-count is deliberate and bounded: the tool branch's TOCTOU guard cannot know the owner of a frame that arrives *during* the oracle await (it is not dequeued yet), so it advances both clocks and defers by a single tick, the same fail-safe trade `_ingress_seq` documents at its increment. A keepalive that lands during the await voids the verdict the same way; it is attributed to this session, so it moves only the tool clock, to the touch's stamp.

**The turn's park is readable from outside the turn.** `parked_for_secs()`, `parked_since`, and `awaiting_permission` exist because this arm cannot report on itself: it only advances when a consumer pulls the generator, so a consumer-side await freezes it and it never executes again for that turn. `session.md`'s `stuck_turn` hook reads those accessors from a loop with its own timer. Answering a permission calls `_end_human_wait()`, which banks the human's thinking time into `_parked_total` and restarts `_parked_since`, so the in-band correction stays exact while the external reading counts only what the consumer itself has spent since the answer.

Both transports offload the oracle consult to `subprocess_executor()`, so both carry the same two obligations, and both discharge them through ONE shared guard — `liveness.consult_offloaded()`, which owns the prior-future check, the in-try submission, the submission-time exception callback, the shielded bounded await, and the degrade-to-UNKNOWN arm, so a fix to that sequence lands at both call sites at once (each caller keeps only which oracle check runs and where its tracked future lives). **One outstanding walk per liveness generation:** `_consult_oracle_offloaded()` tracks the submitted future and answers `UNKNOWN`/`"prior consult still in flight"` on any tick that finds it unfinished, so a `/proc` read wedged on a stuck fd does not add a blocked worker every `check_after_secs` to the pool teardown's `_get_child_pids` also draws from. The no-in-flight-tool answer is resolved *before* that guard, because it is pure handle state and needs no worker. Its exception is retrieved via a callback attached at submission — not in an `except Exception` arm, which `CancelledError` (a `BaseException`) would skip — so a probe that fails after its awaiter left is not recorded as an unhandled-asyncio crash. **Retire, don't `reset()`:** turn start in `prompt()` and every new tool dispatch call `_retire_liveness_state()`, releasing the tracked future *together with* the oracle (`LivenessOracle.fresh()`, so the per-session `wellness_sample_secs` survives). Splitting them either way is a defect: clearing the oracle in place leaves a detached walk writing into the live baseline (samples are keyed without a PID, and any nonzero delta counts as movement), while replacing only the oracle leaves a walk wedged in the previous generation answering every later tick "still in flight" so the new generation never samples its own process. The tool path has a sharper version of the first hazard than the capture path does: a walk carrying the *previous* tool's `ToolCallState` matches a descendant of the previous command and stores it as `_tracked_child`, after which `_check_shell_child` reports `WORKING "shell child N alive"` for the new tool against an unrelated process. Retirement is not a change to the cross-tick tracked-child contract itself — `fresh()` starts in exactly the state `reset()` produced, and the consult binds `self._oracle` at submission, so ticks after a boundary accumulate on the new instance as before.

**Before adding an await to a consumer branch**, read
`../../architecture/design-notes/tool-stall-watchdog-placement.md`. Both
watchdogs above are inside the generator, so a new consumer-side await silently
widens the class of failure neither of them can see; the note records which
failure classes are detectable here and which must be judged out of band.

**The in-flight bookkeeping both clocks read is bounded, and an eviction is the one case where it stops proving anything.** Every structure that holds a turn's calls with no terminal result yet -- the session handle's attribution map (`InFlightToolTracker`, its `ToolCallState` rows and the aux `InteractiveClassification` beside each), its output-seen set, and the `AcpClient`'s open-call set (`_dispatch.InFlightIdSet`) -- holds at most `MAX_ACTIVE_TOOL_CALLS` entries, the provenance caches' count bound by name, under one key (`_dispatch.liveness_key`: a `toolCallId` within `TOOL_CALL_CACHE_MAX_KEY_CHARS` as it is, a longer one or one spelling the `sha256:` prefix as its digest). Each retained string is cut at the point of retention (`MAX_RETAINED_TITLE_CHARS`, `MAX_RETAINED_NAME_CHARS`, `MAX_RETAINED_COMMAND_CHARS` keeping a long command's head and tail so the stall nudge's `> file` redirect survives, the same cut on the classifier's git editor hint); the output-seen set admits only a call the map still holds. At the cap the oldest entry is evicted and the call just dispatched is kept, so the judged call stays the one actually in flight. **After an eviction the turn is judged as an opaque tool in flight until it resets:** the evicted call may still be running, so an empty map does not arm the stale clock (`_no_tool_in_flight()`, `InFlightIdSet.possibly_open`) and the tool clock stays armed. On the handle the oracle then has no state to read and answers UNKNOWN, so a turn that wedges past the cap ends by the non-lethal tool-stall recovery after the full build-scale window (`tool_stall_suspect_secs` under `tool_stall_hard_cap_secs`), the same forbearance any opaque in-flight tool gets, never by the quicker stale probe; on `AcpClient` the blanket `_TOOL_STALL_TIMEOUT` applies. The cost -- that window instead of the stale window for one runaway turn -- is accepted because a turn cannot legitimately hold that many open calls. The overflow is counted and said once per turn: the next turn's reset warns with the bound and how many entries the turn dropped.

### Platform evidence matrix (declared degradation)

The oracle's evidence differs per host, and every gap is DECLARED rather than
inferred: an absent row never yields `DEAD` or `STUCK_INPUT`, and never yields
`WORKING` on process liveness alone. The table is the contract the module
docstring of `acp/liveness.py` carries and `test_acp_liveness*.py` pins
(RFC `rfc-overload-resilience.md` §14.9).

| Evidence | Linux (`/proc`) | macOS (`libproc`) | Windows (no tree backend) | Declared degradation |
|---|---|---|---|---|
| process tree | `task/*/children` (`iter_descendants`) | `proc_listchildpids` (`LibprocBackend`) | absent | Windows: every shell/MCP tool verdict is `UNKNOWN` tagged `platform_limited`, bounded by the no-progress budget |
| shell child match / exit | cmdline + `starttime` | argv/path + start time | absent | same |
| subtree movement | CPU jiffies + IO bytes | CPU ns only, at least one 10 ms tick per sample | root-pid CPU (model-wait probe only) | a moving subtree is `WORKING` on any host: "no output" alone never kills a task whose CPU/IO moves |
| blocked on stdin (`STUCK_INPUT`) | `wchan` + blocked fd; a stdin pipe reader whose pipe has a live writer in the watched subtree (`producer \| consumer`) is a producer wait, not `STUCK_INPUT` (procfs only) | absent | absent | a live tracked shell child whose subtree is FLAT (real two-sample delta) is `UNKNOWN` tagged `platform_limited`, never `WORKING` — "alive" is never sufficient for indefinite deferral |
| socket / LLM wait (`established_flat`) | `/proc/net` established → `UNKNOWN` or `DEAD` | absent → `UNKNOWN` tagged `platform_limited`, never `DEAD`; probed past `model_silent_probe_secs` | absent | same |
| socket / remote call (`remote_flat`) | fd inodes + `/proc/net` established, non-loopback peer, in the in-flight tool's own server subtree → `UNKNOWN` tagged | `PROC_PIDFDSOCKETINFO` established, non-loopback peer, in the in-flight tool's own server subtree → `UNKNOWN` tagged | absent → never tagged | same |
| business progress | stream events, `kirocrew/status` | same | same | platform-independent; outranks every row above |

Consequences in the dispatch loop: `platform_limited` is an `UNKNOWN`, so
the standard bounded budget governs it — `tool_stall_suspect_secs` capped by
`tool_stall_hard_cap_secs` — exactly as for any untagged `UNKNOWN`. The one
narrowing keyed on it is conditional on the tool layer: when the dispatched
shell command was classified prompt-shaped (below), the window narrows to
`stale_window_secs`, because that is the case the missing stdin evidence would
have answered. The tag has its own closed metric bucket
(`evidence_class=platform_limited` on `kirocrew.watchdog.action`) so the
degradation is visible in telemetry rather than folded into `degraded`.

### Interactive-command policy (W4 `waiting_input`)

The tool layer classifies a SHELL command **before** it runs
(`liveness.classify_interactive_command`, keyed on the trusted
`AcpEvent.shell_command`, never the LLM-authored title). The classifier is a
table of known programs, not a rewrite of arbitrary flags: pagers (`less`,
`man`, `git log|diff|show|blame|…` without `-P`/`--no-pager` and with a
terminal stdout), editors (`vim`, `nano`, `git commit` without `-m`, `git
rebase -i`, a git sequencer `--continue` (`rebase`/`cherry-pick`/`merge`/`revert`) without `--no-edit`, and `git revert` without `--no-edit`/`-n`/`--no-commit`; a command whose `GIT_EDITOR`/`core.editor` is `true` or `:`, or whose `GIT_SEQUENCE_EDITOR`/`sequence.editor` is set for `rebase -i`, is not treated as an editor), REPLs that read stdin when given nothing to run (`python`,
`node`, `psql`, `cat`, …), package-manager confirmations (`apt-get install`
without `-y`, `pacman -S` without `--noconfirm`, `pip uninstall`, `npm init`,
…) and credential prompts (`sudo` without `-n`, `ssh`/`scp` without
`BatchMode=yes`, `gpg` without `--batch`, `docker login`, `gh auth login`, `aws
configure`). It yields an `InteractiveClassification` — `risk`, `program`,
`reason`, a non-interactive **hint**, `side_effecting` — carried on
`ToolCallState.interactive_risk` and `AcpSessionHandle.inflight_interactive`.
A pager is advisory only (under the tool, stdout is a pipe, so a pager degrades
to `cat` on its own); the other four classes are `INTERACTIVE_NARROWING_RISKS`.

The classifier never rewrites the command. The repo has no environment layer on
the ACP tool path — the hook gate (`ToolHookResult`) can only allow or deny, and
the `GIT_PAGER=cat` layer in `dashboard/terminal_commands.py` serves the human
terminal — so `PAGER=cat` / `-y` / the ssh options are PROPOSED via the hint the
recovery nudge carries, not applied. Applying them at the harness is a
follow-up seam, not a matcher's job.

The ssh hint does not propose `BatchMode=yes`. The hint proposes `-o
StrictHostKeyChecking=yes -o NumberOfPasswordPrompts=0 -o
PasswordAuthentication=no -o KbdInteractiveAuthentication=no` instead, aimed at
the prompts ssh_config(5) says BatchMode disables: password prompts and host key
confirmation. The classifier still counts only `BatchMode` as proof that ssh will
not prompt. A command using the proposed options stays prompt-shaped, because a
FIDO/sk key PIN or an encrypted key's passphrase may still be asked for under
them, and the conservative reading keeps the shorter stall window.

**Post-stall classification.** When the tool branch acts, the stall is a wait
for input on exactly two grounds: the oracle's own `STUCK_INPUT` (Linux), or a
`platform_limited` no-progress verdict on a command whose classification is
narrowing-eligible. Either yields an `EVENT_STRUCTURED_STATUS` carrying a
`StructuredStatus{phase=waiting, wait_reason=waiting_input,
origin=liveness_oracle, cancellable=True, resumable=False, safe_retry}` **before**
any action, so a scheduler can release the lane slot on it; a flat build with
no interactive classification stays an opaque tool stall (no status). Then
`WatchdogSettings.interactive_command_policy` decides: `cancel` (the default —
today's non-lethal `session/cancel` + `STOP_REASON_TOOL_STALL`, whose terminal
carries the same `status`), or `wait` (keep the turn open for real input; the
blocked process is kept and its residency stays charged; the bound is the
turn's own ceiling, i.e. the task's `deadline_at` — the hard cap does not
cancel a declared input wait). Neither policy answers anything: no `yes`, no
Enter, no synthesized stdin. `safe_retry` is True only when the classifier's
read-only verdict holds AND the call streamed no output
(`_tool_output_seen`); a command that printed may have acted, so a
non-interactive retry is never proposed as safe, and any retry stays inside the
already-granted approval scope with the original parameters. The config key
behind the policy is `agent.interactive_command_policy` (`config/sections_agent.py`,
read by `_load_watchdog_settings`); the snapshot field is the seam.

### Structured status protocol (`kirocrew/status`, version 1)

Existing signals are reused as-is — `session/update` for tool-call
start/complete and agent text, `_kiro.dev/metadata` for the harness
`stopReason`, `_kiro.dev/subagent/list_update` for native sub-agent identity,
MCP `notifications/progress` routed per request by `mcp_gateway/backend_routing.py`.
None carries a wait reason or a resume condition, so one versioned extension is
added under `params._meta["kirocrew/status"]` (or the inner `update._meta`,
where kiro-cli carries `_meta.kiro`):

```text
"kirocrew/status": { version: 1,
  task_id, session_id, parent_id, tool_call_id, generation,
  phase: starting|running|waiting|recovering,
  wait_reason: waiting_input|waiting_permission|waiting_dependency|waiting_children|retry_wait   # iff waiting
  dependency_scope, retry_at (epoch secs),
  progress_source: tool_output|stream_event|checkpoint|process_evidence,
  cancellable, resumable, safe_retry,   # each defaults False: saying nothing grants nothing
  checkpoint_ref }
```

`StructuredStatus.from_meta` (`acp/types.py`) owns the shape and the version
gate: an unsupported `version` rejects the WHOLE frame (a newer schema is never
half-applied), an unknown `phase` or a non-vocabulary `wait_reason` rejects it,
a `wait_reason` outside the waiting phase is dropped, non-bool capabilities are
False, a non-int `generation` is 0, a non-finite `retry_at` is None, string
fields are bounded to 512 chars and unknown fields are ignored. The parsed
record is an `AcpEvent(kind=EVENT_STRUCTURED_STATUS, status=…)` and
`AcpEvent.status` also rides a tool-stall terminal (above).

**Origin rule** (`AcpSessionHandle._structured_status_event`): a status is
trusted only from the execution layer of the session it names. It is accepted
iff the frame was ROUTED to this session (`msg.fanout_no_owner` is False — an
ownerless frame fanned out to several co-tenants names no owner), the frame's
`sessionId` is this handle's (a child-routed frame is rejected as
`child_origin`: the native child's boundary is the parent, see below), the
frame is not a model-text frame (`agent_message_chunk` / `agent_thought_chunk`
— a wait is never created by anything arriving alongside prose), the
extension's `session_id` is empty or equal to this session's, and the version
gate passes. Model text that spells out the wire shape is text and nothing
else. Every rejection is counted per reason (`status_rejections`) and logged as
`status_rejected` once per reason per turn; an absent extension is silent. An
old harness that never sends the frame degrades to today's evidence — transport
heartbeat, process liveness, stream events — which the oracle already separates
from business progress.

**MCP side (not implemented).** Only the ACP `session/update` `kirocrew/status`
carrier is parsed. The MCP carrier — the same object as `_meta.kirocrew_status`
on a `notifications/progress` sibling for a tool's `progressToken` — is not
parsed. If it is added, the parser is shared (`StructuredStatus.from_meta` over
the `_meta` dict), and the origin check is "the routed `progressToken` belongs to
a tool call this session issued"; the gateway backend already routes that
notification to the requesting session. A tool result body containing the shape is content, never status.

**Consumers.** `providers/acp_turns.py` forwards `AcpEvent.status` unchanged. The main chat's tool-stall continuation (`_turn_stop_recovery` in `chat_turn/stop_recovery.py`) takes `stuck_input` from `status.wait_reason == waiting_input` on the terminal completion before the evidence-text marker; the sub-agent run loop yields its lane slot on an `EVENT_STRUCTURED_STATUS` frame with `waiting_input` (`WaitRecord.input(tool_call_id)`, residency kept — [subagent.md](subagent.md) § Typed input wait) and steers its recovery prompt from the same field.

### Harness-native subtasks: inventory and recovery boundary

Inventory of what each backend exposes for subtasks that run INSIDE the harness
(kiro-cli `use_subagent`, the Claude adapter's in-harness task tool), as
opposed to `spawn_run` / `spawn_sub_agents` tasks Kiro Crew manages
(RFC §14.8, parity row H16 in
[harness-parity.md](harness-parity.md); pinned by
`test_native_subagent_boundary.py`):

| Backend | Identity | Tool events | Cancel | Resume | Counting / scheduler treatment |
|---|---|---|---|---|---|
| kiro-cli (`ACP_BACKEND_KIRO`) `use_subagent` | yes — `_kiro.dev/subagent/list_update` roster (`sessionId`, `status`), and child-routed `session/update` / `_kiro.dev/session/update` frames carry the child `sessionId` (`AcpEvent.sub_session_id`) | attributable: child frames are parsed with the shared parser into origin-scoped caches and re-tagged `EVENT_SUBAGENT_ACTIVITY`; child approvals surface on the PARENT with `sub_session_id` set (`subagent_manager/run_turn.py` `_run_inner_impl` counts them separately) | parent only — `session/cancel` on the parent session takes every child | none independent of the parent | not a task row. `AcpSessionHandle.native_child_sessions` counts child ids seen this turn through `_note_native_child` at three execution-layer seams — the child-routed branch of `_handle_update`, the `_kiro.dev/session/update` child stream in `_dispatch_events`, and the `subagent_list` roster when this handle is its sole owner (`fanout_no_owner` False; a fanned-out roster is counted on nobody) — never from model text. No `HostBudget` charge, because a child lives inside the parent's already-charged runtime process; `report_native_children(budget)` reports the count as `uncharged["native_children"]`. Recovery boundary = the parent session. A child-routed `kirocrew/status` is rejected (`child_origin`), never re-attributed |
| KAS (`ACP_BACKEND_KAS`) | yes — `_meta.kiro.agentSubtaskId` / `pipeline` on parent `tool_call` frames (`_handle_kas_subagent`), roster keyed by `agentSubtaskId` | child nested tool frames emit an activity prefix and populate the caches | parent only | none | same boundary as kiro-cli; every roster entry (individual `agent-subtask` frame or pipeline stage) is counted through the same `_note_native_child` seam |
| Claude backend (`ACP_BACKEND_CLAUDE`) | yes — `AcpClient` declares `subagents`, so claude-agent-acp announces each child on the parent (`subagent_update`: the child `sessionId`, title, description and state) and sends the delegated prompt (`session_message`, whose `messageId` is the Agent call's `toolCallId`) and the child's own frames under the child's sessionId; read by `client_native_children` into the same roster shape the cards take | attributable: a child's tool call yields an `EVENT_SUBAGENT_ACTIVITY` prefix and then continues to the ordinary tool path, as a kiro-cli child's does on `AcpClient`; a child's text becomes `EVENT_SUBAGENT_ACTIVITY`, never the parent's text; its other frames are dropped | parent only — the adapter offers `capabilities.cancel` per child, and Kiro Crew does not send it | none | same boundary; the child ids and Agent-call links are kept under `NATIVE_CHILD_ROSTER_CAP` with the overflow counted and logged at the next turn; a child past the cap gets no card, its text is dropped and its tool calls render as ordinary rows. They are not yet reported as uncharged residency, and a resume naming one misses as an unknown conversation. On a session that declares nothing, the Task tool is an ordinary `tool_call` on the parent and the liveness oracle bounds the parent turn identically (`test_claude_backend_counts_no_children_but_oracle_still_bounds_the_parent`, `test_claude_native_subagent_cards.py`) |
| `spawn_run` / `spawn_sub_agents` (all backends) | task rows (`taskq`) | yes | per task | per task (continuable) | full scheduling. A `spawn_run` child that itself uses `use_subagent` is the boundary for ITS native grandchildren: they are counted on the child's handle only, never on the grandparent |

The minimal recovery boundary for every native subtask is therefore the parent
session: it is counted there, cancelled there, and recovered there. Concretely:

- **Counted, bounded.** `native_child_sessions` holds at most
  `NATIVE_CHILD_ROSTER_CAP` (4096) distinct ids per turn; ids past the cap are
  counted in `native_child_overflow`, not stored. Non-string, empty, over-long
  (>128) and own-session ids are ignored. Both reset at the top of every turn.
  That set is the SINGLE bound on every native-child store on the handle:
  `_note_native_child` **returns** whether the id is tracked after the call
  (True also for the ordinary re-report of a known id, so its row still
  updates), and the KAS display roster `_kas_subagent_roster` writes a row only
  for a True answer. A row the counted set does not hold could never be
  recognised as a duplicate, so it would reintroduce exactly the unbounded
  growth the cap refuses — and the parent's own id, refused by the count, must
  not render the session as a sub-agent of itself. The frame stays a PARENT
  sub-agent frame either way and still emits its `EVENT_SUBAGENT_LIST` (returning
  `None` would re-render it as an ordinary tool call). A row cap bounds memory
  only when the row does too, so every backend-authored display string STORED in
  a row (name, title, status) is clipped to `NATIVE_CHILD_LABEL_CAP` (512) at the
  write, not at a render site downstream.
- **Never charged.** `HostBudget.report_uncharged(kind, count, label=)` records
  observed-but-uncharged residency per `(kind, label)` — idempotent (a re-report
  replaces), zero removes, 256 labels per kind — and `snapshot()["uncharged"]`
  exposes `{kind: total}` for health ("native children (uncharged)"). It touches
  no `procs` / `rss_mb` / `fds` counter and no admission decision; a parent with
  200 native children admits exactly what it admitted with none, and contributes
  one `record_start` / one `record_completion` to the adaptive controller, so
  the timeout-rate signal cannot be tripped by fan-out.
- **Never a lane slot or a task row.** The handle module imports neither
  `taskq` nor `admission` (structural pin), and a child's frames yield only
  `EVENT_SUBAGENT_ACTIVITY` / `EVENT_SUBAGENT_LIST` — no `EVENT_TOOL_CALL`,
  `EVENT_COMPLETE` or `EVENT_STRUCTURED_STATUS` a runner would act on.
- **One cancel.** `cancel()` sends a single `session/cancel` naming the parent;
  the tool-stall watchdog recovers a stalled parent with that same single cancel
  however many children it fanned out. Kiro Crew sends no per-child lever, even where an adapter offers one (claude-agent-acp's per-child `capabilities.cancel`).
- **No independent resume.** `native_child_resume_refusal(conversation_id)`
  returns a typed `native_child_not_resumable: <id> is a harness-native child of
  session <parent> …` for a `spawn_continue`-style resume naming a child id, or
  `None` when the id is not this handle's child.
  `ContinuationCoordinator.native_child_resume_refusal` asks every live
  handle (the session provider's `client`) and `continue_conversation` returns
  that typed error BEFORE the `conversation_gone` lookup miss;
  `POST /api/spawn/{id}/continue` and `/steer` answer 409
  `native_child_not_resumable` for such an id (404 `conversation_gone` /
  `not_found` stay for ids nobody owns).
- **Recognised under the SAME cap, and the refusal says which.**
  `AcpRuntime._snapshot_subagent_sessions` recognises at most
  `NATIVE_CHILD_ROSTER_CAP` distinct ids from a `subagent/list_update` — the same
  number the handle counts under, never a tighter per-frame slice. Membership is
  what the routing branch decides a child's approvals on, so a tighter bound
  would split ONE announced roster into two governance classes by list position:
  an unrecognised id's `session/update` is a counted drop (empty caches, so every
  auto-approve path falls to the interactive card) and its permission request is
  auto-rejected without reaching the approval pipeline. Ids past the cap are
  counted in `_subagent_roster_overflow` and reported through
  `_note_roster_overflow`, which is **loud once per truncation EPISODE and
  throttled after that** — never one record per truncated id, and never one per
  FRAME. Both would be the retention hazard the drop counter below exists to
  avoid, one level louder: per id, a single backend-controlled frame is worth
  thousands of WARNING lines; per frame, `subagent/list_update` is a
  backend-controlled notification kiro-cli re-broadcasts on every child status
  change, so above the cap the warning is a steady state at the backend's frame
  rate. Measured on the handler with the throttle removed: 10 over-cap snapshots →
  10 identical WARNING records (~245 message bytes each), with the tail count
  holding at 40 — the log VOLUME grows, the number does not. So the first
  truncated snapshot of an episode is a `WARNING` naming the count (a truncated
  tail is otherwise indistinguishable from a roster that never named those
  children, and an operator must see it to decide whether to raise the cap), and
  every later truncated snapshot is tallied into one throttled `DEBUG` summary
  carrying how many snapshots repeated and the LARGEST tail they named. The
  summary rides the same interval as the unroutable-frame summary below
  (`_ROSTER_OVERFLOW_SUMMARY_INTERVAL_SECS` is bound to
  `_DROP_SUMMARY_INTERVAL_SECS`, 60s) and the same shape: a monotonic window, no
  timer task on the demux loop, and a residual flush — on the episode's end and in
  `_reader_loop`'s `finally` — so a storm that stops inside one interval still
  reports more than its first frame. The peak, not the latest tail, because sizing
  the cap reads the worst case. The auto-reject reason then names which case it
  was: `roster_overflow_auto_reject` when the last snapshot truncated,
  `unregistered_session_auto_reject` when it did not, so the one signal that a cap
  truncation cost a real approval is not lost in ordinary unknown-session traffic.
  The COUNT is SNAPSHOT-scoped — the frame is the backend's full list, so it is
  replaced (idempotent under a repeated roster, even above the cap) and cleared
  when the owning session unregisters; a sticky count would invent cap pressure on
  a runtime whose roster is empty. The LOG is EPISODE-scoped, and the episode's
  boundary is exactly that count returning to 0: a roster inside the cap, or the
  owner unregistering. One lifetime governs both halves of the same signal, so the
  loud line and the auto-reject reason can never disagree about whether the cap is
  under pressure, and the first truncation after a recovery is loud again rather
  than swallowed. A plain per-interval re-arm is the alternative and is what the
  drop counter does, but that summary is `DEBUG`: re-arming a WARNING on a timer
  restates it every interval for as long as the steady state lasts, which is the
  volume being removed. One residual stated rather than implied: a tail that GROWS
  inside one episode (40 ids, later 40000) is loud only at its first value, and the
  growth shows up as the summary's peak at `DEBUG`; re-warning on growth would need
  a second threshold, i.e. a second throttle shape in a module that already has
  one. A second residual, on ownership: a roster that arrived FANNED OUT (several
  sessions registered, so no owner is provable) has no owner to unregister, so it —
  like `_subagent_sessions` itself, whose lifecycle this shares — survives until
  the next snapshot, and a denial in that window is attributed to a cap truncation
  nobody owns. Attribution only: routing still requires a provable owner. Pinned by
  `test_native_subagent_boundary.py` (the cap and the two reasons end to end) and
  `test_acp_runtime_native_children.py` (one warning per episode however many
  frames re-announce it, the throttled summary's count and peak, both ways an
  episode ends re-arming the warning, and the attribution expiring with the
  snapshot that earned it).
- **Observed, not scheduled.** On a shared runtime with several registered
  sessions, an unannounced child frame names no owner and is dropped (counted by
  `_note_dropped_frame`); the count is best-effort and a display never implies
  the scheduler can pause one child.

The UI showing per-child cards (`_native_subagent_sync` in `chat_turn/native_cards.py`)
never implies the scheduler can pause or restart one child; a backend that later
exposes per-child cancel/resume is integrated by adding a row adapter. On a
shared runtime, recovery of the parent rebuilds only that session handle, never
the runtime shared with unrelated sessions.

### Model-substitution advisory

kiro can return a `-32603` error that is an *advisory* that it substituted a different model, not a fatal failure. `_is_model_substitution_advisory()` (with `_extract_advisory_detail()` for the human-readable reason) recognizes this shape, and the session stays alive and continues the turn instead of tearing down — a real fatal error still propagates.

## Session Update Handling

`_extract_text_chunk()` handles two update types for text streaming:

- `agent_message_chunk` — standard text/content. Detects `type: "thinking"` or `"reasoning"` content blocks for extended thinking (kiro-cli style).
- `agent_thought_chunk` — dedicated reasoning update emitted by `claude-agent-acp`. Always treated as thinking content.

`_track_usage_update()` tracks context window usage from `usage_update` session events, reconciling the frame via the shared `parse_usage_update()` (flat `update.used`/`update.size` primary, nested `update.usage.*` fallback) so `AcpClient` and `AcpRuntime` read the same shape regardless of which kiro emits. A `KNOWN_SESSION_UPDATES` frozenset in `acp/types.py` suppresses false "unhandled session update" logs for plumbing-only update kinds (`plan`, `available_commands_update`, `current_mode_update`, `config_option_update`, `session_info_update`, `user_message_chunk`, `tool_call_update`). Only genuinely unknown kinds are logged. On the **KAS backend**, three of these are not plumbing-only: `current_mode_update`, `config_option_update`, and `session_info_update` are consumed as display signals. KAS folds signals that kiro-cli sends as separate top-level `_kiro.dev/*` methods (agent switch, per-turn metadata, compaction status) into these `session/update` discriminants, so a KAS-gated branch in `AcpSessionHandle._handle_update` maps `current_mode_update` → agent-switch echo, `config_option_update` → effort-option state, and the `session_info_update` `_meta.kiro` union (`context_usage` → context meter, `turn_completion` → per-turn credits, `summarization_*` → compaction status). kiro-cli never emits these discriminants, so the branch is gated to KAS only and the kiro path is untouched. Every KAS `session/prompt` also carries `_meta.kiro = {contextBreakdown: "summary", ttft: true}` (`acp/kas_profile.py`, `PROMPT_META`; no other backend is sent a `_meta`), so the engine measures the turn: `turn_completion` then carries `contextBreakdown` (the turn's LAST model request split into categories, in characters) and the turn's first displayed `agent_message_chunk` / `agent_thought_chunk` carries `ttft` (four epoch-ms stamps). While a prompt is in flight the handle keeps only their numbers on `AcpPromptStats.kas_profile`, which `carry_over()` drops at the next prompt; a frame outside a turn, one marked `replay: true` (every recorded turn is replayed on `session/load`) and a sub-agent's frame (`agentSubtaskId`) are not kept. Summary mode, not detailed: detailed only adds named items (steering files by path, MCP servers, tools), about 3 KB a frame against 1 KB, and nothing reads them. The dashboard turn's closer writes the profile as `context/measured` (see the crew log emitter spec).

**Context-window backfill.** kiro 2.10+ metadata may carry only a context-usage *percentage* (no absolute token counts). `_backfill_context_window(pct)` derives the window and used-token counts from the central `model_registry.model_window(self._resolved_model_id or self._model)` authority (gated on `has_known_window` so an unknown model is never backfilled with a guessed window) and the percentage, so the dashboard token text still renders when only a percentage arrives. `_resolved_model_id` begins as `models.currentModelId`, then becomes a successfully dispatched non-default startup override or explicit switch, whether it uses `session/set_model` or `session/set_config_option`; a policy-substitution advisory on the latter records the model actually served for that request only. Automatic and unusable routes retain the backend-reported default.

**Per-turn kiro billing credits.** `_track_metadata()` parses each `_kiro.dev/metadata` notification via the shared `parse_metadata()`, capturing `meteringUsage` entries with `unit=="credit"` (kiro bills in credits; token fields are 0 for the acp provider) into `AcpPromptStats.credits`, accumulated across the turn and surfaced on `EVENT_COMPLETE`. Credits read between turns by `wait_for_compaction()` and `_drain_post_compaction_metadata()` (`between_turns=True`) are held in `_between_turn_credits` and added once to the next turn's stats right after `carry_over()` in `send_message_stream()`, `_dispatch_events()` and `_read_prompt_response()`. `rekey()` drops the held amount.

**Per-call context readings (kiro).** kiro-cli sends one `_kiro.dev/metadata` frame after every model call, carrying the `contextUsagePercentage` that call left, and closes the turn with a frame whose `meteringUsage` lists one credit entry per call and repeats the last percentage; a session also sends one frame when it opens, before its first call (live-probe confirmed, kiro-cli 2.27.1). Both `_track_metadata()` paths (`AcpClient`, `AcpSessionHandle`) hand every frame to the shared `AcpPromptStats.note_metadata_frame(pct, metering)` after the percentage is applied: a frame with `meteringUsage` adds its credit entries to `metered_calls` and records nothing else, and any other frame adds the call's context in tokens (percentage × known window) to a running sum, counted in `call_readings`, with the turn's first reading kept beside them (fixed size, however many calls the turn makes). Nothing is recorded with the window unknown, or while a real `usage_update` holds the meter. `to_turn_usage()` reports the call count as `TurnUsage.num_turns` (`metered_calls`, else the number of readings) and the sum as `TurnUsage.context_sum_tokens`; a turn with exactly one reading more than it metered calls leaves out its first reading, which is the session-open frame. Any other mismatch keeps every reading, as does a turn that ended before its metering frame (a cancelled first turn then counts the session-open reading), and a turn that compacts mid-way records kiro-cli's post-compaction report like a call's reading, so those sums are approximate. All of it is per-turn and dropped by `carry_over()`. A backend that sends no metadata keeps both at 0, so its `TurnUsage` is unchanged.

**Per-turn cost and token counts (claude seam).** The `claude-agent-acp` adapter bills in cost/tokens instead of credits: a session-cumulative `cost: {amount, currency}` rides `usage_update`, and turn-scoped token counts (`inputTokens`/`outputTokens`/`cachedReadTokens`/`cachedWriteTokens`) ride the PromptResponse. Both are validated at the shared `_dispatch.py` chokepoints (`parse_usage_cost`, `parse_prompt_token_usage` — same defensive posture as `parse_usage_update`). `parse_usage_cost` additionally drops the whole cost when a `currency` is present and not exactly `"USD"`, since every consumer stores the result in USD-denominated fields; an absent currency stays accepted for adapters that omit it. Validated readings are folded into `AcpPromptStats`: the cumulative cost is converted to a per-turn delta by `apply_cost_cumulative`, and the baseline belongs to one ACP session. `session/new` starts it at a known zero (`begin_cost_session`). A restore (`session/load` or `session/resume`, on both `AcpClient` and `AcpRuntime.load_session`) calls `restore_cost_session`, because the claude harness brings a resumed session's cost ledger back from its own transcript: the counter carries the session's whole lifetime spend, not zero. A baseline measured on that same session survives the adapter-process restart (`_reset_state` leaves it alone) and keeps billing deltas; any other baseline is marked unknown, and the next reading is adopted as the baseline and bills $0. A reading below a known baseline also rebases and bills $0 rather than being taken whole, which would re-bill a restored lifetime. Both rebases log a WARNING naming the session and the readings, so a gap in the usage rows explains itself: Crew under-counts at most one turn after a restore and never re-bills earlier spend. The baseline survives `carry_over()` like the context fields and is zeroed by `reset_context_state()`. The token counts accumulate via `apply_prompt_token_usage` (`_track_prompt_usage` on both `AcpClient` and `AcpSessionHandle`). Every `EVENT_COMPLETE` construction site builds its `TurnUsage` through the single `AcpPromptStats.to_turn_usage()` helper, so `cost_usd` and the token dimensions populate uniformly and the per-turn persist gate fires on the claude seam. kiro-cli sends neither signal, so on the kiro path the new dimensions stay 0 and `credits` flows exactly as before (harness parity — no `_is_claude` branch anywhere on this wiring).

## Exceptions

The `AcpError` family is defined in `acp/transport_errors.py` and re-exported by
`acp/client.py`; each class keeps `kiro_crew.acp.client` as its `__module__`.

The full hierarchy, with each class's retry verdict, is in
[error-handling.md](../common/error-handling.md); the notes below cover the classes
whose transport behaviour this module owns.

- `AcpProcessDied` is raised by every stdin writer on `BrokenPipeError` / `ConnectionResetError`, and on a stalled write; see "`AcpProcessDied` on a stalled stdin write" below.
- `AcpError.user_worded` is True only when Kiro Crew wrote the message for a person: a curated `_format_acp_error` branch (reported by `_format_acp_error_worded`), or a raise site with fixed recovery wording (`AcpModelUnavailable`, the signed-out harness error). It is False for the formatter's fallback, which passes the provider's own text or the raw error dict through, and that fallback still carries a `transient` verdict, so a surface that shows backend errors to a chat audience keys on `user_worded`, not on `transient`.
- `AcpAuthRequired` — kiro-cli is not authenticated (`kiro-cli login` needed). Non-retryable: `ensure_ready()` skips the retry ladder and re-raises so callers surface the actionable message rather than reset-and-requeue.
- A context-window overflow remains an ordinary `AcpError`. `_raise_acp_error`
  constructs it through the same path as other protocol failures, obtains the
  retry verdict from `_is_transient_raw_error`, then sets `transient=False`,
  `structural_terminal=True`, and `context_overflow=True` together in the
  data-field-only structural tag block. The JSON-RPC `message` field cannot set
  those tags, and same-session transient retry never receives this failure.
  One exception is anchored, not free text: the claude adapter's own refusal,
  `Internal error: API Error: 400 Input is too long.` (or `Prompt is too long`),
  arrives in `message` with only `{"errorKind": "unknown"}` in `data`, so
  `_RE_ADAPTER_INPUT_TOO_LONG` matches it at the START of `message` and sets the
  same three tags. It is terminal in `_is_transient_raw_error` too, so neither
  the retry ladder nor the throttle fallback chain re-sends a history every model
  refuses, and the formatter words it with the cause and the fix (start a fresh
  session).
- `AcpPromptBusy` — a prompt is already in progress on the session, classified from kiro-cli's "already in progress" text via `_PROMPT_BUSY_RE` and raised at prompt-dispatch sites. `slack/handler_runtime/turn.py` catches it and auto-resets the wedged session (`sessions.reset`) before recording the failure, so the next message cold-starts cleanly.

### `AcpProcessDied` on a stalled stdin write

| Constant | Value | Governs |
|---|---|---|
| `_RESPONSE_WRITE_BOUND_SECS` | 5.0s | the no-progress window for a frame write or lock wait |
| `_RESPONSE_WRITE_MIN_PROGRESS_BYTES` | 4 KiB | the smallest drain per window that counts as progress |
| `_RESPONSE_WRITE_UNOBSERVABLE_BOUND_SECS` | 900s | the single fixed window under the proactor loop, where the level cannot be observed |

`AcpProcessDied` is raised by every stdin writer on `BrokenPipeError` / `ConnectionResetError`, and additionally by the **request-, response- and notification-frame** writers when `stdin.drain()` has not made progress within `_RESPONSE_WRITE_BOUND_SECS` (5.0s, sized like `chat_turn/deny_chokepoints.py`'s `_STEER_NOTICE_BOUND_SECS`): `AcpClient._send_response` / `_send_error` raise it directly, and the shared `AcpRuntime.send_response` / `send_error` (the default kiro backend, one stdin for every multiplexed session) mark the runtime dead and raise `AcpRuntimeDead`, which `AcpSessionProvider` translates to `AcpProcessDied`.

`drain()` returns at once while the pipe has room and parks only when the writer is flow-control paused — the pipe behind it is full — which is what a backend that stopped reading looks like, but also what a healthy backend looks like while it consumes another session's multi-MB prompt frame queued ahead of the response on the shared stdin.

The bound is therefore a **no-progress** bound (`await_under_no_progress_bound` / `write_response_frame_bounded`): it polls the transport's write-buffer level and only gives up when the level held still for the whole window; a level that moved is activity — a shrink is the reader consuming, a growth is a frame from a writer the lock woke ahead of this caller landing on the pipe — and is measured again from the new level, so a large frame never gets a healthy shared runtime marked dead.

A drop only counts when it is at least `_RESPONSE_WRITE_MIN_PROGRESS_BYTES` (4 KiB) per window, and that alone bounds the whole wait by construction: the level is finite and non-negative and every continued window removed at least the floor from it, so a backlog of B bytes is waited on for at most B/floor + 1 windows (plus the same for any sibling frame appended meanwhile). There is deliberately no flat ceiling — the largest frame is unbounded (any number of 5 MiB image blocks), so any fixed figure would either kill a live reader on a valid frame or be arbitrary. An awaitable found complete as a window closes counts as completed. A cancel notification (`cancel_session`, `send_notification`) waits for the lock under the same bound and is appended unlocked if the lock does not come, so the one cooperative signal that can end a wedged turn is never swallowed by the lock. The session handle's own deny sites (`AcpSessionHandle._run_turn`'s stranded-request reject, the `_dispatch_events` fail-close gate and the in-band refusals) write their SEL audit before the (now bounded) reject write.

Under the proactor loop (`_is_proactor_loop`, keyed to the public `asyncio.ProactorEventLoop`; Windows's subprocess pipes report the whole in-flight overlapped write until it completes, so the level is flat while a live reader consumes a large frame) the wait falls back to the single platform-limited window `_RESPONSE_WRITE_UNOBSERVABLE_BOUND_SECS`, a fixed 900s (~30 MiB at ~40 KiB/s, the one place a fixed figure remains because there is no level to derive from; test-pinned) — the same declared degradation as the Windows watchdog.

That measurement is exact only with one writer in flight, so **every stdin write on a transport takes that transport's write lock** (`_stdin_write_lock`, on both `AcpClient` and `AcpRuntime`; request, notification, response and error frames alike), and the response write waits for the lock under the same no-progress bound — waiting for the lock is waiting for the previous frame's drain, and a lock held by a writer whose buffer no longer shrinks is the same stall. Nothing is appended behind a reader judged dead, and a runtime writer that queued for the lock re-checks `_dead` under it before writing, so a frame is never written into a runtime `_mark_dead` has already torn down. It cannot observe delivery of an accepted frame (the protocol gives no ack for a response); it bounds the wait on a paused writer whose reader consumes nothing, the same undeliverable-response condition a closed pipe reports, and routes it into the same session-reset + bounded-requeue recovery instead of pinning the deny path to the turn deadline. The premise that a healthy backend never leaves stdin unread for a whole window is the protocol's own: ACP is JSON-RPC over stdio, and every harness's stdin reader is the loop that delivers the permission response being written — a backend awaiting that response is, by construction, reading stdin; the window fires only when the level held perfectly still (nothing consumed), never on slow consumption. A transport that exposes no buffer size fails closed at the window.

The request id in the warning log and the exception text passes through `_loggable_request_id` (`repr`, the shared `redact_text` scrub over the whole text, then the display cap — redact-before-bound; an id over the input cap is replaced by a length-only marker, never truncated, so a severed secret can never reach the redactor), because the id is backend-authored; every `id=`/`req=`/`method=` log line under `acp/` (client, runtime, session handle, dispatch) uses the same helper for every frame-fed slot on the statement (ids, session ids, tool-call ids, methods), and `test_deny_bounded_write.py` scans for any unlisted one.

`_send_request` / `send_request` / `_send_and_await` (caller-sized payloads — a prompt may carry any number of 5 MiB image blocks) take the **same** no-progress bound under the lock (`write_request_frame_bounded`). A flow-control-paused kiro-cli — busy generating on one multiplexed lane and not reading stdin, as right after `spawn_run` fans several session prompts onto the one pipe — therefore cannot park a request write forever with the shared write lock held. The bound is on PROGRESS, not elapsed time, so a caller-sized frame stays legal. `write_request_frame_bounded` returns which phase stalled (`RequestWriteResult`) for the log; BOTH phases mark the transport dead and raise. The write path never signals the child: on the shared runtime that would bypass the ownership authorization (`authorize_runtime_kill`) and teardown barrier and terminate sibling sessions.

The runtime records the death as a stdin stall (`stdin_stall_death`), and ambiguity is a property of that stalled pipe: the stalling write's own `ambiguous_delivery` (a drain-phase stall left its frame buffered; a lock-phase stall wrote no byte) and, for every co-tenant, a prompt written in a turn that was running when the stall killed the runtime (`AcpSessionHandle.prompt_outstanding_on_stall`, read by `_died` and `AcpSessionProvider._translate_dead` from the runtime's `turn_active_at_stall` snapshot, so it holds after the turn's own teardown). A steer is ambiguous only on its own buffered frame (`AcpSessionProvider.steer` translates with `own_write_only`; codex `_steer_via_steering_request` re-raises a drain-phase stall instead of reporting the steer unwritten, and raises an ambiguous death when the runtime dies of a stdin stall before answering a written steering frame). A stdin-stall death is never re-typed as `AcpRegistrationRateLimited`, an ambiguous `AcpProcessDied` is never transient, and `taskq.dependency.classify_exception` never reads one as a dependency wait, so no retry ladder replays it verbatim. `build_recovery_requeue` takes the restored-state continuation (`_CONN_RECOVER_MSG`, which also says to check current state before repeating any step, since work may have taken effect without appearing in the conversation), the task executor sets the step's `resume_hint`, and a steer whose RPC died ambiguously is requeued by the turn's teardown with a note, on the model input only, that it may already have been delivered.

The owner kills the stalled child before a replacement starts, and starts none until the kill confirms it dead (otherwise the attempt fails and the next one retries the kill): the task-run runtime bootstrap reaps a dead runtime before spawning and, after a stdin-stall death, starts nothing while `stdin_reader_may_live()` (the root by identity, or any live member of its process group, where the pipe is inherited) still answers True, and the direct `AcpClient` marks itself stalled (neither responsive nor mid-turn) and kills its child in the next `ensure_ready`, which raises while that child is still running. What this guarantees is that the host never re-sends possibly-delivered work as fresh work; the child may still act on a frame it reads before that kill lands, which the continuation then inspects instead of repeating.

`cancel_session` / `send_notification` take the best-effort path described above (bounded lock wait, unlocked append fallback, bounded drain) for the notification itself, and no `drain()` or stdin `write()` under `acp/` lives outside `transport_framing.py` (`test_deny_bounded_write.py` pins it); the `cancelled` answers `cancel_session` then writes for open permission requests are response frames and take the response-write bound, and `_cancelled` is set before the write so the cancel-grace kill still ends the turn if even that fails.

## Process Management

Windows physical spawn uses `create_windows_cleanup_owned_process` in both ACP
transports. It reserves separate cleanup capacity before calling the subprocess
factory, pins the original child before resume, and retains only exact handles
and scalar bookkeeping outside the provider. Cancellation settles the factory
and the suspended-resume worker before teardown; a returned child cannot be
refunded as a failed empty launch. Cleanup retires mandatory tracking under the
root pin before returning capacity. Client reset refuses an unretired cleanup
reservation, even if the root has exited. The process-local limits, permanent
manual-overflow quarantine and operator recovery procedure are owned by
[platform-compat](../common/platform-compat.md#windows-session-tree-teardown).
The factory accepts `windows_cleanup_owner` from that reservation and records the
native handle at `CreateProcess` return, before fallible CPython transport setup.
An exception without a returned `Process` therefore still retains a created child.
Cancellation settles both creation and suspended-resume work before returning.
POSIX spawn/cancellation semantics and resource Job settings are unchanged.

The child-enumeration, identity and escaped-child helpers named below
(`_get_child_pids`, `_direct_children`, `_get_start_time`, `_read_basename`,
`_capture_child_records`, `_is_our_child`, `_kill_escaped_children`) and the
RSS-tree helpers live in `acp/runtime_process_tree.py`. Each facade re-exports
the names it defined: `acp/client.py` the child-enumeration, identity and
escaped-child helpers, `acp/runtime.py` the RSS-tree helpers (it also imports
`_get_child_pids` and `_capture_child_records`).

Subprocess lifecycle:

- Spawned with process-tree isolation for clean teardown, dispatched per-platform in the launch tail (`acp.launch.launch`): **POSIX** sets `start_new_session=True` (group leader via `setsid`) so cleanup can `killpg`; **Windows** sets `creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP` (no `setsid`/process groups; an inherited Ctrl-C can't reach the gateway). Both flags are passed explicitly (never via `**dict` unpack, which breaks mypy's Popen overload resolution). Teardown in `_kill_process()` is dispatched per-platform too. **Windows** returns through the owned-handle drain described above and never reaches the signal ladder: `terminate_windows_asyncio_tree` tears the tree down from handles pinned at spawn, so a `taskkill /T` walk that a reaped root leaves empty is not what the teardown depends on. **POSIX** awaits `platform_compat.kill_process_tree_async(pid, SIGTERM)` then `SIGKILL` — `os.killpg(os.getpgid(pid), …)` inline and non-blocking. The Windows `taskkill /T /F` shim in `kill_process_tree_async` remains for callers that hold only a pid, offloaded to `kiro_crew.executors.subprocess_executor` so the event loop is never blocked for the `taskkill.exe` spawn. The escaped-child sweep (`_kill_escaped_children`, which raw-`os.kill`s descendants that reparented out of the killed group) is **POSIX-only** — a no-op on Windows, where the owned-handle drain has already confirmed each retained member's exit before `_kill_process` returns (the drain, not a `taskkill /T` walk, is what the Windows teardown depends on; that shim is only for callers holding a bare pid) and `signal.SIGKILL`/`os.kill(pid,0)` are unavailable/unsafe. The `/proc`+`pgrep`+`ps` child-enumeration helpers (`_direct_children`, `_get_start_time`, `_read_basename`) short-circuit on Windows (return `[]`/`None`) since they only feed that POSIX sweep. `_resolve_ssh_auth_sock()` (called in the spawn prelude) is also a no-op on Windows — its non-darwin branch calls `os.getuid()`, absent on win32, and Windows OpenSSH uses a named pipe with no `SSH_AUTH_SOCK` to repair.
- **The exit error credits Crew only for a signal it recorded sending**: when a read reaches end of stream and the child has exited, `AcpClient._read_message` raises `ACP process exited (code=N)`. For a signal death (`N < 0`) `_exit_origin` appends the signal's name and an origin. The origin is `killed by Crew (<reason>)` only when the signal is the one `_kill_process` last sent, which it records in `_kill_sent` (SIGTERM with `stop requested`, SIGKILL with `forced stop` or `SIGTERM timed out`); `_spawn` clears the record for every new child, so one child's kill is never credited to the next. Every other signal reads `not sent by this client`, because a crash the child raises on itself (SIGSEGV, SIGABRT), a signal from another Crew path (for example the `session_pid` orphan sweep) and a kill from outside look the same to the client. `test/test_acp_exit_signal_origin.py` pins each case.
- **The stderr tail on an exit error is published only when neither spelling carries a secret**: the `ACP process exited` detail and the runtime death summary's `stderr_tail` both go through `acp/stderr_tail.py` `safe_stderr_tail`. The ring holds one entry per line, so a secret a harness wrapped across two lines comes back with the join separator inside it and the bare-secret patterns (one contiguous run) miss it. The helper redacts the joined tail AND the same lines joined with no separator; if either is rewritten, the whole tail becomes `stderr withheld: redacted content`. Both spellings are checked on the whole tail first; only a tail that passes is then cut to its last `STDERR_TAIL_MAX_CHARS` (2000) characters, so a secret straddling the cut is still caught. The cut exists because the ring bounds lines, not their length. `test/test_acp_stderr_tail_redaction.py` pins it (#11671).
- **Off-loop PID inspection**: the PID-recycling/ownership helpers that shell out on macOS — `_get_start_time` / `_read_basename` (`ps`), `_get_child_pids` → `_direct_children` (`pgrep`), the `_capture_child_records` batch wrapper, and the `_kill_escaped_children` sweep — MUST run via `run_in_executor(subprocess_executor(), ...)`, never directly on the event loop. The PID-file tracking writes — `_track_pid`, `_track_session_pid`, `_track_child_pids` in `AcpClient._spawn()`, and `_track_child_pids` plus the `_untrack_child_pids` prune in `AcpRuntime._snapshot_descendants()` / `_prune_dead_descendants()` (the latter reached through `asyncio.to_thread`) — carry the same obligation: each takes an exclusive file lock and does a read-modify-append under it, and `ensure_ready()` awaits `_spawn()` from the loop on every cold start, so an on-loop tracker serializes concurrent spawns behind one file lock with the waiter holding the loop. The subprocess spawn (fork/exec) can block, and on a wedged child the loop would freeze (the macOS wedge class). `subprocess_executor` is a *dedicated* bounded pool (distinct from the `maintenance_executor` orphan sweep) so a wedged scan/close cannot starve the recovery sweep. The `ps` and `pgrep` calls each carry a 2s timeout so no offloaded scan occupies a pool worker indefinitely.
- **Pre-teardown tree refresh**: `_snapshot_process_tree()` records the descendants once, after session initialise, and a turn that runs a command which forks its own children adds processes it never saw. A leaderless process group may only be signalled while a RECORDED member vouches for it (`process_identity._group_member_vouches`), so each teardown path that takes a kill handle -- the subagent run's own `_teardown_run_session`, the subagent reaper's `_force_reap`, and the cron reap's `_end_run_processes` -- first awaits `process_identity.refresh_live_process_tree()`, which calls `refresh_process_tree()` on the client of the session live under the key: an `AcpClient` (non-kiro backends) walks its own leader, and a kiro-backend `AcpSessionProvider` -- whose `_child_pids` the handle reads from its `AcpRuntime` -- hands it to `AcpRuntime.refresh_process_tree()`, the same guarded `_snapshot_descendants()` pass spawn, create and resume run. The `AcpClient` refresh walks only a leader verified as this client's (no return code yet, and the pid reads back the recorded start id before the walk and again before the records are kept), only ever adds records, keeps a new child only when a second walk still finds it under the leader and its start id still reads back as the one captured (a pid reused between the walk and the capture is dropped), persists them through `_replace_child_pids` with that captured id and adds them to the in-memory record only once that write succeeds (a failed write leaves the child unrecorded, so the next refresh retries it), and is bounded by `TREE_REFRESH_TIMEOUT` (3 s); a timeout or error is logged and the teardown goes on with the earlier record. In the subagent run's own teardown the refresh runs before the lease is released, so the key is not claimable while the walk runs; a cancel during the walk still releases the lease and is recorded as an undecided kill, like one during the reset. The bound stops the teardown waiting; it cannot stop a walk already running on `subprocess_executor()`, the same residual the kill's own fresh scan carries. Torn-down sessions are not walked: their handles were captured at the pop. A member started after the last refresh is still never recorded and still cannot vouch, and a leader that already exited before teardown gets no walk at all, so its late children stay unrecorded.
- **Windows exe-casing normalization** (`acp.launch._normalize_exe_casing`, applied to the Node-adapter ladder (`_resolve_node_adapter_argv`), the self-served binary ladder (`_resolve_self_served_bin`) and the claude-code and pi resolvers (`_resolve_claude_code_executable`, `_resolve_pi_bin`); kiro-cli's own discovery in `kiro_cli.py` applies its own Windows `os.path.realpath` instead): `shutil.which` builds the resolved name's extension from `PATHEXT`, which lists `.EXE` upper-case, so it returns e.g. `…\kiro-cli.EXE` even though the on-disk file is `kiro-cli.exe`. A case-sensitive multiplexer shim spawned as `kiro-cli.EXE` fails to dispatch, exits instantly, and the ACP pipe breaks (`AcpProcessDied`) → the dashboard shows **"session stuck"** on the first chat turn. `os.path.realpath()` restores the true directory-entry casing. No-op on POSIX (case-sensitive FS). Runnability is checked via `platform_compat.is_executable_file()` (POSIX execute bit; on Windows the X-bit is meaningless so a known runnable extension is required instead), so a bare `.js` adapter entry is correctly treated as **not** directly runnable on Windows and gets wrapped with `node`.
- **Sandbox ownership**: the launch tail both drivers call (`acp.launch.launch`) calls `sandbox.wrap_argv()` to wrap the command with platform-native isolation (Linux: two-stage `unshare -rm` → `unshare -U` bind-mounts + UID drop; macOS: `sandbox-exec` Seatbelt profile). On Windows, where Kiro Crew has no native OS wrapper, an explicitly classified official Kiro backend delegates to Kiro CLI's built-in sandbox; every other backend retains the no-backend fail-closed policy. The parent passes a fully scrubbed child environment on every platform, which is the enforcement point for raw Windows delegation. Configurable via `sandbox_mode` constructor param (`"auto"` default, `"off"` to disable). See `docs/system-specs/modules/security.md`.
- **Parent-level channel-credential scrub**: both spawn paths (`AcpClient._spawn` and `AcpRuntime._spawn`) hand their plan to the one launch tail (`acp.launch.launch`), which builds the child environment from a raw `os.environ` copy (plus `_extra_env`) and, after merging `_extra_env`, runs `sandbox.scrub_agent_subprocess_env(env, forward_ssh_auth_sock=...)` on it before `create_subprocess_exec`. That one scrub composes `scrub_env` (the `_SPAWN_SCRUB_ENV_PREFIXES` credential families: AWS secret and session variables, `SSH_AUTH_SOCK` unless the operator's forward consent is on, `GNUPGHOME`, `GIT_ASKPASS`, and the Slack/WeCom/Telegram tokens and owner id in `_AGENT_DENIED_ENV_KEYS` seeded into `os.environ` by `config.loader.load_credentials`) with the `_PYTHON_ENV_PREFIXES` interpreter variables. This is required because these paths do NOT route through `sandboxed_spawn_argv`, and the OS-sandbox launcher only strips those keys for the `cc`/`strict` tiers — on the default `auto`/`standard` tier the launcher leaves them in place, so without the parent scrub they would be inherited by the agent subprocess. The exact set and the Windows-delegation rationale are owned by `security.md` (Conditional Python-interpreter env strip). One credential is settled per-backend rather than by the deny list: `KIRO_API_KEY` (kiro-cli's own model credential, in `CREDENTIAL_KEYS` but deliberately NOT in `_AGENT_DENIED_ENV_KEYS`) is re-injected from the data home's `.env` via `config.loader.inject_kiro_cli_api_key` for a kiro-cli child (whose environment is where the CLI reads it — required after the Docker entrypoint scrubs it from the gateway's environ) and actively stripped via `strip_kiro_cli_api_key` for a foreign backend (Claude seam), which must never receive it. The KAS relay is a kiro-cli, so its answer follows the spawn plan's auth owner (`apply_spawn_env(..., cli_owned_auth=not plan.host_auth)`): a cli-owned relay (`--auth-method cli`) is handed the key like the kiro backend, because kiro-cli keeps no stored record of an API-key sign-in and the variable is the whole login; a Crew-owned relay has it stripped, because the engine prefers an environment key over the vault callback; both run inside the spawn paths' existing off-loop env hop.
- **`.env` `PATH` extension**: the child's `PATH` is the gateway's, plus `augmented_path` in the launch tail, so the one way the data home's `.env` reaches it is `config.loader.load_credentials`, which hands a `PATH=` line to `env.extend_process_path` instead of seeding it with `setdefault` (a process always has a `PATH`, so `setdefault` would leave the line inert). The line is a list of directories to ADD: each must be an absolute directory (`~` expands; a relative or unexpanded entry such as `$HOME/bin` is dropped with a warning), they land behind the existing `PATH`, which is kept byte for byte, and an entry it already holds is skipped so repeated loads do not grow it. It extends the gateway process's own `PATH`, so everything the gateway spawns sees it, and the terminal completion probe still filters that `PATH` through its ownership check. Every other `.env` key is still seeded with `setdefault`, and no allowlist or scrub list changes. Pinned by `test/test_dotenv_path_extension.py`. A non-`PATH` key the environment already holds with a different value therefore keeps the environment's value; `load_credentials` logs that once, naming the key and never a value. Each key is checked only the first time a load sees it per boot (`_shadow_checked_env_keys`), because later the environment may hold the loader's own seed or a dashboard write; a `secret://` reference is not compared. An in-app restart re-execs with the old environment, so its first load names a key whose `.env` line was edited: that edit is not applied until a stop and start. Pinned by `test/test_dotenv_shadow_warning.py`.
- **Conversation identity on the child env**: `AcpClient._spawn` sets `KIROCREW_CHANNEL_ID` from `_channel_id` and, beside it, `KIROCREW_SLACK_TEAM_ID` = the Slack workspace the channel lives in (`slack.team_ids.apply_slack_team_env`), on every backend that spawns through `acp/client_start.py` (kiro and Claude alike). Each is set when known and POPPED otherwise, because the child env starts from the gateway's own and an inherited value would name a conversation this session is not serving. The workspace is read at spawn from the one channel-to-home-workspace store that already routes outbound calls, `RealSlackClient._channel_team` (filled from `conversations.info`'s `context_team_id` by `ensure_channel_team`); the client hands that store to `slack.team_ids` when it is built, so the spawn path reads it without importing the Slack client and no second copy exists. There is no fallback: a channel whose home workspace was never resolved gets no value, rather than the bot's own workspace, which on an org-wide or Slack Connect install can be the wrong one. Only an id spelled like a Slack conversation (`C`/`G`/`D…`) is looked up, so a Discord or Telegram channel never inherits a Slack workspace, and an inbound event's `team` is never a source (it is the AUTHOR's workspace). A warm-pool process is spawned before it is claimed, so like `KIROCREW_CHANNEL_ID` the value reaches only a process spawned (or respawned) after `rekey()` named the channel. The mirrors' control-plane element (`providers/mirrors/identity.py`) carries the same pair. Consumer: a hook that opens the conversation as `slack://channel?team=<TEAM>&id=<CHANNEL>`. Pinned by `test/test_slack_team_id_env.py`.
- **Client application tag**: every kiro-cli ACP child on the kiro backend (the kiro harness's `apply_spawn_env` and `AcpClient._spawn`, both via `harness._common.apply_client_application_env`) gets `KIRO_CLI_CLIENT_APPLICATION=kirocrew` (`CLIENT_NAME`, the `clientInfo.name` value, is the same constant), overwriting any inherited value. kiro-cli puts it on the user-agent of every request to its model backend as `clientApp/kirocrew`, which is how a backend-side record identifies Crew traffic. `clientInfo.name` does not do this: kiro-cli keeps it in telemetry (`acp_client_name`) and in the `AWS_EXECUTION_ENV` of the tools it spawns, never on its own requests. The KAS harness does not set it: the v3 engine behind the KAS relay builds its own fixed user-agent, so the tag would reach no KAS model request until kiro-cli forwards it there. One-shot kiro-cli commands outside ACP (`/usage`, `whoami`, `--list-models`) run no model turn and are not tagged.
- **Operator env defaults for kiro-cli children** (`agent.child_env_defaults`, `acp/child_env_defaults.py::apply_child_env_defaults`): an operator mapping, empty by default and laid over host-sized defaults (`host_task_sizing.thread_pool_env_defaults` gives each pool name `min(cpus, 4)` only where the agents slice's task ceiling is in force -- `host_task_sizing.task_ceiling_in_force`, which reads `sandbox._probe_cgroup_scope`, the same probe that places agents in that slice -- and nothing on macOS, Windows or a Linux host without the slice; a configured name wins everywhere), filled into the env of every kiro-cli child -- the kiro and KAS harnesses' `apply_spawn_env` (sessions, background sessions, subagents) and `AcpClient._spawn` when `_is_kiro` (inside the `kiro_api_key=True` branch of `_resolve_spawn_env`) -- for keys the inherited env (gateway env plus per-session overlay) does not already carry; a present value, even an empty one, wins. Foreign harnesses get nothing. It runs in the existing off-loop hop and before `scrub_agent_subprocess_env`, so a default cannot reintroduce a denied name. It accepts only an allowlist (`sections.CHILD_ENV_DEFAULT_NAMES`): `TOKIO_WORKER_THREADS` and `RAYON_NUM_THREADS`, each a decimal integer string in [1, 1024]; anything else is dropped with a warning at config load. The allowlist exists because each name is a measured kiro-cli pool knob, while any other variable would reach every process the agent runs. Intended use is capping kiro-cli's core-count-sized pools on many-core hosts. Trade-off: these are ordinary env vars, so every process the agent runs (MCP servers, tools, a user's own Tokio or rayon program) inherits them too and runs with the capped pools. Where the ceiling is in force the host default is still set, because the kernel counts every thread against the agent slice's `TasksMax` and uncapped pools filled that slice at a few dozen sessions on a 128-core host (kiro-cli about 277 threads, a tokio MCP server about 138; about 29 and 14 at four workers); a process that needs more workers sets the variable itself or the operator sets it here. The mechanism also depends on kiro-cli continuing to honour the generic variables; a kiro-cli release that sets its pool sizes explicitly would silently ignore them. A process-scoped cap inside kiro-cli would replace this.
- **Operator environment for every agent child** (`agent.env`, `acp/agent_env.py::agent_env_overlay`, issue #7844): an operator mapping, empty by default, laid over the inherited gateway environment by the shared launch tail (`acp.launch.launch`) for every backend on both drivers, through `LaunchTools.agent_env_overlay` (bound by each driver's `_launch_tools`; the field defaults to an empty answer for a test-built `LaunchTools`). It is applied off-loop through the guarded hop right after `{**os.environ}` and before `request.extra_env`, so a value here wins over the gateway's own value while the driver's `extra_env`, its `env_before_scrub` step (session identity, credential repair) and every variable set after the scrub win over it, and `scrub_agent_subprocess_env` still removes any denied name. The set-if-unset defaults (`agent.child_env_defaults` in `env_before_scrub`, the `PYTEST_XDIST_AUTO_NUM_WORKERS` cap after the scrub) only fill a name `agent.env` left unset, so `agent.env` wins over them. Shell tool calls and the MCP servers the agent starts inherit it from the agent process; Crew's own pooled MCP backends, the gateway and the one-shot helper commands (`whoami`, `/usage`, `--list-models`, the knowledge worker) do not, since they are not agent children. The env-sensitive tool-gate read-backs (opencode, pi, DeepSeek) build their probe environment with the same overlay in the same position (`{**os.environ, **session._agent_env_for_spawn(), **session._extra_env}`), because those harnesses locate their config through the environment and a read-back must vouch for the configuration the session runs under. One spawn reads `agent.env` once: `AcpClient._agent_env_for_spawn` caches the first reading (reset at the start of each `_spawn`), and the client hands that reading to the tail as `LaunchRequest.agent_env`, so a config save between the read-back and the launch cannot point the child at a config the read-back never checked; with no reading yet (`None`, every host without a read-back, and the runtime driver) the tail reads it itself. `agent.env` is a session-manager factory path (`SessionManager._FACTORY_CONFIG_PATHS`), so a change replaces warm pooled processes, and `_on_config_change` also parks the shared background runtime (`retire_for_agent_env_change`, see [session](session.md)) so background and cron sessions move to a process that reads the new map; a running session, and subagents sharing its companion runtime, keep the environment their process started with. On Windows the overlay folds names to upper case, the spelling `os.environ` holds there, and the scrub check compares upper-cased names everywhere. Name and value validation is the config layer's (`sections.coerce_agent_env` at load and `sections.agent_env_write_refusals` at save, which refuses the write; see [config](config.md)); the overlay additionally drops, with a warning, a name the agent scrub would strip (`sections.agent_env_scrub_removes`, the same check the save path makes), so it is never silently undone. Rejected alternatives: per-workspace or per-session scopes (no consumer yet; the global map covers the reported cases) and widening `agent.child_env_defaults` (an allowlist of kiro-cli pool knobs with fill-only semantics, the opposite contract).
- **Inherited scratch exports are judged before any configured layer** (`agent_scratch.drop_inherited_scratch_env`, called by the launch tail right after `{**os.environ}` beside `drop_foreign_inherited_port`, issue #24065): a gateway restarted from inside an agent shell carries that shell's whole `scratch_env` set -- `TMPDIR`/`TMP`/`TEMP`, `KIROCREW_SCRATCH` and `KIRO_CHAT_LOG_FILE` -- naming a per-process directory under the managed scratch root that another pid owns and the hourly sweep reclaims once that owner is dead. A spawn that allocates its own directory overwrites every key, but a spawn whose allocation fails runs "with inherited temp", and without the drop that temp is the dead tree for the child's whole life (`tempfile` raises `ENOENT`; npm spins at 100% CPU with no output). Any of the five keys whose value lies lexically under `agent_scratch.scratch_root()` (the root itself included) is deleted, so the child falls to its platform default; a value outside the root is the operator's own temp directory and is kept, because it is the right inheritance when scratch cannot be allocated. `agent.env`, `extra_env` and `env_before_scrub` are applied after the drop, so a value an operator or driver sets is never judged. This is the agent-spawn twin of the cron child's `_clean_cron_env` re-point (`modules/learn-cron-dashboard.md` § "Temp directory"); it drops rather than re-points because the launch tail has no reason to know the platform default. `test/test_launch_inherited_scratch_env.py` pins the key set to what `scratch_env` writes and drives the dead-tree case through the real client launch.
- `_resolve_kiro_bin()` delegates to the side-effect-free `kiro_cli.resolve_kiro_cli()` discovery module shared with first-run setup. It checks the explicit `KIROCREW_KIRO_BIN` operator/test override first, then the desktop app's bundled copy (`KIROCREW_BUNDLED_KIRO_DIR`, set by the Electron shell only when the payload shipped; the candidate there is `<dir>/kiro-cli-chat`, `kiro_cli.BUNDLED_KIRO_CLI_ENTRY`, because the `kiro-cli` launcher never resolves the chat binary beside itself), then the supported fixed install locations and augmented PATH; the bundled directory sits in the fixed part of the list, so `pin_kiro_cli` (the off-`PATH` pin every unattended spawn uses) resolves it too. Setup status may inspect the same candidates but never mutates the override or other process-global environment. The gateway's prerequisite service and the direct `chat`/`tui`/`run`/`consolidate`/`eval` CLI entry paths both register the override's canonical path and first-observed digest before any provider can be created; process-lifetime first-observation-wins semantics prevent a later service reconstruction from blessing replacement bytes. `runtime.py` imports and reuses the ACP wrapper so both ACP transports select the binary identically. Immediately before OS sandboxing, `sandbox_argv.py` routes argv[0] through the edition-neutral `PlatformContext.agent_executable` resolver; the public Default is identity and a companion can return a direct executable behind an edition-managed launcher without changing the core. One core-side swap exists above that seam and only for the KAS backend: on the Crew-owned branch (Crew's vault holds the identity) the harness replaces a resolved `kiro-cli` with the `kiro-cli-chat` in the same directory when there is one (`kiro_cli.chat_sibling`), because the POSIX launcher checks its own sign-in before it execs the chat binary for `acp`, which is exactly the check a Crew-owned spawn on a signed-out kiro-cli cannot pass; the cli-owned branch keeps the launcher (see [kas-auth](kas-auth.md)).
- When no kiro-cli is found, every spawn path raises the one `kiro_cli_not_found_message` text with the structural tag `kiro_cli_missing`: `AcpKiroCliMissing` (`acp/kiro_cli_missing.py`) from the kiro and KAS harnesses, and an `AcpError` with the tag set on the dedicated client. The dashboard's terminal arm turns the tag into the `kiro_cli_missing` row kind (`_terminal_error_meta`), and a feature-request chat's error card reads that kind to offer the repository's issue form instead of Resume, because "Request a Feature" is an agent turn and cannot run without the CLI. The decision comes from the tag, never the prose. Other backends' not-found refusals (codex-acp, claude-agent-acp, pi) stay untagged: their binaries are an install the operator chose.
- The dashboard `/api/models` one-shot subprocess validates completion before parsing stdout: nonzero exit (with a bounded, redacted stderr tail), empty stdout, malformed JSON, or a payload without a model list each returns HTTP 503 so the client retries. A subprocess failure is never misreported as `JSONDecodeError` or cached as a successful empty model list. Before the spawn, it uses `config.loader.inject_kiro_cli_api_key` off-loop just like the interactive Kiro ACP path, so a headless Docker gateway whose entrypoint moved `KIRO_API_KEY` out of the long-lived parent environment still authenticates this official fixed-argv `kiro-cli` read; the general child-environment scrub remains unchanged. The catalog is held in memory
only (not persisted, with bounded rows). One shared single-flight `--list-models`
fetch serves every request, under a 90 s ceiling
(`_LIST_MODELS_BACKGROUND_TIMEOUT_SECS`) that outlives the 10 s request wait
(`_LIST_MODELS_SUBPROCESS_TIMEOUT_SECS`, after which the request answers 503
`model list timed out`). A cached catalog is served at once and refreshed behind
the reply once it is older than 300 s (`_LIST_MODELS_CATALOG_TTL_SECS`). Every
degraded `/api/models` 503 carries a machine-readable `code`: the generic
`model_list_unavailable` on the timeout and subprocess-failure branches, and
`model_list_sandbox_unavailable` on a genuine sandbox refusal (below). An expired
or signed-out login is classified in `_fetch_kiro_catalog` from the subprocess
output and answers `kiro_prerequisite_required` instead of the generic code, so
the dashboard can tell the user to run `kiro-cli login` rather than report a
transient failure; the model-list banner reads that code to show the sign-in
message, and no token or credential text is logged.
- **One-shot `kiro-cli` calls are supervised.** `--list-models`, `whoami` and the
  `/usage` scrape spawn through `kiro_prerequisite.spawn_supervised_oneshot`: the
  process-group supervisor runs in `--reap-survivors` mode in its own session and
  ends whatever the call left in the group, such as a credential helper a launcher
  wrapper started. Where the supervisor is unavailable (Windows), the wrapper is
  not a trusted system binary, or the host cannot reap (no pidfd, e.g. macOS), the
  call runs unsupervised, noted once at INFO. The helper also defaults `creationflags` to
  `CREATE_NO_WINDOW` (`platform_compat._SUBPROCESS_NO_WINDOW`, `0` off Windows) and keeps a
  caller's own flags, so a console-subsystem child of the console-less gateway opens no
  console window; `/api/models` re-polls `--list-models` every 8 s while degraded.
- **One-shot `kiro-cli` reads spawn at the CONFIGURED sandbox tier**, via `sandbox.configured_sandbox_mode()` (`agent.sandbox`, falling back to `"auto"` and warning when the config cannot be read — an unreadable config must not yield a looser tier). The affected sites are `/api/models` (`--list-models`), and in `handlers/sessions_parts/usage_scrape.py` the `whoami` identity fetch and, in `handlers/sessions.py`, the `/usage` text scrape. On Windows all three pass `is_kiro_cli=True`, so a default `"auto"` install delegates to Kiro's built-in sandbox exactly like interactive chat and needs no broad unsandboxed-exec opt-in. They also pass `scrub_agent_subprocess_env()` as the explicit child environment. The configured-tier seam still matters for an explicit `agent.sandbox="off"` and for platforms with a Crew backend: a one-shot read must not silently request a stricter posture than the same long-lived Kiro binary. Use `configured_sandbox_mode()` for a spawn of the same binary under the same posture as chat — **not** for spawns that deliberately pin their own tier (the prerequisite probes' `strict`, the credential-free registry clones). Governance still clamps the result up via `_clamp_sandbox_mode`, so a `sandbox.min_level` floor overrides it like any other caller-supplied mode.
  - **Accepted trade on the two usage-refresh sites**, stated explicitly because it is a real (small) loosening on hosts that *do* have a backend: they follow the configured tier rather than a fixed `"standard"`, so on Linux with an explicitly configured `agent.sandbox="off"` they spawn with no Kiro Crew wrap and do not hide `_STANDARD_DIRS` (`.gnupg`, `.config/gcloud`, `.azure`, `.docker`, the auth-staging dir). This is deliberate and is the *same* posture the interactive chat spawn of that identical binary already runs under on that identical host — a one-shot `whoami` cannot need stricter confinement than the long-lived chat session, and the previous asymmetry was an accident of a hardcoded literal, not a designed boundary. Both spawns are fixed argv with no agent-influenced arguments, `kiro-cli`'s own internal sandbox is the layer `"off"` defers to, and an operator who wants the wrap back sets `agent.sandbox="auto"` — the shipped default — which then applies uniformly to chat *and* these reads instead of only to these reads. The narrowness matters: this loosening is reachable only on a host where the operator has *already* declared `"off"` and thereby accepted that posture for every chat turn, which is a far larger and longer-lived exposure than one `whoami`.
  - **All three wraps run OFF the event loop**, in `subprocess_executor()`, via one small per-site helper (`_wrap_list_models_argv`, `_wrap_argv_whoami`, `_wrap_argv_usage_scrape` → `_wrap_argv_at_configured_tier`). Two blocking reads are involved and both must land in the worker: `configured_sandbox_mode()` stats — and on a cache miss re-reads and revalidates — `config.json`, and non-delegated `wrap_argv` calls can cold-probe the backend with a synchronous `subprocess.run(..., timeout=5)`. The mode is therefore resolved *inside* the helper. Each helper passes **`is_kiro_cli=True` explicitly**: on Windows this positive classification is the security gate for Kiro's internal-sandbox delegation, and `_spawns_kiro_cli` basename inference is intentionally insufficient. Both ACP spawn paths already use the same capability-set classification; any new one-shot official-Kiro spawn must too. The helpers are deliberately *named* for the chokepoint they call because `test_spawn_audit.py` audits routed spawns structurally.
- A **genuine** sandbox refusal on `/api/models` remains possible when the spawn is not positively classified, requests extra path restrictions, cannot write its critical delegation audit, or is not the official Kiro backend. It is caught as `SandboxUnavailableError` **before** the generic `except`, and answers 503 with `code: "model_list_sandbox_unavailable"`. A normal fresh Windows install of the official Kiro CLI follows the positively classified delegation path instead.
- **Poll-driven spawn sites are readiness-gated.** `kiro-cli` auto-launches an
  interactive browser login for any subcommand run unauthenticated
  (`--no-interactive` does not suppress it; there is no opt-out env var). Every
  dashboard endpoint that shells out to `kiro-cli` on a timer therefore calls
  `reject_if_kiro_unverified()` BEFORE resolving or spawning the binary:
  `/api/models` (polled every 8s while the model list is degraded) and
  `/api/sessions/usage` (polled by the credit pill: fast, ~2.5s, while the
  reading is unsettled — an empty warming cache or a pre-readiness 503 — then a
  30s steady cadence once a reading or a terminal state lands, or once 12 reads
  have settled without one, so a standing 503 or a down gateway is never polled
  faster than 30s for long). Both return the
  shared `kiro_prerequisite_required` 503 — the same degraded response their
  timeout branches already produce — so the client contract is unchanged and
  only the subprocess is skipped. Neither gate runs on the KAS bundle route,
  where no kiro-cli is spawned: `/api/models` dispatches to
  `models_kas_bundle.api_models_kas_bundle` first (see "The model picker follows
  the route" below), and `/api/sessions/usage` takes its vault-owned path. On `/api/models` the gate is not the only
  source of that code: when the gate passes but the spawned `--list-models` read
  itself surfaces an expired or signed-out login, `_fetch_kiro_catalog`
  classifies that output and answers the same `kiro_prerequisite_required`, so an
  auth lapse that opens mid-poll is reported as a sign-in prompt rather than a
  generic degraded 503. Without this gate a signed-out gateway opened
  a browser window every 8 seconds indefinitely. These are the **only** blocking
  readiness gates: ordinary sends are ungated, because a failing ACP attempt
  reports its own `AcpAuthRequired` (see the governance of latched readiness in
  `modules/learn-cron-dashboard.md`), whereas a timer-driven spawn has no turn to
  carry that error. These sites authorize on a **freshly verified** probe
  (`verified_ready`), never the bare latch — a stale `ready=True` would
  green-light exactly the signed-out spawn the gate exists to prevent. The
  freshness bound differs by site: `/api/models` keeps the tight 30s ceiling
  (`_VERIFY_MAX_AGE_SECS`) because it is polled while degraded and has no cooldown
  on its spawn while no catalog is cached, so a wide window would re-open the
  browser-login storm; `/api/sessions/usage` reads on a 5-minute
  `_POLL_GATE_MAX_AGE_SECS` because its spawn is already throttled to one fetch
  per `_USAGE_REFRESH_SECS` (600s), so the wide window cannot storm it and only
  spares the credit pill the inline probe. The fast unsettled-state poll does
  not change this: it serves the cached payload and reads the same 5-minute
  latch, so it neither re-probes readiness nor spawns `kiro-cli` more often —
  it only shortens how long the pill waits to SEE the cache the background
  fetch has already warmed. The widened worst case is one
  stale-`ready=True` usage spawn up to five minutes after an external logout,
  still caught sooner by the identity-change re-probe and by Refresh.
- **`AcpAuthRequired` is the authoritative logout signal.** Readiness is probed
  at gateway start and on explicit user action only, so a mid-session sign-out is
  discovered when the ACP attempt fails, not by a poll. `AcpRuntime`/`AcpClient`
  translate the stderr `not logged in` banner into the non-retryable
  `AcpAuthRequired`; the dashboard turn loop handles it ahead of the generic
  `AcpError` branch (it is a subclass), never re-queues it, surfaces the
  actionable `kiro-cli login` message in the transcript, and latches the
  prerequisite service to signed-out. That error card is the **only** sign-out
  signal the dashboard shows — there is no reauthentication banner and no paused
  session state (see `modules/learn-cron-dashboard.md` § "The dashboard does not
  guide the user to sign in").
- **A signed-out `kiro-cli-chat` gives no stderr banner, so `whoami` decides.**
  The bundled chat binary (the desktop app runs it directly, with no launcher in
  front) starts and opens sessions with no sign-in, then fails every
  `session/prompt` with `-32603 ... dispatch failure` and prints nothing on
  stderr. That frame is classified as a transient connection failure. When such
  an error reaches `AcpSessionProvider.stream` on the kiro backend before any
  event, the provider asks `kiro_prerequisite.kiro_signed_out_confirmed()`: the
  gateway's prerequisite service (registered at dashboard start) runs `whoami`
  alone on the binary its boot probe verified, at most once per 30 s under one
  lock, and answers True only when `whoami` exited non-zero saying
  `not logged in`. This is the one probe on the send path, and it runs only
  after a turn has failed; its verdict is kept apart from the readiness latch,
  which it never writes. Then the provider raises
  `AcpAuthRequired` instead, so the turn takes the sign-in path above with no
  retry ladder. A timed-out or refused `whoami`, a failure after output, the
  KAS backend, and callers with no registered service (the CLI) keep the
  connection-failure retry unchanged. Pinned by
  `test/test_acp_signed_out_dispatch_failure.py`.
- **The readiness `whoami` runs against the real home, like an ACP session.**
  `kiro_prerequisite._run_auth_command(..., isolate_home=False)` runs the
  resolved CLI against the real environment/home under the standard OS sandbox
  with only the Kiro Crew data home hidden, and executes a sandbox-visible
  private snapshot of the resolved bytes (keeping the resolved basename so a
  multiplexer still dispatches). A rewritten `HOME` breaks any CLI whose session
  or tool registry lives in the real home — a toolbox multiplexer cannot even
  resolve itself — so the isolated probe reported such CLIs signed-out even
  though a real session authenticates fine.
- **Sign-in is fully delegated to `kiro-cli`.** `kiro-cli login
  --use-device-flow` runs against the user's REAL home and writes its own
  credential store, exactly as it does from a terminal. Kiro Crew stages no
  credentials and copies none back — the staged-home publish path (and the
  "Kiro identity changed during sign-in" conflict two racing gateways could
  hit) is gone. The isolated credential-minimal home remains available for
  callers that opt into it, so a probe can never read the real `~/.aws` /
  `~/.ssh`; the operator-initiated login runs in the real home inside the same
  OS sandbox posture ACP already uses, with the Kiro Crew data home hidden.
- 10MB stdout buffer for large JSON-RPC lines
- stderr drained in background (`_drain_stderr`) to prevent pipe deadlock. Each line bumps `_last_activity` (liveness for `is_responsive`), is appended to the bounded 20-entry `_stderr_lines` diagnostic ring buffer, and is forwarded as a redacted `WARNING`. **Exception — suppression filter:** lines matching a marker in the module-level `_SUPPRESSED_STDERR_MARKERS` tuple (currently `thinking_tokens`) are dropped — no `WARNING`, not appended to the ring buffer — but **still** bump `_last_activity`. This handles the claude-agent-acp "Unexpected case: {...thinking_tokens...}" stderr noise. **Mechanism** (confirmed by reading the vendored adapter's `dist/acp-agent.js`): claude-code emits a `system` message with subtype `thinking_tokens`, but the adapter's `switch (message.subtype)` enumerates only ~18 known subtypes (`init`, `status`, `compact_boundary`, `memory_recall`, `api_retry`, …) and routes anything else to `default: unreachable(message)`, which writes `logger.error("Unexpected case: " + JSON.stringify(message))` to stderr — one line per token delta, measured at ~10 lines/sec during active thinking (one per 2–4 thinking tokens). The payload is only `estimated_tokens`/`_delta`/`uuid`/`session_id`, so dropping it loses no response content. This is a forward-compat gap in the vendored adapter, **not** new behavior in a specific claude-code build — the `thinking_tokens` event is present in both `2.1.165.357` and `2.1.168.358` (verified by string-matching both bundled `claude` binaries), so it predates the `.168` update that drew attention to it. The cleaner long-term fix is upstream (add a `thinking_tokens` case to the adapter or bump the vendored version); this filter is the version-agnostic stopgap that also absorbs the next unenumerated subtype's flood. (Note `thinking_tokens` is by far the dominant subtype hitting `unreachable` — ~14k occurrences vs. a handful of rare `permission_denied` across retained logs — which is why the marker tuple stays narrow rather than suppressing all "Unexpected case" lines.) Two concrete reasons to drop rather than downgrade the level: (1) **log hygiene** — `gateway.log` uses `RotatingFileHandler(maxBytes=2MB, backupCount=3)` (`cli_logging.py`), so a sustained burst rolls genuine diagnostics out of the retained 8MB window; (2) **event-loop load** — the file handler is a plain *synchronous* handler and `_drain_stderr` runs on the gateway event loop, so each forwarded line costs a synchronous file write + two regex redaction passes on the same loop that streams responses (small per session, compounding across concurrent thinking sessions). Keeping liveness prevents the idle watchdog from killing an actively-thinking turn; skipping the ring buffer stops a burst from evicting the last real errors. A throttled `DEBUG` summary (≥ `_SUPPRESSED_STDERR_SUMMARY_INTERVAL_SECS` apart, plus a flush at EOF) keeps the suppression observable. Match substrings are kept narrow so a genuine error is never silently swallowed. This is a log-volume / event-loop-load reduction — **not** a fix for any turn-stall or "agent not responding" symptom (no such causal link was established).
- A failure path reads that ring only after a bounded settle of the drain (`settle_drain`, 0.5 s): the EOF branch of `_read_message`, the failure classifiers behind `AcpClient._settle_stderr` and `AcpRuntime.settle_stderr` (at startup, and the per-turn death check in `_prompt_loop`, where a stopped turn ends as the cancel rather than `AcpProcessDied`), and the shared runtime's failed-start cleanup. The settle absorbs the drain's own outcome — its timeout, its error, its cancellation by a teardown — but never a cancel of its caller, told apart by the caller's `cancelling()` count growing during the wait. A Stop, shutdown or outer deadline that lands there propagates: `ensure_ready` ends instead of spawning a second child on its retry arm, after running the cleanup and reset that arm had already decided on (`_classify_failed_start`, which logs a cleanup failure rather than letting it replace the cancel), and a shared runtime's startup site raises the cancel rather than a generic death. Two callers differ on purpose. The EOF branch waits unshielded, because its child has exited and a drain outliving the budget is held open only by a descendant, so later settles on the same failure would wait again. `_failed_start_cleanup` absorbs a cancel of its own task, as its tree scan does, so the kill still runs. Pinned by `test/test_acp_sandbox_init_classification.py` and `test/test_acp_provider.py`.

### Member MCP routing

Member clients and runtimes use the ordinary direct or pooled MCP transport
supported by their backend. The gateway captures canonical member/store routing
from authenticated session identity; `member_context` controls native instruction
deduplication. Memory V2 neither discards the shared broker overlay nor requires
a member-specific sandbox or direct-MCP capability.

KAS projects the gateway's validated `KIROCREW_BOUND_PORT` as `KIROCREW_PORT`
for native managed MCP servers because native children do not inherit the
gateway environment. The listener address comes from the gateway, not an editable
agent spec. Declared secrets remain withheld, a managed server relays no other
literal, and non-managed servers receive no gateway port.

An `env` value that is exactly `${NAME}` survives projection on every projected server
when the edition lists `NAME` through
`McpToolingProvider.kas_relayed_env_references()`; the public default lists none.
KAS resolves the reference from its own environment at spawn, so the wire carries
the name and never the value, and a launcher's per-process variable reaches an
MCP child that KAS starts with a minimal environment of its own. Any other
reference is withheld, because the KAS environment also holds credentials such as
the CLI's `KIRO_API_KEY`.

A server Crew did not author also keeps each literal `env` value that is exactly
`true` or `false` (case-insensitive, trimmed; `kas_agents._relayable_literal`), so
a server that takes boolean feature flags from `env` starts with them on KAS as it
does on kiro-cli, which reads the spec off disk. No secret is a boolean, so the
rule needs no guess about which names or shapes carry one. A boolean is still
withheld when its key matches the shared `AGENT_ENV_CREDENTIAL_CLASS` (split at
punctuation and camelCase) or the credential redactor would change it alone or
beside its key. Every other literal, numbers included, is withheld; #25362 tracks
relaying it through a local launcher so env stays off the wire. The INFO line
names the withheld keys whose names are plain words the redactor leaves alone and
counts the rest, never a
value. Crew's own managed servers relay no literal but
`KIROCREW_HOME`. Hoisting does not change this: `hoist_servers_for_session` moves
an entry into `session/new`'s own `mcpServers`, the same frame, as projected.
Pinned by `test/test_kas_agents_mcp_env_relay.py`; the residual (a secret with no
credential shape under an innocent key) is stated in `security.md`'s threat model.

The provider factory selects `agent.member_acp_backend` for member-DM session
keys and the configured default backend otherwise; a stored `kiro` in the member
field has already been read as KAS by the config loader, and one in
`agent.acp_backend` loads as KAS once the gateway confirmed v3. Ordinary backend governance,
selectability, member-capability checks and host sandbox rules remain independent
requirements; memory version adds no direct-MCP refusal.

### Cold-start admission and startup telemetry

The admission coordinator here and the session-start gate and collector below are
defined in `acp/runtime_start.py`; `AcpRuntime` drives them, and `acp/runtime.py`
re-exports every name.

Every `AcpRuntime.spawn()` enters one gateway-wide, event-loop-affine admission
coordinator before subprocess preparation and holds the permit through
`initialize`. It orders spawns by start priority; see § Session-start gate. The cap is
`agent.runtime_spawn_concurrency` (`restart=True`): `"auto"` by default, which
`cold_start_sizing` resolves to half the effective `session_start_concurrency`
width, at least 2 and at most 8, so 8 once that width reaches 16; an integer is
clamped to 1..32. `SessionManager.__init__` publishes it for the process
(`cold_start_sizing.configure_runtime_spawn_width`) from the cached host reading
only, so `auto` is the floor until that reading exists; the gateway's post-bind
sizing task reads the host in a worker thread and then republishes it
(`SessionManager.widen_cold_start_queues`). Every admission lookup grows the loop's
admission to the published width (an int compare, never a shrink), so a spawn that
ran before the manager published does not pin the loop at the floor. `spawn()`
takes no await before admission, so concurrent spawns queue in arrival order. A process that built no manager (an embedder's, a
test's) keeps `_COLD_START_MAX_CONCURRENT` (2). This is
the common backstop for interactive, authoring, background, shared, and unpooled
runtime callers, including callers that bypass `SessionManager` or a worker pool.
The coordinator is keyed by event loop because a waiter's future is created on
the running loop, so embedded/test loops never share one `PrioritySemaphore`; cancellation while queued or starting returns the permit,
and the existing spawn guard still kills a subprocess when initialization is
cancelled or fails. It uses only asyncio/threading primitives and has no POSIX-only
behavior. A spawn-level `OSError` gets exactly one retry after two seconds while
holding its permit: this bridges a Kiro CLI self-update that replaces its executable
in place without exceeding the cold-start cap. The post-exec shim retries its own
`execv` on `ENOENT`/`ETXTBSY` (six tries over two seconds), so the same replacement
window does not surface as an exec failure on the shim path, where the parent's
spawn has already succeeded and only exit 127 would be left to report it.
Authentication, protocol, and configuration errors are not retried.

Structured `acp_cold_start` logs distinguish queue wait and spawn, and include
bounded active/queued counts, outcome, duration, backend class, and coarse process
state. Structured `acp_startup_stage` logs distinguish `initialize`,
`session/new`, `session/load`, and `session/set_mode`; timeout records carry the
method and budget plus bounded stderr-line count. They never include prompts,
workflow source, credentials, session ids, request ids, or raw process ids.

`ensure_ready()` emits the `kirocrew.session.startup.duration` histogram (unit `ms`) timing the cold-start work — subprocess spawn + session init. The warm fast-path (an already-spawned, already-initialized session returns early) is intentionally **not** measured, since it does no startup work. The emit lives in a `finally` covering **every** exit path, with `outcome` recorded as one of `ready` / `auth_required` / `error` (defaulting to `error`, so any unexpected exception propagating through the `finally` is counted as a failure, never a false `ready`) and `spawned` (bool — whether this call actually forked a new process). `get_recorder` is **lazily imported** inside the `finally` to break the `config.loader → acp.types → acp.client → metrics.provider → config.loader` import cycle, and the entire emit is wrapped in `try/except` so a telemetry failure can never break session startup.

### Session-start gate and tracked start ownership (`SessionStartGate`, `StartCollector`)

Cold-start admission bounds runtime spawn + `initialize`. The **session-start
gate** bounds the other expensive start: `session/new` on an already-running
runtime, which blocks while the backend initializes the session's MCP servers.
`AcpRuntime.create_session` acquires the current loop's `SessionStartGate`
(`agent.session_start_concurrency`, default `auto`, FIFO within a start priority,
sized once per loop from config; `restart=True`; `auto` resolves once per process in
`kiro_crew/session_start_sizing.py` to `clamp(min(cpus // 4, available_GB // 3), 2, 16)`,
with cpus the affinity count capped by a cgroup v2 `cpu.max` quota and memory from the
`resource_status` probe; the gateway resolves it in a worker thread at boot and logs the
value in force with its inputs, and `kirocrew doctor` prints what `auto` picks for the
host at the time it runs; an explicit integer wins) BEFORE `session/new` goes on
the wire and releases it as soon as the answer arrives. The gate is a FIXED semaphore: the adaptive
loop is the gatewayd spawn gate plus the execution-cap controller, and two
adapting loops on one resource oscillate. It is one gate for every harness
(kiro-cli, KAS, a later Claude host), because every backend's session start
runs through `create_session` (harness-parity: no per-backend branch).

**The shared `_bg` runtime starts under its own gate.** Every `run_bg_oneliner`
(auto-titles, nav labels, folder icons, summaries, STT endpointing) opens a fresh
`session/new` on the one shared `_bg` runtime, which answers them serially.
`get_bg_session` passes `bg_runtime_start=True`, so those starts take a permit
from `bg_runtime_session_start_gate()`: a separate per-loop `SessionStartGate`
of one permit, ordered by start priority like the user gate. Priority alone
cannot keep them off a person's path: a title fires on the first send and reaches
the gate before the sender's own start has finished spawning, so in a burst the
titles take the user permits first and hold each one while they wait inside the
`_bg` process. Pinned by `test/test_bg_runtime_session_start_gate.py`.

**Start priority: a start a person is waiting on is served ahead of background
starts.** Three in-process queues bound a start, and all three are one primitive,
`kiro_crew.start_priority.PrioritySemaphore`: `SessionManager._start_sem` (cold
starts, held across the provider's own spawn and `session/new` waits), the
cold-start admission (`agent.runtime_spawn_concurrency` spawns) and this gate
(`agent.session_start_concurrency`, collector headroom unchanged). The rule is
owned by the `kiro_crew.start_priority` module docstring; this section lists who
applies it.

- **Everything is BACKGROUND unless its caller claims FOREGROUND.** Nothing is
  derived from a session key: automation runs on interactive keys too (an
  auto-nudge or monitor wake, a cron or sub-agent completion injection, an
  agent's `session_send` into a slot), so a key says where a turn lands, not who
  waits for it. `get_or_create`, `open_task_session`, `get_bg_session`,
  `get_subagent_runtime`, `AcpRuntime.spawn` and `create_session` all default to
  BACKGROUND; the session layer hands the resolved priority to the provider it
  starts (`LLMProvider.start_priority`), which passes it to both of its runtime
  queues, and a task run that falls back to the companion runtime hands it to
  that runtime's `spawn`.
- **The claimers.** The dashboard runner, for a turn a person sent
  (`_turn_start_priority`: user-origin provenance and `_crew_log_actor ==
  "user"`, not a restored queue entry — a typed turn, a Resume/Continue press,
  the runner's requeue of a person's failed turn, a typed follow-up in a cron
  tab). The eager respawn that turn arms takes its priority, and so does the
  poisoned-conversation canary inside it. An eager spawn from the dashboard
  owner's own slot create, agent switch or project switch; the focus resume
  prefetch, a reload and any app-token request stay BACKGROUND. Every messaging
  channel, for a message a person sent: each transport's `receive` sets
  `person_origin` on the inbound it dispatches, a message the gateway built
  itself (a nudge or monitor wake, built by `build_inbound`) does not, and the
  dispatchers pass `person_priority(inbound.person_origin)`; a dispatcher that
  REBUILDS an inbound for a person (a button press, a slash command) carries the
  flag over, and a drained replay takes the flag its queued entries recorded at
  enqueue time (messaging.md § `InboundMessage`), because a gateway wake can be
  queued too. Slack's event and interaction
  paths pass FOREGROUND unless a trusted bot posted. The CLI chat. **Every
  dashboard claimer reads WHO ASKED**
  (`dashboard/request_priority.owner_start_priority`, which reads
  `is_owner_dashboard_request`): the composer optimizer, Side Chat, mate
  reply threads, Task Runner Refine and Plan, workflow authoring, Issue Radar's
  AI routes (`_run_oneshot_model`), and the eager spawn a slot action arms. An
  app token on its own slot is BACKGROUND there, because the person-only
  reserve is the one resource an app must not take from a person. STT
  endpointing, and the poisoned-conversation canary inside a person's turn,
  claim it through `run_bg_oneliner` -> `get_bg_session`, which passes it to its
  own `session/new` on the shared `_bg` runtime, to that runtime's (re)spawn
  admission when this call is the one that respawns it, and to the
  provider-backed entry's cold start on a backend with no shared runtime. A
  respawn runs under `_bg_runtime_lock` at the priority of the caller holding
  the lock (see the residuals below). The persistent `_bg` session's cold start
  (`_ensure_background`) is single-flight per priority: a BACKGROUND caller
  waits for any start in flight, a FOREGROUND caller only for a FOREGROUND one,
  so a person never queues behind a background start.
  Meeting translation is a per-line stream nobody waits on, so it claims nothing
  and takes the BACKGROUND default; so does STT polish, which the browser does
  not wait for.
- **Ordering.** FIFO within a priority, FOREGROUND first; a BACKGROUND waiter is
  passed by at most `_FOREGROUND_BYPASS_LIMIT` later FOREGROUND grants before it
  is served. Strict priority plus a named bypass, not weighted round-robin,
  because a person waiting must win outright whenever the bypass allows.
- **The outer reserve.** Ordering alone cannot help a person when every
  `_start_sem` permit belongs to a background start stuck behind a fan-out at an
  inner queue, so `_start_sem` carries `FOREGROUND_COLD_START_RESERVE` permits on
  top of the `agent.cold_start_concurrency` background width that only a FOREGROUND start may hold
  (`session_allocation.new_cold_start_semaphore`, which owns the rationale).
  Background keeps its full width; the first concurrent person start always gets
  an outer permit at once and then goes ahead at the inner queues; further
  concurrent person starts are served ahead of background ones as permits free.
  The inner queues keep no reserve: at their width it would halve background and
  collide with `_COLLECTOR_PERMIT_HEADROOM`.
- **The identity-sweep barrier** takes every `_start_sem` permit through
  `PrioritySemaphore.drain()`, which outranks both priorities while it waits and
  ignores the reserve, and hands back what it holds even when cancelled.
- **The guarantee, precisely.** At each in-process queue a person's start waits
  only for the permits held there and for at most `_FOREGROUND_BYPASS_LIMIT`
  background grants per queued background start. One exception: while the
  identity sweep runs (after an account change, after an incomplete sweep, or
  on every turn while the identity store cannot be fingerprinted), the person's
  turn waits at the drain for every holder, background ones included. A fourth
  queue is not covered yet: with `mcp_gateway.stub_servers` configured, a stub's
  `ensure_backend` waits in gatewayd's daemon-wide FIFO `SpawnGate` (#15830).
  A warm-pool refill (`session.pool_size > 0`) waits for its BACKGROUND permit
  with `_pool_fill_lock` released (#15857), so that lock's other waiters are not
  held behind foreground load. Known residual: `get_bg_session`
  (re)spawns the shared `_bg` runtime while holding `_bg_runtime_lock`, at the
  lock holder's priority: a FOREGROUND caller that arrives while a BACKGROUND
  caller's respawn is queued at the spawn admission waits on the lock for that
  whole background spawn, and its priority reaches only its own `session/new`.
  This needs the `_bg` runtime to be respawning (it died, or was recycled for
  age or RSS) at the moment a person's call arrives.
- **Clocks and telemetry.** A subagent's startup clock PAUSES while it is queued:
  the dedicated path at all three queues, the shared path at this gate and, when
  it needs a companion runtime, at that runtime's per-parent lock
  (`START_QUEUE_COMPANION`) and its spawn's admission, never across the spawn's
  own work. The
  callbacks receive the queue's name, and each wait accumulates into
  `_start_queue_wait_ms` -- measured on the WATCHDOG's own clock, between the
  mark and the grant, not on the queue's monotonic reading, which stops while a
  laptop is suspended and would charge that sleep as start time. The watchdog
  measures `_exec_started` minus that total, and a re-stamped `_exec_started` (a
  recovery respawn, a late adoption) clears it: the new attempt did not pay the
  old one's waits. Because one clock spans the spawn handshake,
  `session/new` and the collector wait, `_startup_deadline` includes the
  `initialize` budget as well, once per handshake round of a retried start
  (subagent.md § startup deadline). A paused total
  past `_START_QUEUE_MAX_SECS` ends the run as "Never started: start queues
  saturated". Connection warm-up's 90 s `wait_for(spawn())` includes queue time;
  a timed-out warm-up degrades to a cold first use, so it is left as is.
  `run_bg_oneliner`'s `timeout` bounds the DRIVE only: cancelling its
  acquisition would kill the shared `_bg` (re)spawn that every later caller
  reuses, and leak a backend session whose `session/new` had already gone out.
  `acp_cold_start stage=queue_wait`, the collector line and
  `session_cold_start stage=queue_wait` (logged only above
  `START_QUEUE_LOG_MIN_MS`) carry the priority and `PrioritySemaphore.describe()`
  (per-class depth and bypass count).

Pinned by `test/test_start_priority.py` and
`test_session_start_gate.py::test_a_chat_start_acquires_the_gate_ahead_of_queued_child_starts`.

`on_gate_queued(queue)` fires before the wait and
`on_gate_acquired(queue_wait_ms, queue)` at gate EXIT, both naming which queue
fired them, so a caller can PAUSE its own clock for exactly the span spent
queued: queue time never counts against the session-start budget
(`agent.session_start_timeout_secs`, 90s floor) or the subagent startup
watchdog, which accumulates the pauses rather than restarting its clock (see
§ Start priority, and subagent.md § startup deadline). Structured
`acp_startup_stage ... outcome=collecting` logs carry the gate's active/queued
counts and its per-class depth.

**Timeout never abandons the request.** On `session/new` timeout
`_send_and_await` does NOT pop the request: it re-registers a fresh future
under the same id and marks it adopted (`_pending_requests.adopt(req_id)`; the
map is a `_PendingRequests` dict that records adopted ids and drops them on
`pop`/`clear`). `create_session` hands that future and the gate permit to a
detached `StartCollector` and raises `AcpSessionStartTimeout(collector=...)` (a
subclass of `AcpRequestTimeout`; `collector` is None when the request never
reached the wire, in which case the permit is released on the spot). The
collector waits up to `agent.start_collect_timeout_secs` (300s) and settles
exactly one way:

| Late event | Outcome | Effect |
|---|---|---|
| answer + `late_adopter` returned True | `adopted` | the session is finished through the same tail as a direct start (`_finish_create_session`: queue, handle, mode activation, drain) and handed to the adopter, seeded with the init frames staged under its own session id |
| answer, no adopter or adopter declined/raised | `torn_down` | `_teardown_late_session` → per-session `terminate_session`; the shared runtime is never killed |
| runtime died | `runtime_dead` | nothing to tear down |
| cleanup deadline | `abandoned` | request id dropped from `_pending_requests` |

The last two rows are ORDER-INDEPENDENT, and that is a requirement rather than an
observation. `_mark_dead` resolves the collector's future with `AcpRuntimeDead`, so
"runtime died" is reached only when that resolution wins the race against the
collector's own timeout — and on a slow host it loses, which would report the one
outcome an operator can act on ("the process is gone") as the one they cannot ("it
never answered"). The timeout arm therefore reads the runtime's `_dead` flag, which
is not a clock, and settles `runtime_dead` when the runtime is already gone whatever
fired first. Measured: this read as `abandoned` on a Windows shard while every Linux
run said `runtime_dead`. Both orders are pinned —
`test_session_start_gate.py::test_runtime_death_settles_collector_without_teardown`
has the death win, `::test_a_death_the_collector_timeout_beat_is_still_runtime_dead`
has the timeout win with the future deliberately left unresolved, so the flag is the
only route to the right answer.

**Init-frame staging follows the ownership.** While a collector owns a start, the
reader keeps staging that start's MCP-init frames — on the collector
(`stage_init_frame`), and `_collect_late_start` seeds it with whatever the
timed-out attempt had already staged. An adoption claims the frames naming its own
session id (`take_init_frames`) and seeds the handle's queue with them, so a
late-adopted session arms `drain_init()`'s idle shortcut instead of paying the
no-report ceiling and its `mcp_session_report()` is populated. Those frames are
registration EVIDENCE: a session missing them was never proof that its servers
were absent (see `mcp_session_report`), so what the earlier drop cost was the
report and the ceiling, not the tools. Every other outcome drops the frames in the
same `finally` that releases the permit. Two holders rather than one because
`_mcp_init_progress` reads the in-flight buffer un-keyed, **by server name**: a
collector-owned attempt's frames left there would be read as the NEXT
session-start timeout's progress and hide the servers that never reported for it.
Both holders are bounded (`_INIT_NOTIFICATION_BUFFER_LIMIT`) and claim by the
session id inside the frame, so no frame can reach two sessions.

**Session-open mount check.** A harness in `ACP_BACKENDS_SESSION_MCP_ARRAY` can drop
an array element it cannot start and still create the session (goose 1.50.1,
`goose/mcp-stdio-dropped-live.jsonl`). After `session/new` or `session/load` on
such a harness, both composers (`AcpClient._start_mount_check` and
`AcpRuntime._start_mount_check`) start `acp/mcp_mount_check.py` as a background
task. It asks gatewayd's `session-mounts` frame, for up to `MOUNT_WINDOW_SECS`
(30 s), which of the session's broker stubs registered under its stub token. A stub
still absent when the window closes gets a WARNING line, an SEL event
(`mcp_session_mount_missing`: backend, session id, server names; never the token,
an env value or a command line) and a FAILED row on the report with
`NOT_MOUNTED_REASON`. Nothing is recorded before the window closes, so a stub not
seen yet reads as not reported, never as missing. A broker that cannot prove
absence records nothing and logs at INFO: no socket, an older daemon, a ledger
that began after this session opened or evicted this session's entry since, or more stubs than
`MOUNT_SERVERS_CAP`. A session closed inside the window gets nothing. Only broker
stubs are checked: an element the harness execs directly never reaches the
broker. `test_acp_mcp_mount_check.py` pins it.

**What the progress suffix counts, and says it counts.** The roster
`_mcp_init_progress` (and the dedicated client's `_mcp_timeout_progress`) reports
against is the `mcpServers` array the session put ON THE WIRE — on kiro-cli the
broker stubs Kiro Crew injects (`pooled_session_servers`), never the agent spec's
own servers, which the backend starts itself and which are not in the roster; and
nothing after MCP init inside the backend's session start is observable from the
runtime at all. A suffix of the bare shape `4/4 MCP server(s) reported` was
therefore read as "all MCP is up, so MCP is the problem", and a field report of a
90 s `session/new` was triaged as an MCP failure on the strength of that suffix
alone. The count is labelled `N/M session-injected MCP server(s) reported`
(the fraction itself is the bare count, so a grep on it works), a partial roster still lists `no report from …`, and a COMPLETE roster is
followed by `types.MCP_ROSTER_COMPLETE_NOTE` — "the stall is later in session
startup, not in those servers" (the fraction already says every server reported,
so the note carries only the conclusion; what the count does not cover is
documented here, not in the error line). The note is withheld when a roster
member reported an init FAILURE: that member counts as reported, so it is not
chased as silent, but it is named under `failed:` and the stall may be in it.
One string for both start paths so the two messages cannot drift. With NO roster (an empty `mcpServers` array) the reports can only
belong to the agent spec's own servers or a concurrent start, so that branch
says `N MCP server report(s), roster unknown` and does not claim them as
session-injected. The `failed:` and `awaiting authorization:` buckets are
unchanged: an out-of-roster server is still named there, where naming it is the
point. Pinned by
`test_session_start_timeout_diagnostics.py::test_a_complete_roster_says_the_stall_is_not_in_those_servers`.

**A start that queued behind another one says so.** kiro-cli (2.26 and earlier)
answers `session/new`, `session/load` and `session/set_mode` one at a time per
process. Its ACP handlers await the whole start, or the mode switch's MCP init,
inside the connection's dispatch loop, and that loop reads nothing else
meanwhile. A start sent while one of those is unanswered therefore spends its own
budget waiting for it. The commonest case is a retry sent behind the start that
just timed out, which its collector still owns.

On a backend in `ACP_BACKENDS_SERIAL_SESSION_STARTS` (kiro-cli, the one measured;
harness-parity H6), `_send_and_await` records the method and send time of every
awaited request in `_ONE_AT_A_TIME_METHODS` (`_one_at_a_time_sent`). Any other
backend records nothing and its timeouts read as before. An id counts only while
`_pending_requests` still holds it, so an answered or abandoned request drops out
at the next send and the map cannot outgrow the pending set.

When a session start times out, it carries only the requests ahead of it that
were STILL unanswered at its deadline (`queued_behind`). On a serializing backend,
this start had not begun while such a request was outstanding. A request answered
before the deadline may have cost a few seconds or nothing, so it is not blamed
for the budget. The same rule covers a request answered while this start was
still waiting for the stdin write lock. `_session_start_stalled` then puts `queued behind N earlier
request(s) this agent process had not answered by the deadline (…; oldest sent
Xs earlier), and it answers those one at a time` ahead of the MCP progress. The
roster-complete note is withheld in that case, because the budget went to those
requests, which is not "later in session startup". Pinned by
`test_session_start_queued_behind.py`.

**One permit is reserved for a start that has not gone out.** A collector holding
its permit is the intended back-pressure — the backend really is still working on
that request — but at `session_start_concurrency = 2` two collectors hold the
WHOLE gate for up to 300 s, and every `session/new` gateway-wide then queues
behind requests whose callers already gave up. The failures those queued starts
produce time out too and create more collectors, so the gate self-locks.
Measured on an operator host: one member slot spent 15 consecutive auto-nudge
cycles on `session/new timed out after 90s (0/10 MCP server(s) reported)` while
no new agent process was ever spawned — every attempt was in this queue.

So the hand-off is now conditional. `StartPermit.hold_for_collector()` claims one
of at most `limit - _COLLECTOR_PERMIT_HEADROOM` (1) collector holds
(`SessionStartGate.collector_holds`, ceiling `collector_hold_ceiling`); denied,
`_collect_late_start` releases the permit itself, logs
`outcome=gate_permit_returned` with the counts, and gives the collector `None`.
A collector without a permit is unchanged in every other respect: it still owns
the request id and still adopts or tears down the late session. At `limit == 1`
the ceiling is 0, which is the only reading of "always keep one free" a
single-permit gate admits. A refusal is logged with both counts rather than tallied: the log line is what an operator reads when a start queues, and a counter no runtime reader consults is dead weight.

Every path releases the permit exactly once (`StartPermit.release()` is
idempotent; `SessionStartGate.releases` counts real releases, and a release
returns the collector hold so the ceiling recovers) and unregisters
the collector (`AcpRuntime.start_collectors()` lists live ones). A caller
never re-issues `session/new` for the same attempt while a collector owns it,
and never starts a dedicated process in its place: congestion is answered by
waiting for the collector's verdict (subagent.md § Session sharing).

Pinned by `test/test_session_start_gate.py`: gate before `session/new` with
limit 2 across three starts; queue wait reported at exit; adopt / teardown /
decline / abandon / runtime-dead on both kiro and KAS harnesses; permit
released exactly once on every path; no collector for a pre-wire timeout; an
adopted session receives the init frames staged both before and during the
collector window and none of them is counted as a dropped frame; a settled
collector (torn down, abandoned or runtime-dead) keeps none and the next start's
timeout names its own silent servers; two live collectors never claim each
other's frames; `subagent_manager/turn_session.py` (`acquire_session`) never calls `get_or_create` on
congestion and continues on a session `subagent_manager/run_start.py` adopted late.

**Adaptive-controller sample.** Every `session/new` outcome is one `AdaptiveController.record_start(duration_ms, ok, attributable_timeout, key="acp:session/new")` when a controller is registered (`adaptive.controller.current()`; a no-op otherwise): duration is measured from gate EXIT (the queue wait is admission's cost), `ok=True` on an answer, `ok=False, attributable_timeout=True` on `AcpRequestTimeout` (the congestion signal the controller's decrease keys on), `ok=False` on any other failure. `test_runloop_integration.py` pins the three shapes.

### Tool audit for non-chat clients (`AcpClient(audit_source=…)`)

The `audit_source` constructor param of `AcpClient` (default `None`) tags a client that runs tools **outside** the chat_runner / SubagentManager audit loop — the knowledge `llm_pool` worker-pool client, whose tool calls would otherwise never reach the security audit log. When set, `_maybe_audit_tool_call()` emits a per-tool-call SEL `tool_invocation` record; when `None` (chat / subagent clients) it is a no-op so those paths never double-log. The `sel().log_tool_invocation` call is offloaded onto `subprocess_executor()` (so SEL-backend I/O can never block the event loop) and bounded by `asyncio.wait_for(..., _SEL_AUDIT_TIMEOUT_SECONDS=5.0)`; a timeout or any SEL failure is swallowed (logged at `WARNING`) so tool dispatch always proceeds. **Note:** Code Review Sage's `ReviewPool` runs on the shared `AcpRuntime` path (see "Additional consumers" below) rather than as an `audit_source` client. The runtime layer has no `audit_source`, so the pool emits the same per-tool SEL `tool_invocation` audit itself from `sage_lib/review_pool.py`, which is what keeps audit parity.

## Image Support

`_send_prompt()` auto-detects image file paths in messages (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`) via regex. When a valid image path is found:

1. Reads the file (paths over `MAX_IMAGE_BYTES` = 10 MB stay as text, not inlined)
2. Identifies the raster type from its leading bytes; unsupported or truncated content stays as a path
3. Downscales so the longest edge is <= `MAX_IMAGE_EDGE_PX` (2000 px), preserving aspect ratio and re-encoding according to the decoded format (an oversized GIF becomes a PNG still frame)
4. Shrinks further while the base64 payload still exceeds `MAX_IMAGE_B64_BYTES` (5 MiB), stopping at `MIN_IMAGE_EDGE_PX` (256 px)
5. Base64-encodes the (possibly downscaled) bytes
6. Appends an image content block with the content-derived `mimeType`
7. Replaces the path in the text with `[image: filename.png]`
8. Sends both text and image blocks in the `prompt` array

**Only the user's own segment is upgraded** (`kiro_crew.prompt_segments`). A prompt is a `SegmentedPrompt`: a `str` whose value is the concatenation of `(source, text)` segments, where `user` is the turn's own text and `injected` is everything the assembly added (memory, lessons, a member's rules and notes, steering, replayed history, a ledger snapshot, a cron job's previous result, the blocks re-sent after a compaction). `build_prompt_blocks` builds extra blocks (an image today; a file, PDF or `resource_link` later goes through the same gate) only from text inside a `user` segment, and sends every `injected` segment as written: no path or rule in it is rewritten. The one edit made in every segment is the backslash before a typed `[image` marker, because an unescaped one reads to the model as a picture attached to this request. A plain `str` is one `user` segment, so a caller that has not moved to segments behaves as before. `ContextBuilder.build_message` returns the segmented prompt, with the measured user span as the `user` segment (or the whole turn part when the caller gave no range), so every channel that sends its result unchanged is covered; when nothing attributable was typed (an `@prompt` body or a quick-prompt expansion), the `user` segment runs from where the typed text stood to the end of the turn, so a prefixed envelope stays injected. The dashboard runner keeps `build_message`'s segments on the turn and re-applies them under its own prefixes, never sending a plain string once segments exist: if the sent tail no longer matches, the whole prompt is marked injected and nothing is upgraded. The subagent run keeps the builder's segments under its hook prefix with `prepend_injected`, marking only the added prefix injected (an empty prefix leaves the prompt object unchanged); initial sends and zero-activity resends reuse that segmented prompt. And the provider's essential-envelope dedup rewrites through `replace_in_injected`, which keeps the segments.

**One prompt's own rules** (`build_prompt_blocks`). The marker lands only where the path grammar matched: a URL's own path or a longer path that merely contains the same characters is left alone (one pass over the grammar's match spans, never a whole-text replace), while a local path quoted as a URL query value (`?src=/tmp/a.png`) is a path to the grammar and is rewritten like any other; and the shared path grammar refuses a name that merely begins with a picture's path (`/var/a.png.backup`, `/var/a.png~`, `/var/a.png/other`), so neither this builder nor the replay scrubber treats it as a picture -- nothing is read, rewritten or scrubbed for it; a period followed by a capital letter is sentence punctuation (`/var/a.png.Then`), so the path ends there and the picture is attached. A second distinct file with the same basename in one message gets `[image: filename.png (2)]`, and so on, so every block's marker is unique within the prompt. The same bytes under two names are one block, marked at both places with the first name: the digest of the file's bytes is compared before any cap is consulted, so a duplicate of an attached picture past the cap maps to that picture's block instead of being dropped. A `[image: ...]` or `[image not attached: ...]` token the user typed, in any case, is escaped with a backslash, so only a marker this builder wrote reads as an attachment; the replay scrubber's own `[image not carried into this context]` is left as it is. A kept-out picture written as a markdown image (`![alt](path)`, `![alt](<path>)` or `![alt](path "title")`) gets its note after the closing parenthesis, so the link stays intact. A typed marker is escaped even when the agent takes no images, since the text still reaches the model. One prompt inlines at most `MAX_PROMPT_IMAGE_BLOCKS` (20) images and `MAX_PROMPT_IMAGE_B64_BYTES` (12 MiB) of base64; a picture past either cap stays a path in the text, followed by `[image not attached: prompt image limit]`, exactly as one over the per-image cap (or one that cannot be rendered within the size caps) is followed by `[image not attached: image size limit]`, so neither the user nor the model takes a dropped picture as seen (the notes speak about images, so only bytes that sniff as a raster earn one -- a text file named like a picture stays plain text, and so does an oversize picture reached through a hardlink, which the bounded sniff refuses to read); each is logged, the pictures past the block cap tallied into one line, and a picture past the block cap is read only to tell a duplicate from a new one, never decoded. The byte cap is half of the images' share (three quarters) of the smallest backend request-body ceiling measured so far -- 32 MiB, bracketed by a replayed request of 30.4 MB of base64 that was accepted and one of 33.8 MB that was refused as improperly formed; the count is where the backend's many-image dimension rule begins, and each replayed image costs about 1,600 tokens on every later turn. Both caps bound one prompt alone; what the conversation's replayed history carries in total is not measured here. Nothing here remembers earlier prompts: what a conversation's replayed history may carry in total is the backend's contract, not this builder's.

Within a path run delimited by whitespace or punctuation -- ASCII punctuation
outside a path, Unicode punctuation such as a dash or a bullet, the arrow blocks,
CJK/fullwidth punctuation, and for the replay scrubber a code span -- the shared grammar takes the
last supported image suffix: `/var/a.png版本/final.jpg` and `/var/a.png.jpg` each
name one picture. Whitespace, commas, parentheses and the other delimiters stop that
lookahead, so pictures separated by any of them remain separate
(`/tmp/a.png—/tmp/b.png` inlines two pictures, as a comma between them would), and
CJK prose without a later suffix can still follow a path. A path begins only at
the start of the text or after a delimiter, except a colon, which reads as a URL
scheme's (`/a.png:/b.png` names the first picture only). Two paths glued by letters
with nothing between them (`/tmp/a.png和/tmp/b.png`, and on Windows
`C:/x/a.png和C:/y/b.png` -- the second path's drive colon does not split the token,
though only a drive letter glued to the token continues it: after whitespace a
drive begins its own path, and a URL scheme's colon ends the token, so
`C:\me\report.md and https://example.com/logo.png` names no picture, the URL
stays as written, and `C:\docs\readme.txt and D:\tmp\shot.png` names exactly
the picture)
read as one token, so the
builder inlines nothing for them and the replay scrubber replaces them with one
marker; two paths glued by a symbol (an emoji, or on POSIX a backslash, which is
not a path character there) start no path at all, so the builder inlines nothing
and the replay scrubber leaves the text as written -- never the second picture
alone with the first dropped silently. On Windows a backslash is a path
character, so a backslash-glued pair is one path like the letter-glued one. A later
separator in that run makes the earlier suffix a directory
component, so non-image descendants stay text; without a later separator or image
suffix, non-ASCII text glued to the suffix is read as prose even if it could be a
directory name mentioned alone. The path body and every guard scan inspect at
most 512 characters (a UNC share's host segment too), so a path longer than that
stays text: space is legal inside a path, so an unbounded body would re-walk a
message of spaced fragments from every start before the tail guard refused it.

This leverages kiro-cli's `promptCapabilities.image: true` capability. The LLM receives the image inline — no tool call needed.

**Text-only turns.** Every prompt entry point (`LLMProvider.stream`, the handle's `prompt`, the direct client's `stream_events`) takes a keyword-only `allow_image` (default `True`) and forwards it down to `build_prompt_blocks`. `False` builds the blocks with `allow_image=False`, whatever the agent advertises: no path in the message is read or inlined, and it stays in the text. `llm_helpers.stream_and_collect` (and `stream_and_collect_json`) forward the same keyword to every attempt. `run_bg_oneliner` sends every background one-liner this way, and the history consolidator every turn it sends, because its prompt is text about a session and any path in it is quoted history (see the history-scrub section of [session](session.md)). The default call shape is unchanged for every other caller.

The suffix selects only which paths are candidates. `messaging.raster.sniff_raster_mime` derives the wire media type from the file content, and Pillow verifies the complete container when available. A real image with a misleading name is still inlined with truthful metadata; non-raster, unsupported, or truncated content fails closed and remains a path that a tool-capable agent can inspect.

**Dimension backstop** (`build_prompt_blocks` in `acp/prompt_blocks.py`). This shared builder is the single funnel every channel's images cross before reaching kiro-cli, so the `MAX_IMAGE_EDGE_PX` (2000 px) downscale runs for all of them — dashboard upload/paste/screenshot, Slack, Discord. Anthropic rejects the ENTIRE request when a many-image conversation (>20 images) carries any image over 2000 px on a side; because kiro-cli replays the full message history every turn, one oversized image would otherwise sit at a fixed history index and wedge the session permanently (a follow-up resize cannot evict the original). The browser's client-side resize (1568 px, `website/src/utils/resizeImage.ts`) is a token-cost optimization on top; this server-side cap is the correctness guarantee that still holds when that resize is skipped or bypassed (e.g. the native `/api/screenshot` capture, or non-dashboard channels).

**Encoded-size backstop** (`_fit_encoded_budget` in `kiro_crew/imaging.py`). The dimension cap alone does not bound the payload: a raster can sit well inside 2000 px and still encode past the backend's per-image byte ceiling. `MAX_IMAGE_B64_BYTES` is **5 MiB, read out of the backend's own rejection** rather than derived from which provider kiro-cli routes through (which we treat as opaque) — the error names the limit in bytes, `image exceeds 5 MB maximum: 6714372 bytes > 5242880`, and 5242880 is exactly 5 × 1024 × 1024. Anthropic's published per-image ceiling for Bedrock and Google Cloud agrees, which is corroboration rather than the basis. The check must run on the ENCODED payload AFTER any downscale: `MAX_IMAGE_BYTES` measures the file before the re-encode and cannot see base64's 4/3 inflation, so a ~3.9 MiB raster passes every pre-encode gate and is still rejected on the wire. Because a rejected image is replayed from a fixed history index on every later turn, this has the same wedge-the-session consequence as the dimension case. Erring low merely ships a smaller image while erring high ships a refused payload, so the cap is set to the observed value and callers can override it via `max_image_b64_bytes` if a backend ever reports a different number. `_fit_encoded_budget` applies the dimension cap, then keeps shrinking (0.8 per pass, up to 6 passes, from the rendition's OWN long edge so an already-in-cap image still makes progress) until the encoding fits. If nothing fits above `MIN_IMAGE_EDGE_PX` (256 px) it fails CLOSED — the path stays in the text and no image block is emitted, because inlining a payload the backend refuses is strictly worse than sending a reference a tool-capable agent can open.

**Reusable entry point** (`downscale_image_block` in `kiro_crew/imaging.py`). The budget constants and Pillow machinery live in that LEAF module — `prompt_blocks` re-exports them — because the second consumer is the MCP gateway's tool-result rewrite (`mcp_gateway/image_budget.py`), which runs inside the gateway daemon and must not import the ACP package (doing so pulls the whole ACP client into the broker and closes an import cycle back into `mcp_gateway`). That rewrite holds every image content block in a brokered `tools/call` response to this same budget before the response reaches kiro-cli's conversation history — the tool-result counterpart of the prompt-path backstops above (see `docs/architecture/design-notes/mcp-gateway-oversize-response.md`, Layer 3). Images produced by kiro-cli's own built-in tools never transit Kiro Crew and must be capped upstream in kiro-cli.


### Outbound-request structure diagnostics (content-free)

`summarize_prompt_structure(blocks)` in `acp/prompt_blocks.py` returns a **purely structural** summary of the outbound `session/prompt` block list — total block count, a count per block `type` (`text` / `image` / `tool_use` / `tool_result` / `other`), the number of empty (blank/whitespace-only) text blocks, the `tool_use` vs `tool_result` counts (so a pairing imbalance is visible), and `total_bytes` (the serialized `json.dumps` size, or `-1` if the content will not serialize). `AcpSessionHandle.prompt` logs this summary once per turn build at **DEBUG** (the level this module reserves for per-turn diagnostics), tagged with the `sessionId`.

The summary carries **no message content** — only counts, types, and sizes — which is a hard requirement (issue #6022): the kiro-cli data dir is fenced precisely because it holds SSO tokens, so the diagnostics must never record block text, image bytes, or tool arguments. This lets an operator tell a stale/invalid model id apart from a structurally malformed payload the next time a turn is rejected as `Improperly formed request` (see the `_RE_MALFORMED_REQUEST` classifier), without ever exposing what the turn contained. The helper is defensive by contract: it never raises into the live prompt path (a malformed block list yields a partial/minimal summary), so a diagnostics failure can never break a turn.

### Turn-boundary loss diagnostics (content-free)

Two places in `AcpSessionHandle.prompt` could destroy or omit a turn's evidence
silently. Both now report, and both report **only** a count or the bare fact —
never frame text, tool arguments, tool results, or frame SIZE, since a size leaks
response length.

- **Pre-turn stale drain.** The drain empties the session queue of frames left by
  an abandoned turn (see the cancel-unacked / stale / tool-stall / timeout paths,
  which synthesize a terminal and return while the real kiro-cli turn keeps
  emitting). A consumer that leaves before the turn's terminal (a caller timeout, a
  cancelled task, or a closed stream raises `CancelledError`/`GeneratorExit` before
  any terminal is yielded) synthesizes no terminal: the handle records that prompt's
  request id (`_abandoned_prompt_id`), and the next prompt first calls
  `_close_abandoned_turn`, which sends `session/cancel` and waits up to
  `_CANCEL_GRACE_SECS` for that abandoned prompt's own answer (the only frame
  boundary, since `session/update` frames carry no request id). `_wait_for_response`
  re-queues every frame it read on the way, and this drain then discards them. The id
  clears only once the answer arrives or the grace ends, so a turn cancelled while it
  waits, or one that fails to send the cancel, leaves the id set and the next turn
  closes the abandoned turn again. Permission REQUESTS are answered rather than dropped; everything else
  is discarded. The drain counts the discarded frames, classifies each one, and emits **one** WARNING per turn — one
  line regardless of how many frames drained, so a burst cannot flood the log.
  The classification is structural: a JSON-RPC response (`method` is `None`, `id`
  set) whose result carries a non-empty string `stopReason` is by construction
  the abandoned turn's terminal — a response can never reach the permission
  branch, which requires `method` — and an error response is terminal-shaped
  too (a failed turn was still terminated). The warning states the total AND how
  many of the discards were terminal-shaped (explicitly including zero), plus
  their distinct `stopReason` values — closed protocol values (`STOP_REASON_*`)
  only, whitespace-normalized before matching; any other wire string (including
  a non-string) is never logged verbatim. It deliberately does NOT attribute a
  counted response to the abandoned turn: a late error answer to a concurrently
  timed-out command call (re-injected by `_wait_for_response`'s `finally`) is
  indistinguishable in the drain, so the line states the shape, not the owner.
  This matters downstream: a turn whose terminal was destroyed here reaches the
  dashboard as an empty response with no attributable cause. The count is NOT
  bridged into `chat_runner` — see the note below.
- **A prompt stream that ends without a terminal.** `_dispatch_events`
  synthesizes an `EVENT_COMPLETE` on every exit path it knows about, so a
  consumer that never receives one is looking at a path that has none. The
  generator warns when it exhausts CLEANLY having yielded no terminal.
  "Cleanly" is what makes the line spam-free: a consumer close
  (`GeneratorExit`), a cancellation, and any raised error all skip it, and each
  is already logged by whoever caused it. The terminal is marked as delivered
  BEFORE its `yield`, so a consumer that closes the stream on the terminal is not
  reported as having lost it.

**Deliberately not bridged to the runner.** The drain count stays inside this
layer. Reaching `chat_runner` would mean a new field on the `AcpEvent` /
`LLMEvent` provider contract plus plumbing through `_dispatch` and the provider,
and `scripts/check_agent_sdk_boundary.py` baselines the dashboard modules at a
count that may not grow — a materially larger change than the fault it would
report. Unknown `sessionUpdate` discriminants in `acp/_dispatch_parts/session_update_parser.py` are still ignored
silently for a related reason: `parse_session_update` is a pure function on a hot
path with no per-session state, so a bounded log there needs a dedupe set it does
not have, and an unbounded one would log per frame.


## AcpRuntime & AcpSessionHandle (session multiplexing)

Alongside `AcpClient` (one `kiro-cli` process per session, guarded by
`_turn_lock`), the ACP package provides **`AcpRuntime`** — a single `kiro-cli`
process that multiplexes **N concurrent sessions** via a single stdout reader
that demuxes frames by `params.sessionId` into per-session queues (no
`_turn_lock`). Each session is fronted by an **`AcpSessionHandle`**; an
**`AcpSessionProvider`** adapts a handle to the `LLMProvider` interface so it is
a drop-in replacement for `AcpClient`.

Both transports share one parser — `acp/_dispatch.py` and its owners in
`acp/_dispatch_parts/` (`parse_session_update`, `build_permission_event`,
`parse_usage_update`, …) — so they cannot drift. `AcpRuntime.load_session()` mirrors `AcpClient`'s resume
handshake: it issues `session/load` directly under the original sessionId and
registers the session queue **after** the load response so replayed transcript
frames are dropped rather than counted against the current turn.

They share the stdio framing (`acp/transport_framing.py`), the error taxonomy
(`acp/transport_errors.py`), the model catalog (`acp/runtime_models.py`) and
process-tree inspection (`acp/runtime_process_tree.py`) the same way.

**The runtime tracks its descendants, not just its root.** `spawn()` registers
the launcher PID it created, which on a sandboxed host is two forks above the
process that holds the memory. `_snapshot_descendants()` records the rest into
`kiro_pids.txt` — after the initialize handshake, and at the end of every
`_finish_create_session()` and `load_session()` — and keeps the same
`(start_id, basename)` record shape in `self._child_pids` that
`session_pid._provider_descendant_records` verifies a pid's identity against.
Each pass writes the whole answer for that root — `_replace_child_pids`, one
atomic rewrite that returns whether it landed — because nothing reports a
grandchild's exit, so a record is only ever as good as the last look. The pass
is serialized per runtime, brackets itself with the root's own start identity,
and writes only identities it captured under confirmation: see
[session](session.md) for why writing a freshly read one is a kill-a-stranger
bug rather than a refresh. Each of
the three call sites runs under a cleanup guard, because the scan is reached
with a runtime already `_initialized` or a session already live in the shared
process: a cancellation inside it must tear that down, not abandon it. That in-memory snapshot is the half the
teardown sweep falls back on when the root cannot be walked: a walk enumerates
descendants FROM the root, so a root that exits first leaves its subtree
reparented to init and unreachable. Teardown prunes by descendant liveness and
retains survivors for the orphan sweep. See
[session](session.md) for the file formats and the sweeps that read them.

**A failed start reaps its MCP servers too.** The group kill that ends a failed
spawn or `initialize` handshake reaches only kiro-cli's own process group, and
kiro-cli launches every stdio MCP server as the leader of a group of its own. The
descendant scan above runs only after a successful start, so a runtime that failed
before it had recorded nothing and its MCP servers outlived it untracked. The
failed-start arm of `AcpRuntime._spawn_admitted` therefore scans the tree first
(`_record_tree_before_failed_start_kill`, while the root still links it; POSIX
only), kills
the group, then `_kill_failed_start_escapees` SIGKILLs each recorded descendant
whose identity still matches the scan and prunes the confirmed-dead entries; a
survivor keeps its entry for the orphan sweep. The whole cleanup (stderr settle,
scan, kill, reap) runs as one task, `_failed_start_cleanup`, that the guard only
waits on behind `asyncio.shield`. A cancel of the spawning task, which ordinary
slot signals deliver to the eager-spawn task, is absorbed until the cleanup
settles and is then propagated in place of the original failure. Without that, a
cancel landing inside the scan would drop the only record of the MCP servers.
Pinned by `test/test_acp_runtime_failed_start_reap.py`.

**A reaped root does not end the teardown.** `kill_process_tree` is
`killpg(getpgid(pid))`, and `getpgid` raises once the root has exited — read as
"already dead", that leaves the launcher's children, the agent and its chat
process unsignalled in the group, and a root that died a few seconds into its
life predates every descendant snapshot, so nothing else can find them either.
`AcpRuntime._signal_tree` runs the tree kill only while the root's number is
provably still ours — its live start id matches the one recorded at spawn.
`returncode` is not that proof: asyncio's child watcher does the `waitpid` in
the background and propagates the code a callback later, so a root can be
reaped, its number free for a fresh session leader whose `getpgid` succeeds,
while `returncode` still reads `None`. A root whose identity cannot be read is
treated as gone. It treats a root that is gone, by either read,
as the START of a second path, not the end: the root was spawned as a
session leader, so its pid IS the group id, and
`session_pid._signal_orphaned_runtime_group` finds that group's members and
signals THEM — each re-verified by start id at the instant of the signal — never
the group number, which is the dead root's pid and can be handed to a fresh
session leader at any moment. A member vouches when it carries this runtime's
`KIROCREW_SPAWN_INSTANCE` — the per-spawn token `spawn()` puts on the root's
environment, inherited by its whole tree — together with the `KIROCREW_SPAWNED`
marker and a runtime argv identity. The instance is the incarnation pin the
generic marker cannot supply: every runtime is a marked session leader, so a
root pid released to a fresh spawn names a group that carries the marker just
as well, and a signal aimed by number and marker alone would terminate that
fresh runtime's live session. No vouching member, no signal — the number may
be somebody else's by now. A vouched signal is followed by
the same grace a live tree gets and a `SIGKILL` pass, since the root's `wait()`
returned at once and drove no escalation. The escalation never re-resolves the
root's number — `_signal_tree` skips `kill_process_tree` when it carries
`expected`, because `getpgid` on a recycled root pid SUCCEEDS for the fresh
runtime holding it and would have signalled that runtime's group before any
identity check ran — and it re-signals only members the `SIGTERM` pass vouched
that are still alive under the same start id. A shutdown that cancels the teardown
inside the grace still runs that `SIGKILL` pass on the way out, shielded from
the cancellation that triggered it: the members were vouched and signalled, and
the escalation is the only thing still owed.  A further cancel arriving while
that shielded pass is awaited raises at the await and leaves it running
unawaited — `shield` bounds one cancellation, it does not confer immunity. Any other `OSError` (a denied signal)
is final: the root is there and may not be signalled, so its group is not
guessed at. The vouching read is Linux-only (the environ read is), so macOS
keeps the missed reap rather than gain a wrong kill (Windows closes it by handle,
below) — and say so: a
teardown that could resolve neither path logs one WARNING naming the root pid,
the signal and which of the three identity verdicts it got, so the leak is
visible in the field instead of reading as a teardown that worked. The verdict
is deliberately three-valued: only a read that happened and disagreed may
describe the root as no longer ours, while an unrecorded or unreadable identity
refuses just as firmly but reports itself as unmeasured.

**The Windows half of that reaped root is closed, by different evidence.** The
vouching read above is Linux-only (the environ read is), so macOS keeps the
missed reap rather than gain a wrong kill. Windows does not reach that ladder at
all: it has neither a process group to signal nor an inherited token to vouch
with, and `taskkill /T` on a reaped root walks nothing — so instead of naming the
survivors after the fact, it pins them BEFORE the fact. `AcpRuntime._kill_inner`
and `AcpClient._kill_process` both return through
`platform_compat.terminate_windows_asyncio_tree`, draining the tree from handles
opened at spawn against exact creation identities and held across the root's own
exit. That is why a Windows drain may not be softened to best-effort: it is the
only path, where POSIX still has the vouched group behind it. A drain that cannot
confirm the exit of every member it retains raises, keeping the pins and the
tracking for maintenance to retry, and a reservation left unretired blocks client
reset rather than reporting a teardown that worked. What that set does NOT
include is a descendant whose sole ancestry edge vanished before any snapshot saw
it; that residue stays with the orphan sweep and is bounded the same way it was
before this change. The capacity bound this retention needs,
and the manual-handling state an unbounded tree lands in, are owned by
[platform-compat](../common/platform-compat.md#windows-session-tree-teardown).

`AcpClient._kill_process` keeps the same reaped-root window **on POSIX**, and
there it is known and
deferred, not closed here. The client holds no spawn of its own and therefore no
`KIROCREW_SPAWN_INSTANCE`, so the vouched group path refuses for it by
construction; reaching its tree needs the client to mint and carry an
incarnation token at spawn time, which is a change to the client's own spawn
path. What it has instead is the descendant snapshot it takes at spawn, which
covers every client root that dies after its first scan; the uncovered case is a
client root that dies before it, and the cost there is the same bounded leak the
orphan sweep reports. On Windows the client needs neither the token nor the scan:
its spawn is owned, so the drain reaches a root that died before any scan ran.

**An ownerless server→client request is answered ONCE, at connection level.**
An inbound frame carrying an `id` **and** a `method` but no `params.sessionId`
is a request that names no session — it expects exactly one response, so the
reader answers it itself with `-32601 Method not found`
(`_answer_ownerless_request`, run off the reader loop) and never broadcasts
it. Broadcasting would hand it to every
registered session's dispatch loop, each of which would reply `-32601` on the
shared stdin — one request id, N responses, widening with session sharing. Only
true notifications (method, no id) broadcast. The routed case — an unknown
request **with** a `sessionId` — still gets its single per-session reply from
that session's dispatch loop (`server_request_unknown`).

**KAS auth has two owners, chosen by the vault.** KAS is reached through
kiro-cli's ACP relay (`build_kas_argv` in `acp/kas_transport.py`), which forwards
unrelated NDJSON frames byte-for-byte. Without a vault identity the relay runs
with `--auth-method cli`, consumes `_kiro/auth/getAccessToken` itself and resolves
tokens from kiro-cli's own store, so Crew never sees that frame. With a vault
identity the flag is omitted and Crew answers `_kiro/auth/getAccessToken` from
the vault (`_answer_get_access_token`); [kas-auth.md](kas-auth.md) owns that
contract. For the relay-owned case, one consequence is
recorded in KAS's auth declaration and reaches callers as
`backends_retired_by_host_logout()`: because the relay signs in from kiro-cli's
store, a KAS runtime is retired by an external `kiro-cli logout` on the same terms
as the kiro backend. A second is that the KAS process gets no OS
sandbox of its own — the relay spawns its server without `--sandbox` and the
agent resolves an absent config to a no-op backend — so Crew's own sandbox stays
engaged for this backend and KAS is excluded from
`ACP_BACKENDS_INTERNAL_SANDBOX`.

**KAS bundle route: the same harness, no kiro-cli.** `agent.kas_bundle = true`
makes `KasHarness.resolve_spawn` spawn the engine from Kiro Crew's OWN install
instead of kiro-cli's relay. `kirocrew kas install` (`cli_kas.py` →
`acp/kas_bundle.py`) fetches the pinned version's manifest from `agent.kas_bundle_url`
(`<base>/kas/<version>/manifest.json`; there is no "current" pointer, because the
release's pin is the only version anything asks for), picks this
host's engine tarball (`platforms[<os>-<arch>]`; Mac is `darwin-universal`) and
Node archive (`node.builds[<os>-<arch>]`, one binary per architecture), transfers
both through `asset_downloader.download_to` (https-only, same-host redirects,
sha256 and exact size pinned by the manifest), extracts into a staging directory,
probes the Node binary (`--version` must match the manifest), and publishes the
version with one rename under `<data home>/engines/kas/<version>/`. On a Linux
host whose glibc is below the official Node floor (2.28; Amazon Linux 2 ships
2.26) the install refuses up front, before any download, naming the found glibc,
the minimum, and that the bundled Node needs glibc 2.28 while Amazon Linux 2-era
hosts (glibc below 2.27) also lack the glibc 2.27 the engine's native onnxruntime
library needs (the glibc reader and floor are `constants._host_glibc_version` /
`_OFFICIAL_NODE_GLIBC_FLOOR`, the same ones doctor's Node remedy uses); nothing is
published. Which
version runs is the release's pin, not a file on disk: `acp/kas_pin.py` holds
`KAS_PIN_VERSION` and `KAS_PIN_MANIFEST_SHA256` (the sha256 of that version's
published `manifest.json`, which in turn pins every artifact), `kirocrew kas
install` installs exactly that and refuses a manifest whose bytes do not hash to
the pin, and `installed_bundle()` looks up exactly that version's directory --
under the data home's `engines/kas` first, then under the desktop app's staged
copy when the Electron shell exported one (`KIROCREW_BUNDLED_KAS_DIR`, set by
`gateway-env.js` only when the build staged the pin for this architecture,
without running it at boot; `packaging/build-desktop.sh`, `BUNDLE_KAS`), or that
the Docker image names as an `ENV` (`docker/Dockerfile` points it at
`/usr/local/share/kirocrew/kas` on every build and, with `--build-arg
BUNDLE_KAS=1`, stages the pin there with the same installer; off by default while
the route is, which leaves the directory empty and the lookup finding nothing). An operator's
own install therefore wins over the app's copy, the precedence `KIROCREW_KIRO_BIN`
has over the bundled kiro-cli, and `InstalledKasBundle.bundled` says which one
answered. A
Kiro Crew update therefore moves the engine by asking for a different directory:
the old one stays, so a still-running process of the previous release keeps its
engine and a rollback finds its own. Another version needs `--unpinned` to
install and `KIROCREW_KAS_VERSION` in the gateway's environment to run (testing
only; never read from config). A manifest whose `node` is the string
`"electron-builtin"` (a build that expected Kiro's own Electron) is refused with
the remedy. `--from DIR` installs from a local manifest and artifacts (offline,
mirrors, development). The publishing side is `scripts/kas_bundle_publish.py`:
the KAS build's `.tar.zst` engine tarballs in, recompressed to `.tar.xz` (same tar
bytes; CPython 3.12 reads xz with the standard library and zstd with nothing, and
stock macOS and Windows carry no `zstd` executable), official Node builds from
nodejs.org verified against that release's `SHASUMS256.txt`, manifest out,
optional `aws s3 cp`. The installer still accepts a `.tar.zst` manifest entry
(stdlib on 3.14+, the `zstandard` package, or a `zstd` executable) for a
locally built bundle handed to `--from`. The harness reads the flag off the loop and, when
on, resolves `kas_bundle.installed_bundle()`; nothing installed is an
`AcpRuntimeError` naming `kirocrew kas install`, never a fallback to the relay —
the operator chose the route, and a silent relay spawn would make a kiro-cli
problem look like a bundle problem. The argv is `build_kas_bundle_argv`:
`node --experimental-wasm-modules <acp-server.js> --transport=stdio
--auth=acp-callback`, byte-for-byte what kiro-cli's relay builds for the engine
minus kiro-cli. There is no `--auth-method` on this route, so the plan carries
`host_auth=True` unconditionally: Crew's vault is the only credential
([kas-auth.md](kas-auth.md)), and an empty vault surfaces as the engine's own
"not signed in" prompt failure, which maps to a sign-in prompt as on the relay.
The spawn is labelled `kas-bundle` (a Node program names no engine). Every seam
after the spawn is unchanged — measured: `initialize`, the
`_kiro/auth/getAccessToken` callback answered from the vault, and the
`AcpRuntime` cold start behave as on the relay. `pin_mandatory_mcps_env` sees a
non-kiro-cli program and keeps every Crew server resident, the conservative
answer. The `engines` leaf is `READONLY` in every sandbox mode and on the
file-tool floor ([security.md](security.md)): a sandboxed KAS process reads and
executes its engine, and nothing in a sandbox rewrites the code the next session
runs. `kirocrew doctor` reports the route when selected (`_report_kas_bundle`:
installed version against the release pin, the `install.json` path and its two
entry points, the exact spawn argv, the vault as the only owner with Crew's own
sign-in remedy) and asks nothing of kiro-cli there; `kirocrew kas status` is the
same facts alone. The one predicate every kiro-cli-facing diagnostic shares is
`agent_sdk.kas_bundle.kas_bundle_route()` -- KAS selected AND `agent.kas_bundle`
on, positive on both halves, read from config at call time -- so doctor's
Dependencies and Connectivity rows, its per-harness `kas auth:` row, the
diagnostics bundle's `versions.txt` (`kiro_cli_version:` says the binary is not
used; `kas_engine:` names the installed engine and the pin), `kirocrew update`'s
and the unattended auto-apply's `kiro-cli update` step (skipped, the reason
printed or logged: the route never runs kiro-cli, and the update does not
install the engine either -- a changed pin needs `kirocrew kas install`, which
the printed or logged line names) and the gateway's boot-time outdated-kiro-cli
warning (skipped) all agree on when kiro-cli is not part of the installation, and all keep their
relay-route behaviour byte for byte when the predicate is false.

**The model picker follows the route.** `GET /api/models` dispatches on the
positive two-fact test `agent_sdk.kas_bundle.kas_bundle_route` (`acp_backend ==
"kas"` AND `agent.kas_bundle`) BEFORE the kiro-cli path, so on this route it never
calls `reject_if_kiro_unverified` and never spawns `--list-models`
(`dashboard/handlers/models_kas_bundle.py`; the kiro-cli path below is unchanged
for every other configuration, the relay route included). Measured on the bundled
engine (`test/fixtures/acp_frames/kas/bundle-session-live.jsonl`), `session/new`
carries no `models` object: the list is the `configOptions` `model` select, each
option with `_meta.kiro.rateMultiplier` and no structured context window. The
shared-runtime driver records that select as the engine's catalog, stamped with
the vault identity fingerprint the session's request went out under
(`agent_sdk/kas_model_catalog.py`, [model-selection.md](../common/model-selection.md)),
and the endpoint serves the recording: readiness is the bundle's (installed
engine, else 503 `kas_bundle_not_installed`; Crew's vault holding a usable
identity, else 503 `kas_bundle_sign_in_required` -- never kiro-cli's code, which
renders a `kiro-cli login` remedy); a cold process answers `auto` alone with 200,
not a degraded 503, because nothing failed and the dashboard refetches the list
when a session spawns -- and "cold" includes a process whose Crew sign-in changed
since the recording, because the rows are served only to the vault identity
they were recorded for, whichever process changed the vault, and the dashboard
resets its own model-list caches on its own sign-in and sign-out (the handler
reads the vault's fingerprint again after its one await, the advertised-store
persist, and answers cold rather than serve a snapshot from before a switch); a recorded select answers its rows with `rate_multiplier`
where the option priced one and `context_window` only where
`model_registry.model_window` knows the id (an operator declaration, an earlier
kiro-cli catalog, the static registry) -- omitted otherwise, never invented. The
rows feed the `acp` bucket and the prices through the kiro-cli catalog's own
`refresh_kiro_catalog` admission. The kiro path's `_entitled_kiro_models` is NOT
run on them: it exists because `--list-models` prints the same rows for every
tier, while the select already lists what the signed-in account is served -- and
it reads the newest live provider in the shared `acp` namespace, which after a
hot switch of `agent.acp_backend` can be a retained kiro-cli session whose own
list (`auto` alone, say) would hide models the bundle account has.

**Setup and the install probe follow the route.** With `agent.kas_bundle` on, KAS never runs kiro-cli, so the two places that decide "is KAS usable on this host" ask the installed engine instead of the binary: `agent_sdk.backend_install._probe_kas` answers `installed` when `installed_bundle()` finds the pinned engine and `missing` with the one component `COMPONENT_KAS_BUNDLE` (and `kirocrew kas install` as the install command) when it does not, consulting kiro's resolver on neither path; and `KiroPrerequisiteService.record_independent_backend_setup("kas")` records first-run completion on the same reader, deciding the route through the shared `agent_sdk.kas_bundle.kas_bundle_route()` (its callers pass the configured backend, so the predicate's backend half is already settled there), spawning no `--version` / `acp --help` probe. A host with no kiro-cli at all therefore completes setup once the engine is installed, which is what the switch's help text promises. The dashboard's install handler evicts the KAS probe (`forget_probe`) when an install lands, so the Agent Harness row flips without waiting out the probe TTL. With the switch off both checks are unchanged: KAS is kiro-cli's relay and its verdict is a copy of kiro's. **The spec `permissions` block follows the route too.** Crew's auto-approve list reaches KAS's policy engine as the spec's `permissions` block, and the one writer (`agent_materialization/auto_approve._write_derived_permissions`, gate `_spec_permissions_accepted`) writes it only when every program that may read the spec accepts it. With the switch off that is the installed kiro-cli alone (`kiro_cli.spec_permissions_supported(installed_kiro_cli_version())`, floor 2.23.0). With `agent.kas_bundle` on it is the installed engine bundle (`installed_bundle().version` through `kas_pin.engine_spec_permissions_supported`, floor `KAS_SPEC_PERMISSIONS_MIN_ENGINE_VERSION` (0.66.33, the pinned release the block was measured against; `test_kas_pin` keeps the floor at or below the pin so a stock install always passes)), and also kiro-cli whenever one is installed (`kiro_cli.kiro_cli_installed`: a `resolve_kiro_cli` stat, never a spawn; a lookup that raises counts as installed; the doctor row reads the same predicate): the switch changes only the kas backend, and kiro-cli reads the same `~/.kiro/agents` files for a native Kiro session, a member on kiro, or a hand-run `kiro-cli`, where a release below its floor refuses the whole spec. A kiro-cli-free host is never version-probed and gets the block from the engine alone. The gate fails closed the way kiro-cli's does: no engine installed, or an engine below the floor (one selected through `KIROCREW_KAS_VERSION`), is an unknown reader and the block is withheld (an inherited block is removed, as on the relay route). `kirocrew doctor`'s bundle branch (`_report_kas_bundle_spec_permissions`) prints the `auto-approve:` row from the same predicates — written when the engine accepts and no installed kiro-cli refuses, withheld with the floor and the remedy when the engine is below it, withheld without a second issue when nothing is installed, and withheld naming kiro-cli and its update when an installed kiro-cli is old or unprobeable. The switch is read on every spec write through the install probe's own reader (`agent_sdk.backend_install._kas_bundle_route`) and never cached by the writer; a config that cannot be loaded selects the relay gate.

**Hooks on the bundle route: the same events, in the same shapes (#20729).** Kiro Crew's PreToolUse gate (`HookManager.on_tool_call`, reached through `hook_gate_kwargs`) and its SEL tool records are fed by three engine frames -- the `tool_call` update, the `session/request_permission` request and the `tool_call_update` frames through `completed` -- not by the `_kiro/hooks/*` surface, which the handshake announces on neither route: `KasHarness.client_capabilities` is the one `KAS_CLIENT_CAPABILITIES` constant with no route branch, so the engine asks for no hook on either and `kas_wire.hooks_announced()` is false for both. Measured live (2026-10-17) on the pinned 0.66.33 engine spawned by the bundle argv through a gateway with `agent.kas_bundle = true` and `KIROCREW_ACP_RECORD_FRAMES` set, one shell call (`run_command`) produced: a `tool_call` with `kind: execute`, `rawInput.command` and `_meta.kiro.toolOrigin: default`; a `session/request_permission` whose `_meta.kiro` carries `toolId: run_command`, `command`, `consent {capability: shell, resource, askType: implicit, triggeringResource, workspaceRoot}` and `consentRound`, with the four options `accept` / `always-accept` / `reject` / `always-reject`; and `tool_call_update` frames `in_progress`, `in_progress`, `completed` with `rawOutput {output, exitCode, message}`. Key path by key path those are the relay capture's shapes (`test/fixtures/acp_frames/kas/session.jsonl`, 0.63.3 through `kiro-cli acp --agent-engine v3`). Replayed through the same dispatch, both routes hand the gate the same permission event (`harness_tool_id` and trusted `mcp_tool_name` `run_command`, `is_shell`, `shell_classified`, `raw_params_trusted`, the same option ids) and the gate reaches the same decision; on the live run the SEL recorded the call through the dashboard dispatcher as `tool_invocation` `invoked`, `auto_approved`, then `refined` for each update, and the engine ran the command only after Crew answered the request. Nothing was adapted: the bundle route reaches the existing seam unchanged. The capture is `test/fixtures/acp_frames/kas/bundle-tool-path.jsonl` (its `_meta` names the spawn and the id reduction) and `test/test_kas_bundle_hooks_parity.py` pins the parity frame class by frame class, through `hook_gate_kwargs`, and through the gate's decision. Two differences from the 0.63.3 capture are engine-version, not route -- the relay forwards session frames byte for byte, so neither can be the relay's doing: 0.66.33 emits an engine-internal `tool_call` of `kind: other` with `_meta.kiro.toolId: fetch_cloud_config` before the first prompt, and the completed update's `content[].text` is the `rawOutput.message` text rather than the JSON rendering of `rawOutput`; the parsed `tool_result` event has the same fields either way. The agent-spec `postToolUse` matcher is compared with the call's title on both routes, because the finished call names no tool (the same `spec_hooks` warning the relay logs).

**Off-loop answers are bounded.** The off-loop answers — the unroutable-permission
auto-reject, the KAS access-token answer and the MCP sign-in answer
(`_kiro/openExternalUrl`) — can block on stdin `drain()` before writing their
response, so the reader schedules them without blocking stdout demux but keeps a
strong reference in one shared `_answer_tasks` set under `_max_answer_tasks` —
every path contends for the same stdin, so a second per-kind cap would allow the
combined resource total to exceed the bound. The done callback removes completed
tasks. At capacity these answers use a bounded discrimination wait: one
completion admits the pending answer, while no progress within the bound marks
the runtime dead so pending waiters resolve explicitly. The one exception is the
`-32601` answer to an ownerless request: past the cap it takes the counted-drop
path by design, so a flooding backend hangs on its own unanswered request instead
of growing the task set.

**Unroutable frames are counted, not logged per frame.** The reader drops any
frame it cannot route; the drop itself is correct and unchanged, but logging one
`DEBUG` line per dropped frame is a log-retention hazard on a multiplexed
backend. Every frame for a torn-down or not-yet-registered sessionId takes that
branch — including the entire transcript replay of a `session/load` (the queue is
registered after the response, above) — and a backend that keeps streaming after
teardown makes it an unbounded **steady state**, not a burst. Measured on an
operator host: ~60 lines/second sustained for 6+ hours from one gateway PID,
33–59% of every `gateway.log` rotation, which at
`RotatingFileHandler(maxBytes=2MB, backupCount=3)` (`cli_logging.py`) rolled the
diagnostics an incident needed out of the retained 8MB window before they could
be read. So `_reader_loop` funnels every **frame-rate** drop — a between-turn update for
a registered session, a frame whose `sessionId` is not registered, an ownerless
request past the answer cap, and a no-`sessionId` global notification arriving
while zero sessions are registered (sentinel `_DROP_NO_SESSION`) — through
`_note_dropped_frame()`, which also emits the `kirocrew.acp.dropped_frames`
counter (`method_class` label), which tallies `(sessionId, method)` and emits
one `DEBUG` summary carrying the accumulated count at most every
`_DROP_SUMMARY_INTERVAL_SECS` (60s). The key stays **per session** deliberately:
the decisive signal in the incident was that two *different* session UUIDs were
flooding at once, which a single global tally would hide. The level stays `DEBUG`
— the goal is far fewer lines, not louder ones. The roster-truncation warning above
reuses this counter's shape rather than inventing a second one, and binds its
interval to `_DROP_SUMMARY_INTERVAL_SECS` for the same reason: one value answers
"how often may a suppressed high-frequency condition restate itself in
`gateway.log`", and a second knob is a second throttle to reason about in the same
module. `_reader_loop`'s `finally` flushes both summaries, because the reader is
the only thing that feeds either.

Three properties make the counter safe on the demux hot path: it never awaits
(no timer task to leak — the flush rides the next drop), the map is bounded
(`_DROP_SUMMARY_MAX_KEYS` = 64 distinct keys forces an early flush instead of
growth, and both backend-controlled key halves pass through
`redact_backend_text` — the same redact-whole-or-refuse-by-length policy behind
`_loggable_request_id` — **before** they are capped to
`_DROP_SUMMARY_KEY_MAX_CHARS` = 80, so the retained and later-logged key can
never hold a credential severed at the cut), and the residual count is flushed in the
loop's `finally` on **every** exit (EOF, exhausted oversize-drain budget, cancel,
crash) so a low-rate trickle is reported late rather than swallowed. No lock is
needed: `_reader_loop` is the sole writer (`spawn()` creates exactly one reader
task). The two response-shaped drop branches (non-numeric id, unmatched id) stay
per-frame on purpose — the id is their whole diagnostic value and is distinct per
frame, so aggregating by it would give the counter an unbounded key space while
aggregating without it would discard the only identifying datum; both are also
bounded by the requests this runtime issued, so neither has the after-teardown
steady state.

**An oversize stdout line is a dropped frame, not a dead runtime.** A single
JSON-RPC line over the reader's `_STDOUT_BUFFER_LIMIT` (10 MB) used to
`_mark_dead` the runtime, which fails every pending future and poisons every
session queue — so one huge frame ended *every* session multiplexed on that
process mid-turn, surfacing to users as "process exited / chat failure". Both ACP
readers did this on the strength of a claim that asyncio leaves the stream
corrupted after an overrun and every subsequent read also fails. That claim is
false: `StreamReader.readline` repairs the buffer *before* raising `ValueError`
(deleting the oversize line through its terminating newline when one is buffered,
else clearing the buffer) and resumes the transport, as its own docstring states.

So `_reader_loop` reads through `readuntil(b"\n")` and, on `LimitOverrunError`,
hands the line to `_drain_oversize_line()`, which consumes it **entirely, through
its terminating newline**, and discards it — the same consume-prefix-and-retry
drain as `mcp_gateway/backend_routing.py::run_stdout_pump`, where a plain `read(n)` would
eat into the *next* frame. Draining the whole line rather than one prefix at a
time is load-bearing, not tidiness: the unterminated branch's discard boundary is
an arbitrary byte offset (`consumed = len(buffer)`), so surfacing the remainder as
a line hands the parser a byte-slice that can start mid-character. The parse step
skips such a slice: it reads every line through
`kiro_crew.json_line.parse_json_object_line` with replacement decoding, so a line
that is not a JSON object (undecodable bytes, a bare scalar or array, nesting past
the decoder's ceiling, an integer past the int-string digit limit) is dropped with
a debug log and never reaches the loop's crash handler, which would kill every
multiplexed session. Draining through the newline keeps the reader on a frame
boundary, so the next frame parses whole.

A dropped frame can be the reply to an awaited request -- a `session/new` or
`session/load` reply lists every agent in `~/.kiro/agents` with its description
and `welcomeMessage`, so an agent package with very large welcomeMessage text
pushes it over the limit. The drain therefore keeps the line's first
`_OVERSIZE_HEAD_BYTES` (never parsed as a frame), and when that head is a reply
envelope (no `"method"`) whose numeric `id` is a pending awaited request, that
request fails at once with `AcpFrameTooLarge`, naming the frame's size and the
limit. It is not `transient`, and the `session/new` and `session/load` arms tag it
`session_start_failed`, so a self-driving caller counts it and backs off as it
does for a stalled start. Without the `AcpFrameTooLarge` fast-fail the caller would instead time out much
later under an unrelated name (a `session/new` timeout reads as slow MCP
servers). A frame whose head names no
pending request keeps the timeout path, and the warning still lists the ids in
flight.

Because this reader is a standalone task with no deadline, an endlessly
unterminated stream still needs a terminal state, so the drain carries a budget of
`_OVERSIZE_DRAIN_MAX_BYTES` (160 MB) and raises `OversizeLineUnrecoverable` past
it, which the loop turns into `_mark_dead`. The budget counts **bytes** and is
scoped to a single drain call — deliberately *not* a count of oversize *frames*,
and needing no cross-iteration state because every call that returns ends on a
frame boundary. A replay of properly terminated but oversize frames therefore
stays survivable frame after frame; a frame counter would reproduce the very
defect this replaces. The liveness oracle cannot substitute for the budget: it
judges by CPU/IO movement, and a garbage-spewing stream moves both, so it would
report `WORKING`.

A pending request whose response was in a dropped frame is not orphaned —
`_send_and_await` wraps every future in `wait_for(timeout=…)`, so the caller gets
a timeout; the warning names the request ids in flight at the drop so that timeout
is attributable. `AcpClient._read_message` takes the same drop-and-continue stance
by returning `None` (joining its blank-line and non-JSON paths) but keeps
`readline` and carries **no** budget: every call there is bounded by the caller's
`timeout` and the callers run their own deadlines, so the worst case is one turn
ending on its deadline rather than unbounded state.

Every session on an `ACP_BACKENDS_ACP_RUNTIME` backend (kiro, KAS, Codex) runs on
`AcpRuntime` + `AcpSessionHandle`: `AcpProvider.start()` (`providers/acp_lifecycle.py`)
branches on `is_acp_runtime_backend` and wraps an `AcpSessionHandle` in
`AcpSessionProvider` — so main chat, dashboard, cron, and subagents all run on
the runtime rather than a per-session `AcpClient`. The Claude backend keeps the
per-session `AcpClient`. Additional consumers:
`AcpRuntime` also powers the `_bg` pool, (when `agent.session_sharing` is on)
the shared parent+subagents runtime, and **Code Review Sage's `ReviewPool`**
(`apps/builtins/code_review_sage/sage_lib/review_pool.py`) — one batch-scoped
`AcpRuntime` multiplexing one `AcpSessionHandle` per PR under a concurrency
semaphore (`review.max_concurrent`, default 5, ceiling 30), spawned on batch
start and `kill()`ed when the batch drains, with each per-PR session
`destroy()`ed on completion for context isolation. Because the runtime layer has
no `audit_source`, the pool re-emits the equivalent per-tool SEL audit itself
(see the `audit_source` note above). See `providers.md` and `subagent.md`.

**A process exit has a fixed reason format.** `AcpRuntime._exit_reason` reports
`process exited (rc=N)`, and for a signal death names the signal beside it
(`rc=-15 (signal SIGTERM)`, `_rc_phrase`). A stderr line is promoted to the cause
only when `_proven_death_cause` matches a registration-throttle line or the
`no space left on device` signature (which adds the `kirocrew doctor` hint);
otherwise the redacted last stderr line goes to DEBUG, and the card keeps its
separate `stderr_tail` field.

**Death-log severity is a contract: expected teardowns are INFO, genuine deaths
are WARNING.** `_mark_dead(reason, *, expected=False)` logs the single
`AcpRuntime dead (PID …) [returncode=…] stderr_tail: …` line at INFO when the
death is a deliberate teardown and at WARNING otherwise; the flag changes
severity only — futures still fail with `AcpRuntimeDead`, queues are still
poisoned. `kill(*, expected=False)` plumbs it through, and both defaults are
fail-safe (WARNING), so `initialize()`'s failed-handshake cleanup and any
call site that does not opt in warns. A call site passes `expected=True` when the
death is deliberate: the deliberate teardowns of a healthy runtime (session
shutdown via `AcpSessionProvider`, `_bg`/subagent runtime recycling and shutdown,
`ReviewPool` batch drain), and cleanup kills that have already logged their own
cause, such as `AcpProvider`'s failed-session-setup kill, which emits a WARNING
naming the setup failure before it kills. This list is illustrative, not closed. `_mark_dead` refuses the downgrade when the process already
exited on its own (`returncode` set), so a replacement path reaping a death
the reader loop has not yet marked keeps the WARNING whenever the exit has
already been observed — best-effort: an exit the child watcher has not yet
recorded can still take the INFO path in that narrow window. The warm-pool
health sweep and the claim path (`_drain_and_claim`) follow the same rule: a
TTL recycle of a healthy provider logs at INFO, while a provider found dead —
in a TTL branch or a dead-provider branch — stays WARNING. Note the default
`agent.log_level` is WARNING, so expected teardowns are absent from
`gateway.log` unless the operator raises verbosity; that silence is the point
of the split (issue #4052).

**The death line is written before the signal, so its exit status is filled in
afterwards — on BOTH teardown branches.** A kill marks the death while the child
is still running, so the retained summary carries `[returncode=<not reaped>]`
rather than a bare `returncode=None`. `_note_reaped_after_kill` replaces the
placeholder once the reap has completed and the handle is still held, and logs one
line at the death's own severity. It is called from the POSIX ladder AND from the
Windows owned-handle drain, which returns from `_kill_inner` on its own and so
cannot inherit the ladder's call — a status recorded on one platform only is a
gap the other platform's operator pays for, since the summary outlives the log and
rides `AcpProcessDied` into a turn's error and a cron's `last_error`. The
amendment is silent when the status is still unknown: a POSIX pair of timed-out
waits, or a Windows drain that raised because it could not confirm every member's
exit, both leave `<not reaped>` standing, which is then true.

Pinned on every platform rather than on the Windows shards alone
(`test_the_windows_branch_amends_the_summary_too`,
`test_an_unconfirmed_windows_drain_leaves_the_placeholder` in
`test/test_acp_runtime_death.py`, which force the branch through
`platform_compat.IS_WINDOWS`): a branch one CI lane reaches is a branch whose loss
is invisible in every other lane. The shared kill test double drains the Windows
tree by awaiting `process.wait()` for the same reason the real
`terminate_windows_asyncio_tree` does — that await is what populates `returncode`,
so a double returning without it would report the placeholder on Windows and hide
the amendment behind its own unfaithfulness.

**An exit the reader OBSERVES retires the registry entries too, once it is
confirmed.** `_kill_inner` drops the root's `kiro_pids.txt` /
`kiro_session_pids.txt` lines after its own reap. A root killed from outside — an
OOM kill, a `pkill`, an operator — reaches no kill: the reader loop hits EOF,
`_mark_dead` unshields the pid in memory, and the two lines used to stand until
the periodic sweep's next tick, bounded only by
`SessionCleanup.MAX_TICK_INTERVAL_SECS` (measured on the nightly leak gate: a
SIGKILLed background runtime stayed tracked for 293s). Both observed-EOF returns
of the reader — the empty read, and `IncompleteReadError` while draining an
oversize line — now call `_retire_tracking_after_exit`, which waits for the exit
under the kill path's `_KILL_REAP_TIMEOUT`, amends the death summary with the
measured code (logged as `reaped after an observed exit`, a literal template
distinct from the kill path's `reaped after kill`, because a log line that says
"killed" must name who killed), re-probes `pid_exists`, and retires the ROOT's
lines off the loop (`asyncio.to_thread`, since the untrack takes the file locks
the sweep contends for). EOF is not the only confirmed-exit observer: the
`_exit_watch_loop` task — started beside the reader at spawn and cancelled beside
it in teardown — polls the root's own `process.returncode` and calls the same
`_retire_tracking_after_exit` on the exit. It polls rather than awaiting
`process.wait()` for a load-bearing reason: `wait()`'s waiters are woken only
from the event loop's `_call_connection_lost`, after every pipe transport
disconnects, so a descendant holding stdout open parks `wait()` exactly as long
as it parks the reader — awaiting it would make the watcher inert for the very
case it exists to catch. `returncode` is set by `_process_exited` (the
SIGCHLD/`waitpid` path) the instant the root is reaped, independent of the pipes,
so the poll sees the exit regardless of the still-open stdout.
It covers the case EOF cannot: a
descendant that inherited the root's stdout write end holds that pipe open after
the root dies, so the reader parked on `readuntil` never sees EOF and the death
would otherwise stay invisible until the survivor exits or idle expiry force-reaps
(~1h). It keys on a non-`None` returncode, never on a closed pipe, so the "closed
stdout is NOT an exit" contract holds; `_mark_dead` is idempotent. Before the mark the
watcher drains the final frames in bounded slices: it sleeps `_EXIT_DRAIN_GRACE`
(0.05 s) at a time for at most `_EXIT_DRAIN_MAX` (1 s), continuing while the reader
keeps stamping activity or holds buffered input, so a completed turn's last
response is routed and a same-instant EOF drives the reader's path first, so the two
observers never double-retire. The write is bound to the PROCESS, not the number: the
reader has no process left to re-check, and the freed number can be handed to a
replacement root this gateway just tracked before the write lands, so
`session_pid._untrack_root_by_identity(pid, start_token)` removes, under the
session file's lock, only the line equal to `<gw>:<pid>:<start_token>` where the
token is the one `AcpRuntime` read at spawn (`_spawn_start_token`); a different
or absent token, or no token to compare, touches nothing. The bare
`kiro_pids.txt` line carries no identity, and a successor handed the number may
have no session line of its own yet (its spawn has appended the bare line and not
reached the session write), so the session file cannot say whether the number has
a new holder — the kernel can: `_untrack_pid_if_dead` probes `pid_exists` INSIDE
`_pid_file_lock`, the lock `_track_pid` appends under, and removes the bare line
only when the number is dead at that moment. A live holder keeps it: it cannot be
mid-append while the probe holds the lock, so its bare line is either already
there or is the one its own `_track_pid` writes next, and the sweep prunes any
dead bare line within its tick. `pid_exists` treats EPERM as alive. The two
per-file locks are taken in turn, never nested, and no lock spans the pair. The
call reports whether every write it owed committed — a refused `kiro_pids.txt`
rewrite is reported as `False`, not as a clean retirement. Every other doubt
retains too: a closed stdout is not an exit (a backend can close its pipe and
keep running), a wait that times out or a pid that still answers keeps the lines,
and an untrack that raises is logged at debug like the kill path's. Descendant
lines are deliberately left alone — a child that outlived the root is reparented
and still running, and its own `kiro_pids.txt` line is what the sweep reaps it
by. Pinned by `test_observed_exit_retires_registry_entries_once_reaped`,
`test_observed_exit_keeps_tracking_while_the_root_still_runs` and
`test_eof_inside_an_oversize_line_retires_the_same_way` in
`test/test_acp_runtime_death.py`, and by the `_untrack_root_by_identity` cases in
`test/test_pid_lifecycle.py`.

**Every writer of a root's registry lines is bound to the identity read at
spawn.** Both spawn paths (`AcpRuntime._spawn_admitted`, `AcpClient._spawn`) read
`_pid_start_token` ONCE, keep it as `_spawn_start_token`, and hand it to
`_track_session_pid(pid, start_token)`, so the line written and the line later
compared are one read of the identity rather than two — two probes of one number
are two reads that can disagree once the number changes hands. `_track_session_pid`
keeps ONE line per `gw:pid` number: the exact entry already present is a re-track
and is left alone; a line under the same number carrying a DIFFERENT token is a
predecessor's — the caller is recording the process that holds the number NOW, so
whatever that line named has exited — and it is REPLACED rather than kept (a kept
stale line is the hole identity-bound retirement fell through: the successor never
got a line, so nothing about it was ever in the file); a token-less write never
replaces a tokened line, since it has no identity to offer; a refused replacement
raises, like a refused append, because a live root recorded nowhere is the one
unrecoverable direction. The kill paths retire by identity too: `_kill_inner` and
`AcpClient._reset_state` call `_untrack_root_by_identity(pid, _spawn_start_token)`
after their reap — "confirmed dead" is a fact about the process, not about its
number, which a root spawned since can already hold, and the prefix-matched
untrack they used before took the successor's lines with it. When identity
retirement finds no line of ours (spawn's append failed, or a successor already
replaced it) the bare line goes through `_untrack_pid_if_dead`, never by number
alone; a root whose identity could not be read at spawn has no token to compare
and keeps the prefix-matched untrack it always had. Before replacing another
line, `_track_session_pid` re-reads the number's identity under the lock and
writes only while it still names the process the token belongs to: a LATE tracker
— one running after its own root died and the number was handed on — would
otherwise take the live successor's only record for a stale predecessor's; the
re-read is an occupancy check, never the source of what is written. `AcpRuntime`
now runs its tracking pair off the loop in one executor hop, as `AcpClient._spawn`
does: each tracker takes an exclusive file lock and, on a recycled number, now
rewrites the file under it. The hop is an await between the method's two reap
guards, so its own guard first WAITS for the worker (an append landing after the
reap has untracked the pid would resurrect a line for a dead, recyclable number),
then reaps, then lets the cancellation through — and both steps run as one task
the guard only waits on behind `asyncio.shield`, absorbing a repeated `cancel()`
(a newer slot signal, then a slot deletion, both target the same eager-spawn task)
until the reap has settled, so being cancelled twice cannot skip it. Pinned by the `_track_session_pid` cases in
`test/test_pid_lifecycle.py` and `test/test_pid_lifecycle_tracking_integrity.py`,
`test_reset_state_retires_by_identity_when_a_spawn_token_is_held` in
`test/test_pid_lifecycle_teardown.py`, `TestAcpRuntimePidTracking` in
`test/test_acp_runtime_death.py`, and `TestRuntimeRootTrackingOffLoop` in
`test/test_acp_spawn_offload.py`.

### Codex MCP result envelopes

The parser extracts `rawOutput.result.content` text from successful Codex MCP
results before session-directive decoding. Call identity and raw arguments stay
on the native MCP call, including calls issued through `functions.exec`. A
result wrapper is not an identity source. The session consumer binds fallback
delivery to the identified tool's arguments and the requesting session/turn.

The Codex spawn environment sets `DISABLE_MCP_CONFIG_FILTERING=true` so the
adapter honors the session's MCP overrides even when a global configuration
contains the same server name. This applies to both create and load; it changes
configuration precedence, not authentication or the sandbox's credential mask.

The Codex spawn environment also sets `CODEX_SQLITE_HOME`, so concurrent
`codex app-server` processes (Codex Desktop and each Crew runtime) do not share
one set of SQLite databases and fail new sessions with `database is locked`. The
harness names the need (`SpawnPlan.private_state_env`) and the runtime points the
variable at its own per-process scratch directory -- the one `TMPDIR` already
names, allocated before the sandbox wrap and reclaimed by
`agent_scratch.sweep_dead_scratch` once the process is dead -- so no new tree,
lock or slot exists. Only the databases move: config, auth and the thread
rollouts stay in `CODEX_HOME`, and a thread resumes from its rollout (measured on
codex 0.159), so `spawn_continue` works across runtimes. A value the operator
set, or one a cron or workflow `extra_env` carries, reaches the child as set.
Without a scratch directory nothing is set and one warning is logged: the child
gets codex's shared default rather than a refused spawn.

The scratch directory lives and dies with its process, so each new chat, resume,
subagent, cron run and knowledge worker that starts Codex begins on an empty
database home. Left empty,
codex rebuilds its index from the rollouts in `CODEX_HOME`, and that cost scales
with the user's Codex history: about 60 s and 400 MB per runtime at 3,200 threads
on codex 0.159, and past the 90 s `initialize` timeout at about 17,000 rollouts,
where every retry restarts the same rebuild. So the harness also names a seed
(`SpawnPlan.private_state_seed`, `acp/codex_sqlite.py`), which the runtime calls
on the spawn worker before the child exists, once scratch is allocated and only
when nobody chose a location for the variable. It copies `CODEX_HOME/state_5.sqlite`
into scratch with SQLite's online backup (committed WAL pages included, the
source left unchanged) only when that index reports `backfill_state.status =
'complete'`, inside a 10 s budget, through a staging file renamed into place.
Only the state index is copied, never credentials or the log database, and each
runtime still owns its own copy. A missing, incomplete or unreadable source, an
existing destination, or any copy error leaves scratch as it was, and codex
falls back to its cold initialization. An operator who sets `CODEX_SQLITE_HOME`
in the gateway's own environment (for a systemd user unit,
`Environment=CODEX_SQLITE_HOME=%h/.codex` in a drop-in) gets the shared index
with no seed, and gives up the isolation from other `codex app-server`
processes that this section exists for.
