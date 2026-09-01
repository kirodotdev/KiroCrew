# ACP Client Module

## Overview

The ACP layer spans **five** modules: the legacy per-session client (`acp/client.py`, one subprocess per session), the multiplexed runtime (`acp/runtime.py`, one subprocess fanned out to N sessions), the per-session handle (`acp/session_handle.py`, one `sessionId` + queue + prompt/approve/reject loop), a shared dispatch parser (`acp/_dispatch.py`, pure frame-shaping/redaction helpers all paths route through), and the session provider (`acp/session_provider.py`, `AcpSessionProvider` adapting an `AcpSessionHandle` to the `LLMProvider` ABC so runtime-backed sessions are interchangeable with `AcpClient`). All are JSON-RPC 2.0 over stdio for `kiro-cli acp` or `claude-agent-acp`, managing subprocess lifecycle, session initialization, prompt streaming, and tool permissions. All protocol constants in `acp/types.py`.

## Backend Selection

`AcpClient(acp_backend=...)` selects which subprocess to launch:

- `""` (default): `kiro-cli acp --agent <name>` (resolved by `_resolve_kiro_bin`). Per-session kiro settings are layered in via the workspace overlay `<work_dir>/.kiro/settings/cli.json` (written by `AcpProvider`, not the client): reasoning **effort** (`chat.modelDefaults`) and **MCP Tool Search** (`toolSearch.enabled` + activation thresholds from `agent.tool_search_min_pct` / `tool_search_min_tokens`, gated by `agent.tool_search`, default on) — see providers.md.
- `"claude"` (`ACP_BACKEND_CLAUDE`): `claude-agent-acp` (resolved by `_resolve_claude_acp_bin` → `(list[str] | None, str)` (argv plus the augmented PATH actually searched)). Resolution order: `CLAUDE_AGENT_ACP_BIN` env var, then the **vendored copy** (`_resolve_vendored_claude_acp` — `<node_modules>/@agentclientprotocol/claude-agent-acp/dist/index.js` found under the package's `_vendor/node_modules` from the distribution bundle, the sibling `KiroCrewWebsite/node_modules` in a source checkout, or `KIROCREW_PROJECT_DIR`; needs no global npm install or network — matters on hosts that have no package-registry token at gateway runtime), then `mise which claude-agent-acp` (respects MISE_DATA_DIR and all mise config), then a direct glob under mise's Node installs dir (`_mise_node_installs_dir` — `<mise-data>/installs/node`, root from `env.mise_data_dir` so MISE_DATA_DIR / XDG_DATA_HOME are honoured), then augmented PATH (`env.augmented_path` — mise shims, `~/.npm-packages/bin`, `~/.volta/bin`, `/opt/homebrew/bin`, plus EVERY per-version manager bin dir via `env.node_all_bin_dirs` (mise/asdf/nvm/fnm, all installed versions — a global npm binary can live under any of them), so a non-login launchd/systemd gateway also finds globally-installed binaries). The adapter is vendored into the distribution bundle and the pip build by `setup.py` (`_vendor_acp_into_pkg` → `kiro_crew/_vendor/node_modules`), so every install method ships it without asking the user to `npm i -g`. Vendoring copies the adapter **plus its full transitive dependency closure** (`_acp_dependency_closure` walks `dependencies`/`optionalDependencies` from the resolved website `node_modules`, ~96 flat top-level packages) — npm hoists deps like `@agentclientprotocol/sdk` flat, so copying only the adapter package crashes the ESM loader with `ERR_MODULE_NOT_FOUND`. `_resolve_vendored_claude_acp` accepts a root only when the hoisted dependency marker `@agentclientprotocol/sdk` is present alongside the entry, so an incomplete vendored copy is skipped in favour of a complete one instead of being spawned and crashed. For scripts under mise installs, returns `[node_binary, script_path]` to bypass `#!/usr/bin/env node` shebang resolution which fails in non-interactive daemon contexts. For standalone binaries, returns `[binary_path]`. Pre-spawn the client writes `<work_dir>/.claude/settings.local.json` with `defaultMode: default` so the adapter routes every tool decision back to Kiro Crew via `session/request_permission`. This makes claude-agent-acp participate in the same approve / trust_reads / trust / yolo protocol as kiro-cli — dashboard, subagents, channel agents, cron, and heartbeat all share the path. Kiro Crew still enforces per-tool security via `HooksConfig.auto_deny_tools` (evaluated by `HookManager.on_tool_call` in `hooks.py`) on every `session/request_permission` event. `CLAUDE_CONFIG_DIR` (an isolated config root, distinct from the project-scope `<work_dir>/.claude/settings.local.json` the client writes itself) is **not** set by this core: `_spawn` merges a caller's `extra_env` into the child environment, so an edition can point the adapter's `SettingsManager` and the SDK at a seeded root, but with nothing supplied they read the user's global `~/.claude` — which is a live gate-bypass hazard for inherited `permissions.allow` entries, recorded as a known gap in claude-code-provider.md. The env also carries `CLAUDE_CODE_EXECUTABLE` (claude backend only, set in `_spawn` when unset): the adapter delegates the model turn to `@anthropic-ai/claude-agent-sdk`, which needs a per-platform native Claude binary (~250 MB each) shipped as npm `optionalDependencies` that the website install omits — so the vendored closure does **not** include it and the SDK fails `session/new` with `Claude native binary not found for <platform>`. The SDK does **not** search PATH for `claude` itself (so the host merely having the external agent CLI installed is not enough), and bundling a quarter-GB binary per platform is not viable; instead `_resolve_claude_code_executable` finds an existing `claude` (`CLAUDE_CODE_EXECUTABLE` override → `mise which claude` → augmented PATH incl. `~/.toolbox/bin`, where a managed distribution may ship the external agent CLI) and the adapter forwards it to the SDK as `pathToClaudeCodeExecutable` (no version check). If none is found the var is left unset (with a warning) so the adapter's native-binary error surfaces rather than a guessed bad path; an explicit operator-set value always wins.

When the Claude adapter is not found, the spawn error reports the augmented PATH
captured by that resolution attempt. The failed result and its PATH are cached
together so a later environment change cannot make the diagnostic claim it
searched elsewhere.

**Kiro executable resolution at spawn.** Trust is "the CLI runs": any resolvable
executable Kiro CLI launches for ACP, regardless of install source, owner, or
fixed path — KiroCrew is not the authority on where Kiro CLI is installed, and
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
returns the resolved path; `TrustedAcpExecutableSnapshot` now carries just
`launch_path`.

**The CLI is always launched IN PLACE — never from a copy.** KiroCrew execs the
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
  resolved, **not** its realpath.

**Removed: the resolve-to-exec integrity snapshot.** An earlier design copied the
resolved bytes into a private location and executed that instead — a sealed
`MFD_ALLOW_SEALING | MFD_EXEC` memfd on Linux (executed as `/proc/self/fd/<fd>`),
a verified copy under `<data-home>/run/kiro-cli-snapshots` on macOS (and on Linux
interpreters lacking `os.memfd_create`) — so a binary swapped between resolve and
exec could not reach the running process. That is **deliberately gone**, along
with the descriptor registry, `pass_fds` inheritance, the off-loop
close/unlink cleanup, and `platform_compat.seal_memfd`.

The rationale: the threat it closed is an attacker who already has write access
to the user's own machine, which the rest of the product does not defend against
either — while the cost was breaking every multi-call and multiplexer install
outright. Do NOT reintroduce a copy-then-exec strategy for the Kiro CLI. The
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

**Unknown server→client requests are answered, never dropped.** `session/request_permission` is the only inbound *request* Kiro Crew implements. Any other server→client request (method **and** id — e.g. `fs/read_text_file`, `terminal/create`) is classified by `_process_message` as `"server_request_unknown"`. Every prompt dispatch site (`send_message_stream`, `_dispatch_events`, `_read_prompt_response`) handles that action by calling `_reject_unknown_server_request`, which replies with a JSON-RPC `-32601` (`JSONRPC_METHOD_NOT_FOUND`, "Method not found") error via `_send_error`. Without this, JSON-RPC semantics leave the agent blocked forever on an unanswered request — the turn hangs. Notifications (method, no id) are unaffected and still classified `"skip"`.

`PermissionOption` field names differ between backends — kiro-cli uses `id`/`label`, claude-agent-acp uses `optionId`/`name` (per the public ACP spec). `_build_permission_event` reads both and remembers the optionIds keyed by `kind` (`allow_once`/`allow_always`/`reject_once`/`reject_always`) on the request id — recording an entry when **either** an allow option (for `approve_tool`) **or** a reject option (for a clean `reject_tool`) was advertised. `approve_tool(request_id, *, always=False)` echoes the matching allow id back, so the host doesn't need to know whether it's talking to kiro (`"allow_once"`/`"allow_always"`) or claude-agent-acp (`"allow"`/`"allow_always"`). `reject_tool` prefers a **clean reject**: if a reject optionId was advertised it sends `outcome: "selected"` with that id. Both backends advertise one — claude-agent-acp as `{kind:"reject_once", optionId:"reject"}` (→ `behavior:"deny"`), kiro-cli as `{kind:"reject_once", optionId:"reject_once"}` — and the fallback to `outcome: "cancelled"` therefore only applies to a backend that advertises no reject option at all. The distinction is load-bearing, not cosmetic: a clean reject resolves the tool call to `status:"failed"` with kiro-cli's fixed content `"User denied tool execution"` and the turn continues to the next model-inference boundary (`stopReason: "end_turn"`), whereas `cancelled` ends the turn immediately with `stopReason: "refusal"` and no text — and drops any queued `_session/steer` as `AgentExecutionUserMessageCleared`. That is why the host's in-band deny notice (`_steer_policy_notice`) can only be folded in on the clean-reject path, and why `stopReason: "refusal"` is NOT by itself evidence of a model-side content refusal.

Both the shared runtime and the legacy direct `AcpClient` route permission
frames through `_dispatch.build_permission_event`, including the same provenance
flags. A shell-cache hit whose value is `False` sets `shell_classified=True` —
it is a resolved non-shell call, not a cache miss — and a structured-params
cache hit sets `raw_params_trusted=True`. The raw-params cache is read without
consuming it, so a repeated permission frame for the same `toolCallId` keeps the
original tool-call arguments authoritative instead of falling back to the
permission frame's agent-authored inline input. A genuine miss may carry inline
data for display, but both provenance flags remain false and consumers that
need trusted arguments fail closed.

The host always sends one-shot approvals (`always=False`, the default). KiroCrew — not the agent — owns the trust scope (`slot._trust`, `slot._trust_reads`, `slot._trusted_patterns`, `safety_override`, `channel.trusted`, parent session `approval_policy`). Per-call `session/request_permission` is required so KiroCrew's PreToolUse hooks (`auto_deny_tools`, sensitive-path checks, credential redaction) fire on every tool invocation. The `always=True` path is reserved for a future "skip KiroCrew hooks for this exact tool" feature; no caller passes it today.

The rendered tool-input cache is consumed by the first permission event, but
structured raw params remain keyed by `toolCallId` for the whole turn. A repeated
permission for the same call therefore retains the fact that a non-shell MCP tool
had arguments; it cannot be reclassified as an inputless canonical tool and match
session durable trust merely because the display cache was already consumed.

A remote (HTTP) MCP server's initial `tool_call` legitimately streams an empty or
absent `rawInput`, so the params cache stays empty and every child permission
request for such a tool is low-fidelity (`AcpEvent.child_low_fidelity`). The
`_meta.kiro` identity caches are written unconditionally from the same frame, so
the permission event still carries the verified `mcp_server_name`/`tool_name`
pair plus the explicit `mcp_identity_trusted` provenance flag (set only when
BOTH cache reads hit — mirroring `raw_params_trusted`, so an inline fallback
can never count as verified); `AcpEvent.child_mcp_identity_trusted` exposes
that verified-identity half (arguments unverified) and
`AcpEvent.child_unconditional_grant_eligible` hoists the grant-eligibility
expression for the unconditional grant paths documented in
`security.md` § Child-fidelity split.

The handshake also branches on the backend:

- `protocolVersion` in the `initialize` request: kiro-cli expects the date string `"2025-08-22"`; claude-agent-acp expects an integer (`1`, per the upstream ACP SDK schema).
- claude skips `session/set_mode` and uses `session/set_config_option` (configId `model`) instead of `session/set_model`.

Sending the wrong shape yields `-32602 Invalid params` or `-32601 Method not found`.

**`clientCapabilities` in the `initialize` request.** Both transports (`AcpClient._initialize_session` and `AcpRuntime`) send the shared `ACP_CLIENT_CAPABILITIES` dict from `acp/types.py`. Previously the key was omitted entirely, so the agent assumed the all-false default.

| Key | Value | Why |
|---|---|---|
| `fs.readTextFile` / `fs.writeTextFile` | `false` | We serve no `fs/*` handler; advertising them would invite requests that hit `_reject_unknown_server_request`. |
| `terminal` | `false` | Same — the agent uses its own tools. |
| `elicitation` | `{form: {}, url: {}}` | **Forward-bet.** kiro-cli 2.14.0 compiles the `elicitation/create` schema (form + url modes, `requestedSchema` with `enum`/`oneOf` single-select and array multi-select) and gates it on this capability, but does **not** yet route an MCP server's `elicitation/create` out over ACP — a stub MCP server issuing one gets `-32601 method not found`. Declaring it costs nothing today and makes the richer native prompt available the moment upstream ships the bridge. **Consequence to accept:** once the bridge lands, inbound `elicitation/create` requests will be rejected by `_reject_unknown_server_request` until a handler is wired — the same failure mode as today, but then attributable to us rather than upstream.

**Request-id namespaces are independent.** Our outbound requests (prompt, initialize, set_model, ...) use `_next_req_id()`; the agent's inbound server→client requests (`session/request_permission`) carry their own id counter. The two collide on small integers, so `JsonRpcMessage.is_response_for(req_id)` requires both `id == req_id` **and** `method is None` — a response never has a `method`. Without the `method is None` guard, a permission request whose id equals the in-flight prompt's `req_id` was misclassified as that prompt's completion in `_process_message`, ending the turn early and leaving the tool's permission unanswered → the agent turn hangs on follow-up messages (the agent waits forever for a `session/request_permission` response that never comes).

This same method-aware discipline is enforced in `_wait_for_response()`. While it awaits a specific `req_id`, an inbound server→client **request** (method + id — e.g. a colliding `session/request_permission`) or a **foreign-id response** (id ≠ req_id, no method) must not be misread as the awaited response, must not be dropped, and must not be re-appended to `self._buffer` and `continue`-d. The last is the critical hazard: `_read_message()` pops `self._buffer` first, so re-buffering + looping immediately re-reads the same frame and **spins until the deadline** (the original bug — stuck `init`/`load`/`set_config_option` ending in `AcpTimeoutError`). Instead, non-matching survivable frames are collected into a **local `deferred` list** and re-injected at the **front** of `self._buffer` *in arrival order* once the matching response arrives (or on timeout/shutdown), so a later `_prompt_loop`/`_process_message` can still answer a deferred permission request. Notifications (method, no id) continue to go to `_mcp_notifications` for `_drain_notifications`.

### Removed agent-renderer translation (cc_agent.py, deleted)

When the removed agent renderer generated its agent artifacts, `cc_agent.py` translated kiro-native field names to the removed provider's equivalents using module-level translation tables:

- `_KIRO_TO_CC_TOOL_NAME` — maps kiro tool names (`fs_read`, `execute_bash`, `shell`, `code`, etc.) to the removed provider's names (`Read`, `Bash`, `Edit`, etc.). `@server` prefix becomes `mcp__server`. `use_aws` is dropped (no equivalent).
- `_KIRO_TO_CC_HOOK_EVENT` — maps kiro hook events (lowerCamel: `preToolUse`, `agentSpawn`) to the removed provider's hook events (PascalCase: `PreToolUse`, `SessionStart`).
- `_translate_matcher(glob)` — converts kiro glob matchers to the removed provider's regex matchers (escapes regex metacharacters, `*` becomes `.*`, `?` becomes `.`).

MCP server fields translated: `disabled: true` entries are omitted; `autoApprove: [tool]` maps to `mcp__<server>__<tool>` in settings allow-list; `disabledTools: [tool]` maps to agent-level `disallowedTools`.

## Agent Configuration

Data-driven — no code changes needed:
- `config/defaults.json` — base config (tools, model, permissions), resolved via `_BUNDLED_CFG_DIR` in `agent.py`
- `config/prompt.md` — system prompt, resolved via `_BUNDLED_CFG_DIR` in `agent.py`
- `~/.kiro/crew/agent.json` — user overrides (optional)
- Run `kirocrew setup --agent-only` after editing

Note: there IS a top-level `agents/` directory used at runtime for project-level overrides, but the bundled source lives in `src/kiro_crew/config/`.

Default model: `claude-opus-4.8`. Default tools: `execute_bash`, `fs_read`, `fs_write`, `code`, `grep`, `glob`, `use_aws`, `web_fetch`, `web_search`, `introspect`, `session`, `report`, `@kirocrew-cron`, `@kirocrew-core`.

**Agent compatibility repair** (`agent.py`): `repair_agent_configs()` is the single
entry point (called at install, gateway startup, and periodically ~60s). Its
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

## Custom Agent Support

Custom agents (AIM-installed or user-created) are fully supported. The `--agent`
flag passed to `kiro-cli acp` at spawn time drives all configuration:

- **Model**: `set_model` is skipped for custom agents — kiro-cli uses the
  agent's own `model` field. Only the default kirocrew agent gets KiroCrew's
  configured model override.
- **MCP servers**: backend-dependent.
  - **kiro-cli**: `session/new` passes `mcpServers: []` — kiro-cli loads
    servers from the agent config (respects `mcpServers` in the agent's config
    file). Non-kirocrew agents (e.g. AIM-installed) load only their own
    `mcpServers`. The kirocrew agent loads from global `~/.kiro/settings/mcp.json`
    where `disabled` and `disabledTools` flags are respected. KiroCrew's dashboard
    MCP tab writes directly to the global config.
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
- **deniedCommands**: Enforced by KiroCrew's `_enforce_denied_commands()` on
  all agent configs regardless.

Custom agents use cold start with `--agent <name>` flag at spawn time.

## Protocol Flow

`initialize` → `session/load` or `session/new` → `set_mode` (conditional) → `set_model` (conditional) → drain notifications → `session/prompt`

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

### Session Resume via `session/load`

When `set_resume_session_id(sid)` is called before `ensure_ready()`, the client
attempts `session/load` instead of `session/new`:

1. Check `agentCapabilities.loadSession` from `initialize` response
2. Verify `~/.kiro/sessions/cli/{sid}.json` exists on disk
3. Send `session/load` with `sessionId`, `cwd`, `mcpServers` (the pooled
   broker stubs, re-declared so the resumed session keeps talking to the
   shared gateway — `session/load` re-initializes the session's MCP servers,
   so an empty list would un-pool the session; `[]` only when the gateway is
   disabled), and `_meta: {"_kiro.dev/session_file": "<path>"}` (required —
   without it kiro-cli silently ignores the request). `AcpRuntime.load_session`
   builds the same params for the multiplexed runtime.
4. On success (response contains `modes`): set `_session_id`, `_resumed = True`
5. On failure (JSON-RPC error, timeout, file missing): fall through to `session/new`

The resume ID is consumed on attempt (no retry loop). After successful load,
`client.resumed` returns `True` — callers use this to skip thread history injection.

Step 3 (`set_mode`) is **conditional**: sent for all kiro-cli backend agents.
Skipped for claude-agent-acp backend (which does not support set_mode).

Step 4 (`set_model`) is **conditional**: only sent when `model` is explicitly
set (i.e., for the default kirocrew agent).  Custom agents skip this so
kiro-cli uses the model from their own agent config file.

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
later session.

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
| `_kiro.dev/clear/status` | `clear_status` | (none) |
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

**URL validation**: `_is_safe_oauth_url()` rejects non-http(s) schemes before dedupe — an unsafe URL doesn't consume the dedupe slot.

**Persistence**: Role-aware redaction (`_redact_meta_for_role`) preserves `oauth_url` for `mcp_oauth` messages so the Authorize link survives history rehydrate, while still scrubbing unsafe schemes on the read path.

**API**: `pop_pending_oauth_requests()` drains requests captured during init on
both `AcpClient` and `AcpSessionProvider` (called after `ensure_ready()`).

**Remote-gateway callback relay**: The Connections waiting card and the chat `mcp_oauth` banner both accept the failed browser return address when the browser and gateway run on different machines (the banner surfaces it behind a one-line disclosure, so any server the banner names — including user-added / self-hosted ones — can recover). `POST /api/mcp/oauth/relay` sends that address from the gateway host to kiro-cli's local callback listener. The `server` field is validated with the same `_is_valid_mcp_name` rule that governs which servers can be added at all (128-char bound); it is a bounded audit label, not a registry-membership gate. The handler is intentionally not a generic proxy: it accepts only plain-HTTP URLs whose host is in the fixed loopback set the runtime callback can produce — `127.0.0.1`, `::1`, or `localhost` (the network host is later selected from fixed literals, never from request data) — with an explicit port ≥1024 and exactly one non-empty `code` value; it rejects userinfo, fragments, other hostnames, non-loopback addresses, oversized input, and does not follow redirects. The callback URL and authorization code are never logged or returned; SEL records only the validated server name and completed/failed outcome. Minting approval URLs remains registry-only (parked decision #4286).

## Cancellation

`cancel_session()` sends a `session/cancel` JSON-RPC notification to kiro-cli's stdin. It is fire-and-forget — no response ID is awaited.

### stopReason Parsing

When the ACP agent acknowledges a cancel, the `session/prompt` response carries `result.stopReason`. `_dispatch_events` reads this field on `action == "complete"` and populates `AcpEvent.stop_reason`:

- `"cancelled"` — agent honored the cancel request (`STOP_REASON_CANCELLED`)
- `"end_turn"` — normal turn completion (`STOP_REASON_END_TURN`)
- `""` — field absent or not a dict result

### Cancel Grace Window

Setting `_cancelled = True` no longer short-circuits `_read_message`. Instead, a 10-second grace window (`_CANCEL_GRACE_SECS = 10.0`) allows the agent to deliver its `stopReason` acknowledgement. If no response arrives within the window, `_read_message` raises `AcpError("Cancel grace window exceeded; agent unresponsive")`. This preserves the escape hatch for broken agents without sabotaging cooperative cancels.

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
log since kiro-cli's cancellation is a permission decision outside KiroCrew's control.

### Stale-turn gate (`AcpClient`)

After text has streamed (`_stale_eligible`), a turn whose stdout+stderr fall silent for `_STALE_TURN_TIMEOUT` (90s) is a candidate for "treat as complete". The bare wall-clock reap this once did false-positived on a genuinely-working-but-quiet backend (a long model generation, or a spawned build emitting nothing to the pipe), ending the turn and losing all subsequent output — the *capture*-side analogue of the same blunt-timeout defect the runtime path already fixed for tool-stall. `AcpClient` now **oracle-gates** the reap, converging onto the same `LivenessOracle` (`acp/liveness.py`) contract the shared-runtime path uses: on every silent read while `_stale_eligible`, `_consult_liveness_model_wait()` calls `oracle.check_model_wait(self._pid)` (offloaded to `subprocess_executor()` under a 10s `wait_for`; degrades to `VERDICT_UNKNOWN` on any error — fail toward reaping). Consulting on **every** silent read, not only at the 90s mark, is required: the oracle needs a prior sample to compute a CPU/IO movement delta, so with readable counters a fresh oracle's first *submitted* consult returns `UNKNOWN`/`"sampling"` and a single consult at the cutoff would always reap. A missing runtime PID or unreadable counters also return `UNKNOWN`, each with its own evidence string.

The submitted future is tracked on the client, and polls while it is unfinished return `UNKNOWN` without submitting another job — so a wedged walk can no longer submit a fresh worker on every silent read. It stays tracked until it finishes **or the next liveness-state boundary retires it**, whichever comes first; a still-pending walk is deliberately detached at a boundary rather than waited on. The residual executor-occupancy bound is therefore at most one abandoned worker per boundary — turn start or process reset — rather than one per silent read: a pathological loop of turns against a permanently wedged `/proc` read can still occupy `subprocess_executor()` workers, which teardown (`_get_child_pids`) also uses. Eliminating that entirely needs a killable per-walk process or a dedicated liveness bulkhead, neither of which this gate attempts.

Both boundaries that drop a movement baseline — turn start in `_prompt_loop()` and `_reset_state()` — **retire** the liveness state through `_retire_liveness_state()`, which releases the tracked consult future AND swaps in a fresh oracle via `LivenessOracle.fresh()` (`fresh()` rather than a default construction, so an injected `/proc` root or sampling interval survives the swap). The two must retire together: replacing only the oracle would leave a walk wedged during the previous turn answering every later poll with `"prior consult still in flight"`, so the new turn would never sample its own process and the 90s cutoff would complete it early. Clearing in place is not sufficient either — a consult detached by a timeout keeps a bound reference to the instance it was submitted with, and samples are keyed without a PID, so a late write would repopulate the live baseline after that baseline was taken; since any nonzero delta counts as movement, that reads `WORKING` for a flat turn and defers its reap. Retiring confines a late writer to an instance nobody reads, which is what makes the `"sampling"` behaviour above hold. Retirement sits inside `_prompt_loop()` immediately after `_turn_lock` is acquired, which is load-bearing twice over: it is the single point every prompt path funnels through (`send_message` via `_read_prompt_response`, `send_message_stream`, and `_dispatch_events`), so no public prompt API is left carrying the previous turn's walk; and doing it under the lock stops a queued turn from clearing the *active* turn's tracked consult and thereby allowing a second walk while the first is still pending.

A retired walk that fails afterwards has its exception consumed via a done-callback attached at submission, so an ordinary probe failure is not reported as an unhandled-asyncio crash. Past the cutoff, **only `VERDICT_WORKING`** (moving CPU/IO in the backend subprocess subtree) defers the turn (loop continues); every other verdict (`DEAD`/`UNKNOWN`/`STUCK_INPUT`) preserves the prior end-the-turn behavior, so hang recovery is never weakened — a genuinely dead turn still ends, bounded by the resolved prompt timeout (`_DEFAULT_PROMPT_TIMEOUT`, 2h — raised alongside `agent.chat_turn_timeout_secs` via `resolve_prompt_timeout`) and the tool-stall watchdog below. Unlike the runtime path's `session/cancel` probe, the `AcpClient` reap is a plain `return` (process-per-session: the turn simply completes; no shared runtime to protect).

### Tool-stall watchdog

While a turn is dispatching, both ACP transports run a watchdog over a turn gone silent after a tool was dispatched — and both **recover** rather than just `return` on a dead turn (`AcpClient` keeps the blanket `_TOOL_STALL_TIMEOUT` window; the session handle is verdict-driven, below):

- **`AcpClient`** (process-per-session, `_TOOL_STALL_TIMEOUT = 600s`): the stall clock is measured against `_tool_last_seen = max(last_data_ts, self._last_activity)`, so tools that keepalive-ping without emitting stdout frames (`wait`, `spawn_sub_agents`) don't trip a false stall (`_last_activity` is refreshed out of band by the stderr drain / keepalive). On a real stall it `_kill_process(force=True)` and raises `AcpProcessDied`, routing through the existing pipe-death recovery (dashboard resets the session + re-queues, bounded by `_acp_pipe_death_retries`; cron/other callers get a clean error instead of a wedged slot). `_kill_process` only touches the subprocess/pipes (never `_turn_lock`), and blast radius is one session — each `AcpClient` owns exactly one process.
- **`runtime.py` / `AcpSessionHandle`**: watchdogs are **verdict-driven, not timeout-driven** — the prior design used timeouts as death detectors and killed healthy-but-slow work (a silent 30-min redirected build `long-build > build.log 2>&1` at exactly the blanket window; healthy long non-streamed reasoning at 90s, where the destructive `session/cancel` probe was acked by the LIVE turn and surfaced as "Turn cancelled by user"). Once a turn is idle past `watchdog.check_after_secs` (60s), the per-session `LivenessOracle` (`acp/liveness.py`) returns a verdict with evidence: **WORKING** (a live cmdline-matched shell child, a `wait` tool inside its declared duration + slack, moving CPU/IO counters, backend socket bytes flowing) is never acted on at any elapsed time (logged at most once per 10 min — at INFO below the escalation mark, which is the lower of 30 min and a quarter of this turn's deadline, and at WARNING past it so a deferral able to hold the turn to its ceiling is visible at the default `agent.log_level`); **DEAD** (tracked shell child exited without a result frame past a 15s grace; model-wait with flat counters and NO established backend socket — the done-but-lost-frame wedge signature) acts immediately, so recovery lands seconds after actual death instead of at a blanket window; **STUCK_INPUT** (matched subtree flat across samples with a process blocked reading a tty/stdin pipe) acts immediately with a cause the recovery nudge names; **UNKNOWN** is the only timeout-governed class — stale probe at `watchdog.stale_window_secs` (300s; extended to `watchdog.model_silent_probe_secs` = 900s when the evidence is `established_flat`, i.e. probably a non-streamed server-side think), tool cancel at `watchdog.tool_stall_suspect_secs` (3600s / 1h — generous enough that a long build or MCP tool on macOS, where the liveness oracle degrades without `/proc`, is not falsely cancelled), hard-capped at `watchdog.tool_stall_hard_cap_secs` (3600s / 1h, UNKNOWN only). Three refinements keep the build-scale tool forbearance from sheltering an **LLM-shaped** stall (a model turn riding inside a tool, e.g. kiro-cli `use_subagent`, whose longest legitimate silent gap is minutes) or an **already-finished** one: (1) the oracle tags an UNKNOWN tool verdict with `established_flat` when the subtree's counters are genuinely flat (a real two-sample delta, not the baseline tick) AND the **runtime process itself** holds an established backend socket — deliberately narrower than the model-wait branch's whole-tree socket scan, so an MCP server blocked on *its own* remote call keeps the full tool windows — and the tool branch then uses `min(model_silent_probe_secs, tool_stall_suspect_secs)` as the effective suspect window; plain flat-subtree evidence keeps the full window, and under the OS sandbox (pid = launcher parent, no sockets on it) the tag never fires, failing toward the long build-safe window. (2) the never-matched SHELL fork is split instead of uniformly forgiven: `no matching shell child` conflated a command that already exited — a sub-second `ls | grep | wc` whose result frame was lost is never observed alive, so the DEAD branch's 15s exit grace can never fire for it — with one running unrecognized, and the two got the same 1h. The oracle now tags the first case `shell_child_absent` when the runtime's descendant tree is OBSERVABLE (a readable `/proc/<pid>/task/<tid>/children`, empty or not) and holds no live descendant attributable to this dispatch, and the tool branch then uses `min(stale_window_secs, tool_stall_suspect_secs)` — the ordinary silence budget — instead of the build-scale one. Attribution compares a descendant's `starttime` against a `CLOCK_BOOTTIME` stamp taken at the tool_call frame and widened by the turn's banked consumer parking (`_parked_total`), because `/proc` dates processes on a clock that counts suspended time while `time.monotonic()` does not, and the stamp is taken when the frame is PROCESSED rather than when the runtime spawned (a frame queued behind an approval is stamped that late). Four states each keep the full window, so every unattributable one fails toward build-scale patience: a descendant young enough to be this dispatch's, one whose cmdline matches while predating the stamp (indistinguishable from a coincidental lookalike), an unreadable child list, and a missing stamp or tick rate (no `os.sysconf` off Linux). The verdict stays UNKNOWN, never DEAD — absence is inferred, so it only shortens the non-lethal cancel. (3) An agent definition can override the windows per agent (`agents.<name>.watchdog_tool_stall_suspect_secs` / `watchdog_tool_stall_hard_cap_secs`, 0 = inherit the global — the same empty-inherits convention as the agent's `model`), applied in the `WatchdogSettings` snapshot at handle construction (`_load_watchdog_settings(crew_agent)` — a direct lookup on the CANONICAL crew name, resolved by the surface that owns the identity: the dashboard passes the slot member explicitly through `get_or_create(crew_agent=...)`, and crew-name-passing surfaces (Slack threads, cron, spawned agents) are covered by the provider factory's crew-namespace membership fallback; the identity is plumbed provider → runtime → handle, and a warm-pool claim rebinds the live handle via `rebind_watchdog()` so it travels with the SESSION, not the pool key — a name that is not a crew key simply inherits the global) so a pure-LLM agent like a PR reviewer can declare minutes-scale windows without touching the global build budget; an override is bounded by the same load-time ceiling clamp as the global windows, so it cannot smuggle a window past the prompt timeout. Every idle window is bounded at load by the resolved prompt timeout (`resolve_prompt_timeout` — the one deadline every caller shares; 7200s default, following a raised `agent.chat_turn_timeout_secs`) minus 10% headroom for the cancel + ack grace, and an over-ceiling on-disk value is clamped with a warning: a window at or past the deadline makes the UNKNOWN class unreachable, because the turn's timeout fires first and the user gets the generic turn-limit card instead of the tool-stall recovery below. A window above the DASHBOARD ceiling (`agent.chat_turn_timeout_secs`) is reported but **not** clamped — the same handle serves callers that pass their own larger prompt timeout, and shrinking their windows would cancel live work. **Every watchdog action is non-lethal:** a stale probe's cancel-ack is reclassified in the turn-complete branch (`_stale_probe` + `stopReason==cancelled` → `STOP_REASON_STALE_RECOVER`; the flag is single-shot — consumed on reclassification and superseded by a genuine `cancel()`, so a user cancel arriving after a probe is never misattributed to auto-recovery) so the dashboard auto-recovers instead of logging a user cancellation — an oracle mistake costs a regeneration, never a session. A tool stall ends the turn with `STOP_REASON_TOOL_STALL` (`"error: tool stall"`, in the `error:` family so branch-less callers degrade to generic handling) carrying the tool title / redacted command / evidence on the terminal `AcpEvent`; chat_runner's dedicated branch queues a **continue-nudge** (`build_tool_stall_recovery_prompt` — check partial results, tail any `> file` redirect target, re-run non-interactively on STUCK_INPUT) instead of the legacy verbatim re-queue of the original user message (which restarted the whole task and re-ran the very command that stalled), charged against a separate `slot._tool_stall_retries` budget (3) so a stall never burns the pipe-death reconnect budget. The runtime is **shared** (multiple sessions multiplexed on one process), so recovery is always `session/cancel` for **this `sessionId` only** (bounded by `asyncio.wait_for(..., 5s)`); siblings keep running. `watchdog.*` config is snapshotted at handle construction (`WatchdogSettings`); the dispatch loop never reads config.

**Both idle clocks measure BACKEND silence, so consumer time is subtracted from them.** `_dispatch_events` is an async generator: it is suspended at its `yield` for the whole of a consumer-side await (a tool approval, an IM send, a hook), and `last_data_ts` does not advance while suspended. Charging that interval to the runtime lets the arm cancel a turn moments *after* a human approves a tool — and at that instant the tool has not started, so the oracle draws `UNKNOWN` or `DEAD`, and `DEAD` acts immediately regardless of the window. `prompt()` therefore times each park around its single re-yield (`_parked_since` → `_parked_total`, cleared in a `finally` so an abandoned generator does not read as parked forever), and the timeout arm subtracts the park accumulated since `last_data_ts` was taken. The tool clock is exact; the stale clock can key off the newer stderr/keepalive activity, in which case part of the correction predates its reference point and is subtracted twice — which only makes that branch more patient, never quicker to probe.

**On the shared runtime the tool clock is also SESSION-SCOPED.** `AcpRuntime._reader_loop` marks a frame `fanout_no_owner` when it fans an ownerless frame (no `sessionId`) out to more than one registered session — a lone session is the sole owner and stays unmarked, so a single-session runtime behaves exactly as before. The dispatch loop advances a session-attributable twin of the pair, `last_own_data_ts` / `parked_at_own_data`, only for an unmarked frame, and both the **tool-idle clock** and the **post-compaction-failure budget** read that twin: a co-tenant's roster broadcast (`_kiro.dev/subagent/list_update`) can no longer defer either on traffic this session never produced. The **stale** clock deliberately keeps reading `last_data_ts`, because it already folds in the runtime-wide `_last_activity` (bumped on every stdout line), so runtime-global traffic is inside its contract by construction. Provenance, not the frame's method, is the discriminator — the same notification kind can arrive routed (this session's own progress) or fanned out (a co-tenant's), and only the runtime knows which. The one remaining over-count is deliberate and bounded: the tool branch's TOCTOU guard cannot know the owner of a frame that arrives *during* the oracle await (it is not dequeued yet), so it advances both clocks and defers by a single tick, the same fail-safe trade `_ingress_seq` documents at its increment.

**The turn's park is readable from outside the turn.** `parked_for_secs()`, `parked_since`, and `awaiting_permission` exist because this arm cannot report on itself: it only advances when a consumer pulls the generator, so a consumer-side await freezes it and it never executes again for that turn. `session.md`'s `stuck_turn` hook reads those accessors from a loop with its own timer. Answering a permission calls `_end_human_wait()`, which banks the human's thinking time into `_parked_total` and restarts `_parked_since`, so the in-band correction stays exact while the external reading counts only what the consumer itself has spent since the answer.

Both transports offload the oracle consult to `subprocess_executor()`, so both carry the same two obligations, and both discharge them through ONE shared guard — `liveness.consult_offloaded()`, which owns the prior-future check, the in-try submission, the submission-time exception callback, the shielded bounded await, and the degrade-to-UNKNOWN arm, so a fix to that sequence lands at both call sites at once (each caller keeps only which oracle check runs and where its tracked future lives). **One outstanding walk per liveness generation:** `_consult_oracle_offloaded()` tracks the submitted future and answers `UNKNOWN`/`"prior consult still in flight"` on any tick that finds it unfinished, so a `/proc` read wedged on a stuck fd no longer adds a blocked worker every `check_after_secs` to the pool teardown's `_get_child_pids` also draws from. The no-in-flight-tool answer is resolved *before* that guard, because it is pure handle state and needs no worker. Its exception is retrieved via a callback attached at submission — not in an `except Exception` arm, which `CancelledError` (a `BaseException`) would skip — so a probe that fails after its awaiter left is not recorded as an unhandled-asyncio crash. **Retire, don't `reset()`:** turn start in `prompt()` and every new tool dispatch call `_retire_liveness_state()`, releasing the tracked future *together with* the oracle (`LivenessOracle.fresh()`, so the per-session `wellness_sample_secs` survives). Splitting them either way is a defect: clearing the oracle in place leaves a detached walk writing into the live baseline (samples are keyed without a PID, and any nonzero delta counts as movement), while replacing only the oracle leaves a walk wedged in the previous generation answering every later tick "still in flight" so the new generation never samples its own process. The tool path has a sharper version of the first hazard than the capture path does: a walk carrying the *previous* tool's `ToolCallState` matches a descendant of the previous command and stores it as `_tracked_child`, after which `_check_shell_child` reports `WORKING "shell child N alive"` for the new tool against an unrelated process. Retirement is not a change to the cross-tick tracked-child contract itself — `fresh()` starts in exactly the state `reset()` produced, and the consult binds `self._oracle` at submission, so ticks after a boundary accumulate on the new instance as before.

**Before adding an await to a consumer branch**, read
`../../architecture/design-notes/tool-stall-watchdog-placement.md`. Both
watchdogs above are inside the generator, so a new consumer-side await silently
widens the class of failure neither of them can see; the note records which
failure classes are detectable here and which must be judged out of band.

### Model-substitution advisory

kiro can return a `-32603` error that is an *advisory* that it substituted a different model, not a fatal failure. `_is_model_substitution_advisory()` (with `_extract_advisory_detail()` for the human-readable reason) recognizes this shape, and the session stays alive and continues the turn instead of tearing down — a real fatal error still propagates.

## Session Update Handling

`_extract_text_chunk()` handles two update types for text streaming:

- `agent_message_chunk` — standard text/content. Detects `type: "thinking"` or `"reasoning"` content blocks for extended thinking (kiro-cli style).
- `agent_thought_chunk` — dedicated reasoning update emitted by `claude-agent-acp`. Always treated as thinking content.

`_track_usage_update()` tracks context window usage from `usage_update` session events, reconciling the frame via the shared `parse_usage_update()` (flat `update.used`/`update.size` primary, nested `update.usage.*` fallback) so `AcpClient` and `AcpRuntime` read the same shape regardless of which kiro emits. A `KNOWN_SESSION_UPDATES` frozenset in `acp/types.py` suppresses false "unhandled session update" logs for plumbing-only update kinds (`plan`, `available_commands_update`, `current_mode_update`, `config_option_update`, `session_info_update`, `user_message_chunk`, `tool_call_update`). Only genuinely unknown kinds are logged. On the **KAS backend**, three of these are not plumbing-only: `current_mode_update`, `config_option_update`, and `session_info_update` are consumed as display signals. KAS folds signals that kiro-cli sends as separate top-level `_kiro.dev/*` methods (agent switch, per-turn metadata, compaction status) into these `session/update` discriminants, so a KAS-gated branch in `AcpSessionHandle._handle_update` maps `current_mode_update` → agent-switch echo, `config_option_update` → effort-option state, and the `session_info_update` `_meta.kiro` union (`context_usage` → context meter, `turn_completion` → per-turn credits, `summarization_*` → compaction status). kiro-cli never emits these discriminants, so the branch is gated to KAS only and the kiro path is untouched.

**Context-window backfill.** kiro 2.10+ metadata may carry only a context-usage *percentage* (no absolute token counts). `_backfill_context_window(pct)` derives the window and used-token counts from the central `model_registry.model_window(self._resolved_model_id or self._model)` authority (gated on `has_known_window` so an unknown model is never backfilled with a guessed window) and the percentage, so the dashboard token text still renders when only a percentage arrives. `_resolved_model_id` is recorded from `models.currentModelId` (the model kiro actually served, which may differ from the requested one).

**Per-turn kiro billing credits.** `_track_metadata()` parses each `_kiro.dev/metadata` notification via the shared `parse_metadata()`, capturing `meteringUsage` entries with `unit=="credit"` (kiro bills in credits; token fields are 0 for the acp provider) into `AcpPromptStats.credits`, accumulated across the turn and surfaced on `EVENT_COMPLETE`.

**Per-turn cost and token counts (claude seam).** The `claude-agent-acp` adapter bills in cost/tokens instead of credits: a session-cumulative `cost: {amount, currency}` rides `usage_update`, and turn-scoped token counts (`inputTokens`/`outputTokens`/`cachedReadTokens`/`cachedWriteTokens`) ride the PromptResponse. Both are validated at the shared `_dispatch.py` chokepoints (`parse_usage_cost`, `parse_prompt_token_usage` — same defensive posture as `parse_usage_update`). `parse_usage_cost` additionally drops the whole cost when a `currency` is present and not exactly `"USD"`, since every consumer stores the result in USD-denominated fields; an absent currency stays accepted for adapters that omit it. and folded into `AcpPromptStats`: the cumulative cost is converted to a per-turn delta by `apply_cost_cumulative` (monotonic guard — a reading below the stored baseline means the adapter's counter reset, so the new total is taken whole rather than emitting a negative delta; the baseline survives `carry_over()` like the context fields and is dropped by `reset_context_state()`), and the token counts accumulate via `apply_prompt_token_usage` (`_track_prompt_usage` on both `AcpClient` and `AcpSessionHandle`). Every `EVENT_COMPLETE` construction site builds its `TurnUsage` through the single `AcpPromptStats.to_turn_usage()` helper, so `cost_usd` and the token dimensions populate uniformly and the per-turn persist gate fires on the claude seam. kiro-cli sends neither signal, so on the kiro path the new dimensions stay 0 and `credits` flows exactly as before (harness parity — no `_is_claude` branch anywhere on this wiring).

## Exceptions

`AcpError` (base), `AcpTimeoutError` (has `partial_output`), `AcpPermissionNeeded`, `AcpProcessDied`, `AcpAuthRequired`, `AcpPromptBusy`.

- `AcpAuthRequired` — kiro-cli is not authenticated (`kiro-cli login` needed). Non-retryable: `ensure_ready()` skips the retry ladder and re-raises so callers surface the actionable message rather than reset-and-requeue.
- `AcpPromptBusy` — a prompt is already in progress on the session, classified from kiro-cli's "already in progress" text via `_PROMPT_BUSY_RE` and raised at prompt-dispatch sites. `slack/handler.py` catches it and auto-resets the wedged session (`sessions.reset`) before recording the failure, so the next message cold-starts cleanly.

## Process Management

Subprocess lifecycle:

- Spawned with process-tree isolation for clean teardown, dispatched per-platform in `_spawn()`: **POSIX** sets `start_new_session=True` (group leader via `setsid`) so cleanup can `killpg`; **Windows** sets `creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP` (no `setsid`/process groups; an inherited Ctrl-C can't reach the gateway). Both flags are passed explicitly (never via `**dict` unpack, which breaks mypy's Popen overload resolution). Teardown in `_kill_process()` awaits `platform_compat.kill_process_tree_async(pid, SIGTERM)` then `SIGKILL` — `os.killpg(os.getpgid(pid), …)` on POSIX (inline, non-blocking), `taskkill /T /F` on Windows offloaded to `kiro_crew.executors.subprocess_executor` so the event loop is never blocked for the `taskkill.exe` spawn. The escaped-child sweep (`_kill_escaped_children`, which raw-`os.kill`s descendants that reparented out of the killed group) is **POSIX-only** — a no-op on Windows, where `taskkill /T` already walked the whole tree and `signal.SIGKILL`/`os.kill(pid,0)` are unavailable/unsafe. The `/proc`+`pgrep`+`ps` child-enumeration helpers (`_direct_children`, `_get_start_time`, `_read_basename`) short-circuit on Windows (return `[]`/`None`) since they only feed that POSIX sweep. `_resolve_ssh_auth_sock()` (called in the spawn prelude) is also a no-op on Windows — its non-darwin branch calls `os.getuid()`, absent on win32, and Windows OpenSSH uses a named pipe with no `SSH_AUTH_SOCK` to repair.
- **Off-loop PID inspection**: the PID-recycling/ownership helpers that shell out on macOS — `_get_start_time` / `_read_basename` (`ps`), `_get_child_pids` → `_direct_children` (`pgrep`), the `_capture_child_records` batch wrapper, and the `_kill_escaped_children` sweep — MUST run via `run_in_executor(subprocess_executor(), ...)`, never directly on the event loop. The PID-file tracking writes in `_spawn()` — `_track_pid`, `_track_session_pid`, `_track_child_pids` — carry the same obligation: each takes an exclusive file lock and does a read-modify-append under it, and `ensure_ready()` awaits `_spawn()` from the loop on every cold start, so an on-loop tracker serializes concurrent spawns behind one file lock with the waiter holding the loop. The subprocess spawn (fork/exec) can block, and on a wedged child the loop would freeze (the macOS wedge class). `subprocess_executor` is a *dedicated* bounded pool (distinct from the `maintenance_executor` orphan sweep) so a wedged scan/close cannot starve the recovery sweep. The `ps` and `pgrep` calls each carry a 2s timeout so no offloaded scan occupies a pool worker indefinitely.
- **Windows exe-casing normalization** (`_normalize_exe_casing`, applied to the kiro / claude-agent-acp / claude-code resolver results): `shutil.which` builds the resolved name's extension from `PATHEXT`, which lists `.EXE` upper-case, so it returns e.g. `…\kiro-cli.EXE` even though the on-disk file is `kiro-cli.exe`. A case-sensitive multiplexer shim spawned as `kiro-cli.EXE` fails to dispatch, exits instantly, and the ACP pipe breaks (`AcpProcessDied`) → the dashboard shows **"session stuck"** on the first chat turn. `os.path.realpath()` restores the true directory-entry casing. No-op on POSIX (case-sensitive FS). Runnability is checked via `platform_compat.is_executable_file()` (POSIX execute bit; on Windows the X-bit is meaningless so a known runnable extension is required instead), so a bare `.js` adapter entry is correctly treated as **not** directly runnable on Windows and gets wrapped with `node`.
- **Sandbox ownership**: `_spawn()` calls `sandbox.wrap_argv()` to wrap the command with platform-native isolation (Linux: two-stage `unshare -rm` → `unshare -U` bind-mounts + UID drop; macOS: `sandbox-exec` Seatbelt profile). On Windows, where Kiro Crew has no native OS wrapper, an explicitly classified official Kiro backend delegates to Kiro CLI's built-in sandbox; every other backend retains the no-backend fail-closed policy. The parent passes a fully scrubbed child environment on every platform, which is the enforcement point for raw Windows delegation. Configurable via `sandbox_mode` constructor param (`"auto"` default, `"off"` to disable). See `docs/system-specs/modules/security.md`.
- **Parent-level channel-credential scrub**: both spawn paths (`AcpClient._spawn` and `AcpRuntime._spawn`) build the child environment from a raw `os.environ` copy (plus `_extra_env`) and pass it directly to `create_subprocess_exec`, so they call `sandbox.scrub_agent_denied_env(env)` after merging `_extra_env` to strip `_AGENT_DENIED_ENV_KEYS` (Slack/WeCom/Telegram tokens + owner id seeded into `os.environ` by `config.loader.load_credentials`). This is required because these paths do NOT route through `sandboxed_spawn_argv`, and the OS-sandbox launcher only strips those keys for the `cc`/`strict` tiers — on the default `auto`/`standard` tier the launcher leaves them in place, so without the parent scrub they would be inherited by the agent subprocess. The scrub is deliberately narrower than `scrub_env`: it leaves the AWS/SSH env the `standard` sandbox intentionally exposes (git-over-SSH, AWS CLI, kubectl) untouched. One credential is settled per-backend rather than by the deny list: `KIRO_API_KEY` (kiro-cli's own model credential, in `_CREDENTIAL_KEYS` but deliberately NOT in `_AGENT_DENIED_ENV_KEYS`) is re-injected from the data home's `.env` via `config.loader.inject_kiro_cli_api_key` for a kiro-cli child (whose environment is where the CLI reads it — required after the Docker entrypoint scrubs it from the gateway's environ) and actively stripped via `strip_kiro_cli_api_key` for a foreign backend (Claude seam, KAS), which must never receive it; both run inside the spawn paths' existing off-loop env hop.
- `_resolve_kiro_bin()` delegates to the side-effect-free `kiro_cli.resolve_kiro_cli()` discovery module shared with first-run setup. It checks the explicit `KIROCREW_KIRO_BIN` operator/test override first, then the supported fixed install locations and augmented PATH; setup status may inspect the same candidates but never mutates the override or other process-global environment. The gateway's prerequisite service and the direct `chat`/`tui`/`run`/`consolidate`/`eval` CLI entry paths both register the override's canonical path and first-observed digest before any provider can be created; process-lifetime first-observation-wins semantics prevent a later service reconstruction from blessing replacement bytes. `runtime.py` imports and reuses the ACP wrapper so both ACP transports select the binary identically. Immediately before OS sandboxing, `sandbox.py` routes argv[0] through the edition-neutral `PlatformContext.agent_executable` resolver; the public Default is identity and a companion can return a direct executable behind an edition-managed launcher without changing the core.
- The dashboard `/api/models` one-shot subprocess validates completion before parsing stdout: nonzero exit (with a bounded, redacted stderr tail), empty stdout, malformed JSON, or a payload without a model list each returns HTTP 503 so the client retries. A subprocess failure is never misreported as `JSONDecodeError` or cached as a successful empty model list. Before the spawn, it uses `config.loader.inject_kiro_cli_api_key` off-loop just like the interactive Kiro ACP path, so a headless Docker gateway whose entrypoint moved `KIRO_API_KEY` out of the long-lived parent environment still authenticates this official fixed-argv `kiro-cli` read; the general child-environment scrub remains unchanged.
- **One-shot `kiro-cli` reads spawn at the CONFIGURED sandbox tier**, via `sandbox.configured_sandbox_mode()` (`agent.sandbox`, falling back to `"auto"` and warning when the config cannot be read — an unreadable config must not yield a looser tier). The affected sites are `/api/models` (`--list-models`), and in `handlers/sessions.py` the `whoami` identity fetch and the `/usage` text scrape. On Windows all three pass `is_kiro_cli=True`, so a default `"auto"` install delegates to Kiro's built-in sandbox exactly like interactive chat and needs no broad unsandboxed-exec opt-in. They also pass `scrub_agent_subprocess_env()` as the explicit child environment. The configured-tier seam still matters for an explicit `agent.sandbox="off"` and for platforms with a Crew backend: a one-shot read must not silently request a stricter posture than the same long-lived Kiro binary. Use `configured_sandbox_mode()` for a spawn of the same binary under the same posture as chat — **not** for spawns that deliberately pin their own tier (the prerequisite probes' `strict`, the credential-free registry clones). Governance still clamps the result up via `_clamp_sandbox_mode`, so a `sandbox.min_level` floor overrides it like any other caller-supplied mode.
  - **Accepted trade on the two `sessions.py` sites**, stated explicitly because it is a real (small) loosening on hosts that *do* have a backend: they previously pinned `"standard"`, so on Linux with an explicitly configured `agent.sandbox="off"` they now spawn with no Kiro Crew wrap where they used to hide `_STANDARD_DIRS` (`.gnupg`, `.config/gcloud`, `.azure`, `.docker`, the auth-staging dir). This is deliberate and is the *same* posture the interactive chat spawn of that identical binary already runs under on that identical host — a one-shot `whoami` cannot need stricter confinement than the long-lived chat session, and the previous asymmetry was an accident of a hardcoded literal, not a designed boundary. Both spawns are fixed argv with no agent-influenced arguments, `kiro-cli`'s own internal sandbox is the layer `"off"` defers to, and and an operator who wants the wrap back sets `agent.sandbox="auto"` — the shipped default — which then applies uniformly to chat *and* these reads instead of only to these reads. The narrowness matters: this loosening is reachable only on a host where the operator has *already* declared `"off"` and thereby accepted that posture for every chat turn, which is a far larger and longer-lived exposure than one `whoami`.
  - **All three wraps run OFF the event loop**, in `subprocess_executor()`, via one small per-site helper (`_wrap_list_models_argv`, `_wrap_argv_whoami`, `_wrap_argv_usage_scrape` → `_wrap_argv_at_configured_tier`). Two blocking reads are involved and both must land in the worker: `configured_sandbox_mode()` stats — and on a cache miss re-reads and revalidates — `config.json`, and non-delegated `wrap_argv` calls can cold-probe the backend with a synchronous `subprocess.run(..., timeout=5)`. The mode is therefore resolved *inside* the helper. Each helper passes **`is_kiro_cli=True` explicitly**: on Windows this positive classification is the security gate for Kiro's internal-sandbox delegation, and `_spawns_kiro_cli` basename inference is intentionally insufficient. Both ACP spawn paths already use the same capability-set classification; any new one-shot official-Kiro spawn must too. The helpers are deliberately *named* for the chokepoint they call because `test_spawn_audit.py` audits routed spawns structurally.
- A **genuine** sandbox refusal on `/api/models` remains possible when the spawn is not positively classified, requests extra path restrictions, cannot write its critical delegation audit, or is not the official Kiro backend. It is caught as `SandboxUnavailableError` **before** the generic `except`, and answers 503 with `code: "model_list_sandbox_unavailable"`. A normal fresh Windows install of the official Kiro CLI follows the positively classified delegation path instead.
- **Poll-driven spawn sites are readiness-gated.** `kiro-cli` auto-launches an
  interactive browser login for any subcommand run unauthenticated
  (`--no-interactive` does not suppress it; there is no opt-out env var). Every
  dashboard endpoint that shells out to `kiro-cli` on a timer therefore calls
  `reject_if_kiro_unverified()` BEFORE resolving or spawning the binary:
  `/api/models` (polled every 8s while the model list is degraded) and
  `/api/sessions/usage` (polled every 30s by the credit pill). Both return the
  shared `kiro_prerequisite_required` 503 — the same degraded response their
  timeout branches already produce — so the client contract is unchanged and
  only the subprocess is skipped. Without this gate a signed-out gateway opened
  a browser window every 8 seconds indefinitely. These are the **only** blocking
  readiness gates: ordinary sends are ungated, because a failing ACP attempt
  reports its own `AcpAuthRequired` (see the governance of latched readiness in
  `modules/learn-cron-dashboard.md`), whereas a timer-driven spawn has no turn to
  carry that error. These sites authorize on a **freshly verified** probe
  (`verified_ready`, 30s ceiling), never the bare latch — a stale `ready=True`
  would green-light exactly the signed-out spawn the gate exists to prevent.
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
- **The readiness `whoami` runs against the real home, like an ACP session.**
  `kiro_prerequisite._run_auth_command(..., isolate_home=False)` runs the
  resolved CLI against the real environment/home under the standard OS sandbox
  with only the KiroCrew data home hidden, and executes a sandbox-visible
  private snapshot of the resolved bytes (keeping the resolved basename so a
  multiplexer still dispatches). A rewritten `HOME` breaks any CLI whose session
  or tool registry lives in the real home — a toolbox multiplexer cannot even
  resolve itself — so the isolated probe reported such CLIs signed-out even
  though a real session authenticates fine.
- **Sign-in is fully delegated to `kiro-cli`.** `kiro-cli login
  --use-device-flow` runs against the user's REAL home and writes its own
  credential store, exactly as it does from a terminal. KiroCrew stages no
  credentials and copies none back — the staged-home publish path (and the
  "Kiro identity changed during sign-in" conflict two racing gateways could
  hit) is gone. The isolated credential-minimal home remains available for
  callers that opt into it, so a probe can never read the real `~/.aws` /
  `~/.ssh`; the operator-initiated login runs in the real home inside the same
  OS sandbox posture ACP already uses, with the KiroCrew data home hidden.
- 10MB stdout buffer for large JSON-RPC lines
- stderr drained in background (`_drain_stderr`) to prevent pipe deadlock. Each line bumps `_last_activity` (liveness for `is_responsive`), is appended to the bounded 20-entry `_stderr_lines` diagnostic ring buffer, and is forwarded as a redacted `WARNING`. **Exception — suppression filter:** lines matching a marker in the module-level `_SUPPRESSED_STDERR_MARKERS` tuple (currently `thinking_tokens`) are dropped — no `WARNING`, not appended to the ring buffer — but **still** bump `_last_activity`. This handles the claude-agent-acp "Unexpected case: {...thinking_tokens...}" stderr noise. **Mechanism** (confirmed by reading the vendored adapter's `dist/acp-agent.js`): claude-code emits a `system` message with subtype `thinking_tokens`, but the adapter's `switch (message.subtype)` enumerates only ~18 known subtypes (`init`, `status`, `compact_boundary`, `memory_recall`, `api_retry`, …) and routes anything else to `default: unreachable(message)`, which writes `logger.error("Unexpected case: " + JSON.stringify(message))` to stderr — one line per token delta, measured at ~10 lines/sec during active thinking (one per 2–4 thinking tokens). The payload is only `estimated_tokens`/`_delta`/`uuid`/`session_id`, so dropping it loses no response content. This is a forward-compat gap in the vendored adapter, **not** new behavior in a specific claude-code build — the `thinking_tokens` event is present in both `2.1.165.357` and `2.1.168.358` (verified by string-matching both bundled `claude` binaries), so it predates the `.168` update that drew attention to it. The cleaner long-term fix is upstream (add a `thinking_tokens` case to the adapter or bump the vendored version); this filter is the version-agnostic stopgap that also absorbs the next unenumerated subtype's flood. (Note `thinking_tokens` is by far the dominant subtype hitting `unreachable` — ~14k occurrences vs. a handful of rare `permission_denied` across retained logs — which is why the marker tuple stays narrow rather than suppressing all "Unexpected case" lines.) Two concrete reasons to drop rather than downgrade the level: (1) **log hygiene** — `gateway.log` uses `RotatingFileHandler(maxBytes=2MB, backupCount=3)` (`cli.py`), so a sustained burst rolls genuine diagnostics out of the retained 8MB window; (2) **event-loop load** — the file handler is a plain *synchronous* handler and `_drain_stderr` runs on the gateway event loop, so each forwarded line costs a synchronous file write + two regex redaction passes on the same loop that streams responses (small per session, compounding across concurrent thinking sessions). Keeping liveness prevents the idle watchdog from killing an actively-thinking turn; skipping the ring buffer stops a burst from evicting the last real errors. A throttled `DEBUG` summary (≥ `_SUPPRESSED_STDERR_SUMMARY_INTERVAL_SECS` apart, plus a flush at EOF) keeps the suppression observable. Match substrings are kept narrow so a genuine error is never silently swallowed. This is a log-volume / event-loop-load reduction — **not** a fix for any turn-stall or "agent not responding" symptom (no such causal link was established).

### Cold-start admission and startup telemetry

Every `AcpRuntime.spawn()` enters one gateway-wide, event-loop-affine admission
coordinator before subprocess preparation and holds the permit through
`initialize`. The default cap is 2, matching worker-pool `max_starting`; this is
the common backstop for interactive, authoring, background, shared, and unpooled
runtime callers, including callers that bypass `SessionManager` or a worker pool.
The coordinator is keyed by event loop so embedded/test loops never share an
`asyncio.Semaphore`; cancellation while queued or starting returns the permit,
and the existing spawn guard still kills a subprocess when initialization is
cancelled or fails. It uses only asyncio/threading primitives and has no POSIX-only
behavior.

Structured `acp_cold_start` logs distinguish queue wait and spawn, and include
bounded active/queued counts, outcome, duration, backend class, and coarse process
state. Structured `acp_startup_stage` logs distinguish `initialize`,
`session/new`, `session/load`, and `session/set_mode`; timeout records carry the
method and budget plus bounded stderr-line count. They never include prompts,
workflow source, credentials, session ids, request ids, or raw process ids.

`ensure_ready()` emits the `kirocrew.session.startup.duration` histogram (unit `ms`) timing the cold-start work — subprocess spawn + session init. The warm fast-path (an already-spawned, already-initialized session returns early) is intentionally **not** measured, since it does no startup work. The emit lives in a `finally` covering **every** exit path, with `outcome` recorded as one of `ready` / `auth_required` / `error` (defaulting to `error`, so any unexpected exception propagating through the `finally` is counted as a failure, never a false `ready`) and `spawned` (bool — whether this call actually forked a new process). `get_recorder` is **lazily imported** inside the `finally` to break the `config.loader → acp.types → acp.client → metrics.provider → config.loader` import cycle, and the entire emit is wrapped in `try/except` so a telemetry failure can never break session startup.

### Worker-pool tool audit (`audit_source`)

The `audit_source` constructor param (default `None`) tags an `AcpClient` that runs tools **outside** the chat_runner / SubagentManager audit loop — the knowledge `llm_pool` worker-pool client, whose tool calls would otherwise never reach the security audit log. When set, `_maybe_audit_tool_call()` emits a per-tool-call SEL `tool_invocation` record; when `None` (chat / subagent clients) it is a no-op so those paths never double-log. The `sel().log_tool_invocation` call is offloaded onto `subprocess_executor()` (so SEL-backend I/O can never block the event loop) and bounded by `asyncio.wait_for(..., _SEL_AUDIT_TIMEOUT_SECONDS=5.0)`; a timeout or any SEL failure is swallowed (logged at `WARNING`) so tool dispatch always proceeds. **Note:** Code Review Sage's `ReviewPool` used to be an `audit_source` `AcpClient` consumer here, but it migrated to the shared `AcpRuntime` path (see "Additional consumers" above) — the runtime layer has no `audit_source`, so the pool re-emits the same per-tool SEL `tool_invocation` audit itself from `sage_lib/review_pool.py` (preserving audit parity).

## Image Support

`_send_prompt()` auto-detects image file paths in messages (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`) via regex. When a valid image path is found:

1. Reads the file (paths over `MAX_IMAGE_BYTES` = 10 MB stay as text, not inlined)
2. Downscales so the longest edge is <= `MAX_IMAGE_EDGE_PX` (2000 px), preserving aspect ratio and re-encoding to the same format (an oversized GIF becomes a PNG still frame)
3. Shrinks further while the base64 payload still exceeds `MAX_IMAGE_B64_BYTES` (5 MiB), stopping at `MIN_IMAGE_EDGE_PX` (256 px)
4. Base64-encodes the (possibly downscaled) bytes
5. Appends an image content block: `{"type": "image", "data": "<base64>", "mimeType": "image/png"}`
6. Replaces the path in the text with `[image: filename.png]`
7. Sends both text and image blocks in the `prompt` array

This leverages kiro-cli's `promptCapabilities.image: true` capability. The LLM receives the image inline — no tool call needed.

**Dimension backstop** (`build_prompt_blocks` in `acp/prompt_blocks.py`). This shared builder is the single funnel every channel's images cross before reaching kiro-cli, so the `MAX_IMAGE_EDGE_PX` (2000 px) downscale runs for all of them — dashboard upload/paste/screenshot, Slack, Discord. Anthropic rejects the ENTIRE request when a many-image conversation (>20 images) carries any image over 2000 px on a side; because kiro-cli replays the full message history every turn, one oversized image would otherwise sit at a fixed history index and wedge the session permanently (a follow-up resize cannot evict the original). The browser's client-side resize (1568 px, `website/src/utils/resizeImage.ts`) is a token-cost optimization on top; this server-side cap is the correctness guarantee that still holds when that resize is skipped or bypassed (e.g. the native `/api/screenshot` capture, or non-dashboard channels).

**Encoded-size backstop** (`_fit_encoded_budget` in `kiro_crew/imaging.py`). The dimension cap alone does not bound the payload: a raster can sit well inside 2000 px and still encode past the backend's per-image byte ceiling. `MAX_IMAGE_B64_BYTES` is **5 MiB, read out of the backend's own rejection** rather than derived from which provider kiro-cli routes through (which we treat as opaque) — the error names the limit in bytes, `image exceeds 5 MB maximum: 6714372 bytes > 5242880`, and 5242880 is exactly 5 × 1024 × 1024. Anthropic's published per-image ceiling for Bedrock and Google Cloud agrees, which is corroboration rather than the basis. The check must run on the ENCODED payload AFTER any downscale: `MAX_IMAGE_BYTES` measures the file before the re-encode and cannot see base64's 4/3 inflation, so a ~3.9 MiB raster passes every pre-encode gate and is still rejected on the wire. Because a rejected image is replayed from a fixed history index on every later turn, this has the same wedge-the-session consequence as the dimension case. Erring low merely ships a smaller image while erring high ships a refused payload, so the cap is set to the observed value and callers can override it via `max_image_b64_bytes` if a backend ever reports a different number. `_fit_encoded_budget` applies the dimension cap, then keeps shrinking (0.8 per pass, up to 6 passes, from the rendition's OWN long edge so an already-in-cap image still makes progress) until the encoding fits. If nothing fits above `MIN_IMAGE_EDGE_PX` (256 px) it fails CLOSED — the path stays in the text and no image block is emitted, because inlining a payload the backend refuses is strictly worse than sending a reference a tool-capable agent can open.

**Reusable entry point** (`downscale_image_block` in `kiro_crew/imaging.py`). The budget constants and Pillow machinery live in that LEAF module — `prompt_blocks` re-exports them — because the second consumer is the MCP gateway's tool-result rewrite (`mcp_gateway/image_budget.py`), which runs inside the gateway daemon and must not import the ACP package (doing so pulls the whole ACP client into the broker and closes an import cycle back into `mcp_gateway`). That rewrite holds every image content block in a brokered `tools/call` response to this same budget before the response reaches kiro-cli's conversation history — the tool-result counterpart of the prompt-path backstops above (see `docs/architecture/design-notes/mcp-gateway-oversize-response.md`, Layer 3). Images produced by kiro-cli's own built-in tools never transit Kiro Crew and must be capped upstream in kiro-cli.


### Outbound-request structure diagnostics (content-free)

`summarize_prompt_structure(blocks)` in `acp/prompt_blocks.py` returns a **purely structural** summary of the outbound `session/prompt` block list — total block count, a count per block `type` (`text` / `image` / `tool_use` / `tool_result` / `other`), the number of empty (blank/whitespace-only) text blocks, the `tool_use` vs `tool_result` counts (so a pairing imbalance is visible), and `total_bytes` (the serialized `json.dumps` size, or `-1` if the content will not serialize). `AcpSessionHandle.prompt` logs this summary once per turn build at **DEBUG** (the level this module reserves for per-turn diagnostics), tagged with the `sessionId`.

The summary carries **no message content** — only counts, types, and sizes — which is a hard requirement (issue #6022): the kiro-cli data dir is fenced precisely because it holds SSO tokens, so the diagnostics must never record block text, image bytes, or tool arguments. This lets an operator tell a stale/invalid model id apart from a structurally malformed payload the next time a turn is rejected as `Improperly formed request` (see the `_RE_MALFORMED_REQUEST` classifier), without ever exposing what the turn contained. The helper is defensive by contract: it never raises into the live prompt path (a malformed block list yields a partial/minimal summary), so a diagnostics failure can never break a turn.


## AcpRuntime & AcpSessionHandle (session multiplexing)

Alongside `AcpClient` (one `kiro-cli` process per session, guarded by
`_turn_lock`), the ACP package provides **`AcpRuntime`** — a single `kiro-cli`
process that multiplexes **N concurrent sessions** via a single stdout reader
that demuxes frames by `params.sessionId` into per-session queues (no
`_turn_lock`). Each session is fronted by an **`AcpSessionHandle`**; an
**`AcpSessionProvider`** adapts a handle to the `LLMProvider` interface so it is
a drop-in replacement for `AcpClient`.

Both transports share one parser — `acp/_dispatch.py`
(`parse_session_update`, `build_permission_event`, `parse_usage_update`, …) — so
they cannot drift. `AcpRuntime.load_session()` mirrors `AcpClient`'s resume
handshake: it issues `session/load` directly under the original sessionId and
registers the session queue **after** the load response so replayed transcript
frames are dropped rather than counted against the current turn.

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

**Crew answers no credential callback; kiro-cli's relay owns KAS auth.** KAS is
reached as `kiro-cli acp --agent-engine v3 --auth-method cli`, whose relay
forwards unrelated NDJSON frames byte-for-byte and consumes
`_kiro/auth/getAccessToken` itself, resolving tokens from kiro-cli's own store.
Crew therefore never sees that frame and holds no KAS token. One consequence is
recorded in `ACP_BACKENDS_KIRO_IDENTITY_STORE`: because the relay signs in from
kiro-cli's store, a KAS runtime is retired by an external `kiro-cli logout` on
the same terms as the kiro backend. A second is that the KAS process gets no OS
sandbox of its own — the relay spawns its server without `--sandbox` and the
agent resolves an absent config to a no-op backend — so Crew's own sandbox stays
engaged for this backend and KAS is excluded from
`ACP_BACKENDS_INTERNAL_SANDBOX`.

**Off-loop answers are bounded.** The remaining off-loop answer is the
unroutable-permission auto-reject, which can block on stdin `drain()` before
writing its response, so the reader schedules it without blocking stdout demux
but keeps a strong reference in `_answer_tasks` under `_max_answer_tasks` —
every path ultimately contends for the same stdin, so a second per-kind cap
would allow the combined resource total to exceed the bound. The done callback
removes completed tasks. At capacity the reader uses a bounded discrimination
wait: one completion admits the pending answer, while no progress within the
bound marks the runtime dead so pending waiters resolve explicitly.
Server-to-client requests never take the notification counted-drop path, because
that would leave the remote requester unanswered.

**Unroutable frames are counted, not logged per frame.** The reader drops any
frame it cannot route; the drop itself is correct and unchanged, but logging one
`DEBUG` line per dropped frame is a log-retention hazard on a multiplexed
backend. Every frame for a torn-down or not-yet-registered sessionId takes that
branch — including the entire transcript replay of a `session/load` (the queue is
registered after the response, above) — and a backend that keeps streaming after
teardown makes it an unbounded **steady state**, not a burst. Measured on an
operator host: ~60 lines/second sustained for 6+ hours from one gateway PID,
33–59% of every `gateway.log` rotation, which at
`RotatingFileHandler(maxBytes=2MB, backupCount=3)` (`cli.py`) rolled the
diagnostics an incident needed out of the retained 8MB window before they could
be read. So `_reader_loop` funnels the two **frame-rate** drop paths — a frame
whose `sessionId` is not registered, and a no-`sessionId` global notification
arriving while zero sessions are registered (sentinel `_DROP_NO_SESSION`) —
through `_note_dropped_frame()`, which tallies `(sessionId, method)` and emits
one `DEBUG` summary carrying the accumulated count at most every
`_DROP_SUMMARY_INTERVAL_SECS` (60s). The key stays **per session** deliberately:
the decisive signal in the incident was that two *different* session UUIDs were
flooding at once, which a single global tally would hide. The level stays `DEBUG`
— the goal is far fewer lines, not louder ones.

Three properties make the counter safe on the demux hot path: it never awaits
(no timer task to leak — the flush rides the next drop), the map is bounded
(`_DROP_SUMMARY_MAX_KEYS` = 64 distinct keys forces an early flush instead of
growth, and both backend-controlled key halves are truncated to
`_DROP_SUMMARY_KEY_MAX_CHARS` = 80), and the residual count is flushed in the
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
drain as `mcp_gateway/backend.py::run_stdout_pump`, where a plain `read(n)` would
eat into the *next* frame. Draining the whole line rather than one prefix at a
time is load-bearing, not tidiness: the unterminated branch's discard boundary is
an arbitrary byte offset (`consumed = len(buffer)`), so surfacing the remainder as
a line hands the parser a byte-slice that can start mid-character. `json.loads`
then raises `UnicodeDecodeError`, which is **not** a `json.JSONDecodeError` — it
escapes the loop's non-JSON guard into its crash handler and kills every
multiplexed session, the very outcome this replaces. Any oversize frame carrying
CJK or emoji reaches it whenever the final remainder falls under the reader limit.

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

Every kiro session runs on `AcpRuntime` + `AcpSessionHandle`:
`AcpProvider.start()` (`providers/acp.py`) unconditionally calls
`_start_kiro_runtime()` for the kiro backend, wrapping an `AcpSessionHandle` in
`AcpSessionProvider` — so main chat, dashboard, cron, and subagents all run on
the runtime rather than a per-session `AcpClient`. Additional consumers:
`AcpRuntime` also powers the `_bg` pool, (when `agent.session_sharing` is on)
the shared parent+subagents runtime, and **Code Review Sage's `ReviewPool`**
(`apps/builtins/code_review_sage/sage_lib/review_pool.py`) — one batch-scoped
`AcpRuntime` multiplexing one `AcpSessionHandle` per PR under a concurrency
semaphore (`review.max_concurrent`, default 5, ceiling 30), spawned on batch
start and `kill()`ed when the batch drains, with each per-PR session
`destroy()`ed on completion for context isolation. Because the runtime layer has
no `audit_source`, the pool re-emits the equivalent per-tool SEL audit itself
(see the `audit_source` note above). See `providers.md` and `subagent.md`.

**Death-log severity is a contract: expected teardowns are INFO, genuine deaths
are WARNING.** `_mark_dead(reason, *, expected=False)` logs the single
`AcpRuntime dead (PID …) [returncode=…] stderr_tail: …` line at INFO when the
death is a deliberate teardown and at WARNING otherwise; the flag changes
severity only — futures still fail with `AcpRuntimeDead`, queues are still
poisoned. `kill(*, expected=False)` plumbs it through, and both defaults are
fail-safe (WARNING), so a cleanup kill on a failure path — `initialize()`'s
failed-spawn cleanup, `AcpProvider`'s failed-session-setup kill — and any
future call site warns without opting in; only the deliberate teardowns of a
healthy runtime (session shutdown via `AcpSessionProvider`, `_bg`/subagent
runtime recycling and shutdown, `ReviewPool` batch drain) pass
`expected=True`. `_mark_dead` refuses the downgrade when the process already
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
