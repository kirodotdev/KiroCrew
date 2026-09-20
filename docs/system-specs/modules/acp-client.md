# ACP Client Module

## Overview

The ACP layer spans **five** modules: the legacy per-session client (`acp/client.py`, one subprocess per session), the multiplexed runtime (`acp/runtime.py`, one subprocess fanned out to N sessions), the per-session handle (`acp/session_handle.py`, one `sessionId` + queue + prompt/approve/reject loop), a shared dispatch parser (`acp/_dispatch.py`, pure frame-shaping/redaction helpers all paths route through), and the session provider (`acp/session_provider.py`, `AcpSessionProvider` adapting an `AcpSessionHandle` to the `LLMProvider` ABC so runtime-backed sessions are interchangeable with `AcpClient`). All are JSON-RPC 2.0 over stdio for `kiro-cli acp` or `claude-agent-acp`, managing subprocess lifecycle, session initialization, prompt streaming, and tool permissions. All protocol constants in `acp/types.py`.

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
cache hit sets `raw_params_trusted=True`.

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

The host always sends one-shot approvals (`always=False`, the default). KiroCrew — not the agent — owns the trust scope (`slot._trust`, `slot._trust_reads`, `slot._trusted_patterns`, `safety_override`, `channel.trusted`, parent session `approval_policy`). Per-call `session/request_permission` is required so KiroCrew's PreToolUse hooks (`auto_deny_tools`, sensitive-path checks, credential redaction) fire on every tool invocation. The `always=True` path is reserved for a future "skip KiroCrew hooks for this exact tool" feature; no caller passes it today.

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

**`clientCapabilities` in the `initialize` request.** Both transports (`AcpClient._initialize_session` and `AcpRuntime`) send the shared `ACP_CLIENT_CAPABILITIES` dict from `acp/types.py`. Previously the key was omitted entirely, so the agent assumed the all-false default.

**`agentInfo.version` from the `initialize` response.** Both transports retain it (`AcpClient.agent_version`, `AcpRuntime.agent_version`, surfaced through `AcpSessionHandle` → `AcpSessionProvider` → `AcpProvider.agent_version`; `""` until the handshake completes). It is the version the spawned process RUNS, which after an in-place kiro-cli upgrade differs from the binary on disk — the MCP hot-reload gate reads it for that reason. Parsed with the shared `agent_version_from_init` in `acp/_dispatch.py`; a missing or non-string value reads as unknown rather than failing the handshake.

| Key | Value | Why |
|---|---|---|
| `fs.readTextFile` / `fs.writeTextFile` | `false` | We serve no `fs/*` handler; advertising them would invite requests that hit `_reject_unknown_server_request`. |
| `terminal` | `false` | Same — the agent uses its own tools. |
| `elicitation` | *(absent)* | **Withdrawn forward-bet.** It was declared while kiro-cli 2.14.0 compiled the `elicitation/create` schema but did not yet route an MCP server's request out over ACP, on the reasoning that declaring it cost nothing until a handler existed. It costs something. A client that sees the capability sends its human-in-the-loop prompts as `elicitation/create` **instead of** falling back to `session/request_permission` — codex-acp gates on `clientCapabilities.elicitation.form` exactly that way — so the declaration does not wait inertly for a handler, it diverts a working path onto one that answers `-32601`, which that client turns into a cancellation of the tool call the human was approving. Absent, every affected client returns to the fallback that works. Re-add the key **in the same change** that registers the handler: `test_elicitation_is_not_advertised_without_a_handler` fails the moment it reappears. Handler work is tracked in #891. |

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

The normal and orchestrator prompts are alternative, self-contained inputs, not
layers concatenated together; custom/app agents can supply their own prompts.
Their compact tool directories retain exact callable syntax, session ownership,
privacy boundaries, stop conditions and output formats. Shared wording is not
moved into a common include: source deduplication alone would not reduce the
selected prompt sent to the model. `test/test_prompt_compact_contract.py` checks
each file's UTF-8 size budget, template slots, critical operational clauses and
the orchestrator example against the real plan parser. The size budget is an
absolute UTF-8 byte ceiling per selectable prompt: a context-cost guard, not a
token count and not a permanent ban on new rules. A maintainer may raise a
ceiling in a reviewed change when a rule earns its bytes; the clause tests, not
the ceiling, decide whether a contract survived. Prompt tests guard text
contracts, not a guarantee that a model follows them; runtime controls remain
authoritative. The normal prompt's browser instructions keep approval groups,
borrowed-browser ownership and subagent session isolation inline rather than
relying on a skill pointer for those controls.

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
  - **kiro-cli**: kiro-cli loads ordinary servers from the agent config. The
    shared runtime adds eligible managed control-plane elements carrying the
    per-session token, plus configured broker stubs, to `mcpServers`; it does not
    project third-party declarations. Override eligibility reads project and
    global MCP settings through the bounded sensitive-path reader. A refused,
    unreadable or malformed settings file withholds these overrides, preserving
    native restrictions; only an absent settings file contributes no restrictions.
    kiro-cli loads
    servers from the agent config (respects `mcpServers` in the agent's config
    file). Non-kirocrew agents (e.g. AIM-installed) load only their own
    `mcpServers`. The kirocrew agent loads from global `~/.kiro/settings/mcp.json`
    where `disabled` and `disabledTools` flags are respected. KiroCrew's dashboard
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
   so an empty list would un-pool the session), plus eligible unpooled managed
   elements carrying their session token, and
   `_meta: {"_kiro.dev/session_file": "<path>"}` (required —
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
declarations in the session-level array (`kas_agents.hoist_managed_servers`).
Captured released 2.18.0 honours that array over a same-named global or
workspace `mcp.json` server on new and load alike (the member dispatch server
already travels this way); the retained 2.20.0 capture proves a session-level
injection reports `origin: client` and reaches readiness, and its same-name
collision behaviour was not probed. The entries moved are the
already projected ones (credential fields withheld, `autoApprove` dropped,
`KIROCREW_PORT` / `KIROCREW_SESSION_KEY` applied), converted with
`session_mcp.acp_server_element`; no spec is re-read and the derived-spec
snapshot is unchanged. The move is bounded: only managed names, only the active
agent, never a name the caller's array already carries (a broker stub or the
member dispatch entry stays authoritative and a name appears once), and only a
stdio entry whose keys are drawn only from `command`, `args`, `env`, `type` — a
`disabled`, `disabledTools` or `timeout` customization, a registry marker, a
remote URL or a command-less entry keeps the agent-block path where that field
is honoured. The agent's `tools`, `excludedTools` and `permissions` are
untouched: refs resolve wherever the server was declared, and on 2.18.0 an
allowlist-only or `excludedTools` restriction on the hoisted server still
yields readiness with only the permitted tag advertised. Exact legacy
limitation: on a release that stamps no provenance, a managed entry left in the
block by one of those extra fields is still refused as
`connected without provenance`; it works on a release that stamps `origin:
client`.

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
Errors name the required server and sanitize backend failure text. On failure,
runtime death, or cancellation, no handle escapes. A failed fresh `session/new`
is terminated through the bounded per-session teardown, freeing its resident state
and MCP children without touching siblings. A failed `session/load` is only
unregistered locally because KAS's delete verb would destroy the existing native
history; that history remains available to a later resume. No first prompt is
sent on these paths. Harnesses without this opt-in retain their existing
initialization drain.

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

A retired walk that fails afterwards has its exception consumed via a done-callback attached at submission, so an ordinary probe failure is not reported as an unhandled-asyncio crash. Past the cutoff, **only `VERDICT_WORKING`** (moving CPU/IO in the backend subprocess subtree) defers the turn (loop continues); every other verdict (`DEAD`/`UNKNOWN`/`STUCK_INPUT`) preserves the prior end-the-turn behavior, so hang recovery is never weakened — a genuinely dead turn still ends, bounded by the resolved prompt timeout (`_DEFAULT_PROMPT_TIMEOUT`, 4h — raised alongside `agent.chat_turn_timeout_secs` via `resolve_prompt_timeout`) and the tool-stall watchdog below. Unlike the runtime path's `session/cancel` probe, the `AcpClient` reap is a plain `return` (process-per-session: the turn simply completes; no shared runtime to protect).

The compatibility reap emits `EVENT_COMPLETE` with `stop_reason=end_turn` so
existing consumers finalize normally, but also sets `synthetic_completion=true`.
The provenance distinguishes it from the provider's genuine `end_turn` result;
accounting consumers must reject the synthetic form.

### Tool-stall watchdog

While a turn is dispatching, both ACP transports run a watchdog over a turn gone silent after a tool was dispatched — and both **recover** rather than just `return` on a dead turn (`AcpClient` keeps the blanket `_TOOL_STALL_TIMEOUT` window; the session handle is verdict-driven, below):

- **`AcpClient`** (process-per-session, `_TOOL_STALL_TIMEOUT = 600s`): the stall clock is measured against `_tool_last_seen = max(last_data_ts, self._last_activity)`, so tools that keepalive-ping without emitting stdout frames (`wait`, `spawn_sub_agents`) don't trip a false stall (`_last_activity` is refreshed out of band by the stderr drain / keepalive). On a real stall it `_kill_process(force=True)` and raises `AcpProcessDied`, routing through the existing pipe-death recovery (dashboard resets the session + re-queues, bounded by `_acp_pipe_death_retries`; cron/other callers get a clean error instead of a wedged slot). `_kill_process` only touches the subprocess/pipes (never `_turn_lock`), and blast radius is one session — each `AcpClient` owns exactly one process.
- **`runtime.py` / `AcpSessionHandle`**: watchdogs are **verdict-driven, not timeout-driven** — the prior design used timeouts as death detectors and killed healthy-but-slow work (a silent 30-min redirected build `long-build > build.log 2>&1` at exactly the blanket window; healthy long non-streamed reasoning at 90s, where the destructive `session/cancel` probe was acked by the LIVE turn and surfaced as "Turn cancelled by user"). Once a turn is idle past `watchdog.check_after_secs` (60s), the per-session `LivenessOracle` (`acp/liveness.py`) returns a verdict with evidence: **WORKING** (a live cmdline-matched shell child, a `wait` tool inside its declared duration + slack, moving CPU/IO counters, backend socket bytes flowing) is never acted on at any elapsed time (logged at most once per 10 min — at INFO below the escalation mark, which is the lower of 30 min and a quarter of this turn's deadline, and at WARNING past it so a deferral able to hold the turn to its ceiling is visible at the default `agent.log_level`); **DEAD** (tracked shell child exited without a result frame past a 15s grace; model-wait with flat counters and NO established backend socket — the done-but-lost-frame wedge signature) acts immediately, so recovery lands seconds after actual death instead of at a blanket window; **STUCK_INPUT** (matched subtree flat across samples with a process blocked reading a tty/stdin pipe) acts immediately with a cause the recovery nudge names; **UNKNOWN** is the only timeout-governed class — stale probe at `watchdog.stale_window_secs` (600s; extended to `watchdog.model_silent_probe_secs` = 1800s when the evidence is `established_flat`, i.e. probably a non-streamed server-side think), tool cancel at `watchdog.tool_stall_suspect_secs` (5400s / 90 min — clears every shipped budget a single tool call can legitimately spend silent, such as the task runner's 90-minute test command), hard-capped at `watchdog.tool_stall_hard_cap_secs` (7200s / 2h, UNKNOWN only; also bounds the per-agent overrides). The oracle's evidence is Linux `/proc` where it exists; on macOS (no procfs) it selects an in-process **libproc backend** once per oracle instance (`select_darwin_backend`, injectable for tests): `proc_listchildpids` enumerates the runtime's descendants, `PROC_PIDTBSDINFO` supplies ppid / zombie state / start time, `proc_pidpath` and `sysctl KERN_PROCARGS2` supply the executable and argv for the same cmdline match the `/proc` walk performs, and `PROC_PIDTASKINFO` supplies per-process CPU time summed over the subtree (evidence labelled `darwin cpu-only`, since IO bytes are not readable there). So a shell command is WORKING/DEAD/`shell_child_absent` on macOS by the same rules as Linux, and an active MCP subtree reads WORKING instead of running out the suspect window; evidence only `/proc` carries — the `established_flat` socket tag, the `blocked_read_fd` STUCK_INPUT check, `wchan` — is never invented on macOS: a flat model wait keeps the plain UNKNOWN, and a live tracked shell child whose subtree is flat is UNKNOWN tagged `platform_limited` — bounded by the standard no-progress budget — rather than WORKING, because without stdin-block evidence a stuck child and a quiet one are one state and "alive" must not buy an indefinite deferral (see "Platform evidence matrix" below). Dispatch stamps on darwin are wall-clock (`time.time()`), the clock libproc dates processes on, and a wall clock can step: a backward NTP or VM-resume correction between the dispatch stamp and the runtime's fork dates a live child before its own dispatch. The stamp is therefore paired with a steady one (`steady_now()`, darwin `CLOCK_MONOTONIC`, which counts sleep), and when wall elapsed and steady elapsed disagree by more than the attribution tolerance the oracle declines to attribute by start time at all — every row reads as possibly this tool's, so a matched child stays WORKING and no `shell_child_absent` claim is made. A missing steady stamp gets the same fail-open answer. **Windows has no tree backend yet**: the oracle there reads only the runtime's own CPU time (`proc_cpu_nanos_for_pid` via `GetProcessTimes`, root pid only), so every shell and MCP tool call stays UNKNOWN — tagged `platform_limited` so the degradation is visible in the evidence and the metric bucket — and the 90-minute suspect window is the effective tool timeout on that platform (narrowed to the ordinary silence window when the command was classified prompt-shaped, see "Interactive-command policy" below) — a genuinely hung non-interactive tool holds its slot for that long. The trade is accepted rather than sized around: a Toolhelp-based descendant walk is the follow-up that closes it, the same way the darwin backend did for macOS. Three refinements keep the build-scale tool forbearance from sheltering an **LLM-shaped** stall (a model turn riding inside a tool, e.g. kiro-cli `use_subagent`, whose longest legitimate silent gap is minutes) or an **already-finished** one: (1) the oracle tags an UNKNOWN tool verdict with `established_flat` when the subtree's counters are genuinely flat (a real two-sample delta, not the baseline tick) AND the **runtime process itself** holds an established backend socket — deliberately narrower than the model-wait branch's whole-tree socket scan, so an MCP server blocked on *its own* remote call keeps the full tool windows — and the tool branch then uses `min(model_silent_probe_secs, tool_stall_suspect_secs)` as the effective suspect window; plain flat-subtree evidence keeps the full window, and under the OS sandbox (pid = launcher parent, no sockets on it) the tag never fires, failing toward the long build-safe window. (2) the never-matched SHELL fork is split instead of uniformly forgiven: `no matching shell child` conflated a command that already exited — a sub-second `ls | grep | wc` whose result frame was lost is never observed alive, so the DEAD branch's 15s exit grace can never fire for it — with one running unrecognized, and the two got the same 1h. The oracle now tags the first case `shell_child_absent` when the runtime's descendant tree is OBSERVABLE (a readable `/proc/<pid>/task/<tid>/children`, empty or not) and holds no live descendant attributable to this dispatch, and the tool branch then uses `min(stale_window_secs, tool_stall_suspect_secs)` — the ordinary silence budget — instead of the build-scale one. Attribution compares a descendant's `starttime` against a `CLOCK_BOOTTIME` stamp taken at the tool_call frame and widened by the turn's banked consumer parking (`_parked_total`), because `/proc` dates processes on a clock that counts suspended time while `time.monotonic()` does not, and the stamp is taken when the frame is PROCESSED rather than when the runtime spawned (a frame queued behind an approval is stamped that late). Four states each keep the full window, so every unattributable one fails toward build-scale patience: a descendant young enough to be this dispatch's, one whose cmdline matches while predating the stamp (indistinguishable from a coincidental lookalike), an unreadable child list, and a missing stamp or tick rate (no `os.sysconf` off Linux). The verdict stays UNKNOWN, never DEAD — absence is inferred, so it only shortens the non-lethal cancel. (3) An agent definition can override the windows per agent (`agents.<name>.watchdog_tool_stall_suspect_secs` / `watchdog_tool_stall_hard_cap_secs`, 0 = inherit the global — the same empty-inherits convention as the agent's `model`), applied in the `WatchdogSettings` snapshot at handle construction (`_load_watchdog_settings(crew_agent)` — a direct lookup on the CANONICAL crew name, resolved by the surface that owns the identity: the dashboard passes the slot member explicitly through `get_or_create(crew_agent=...)`, and crew-name-passing surfaces (Slack threads, cron, spawned agents) are covered by the provider factory's crew-namespace membership fallback; the identity is plumbed provider → runtime → handle, and a warm-pool claim rebinds the live handle via `rebind_watchdog()` so it travels with the SESSION, not the pool key — a name that is not a crew key simply inherits the global) so a pure-LLM agent like a PR reviewer can declare minutes-scale windows without touching the global build budget; an override is bounded by the same load-time ceiling clamp as the global windows, so it cannot smuggle a window past the prompt timeout. Every idle window is bounded at load by the resolved prompt timeout (`resolve_prompt_timeout` — the one deadline every caller shares; 14400s default, following a raised `agent.chat_turn_timeout_secs`) minus 10% headroom for the cancel + ack grace, and an over-ceiling on-disk value is clamped with a warning: a window at or past the deadline makes the UNKNOWN class unreachable, because the turn's timeout fires first and the user gets the generic turn-limit card instead of the tool-stall recovery below. A window above the DASHBOARD ceiling (`agent.chat_turn_timeout_secs`) is reported but **not** clamped — the same handle serves callers that pass their own larger prompt timeout, and shrinking their windows would cancel live work. **Every watchdog action is non-lethal:** a stale probe's cancel-ack is reclassified in the turn-complete branch (`_stale_probe` + `stopReason==cancelled` → `STOP_REASON_STALE_RECOVER`; the flag is single-shot — consumed on reclassification and superseded by a genuine `cancel()`, so a user cancel arriving after a probe is never misattributed to auto-recovery) so the dashboard auto-recovers instead of logging a user cancellation — an oracle mistake costs a regeneration, never a session. A tool stall ends the turn with `STOP_REASON_TOOL_STALL` (`"error: tool stall"`, in the `error:` family so branch-less callers degrade to generic handling) carrying the tool title / redacted command / evidence on the terminal `AcpEvent`; chat_runner's dedicated branch queues a **continue-nudge** (`build_tool_stall_recovery_prompt` — check partial results, tail any `> file` redirect target, re-run non-interactively on STUCK_INPUT) instead of the legacy verbatim re-queue of the original user message (which restarted the whole task and re-ran the very command that stalled), charged against a separate `slot._tool_stall_retries` budget (3) so a stall never burns the pipe-death reconnect budget. The runtime is **shared** (multiple sessions multiplexed on one process), so recovery is always `session/cancel` for **this `sessionId` only** (bounded by `asyncio.wait_for(..., 5s)`); siblings keep running. `watchdog.*` config is snapshotted at handle construction (`WatchdogSettings`); the dispatch loop never reads config. The snapshot is **re-bound on a config reload**, so a live handle does not keep boot's windows until its turn ends: `SessionManager._rebind_live_watchdogs(cfg)` fires when a reload touches `watchdog.*`, `agent.chat_turn_timeout_secs`, or any `agents.<name>.watchdog_*` key, walks every registered session's provider (`_watchdog_handle_of` resolves both shapes — `AcpSessionProvider._handle` and `AcpProvider._client._handle`) and calls `handle.rebind_watchdog(crew, _load_watchdog_settings(crew, cfg=cfg))`. It re-runs the loader for the handle's OWN crew identity rather than copying raw seconds across, so the per-agent override overlay and the prompt-timeout ceiling clamp above are re-applied per handle — a raised `agent.chat_turn_timeout_secs` lifts the ceiling that was clamping a window, and a lowered one re-clamps it. The config is the one the watcher already loaded, so the fan-out touches no disk on the loop, and the dispatch loop reads the snapshot every tick, so the new windows govern the next check. A handle that raises is skipped and logged at DEBUG; the rest still rebind.

**Both idle clocks measure BACKEND silence, so consumer time is subtracted from them.** `_dispatch_events` is an async generator: it is suspended at its `yield` for the whole of a consumer-side await (a tool approval, an IM send, a hook), and `last_data_ts` does not advance while suspended. Charging that interval to the runtime lets the arm cancel a turn moments *after* a human approves a tool — and at that instant the tool has not started, so the oracle draws `UNKNOWN` or `DEAD`, and `DEAD` acts immediately regardless of the window. `prompt()` therefore times each park around its single re-yield (`_parked_since` → `_parked_total`, cleared in a `finally` so an abandoned generator does not read as parked forever), and the timeout arm subtracts the park accumulated since `last_data_ts` was taken. The tool clock is exact; the stale clock can key off the newer stderr/keepalive activity, in which case part of the correction predates its reference point and is subtracted twice — which only makes that branch more patient, never quicker to probe.

**On the shared runtime the tool clock is also SESSION-SCOPED.** `AcpRuntime._reader_loop` marks a frame `fanout_no_owner` when it fans an ownerless frame (no `sessionId`) out to more than one registered session — a lone session is the sole owner and stays unmarked, so a single-session runtime behaves exactly as before. The dispatch loop advances a session-attributable twin of the pair, `last_own_data_ts` / `parked_at_own_data`, only for an unmarked frame, and both the **tool-idle clock** and the **post-compaction-failure budget** read that twin: a co-tenant's roster broadcast (`_kiro.dev/subagent/list_update`) can no longer defer either on traffic this session never produced. The **stale** clock deliberately keeps reading `last_data_ts`, because it already folds in the runtime-wide `_last_activity` (bumped on every stdout line), so runtime-global traffic is inside its contract by construction. Provenance, not the frame's method, is the discriminator — the same notification kind can arrive routed (this session's own progress) or fanned out (a co-tenant's), and only the runtime knows which. The one remaining over-count is deliberate and bounded: the tool branch's TOCTOU guard cannot know the owner of a frame that arrives *during* the oracle await (it is not dequeued yet), so it advances both clocks and defers by a single tick, the same fail-safe trade `_ingress_seq` documents at its increment.

**The turn's park is readable from outside the turn.** `parked_for_secs()`, `parked_since`, and `awaiting_permission` exist because this arm cannot report on itself: it only advances when a consumer pulls the generator, so a consumer-side await freezes it and it never executes again for that turn. `session.md`'s `stuck_turn` hook reads those accessors from a loop with its own timer. Answering a permission calls `_end_human_wait()`, which banks the human's thinking time into `_parked_total` and restarts `_parked_since`, so the in-band correction stays exact while the external reading counts only what the consumer itself has spent since the answer.

Both transports offload the oracle consult to `subprocess_executor()`, so both carry the same two obligations, and both discharge them through ONE shared guard — `liveness.consult_offloaded()`, which owns the prior-future check, the in-try submission, the submission-time exception callback, the shielded bounded await, and the degrade-to-UNKNOWN arm, so a fix to that sequence lands at both call sites at once (each caller keeps only which oracle check runs and where its tracked future lives). **One outstanding walk per liveness generation:** `_consult_oracle_offloaded()` tracks the submitted future and answers `UNKNOWN`/`"prior consult still in flight"` on any tick that finds it unfinished, so a `/proc` read wedged on a stuck fd no longer adds a blocked worker every `check_after_secs` to the pool teardown's `_get_child_pids` also draws from. The no-in-flight-tool answer is resolved *before* that guard, because it is pure handle state and needs no worker. Its exception is retrieved via a callback attached at submission — not in an `except Exception` arm, which `CancelledError` (a `BaseException`) would skip — so a probe that fails after its awaiter left is not recorded as an unhandled-asyncio crash. **Retire, don't `reset()`:** turn start in `prompt()` and every new tool dispatch call `_retire_liveness_state()`, releasing the tracked future *together with* the oracle (`LivenessOracle.fresh()`, so the per-session `wellness_sample_secs` survives). Splitting them either way is a defect: clearing the oracle in place leaves a detached walk writing into the live baseline (samples are keyed without a PID, and any nonzero delta counts as movement), while replacing only the oracle leaves a walk wedged in the previous generation answering every later tick "still in flight" so the new generation never samples its own process. The tool path has a sharper version of the first hazard than the capture path does: a walk carrying the *previous* tool's `ToolCallState` matches a descendant of the previous command and stores it as `_tracked_child`, after which `_check_shell_child` reports `WORKING "shell child N alive"` for the new tool against an unrelated process. Retirement is not a change to the cross-tick tracked-child contract itself — `fresh()` starts in exactly the state `reset()` produced, and the consult binds `self._oracle` at submission, so ticks after a boundary accumulate on the new instance as before.

**Before adding an await to a consumer branch**, read
`../../architecture/design-notes/tool-stall-watchdog-placement.md`. Both
watchdogs above are inside the generator, so a new consumer-side await silently
widens the class of failure neither of them can see; the note records which
failure classes are detectable here and which must be judged out of band.

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
| subtree movement | CPU jiffies + IO bytes | CPU ns only | root-pid CPU (model-wait probe only) | a moving subtree is `WORKING` on any host: "no output" alone never kills a task whose CPU/IO moves |
| blocked on stdin (`STUCK_INPUT`) | `wchan` + blocked fd | absent | absent | a live tracked shell child whose subtree is FLAT (real two-sample delta) is `UNKNOWN` tagged `platform_limited`, never `WORKING` — "alive" is never sufficient for indefinite deferral |
| socket / LLM wait (`established_flat`) | `/proc/net` established → `UNKNOWN` or `DEAD` | absent → plain `UNKNOWN`, never `DEAD` | absent | same |
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
rebase -i`), REPLs that read stdin when given nothing to run (`python`,
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
terminal — so `PAGER=cat` / `-y` / `BatchMode` are PROPOSED via the hint the
recovery nudge carries, not applied. Applying them at the harness is a
follow-up seam, not a matcher's job.

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
behind the policy is `agent.interactive_command_policy` (`config/sections.py`,
read by `_load_watchdog_settings`); the snapshot field is the seam.

### Structured status protocol (`kirocrew/status`, version 1)

Existing signals are reused as-is — `session/update` for tool-call
start/complete and agent text, `_kiro.dev/metadata` for the harness
`stopReason`, `_kiro.dev/subagent/list_update` for native sub-agent identity,
MCP `notifications/progress` routed per request by `mcp_gateway/backend.py`.
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

**MCP side (documented, not parsed yet).** An MCP tool emits the same object as
`_meta.kirocrew_status` on a `notifications/progress` sibling for its
`progressToken`; the gateway backend already routes that notification to the
requesting session, which is the origin the rule above needs. The stub and
backend are frozen for this PR, so the MCP path is a documented follow-up: the
parser is shared (`StructuredStatus.from_meta` over the `_meta` dict) and the
origin check is "the routed `progressToken` belongs to a tool call this session
issued". A tool result body containing the shape is content, never status.

**Consumers.** `providers/acp.py` forwards `AcpEvent.status` unchanged. The main chat's tool-stall continuation (`chat_runner`) takes `stuck_input` from `status.wait_reason == waiting_input` on the terminal completion before the evidence-text marker; the sub-agent run loop yields its lane slot on an `EVENT_STRUCTURED_STATUS` frame with `waiting_input` (`WaitRecord.input(tool_call_id)`, residency kept — [subagent.md](subagent.md) § Typed input wait) and steers its recovery prompt from the same field.

### Harness-native subtasks: inventory and recovery boundary

Inventory of what each backend exposes for subtasks that run INSIDE the harness
(kiro-cli `use_subagent`, the Claude adapter's in-harness task tool), as
opposed to `spawn_run` / `spawn_sub_agents` tasks Kiro Crew manages
(RFC §14.8, SPEC-ADDENDUM §7, parity row H16 in
[harness-parity.md](harness-parity.md); pinned by
`test_native_subagent_boundary.py`):

| Backend | Identity | Tool events | Cancel | Resume | Counting / scheduler treatment |
|---|---|---|---|---|---|
| kiro-cli (`ACP_BACKEND_KIRO`) `use_subagent` | yes — `_kiro.dev/subagent/list_update` roster (`sessionId`, `status`), and child-routed `session/update` / `_kiro.dev/session/update` frames carry the child `sessionId` (`AcpEvent.sub_session_id`) | attributable: child frames are parsed with the shared parser into origin-scoped caches and re-tagged `EVENT_SUBAGENT_ACTIVITY`; child approvals surface on the PARENT with `sub_session_id` set (`run.py` counts them separately) | parent only — `session/cancel` on the parent session takes every child | none independent of the parent | not a task row. `AcpSessionHandle.native_child_sessions` counts child ids seen this turn through `_note_native_child` at three execution-layer seams — the child-routed branch of `_handle_update`, the `_kiro.dev/session/update` child stream in `_dispatch_events`, and the `subagent_list` roster when this handle is its sole owner (`fanout_no_owner` False; a fanned-out roster is counted on nobody) — never from model text. No `HostBudget` charge, because a child lives inside the parent's already-charged runtime process; `report_native_children(budget)` reports the count as `uncharged["native_children"]`. Recovery boundary = the parent session. A child-routed `kirocrew/status` is rejected (`child_origin`), never re-attributed |
| KAS (`ACP_BACKEND_KAS`) | yes — `_meta.kiro.agentSubtaskId` / `pipeline` on parent `tool_call` frames (`_handle_kas_subagent`), roster keyed by `agentSubtaskId` | child nested tool frames emit an activity prefix and populate the caches | parent only | none | same boundary as kiro-cli; every roster entry (individual `agent-subtask` frame or pipeline stage) is counted through the same `_note_native_child` seam |
| Claude backend (`ACP_BACKEND_CLAUDE`) | no per-child ACP identity today — the Task tool is an ordinary `tool_call` on the parent | no | parent only | none | same boundary; the counter stays 0 because the harness exposes nothing to count, not because nothing runs. A declared capability gap (H16), not a defect: the liveness oracle bounds the parent turn identically (`test_claude_backend_counts_no_children_but_oracle_still_bounds_the_parent`) |
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
  however many children it fanned out. There is no per-child lever to send.
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
  `test_acp_runtime.py` (one warning per episode however many frames re-announce
  it, the throttled summary's count and peak, both ways an episode ends re-arming
  the warning, and the attribution expiring with the snapshot that earned it).
- **Observed, not scheduled.** On a shared runtime with several registered
  sessions, an unannounced child frame names no owner and is dropped (counted by
  `_note_dropped_frame`); the count is best-effort and a display never implies
  the scheduler can pause one child.

The UI showing per-child cards (`_native_subagent_sync` in `chat_runner.py`)
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

`_track_usage_update()` tracks context window usage from `usage_update` session events, reconciling the frame via the shared `parse_usage_update()` (flat `update.used`/`update.size` primary, nested `update.usage.*` fallback) so `AcpClient` and `AcpRuntime` read the same shape regardless of which kiro emits. A `KNOWN_SESSION_UPDATES` frozenset in `acp/types.py` suppresses false "unhandled session update" logs for plumbing-only update kinds (`plan`, `available_commands_update`, `current_mode_update`, `config_option_update`, `session_info_update`, `user_message_chunk`, `tool_call_update`). Only genuinely unknown kinds are logged. On the **KAS backend**, three of these are not plumbing-only: `current_mode_update`, `config_option_update`, and `session_info_update` are consumed as display signals. KAS folds signals that kiro-cli sends as separate top-level `_kiro.dev/*` methods (agent switch, per-turn metadata, compaction status) into these `session/update` discriminants, so a KAS-gated branch in `AcpSessionHandle._handle_update` maps `current_mode_update` → agent-switch echo, `config_option_update` → effort-option state, and the `session_info_update` `_meta.kiro` union (`context_usage` → context meter, `turn_completion` → per-turn credits, `summarization_*` → compaction status). kiro-cli never emits these discriminants, so the branch is gated to KAS only and the kiro path is untouched.

**Context-window backfill.** kiro 2.10+ metadata may carry only a context-usage *percentage* (no absolute token counts). `_backfill_context_window(pct)` derives the window and used-token counts from the central `model_registry.model_window(self._resolved_model_id or self._model)` authority (gated on `has_known_window` so an unknown model is never backfilled with a guessed window) and the percentage, so the dashboard token text still renders when only a percentage arrives. `_resolved_model_id` begins as `models.currentModelId`, then becomes a successfully dispatched non-default startup override or explicit switch, whether it uses `session/set_model` or `session/set_config_option`; a policy-substitution advisory on the latter records the model actually served for that request only. Automatic and unusable routes retain the backend-reported default.

**Per-turn kiro billing credits.** `_track_metadata()` parses each `_kiro.dev/metadata` notification via the shared `parse_metadata()`, capturing `meteringUsage` entries with `unit=="credit"` (kiro bills in credits; token fields are 0 for the acp provider) into `AcpPromptStats.credits`, accumulated across the turn and surfaced on `EVENT_COMPLETE`.

**Per-turn cost and token counts (claude seam).** The `claude-agent-acp` adapter bills in cost/tokens instead of credits: a session-cumulative `cost: {amount, currency}` rides `usage_update`, and turn-scoped token counts (`inputTokens`/`outputTokens`/`cachedReadTokens`/`cachedWriteTokens`) ride the PromptResponse. Both are validated at the shared `_dispatch.py` chokepoints (`parse_usage_cost`, `parse_prompt_token_usage` — same defensive posture as `parse_usage_update`). `parse_usage_cost` additionally drops the whole cost when a `currency` is present and not exactly `"USD"`, since every consumer stores the result in USD-denominated fields; an absent currency stays accepted for adapters that omit it. and folded into `AcpPromptStats`: the cumulative cost is converted to a per-turn delta by `apply_cost_cumulative` (monotonic guard — a reading below the stored baseline means the adapter's counter reset, so the new total is taken whole rather than emitting a negative delta; the baseline survives `carry_over()` like the context fields and is dropped by `reset_context_state()`), and the token counts accumulate via `apply_prompt_token_usage` (`_track_prompt_usage` on both `AcpClient` and `AcpSessionHandle`). Every `EVENT_COMPLETE` construction site builds its `TurnUsage` through the single `AcpPromptStats.to_turn_usage()` helper, so `cost_usd` and the token dimensions populate uniformly and the per-turn persist gate fires on the claude seam. kiro-cli sends neither signal, so on the kiro path the new dimensions stay 0 and `credits` flows exactly as before (harness parity — no `_is_claude` branch anywhere on this wiring).

## Exceptions

`AcpError` (base), `AcpTimeoutError` (has `partial_output`), `AcpPermissionNeeded`, `AcpProcessDied`, `AcpAuthRequired`, `AcpPromptBusy`.

- `AcpAuthRequired` — kiro-cli is not authenticated (`kiro-cli login` needed). Non-retryable: `ensure_ready()` skips the retry ladder and re-raises so callers surface the actionable message rather than reset-and-requeue.
- `AcpPromptBusy` — a prompt is already in progress on the session, classified from kiro-cli's "already in progress" text via `_PROMPT_BUSY_RE` and raised at prompt-dispatch sites. `slack/handler.py` catches it and auto-resets the wedged session (`sessions.reset`) before recording the failure, so the next message cold-starts cleanly.

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

Subprocess lifecycle:

- Spawned with process-tree isolation for clean teardown, dispatched per-platform in `_spawn()`: **POSIX** sets `start_new_session=True` (group leader via `setsid`) so cleanup can `killpg`; **Windows** sets `creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP` (no `setsid`/process groups; an inherited Ctrl-C can't reach the gateway). Both flags are passed explicitly (never via `**dict` unpack, which breaks mypy's Popen overload resolution). Teardown in `_kill_process()` is dispatched per-platform too. **Windows** returns through the owned-handle drain described above and never reaches the signal ladder: `terminate_windows_asyncio_tree` tears the tree down from handles pinned at spawn, so a `taskkill /T` walk that a reaped root leaves empty is not what the teardown depends on. **POSIX** awaits `platform_compat.kill_process_tree_async(pid, SIGTERM)` then `SIGKILL` — `os.killpg(os.getpgid(pid), …)` inline and non-blocking. The Windows `taskkill /T /F` shim in `kill_process_tree_async` remains for callers that hold only a pid, offloaded to `kiro_crew.executors.subprocess_executor` so the event loop is never blocked for the `taskkill.exe` spawn. The escaped-child sweep (`_kill_escaped_children`, which raw-`os.kill`s descendants that reparented out of the killed group) is **POSIX-only** — a no-op on Windows, where the owned-handle drain has already confirmed each retained member's exit before `_kill_process` returns (the drain, not a `taskkill /T` walk, is what the Windows teardown depends on; that shim is only for callers holding a bare pid) and `signal.SIGKILL`/`os.kill(pid,0)` are unavailable/unsafe. The `/proc`+`pgrep`+`ps` child-enumeration helpers (`_direct_children`, `_get_start_time`, `_read_basename`) short-circuit on Windows (return `[]`/`None`) since they only feed that POSIX sweep. `_resolve_ssh_auth_sock()` (called in the spawn prelude) is also a no-op on Windows — its non-darwin branch calls `os.getuid()`, absent on win32, and Windows OpenSSH uses a named pipe with no `SSH_AUTH_SOCK` to repair.
- **Off-loop PID inspection**: the PID-recycling/ownership helpers that shell out on macOS — `_get_start_time` / `_read_basename` (`ps`), `_get_child_pids` → `_direct_children` (`pgrep`), the `_capture_child_records` batch wrapper, and the `_kill_escaped_children` sweep — MUST run via `run_in_executor(subprocess_executor(), ...)`, never directly on the event loop. The PID-file tracking writes — `_track_pid`, `_track_session_pid`, `_track_child_pids` in `AcpClient._spawn()`, and `_track_child_pids` plus the `_untrack_child_pids` prune in `AcpRuntime._snapshot_descendants()` / `_prune_dead_descendants()` (the latter reached through `asyncio.to_thread`) — carry the same obligation: each takes an exclusive file lock and does a read-modify-append under it, and `ensure_ready()` awaits `_spawn()` from the loop on every cold start, so an on-loop tracker serializes concurrent spawns behind one file lock with the waiter holding the loop. The subprocess spawn (fork/exec) can block, and on a wedged child the loop would freeze (the macOS wedge class). `subprocess_executor` is a *dedicated* bounded pool (distinct from the `maintenance_executor` orphan sweep) so a wedged scan/close cannot starve the recovery sweep. The `ps` and `pgrep` calls each carry a 2s timeout so no offloaded scan occupies a pool worker indefinitely.
- **Windows exe-casing normalization** (`_normalize_exe_casing`, applied to the kiro / claude-agent-acp / claude-code resolver results): `shutil.which` builds the resolved name's extension from `PATHEXT`, which lists `.EXE` upper-case, so it returns e.g. `…\kiro-cli.EXE` even though the on-disk file is `kiro-cli.exe`. A case-sensitive multiplexer shim spawned as `kiro-cli.EXE` fails to dispatch, exits instantly, and the ACP pipe breaks (`AcpProcessDied`) → the dashboard shows **"session stuck"** on the first chat turn. `os.path.realpath()` restores the true directory-entry casing. No-op on POSIX (case-sensitive FS). Runnability is checked via `platform_compat.is_executable_file()` (POSIX execute bit; on Windows the X-bit is meaningless so a known runnable extension is required instead), so a bare `.js` adapter entry is correctly treated as **not** directly runnable on Windows and gets wrapped with `node`.
- **Sandbox ownership**: `_spawn()` calls `sandbox.wrap_argv()` to wrap the command with platform-native isolation (Linux: two-stage `unshare -rm` → `unshare -U` bind-mounts + UID drop; macOS: `sandbox-exec` Seatbelt profile). On Windows, where Kiro Crew has no native OS wrapper, an explicitly classified official Kiro backend delegates to Kiro CLI's built-in sandbox; every other backend retains the no-backend fail-closed policy. The parent passes a fully scrubbed child environment on every platform, which is the enforcement point for raw Windows delegation. Configurable via `sandbox_mode` constructor param (`"auto"` default, `"off"` to disable). See `docs/system-specs/modules/security.md`.
- **Parent-level channel-credential scrub**: both spawn paths (`AcpClient._spawn` and `AcpRuntime._spawn`) build the child environment from a raw `os.environ` copy (plus `_extra_env`) and pass it directly to `create_subprocess_exec`, so they call `sandbox.scrub_agent_denied_env(env)` after merging `_extra_env` to strip `_AGENT_DENIED_ENV_KEYS` (Slack/WeCom/Telegram tokens + owner id seeded into `os.environ` by `config.loader.load_credentials`). This is required because these paths do NOT route through `sandboxed_spawn_argv`, and the OS-sandbox launcher only strips those keys for the `cc`/`strict` tiers — on the default `auto`/`standard` tier the launcher leaves them in place, so without the parent scrub they would be inherited by the agent subprocess. The scrub is deliberately narrower than `scrub_env`: it leaves the AWS/SSH env the `standard` sandbox intentionally exposes (git-over-SSH, AWS CLI, kubectl) untouched. One credential is settled per-backend rather than by the deny list: `KIRO_API_KEY` (kiro-cli's own model credential, in `CREDENTIAL_KEYS` but deliberately NOT in `_AGENT_DENIED_ENV_KEYS`) is re-injected from the data home's `.env` via `config.loader.inject_kiro_cli_api_key` for a kiro-cli child (whose environment is where the CLI reads it — required after the Docker entrypoint scrubs it from the gateway's environ) and actively stripped via `strip_kiro_cli_api_key` for a foreign backend (Claude seam, KAS), which must never receive it; both run inside the spawn paths' existing off-loop env hop.
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

### Member MCP routing

Member clients and runtimes use the ordinary direct or pooled MCP transport
supported by their backend. The gateway captures canonical member/store routing
from authenticated session identity; `member_context` controls native instruction
deduplication. Memory V2 neither discards the shared broker overlay nor requires
a member-specific sandbox or direct-MCP capability.

KAS projects the gateway's validated `KIROCREW_BOUND_PORT` as `KIROCREW_PORT`
for native managed MCP servers because native children do not inherit the
gateway environment. The listener address comes from the gateway, not an editable
agent spec. Declared secrets and arbitrary environment values remain withheld,
and non-managed servers receive no gateway port.

The provider factory selects `agent.member_acp_backend` for member-DM session
keys and the configured default backend otherwise. Ordinary backend governance,
selectability, member-capability checks and host sandbox rules remain independent
requirements; memory version adds no direct-MCP refusal.

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

### Session-start gate and tracked start ownership (`SessionStartGate`, `StartCollector`)

Cold-start admission bounds runtime spawn + `initialize`. The **session-start
gate** bounds the other expensive start: `session/new` on an already-running
runtime, which blocks while the backend initializes the session's MCP servers.
`AcpRuntime.create_session` acquires the current loop's `SessionStartGate`
(`agent.session_start_concurrency`, default 2, FIFO, sized once per loop from
config; `restart=True`) BEFORE `session/new` goes on the wire and releases it
as soon as the answer arrives. The gate is a FIXED semaphore: the adaptive
loop is the gatewayd spawn gate plus the execution-cap controller, and two
adapting loops on one resource oscillate. It is one gate for every harness
(kiro-cli, KAS, a later Claude host), because every backend's session start
runs through `create_session` (harness-parity: no per-backend branch).

`on_gate_acquired(queue_wait_ms)` fires at gate EXIT: the caller starts its
own clocks there, so queue time behind the gate never counts against the
session-start budget (`agent.session_start_timeout_secs`, 90s floor) or the
subagent startup watchdog. Structured `acp_startup_stage ... outcome=collecting`
logs carry the gate's active/queued counts.

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

Every path releases the permit exactly once (`StartPermit.release()` is
idempotent; `SessionStartGate.releases` counts real releases) and unregisters
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
other's frames; `run.py` never calls `get_or_create` on congestion and continues
on an adopted late session.

**Adaptive-controller sample.** Every `session/new` outcome is one `AdaptiveController.record_start(duration_ms, ok, attributable_timeout, key="acp:session/new")` when a controller is registered (`adaptive.controller.current()`; a no-op otherwise): duration is measured from gate EXIT (the queue wait is admission's cost), `ok=True` on an answer, `ok=False, attributable_timeout=True` on `AcpRequestTimeout` (the congestion signal the controller's decrease keys on), `ok=False` on any other failure. `test_runloop_integration.py` pins the three shapes.

### Tool audit for non-chat clients (`AcpClient(audit_source=…)`)

The `audit_source` constructor param of `AcpClient` (default `None`) tags a client that runs tools **outside** the chat_runner / SubagentManager audit loop — the knowledge `llm_pool` worker-pool client, whose tool calls would otherwise never reach the security audit log. When set, `_maybe_audit_tool_call()` emits a per-tool-call SEL `tool_invocation` record; when `None` (chat / subagent clients) it is a no-op so those paths never double-log. The `sel().log_tool_invocation` call is offloaded onto `subprocess_executor()` (so SEL-backend I/O can never block the event loop) and bounded by `asyncio.wait_for(..., _SEL_AUDIT_TIMEOUT_SECONDS=5.0)`; a timeout or any SEL failure is swallowed (logged at `WARNING`) so tool dispatch always proceeds. **Note:** Code Review Sage's `ReviewPool` runs on the shared `AcpRuntime` path (see "Additional consumers" above) rather than as an `audit_source` client. The runtime layer has no `audit_source`, so the pool emits the same per-tool SEL `tool_invocation` audit itself from `sage_lib/review_pool.py`, which is what keeps audit parity.

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

### Turn-boundary loss diagnostics (content-free)

Two places in `AcpSessionHandle.prompt` could destroy or omit a turn's evidence
silently. Both now report, and both report **only** a count or the bare fact —
never frame text, tool arguments, tool results, or frame SIZE, since a size leaks
response length.

- **Pre-turn stale drain.** The drain empties the session queue of frames left by
  an abandoned turn (see the cancel-unacked / stale / tool-stall / timeout paths,
  which synthesize a terminal and return while the real kiro-cli turn keeps
  emitting). Permission REQUESTS are answered rather than dropped; everything else
  is discarded, which used to happen with no count and no log. It now counts the
  discarded frames, classifies each one, and emits **one** WARNING per turn — one
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
report. Unknown `sessionUpdate` discriminants in `_dispatch.py` are still ignored
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

Both transports share one parser — `acp/_dispatch.py`
(`parse_session_update`, `build_permission_event`, `parse_usage_update`, …) — so
they cannot drift. `AcpRuntime.load_session()` mirrors `AcpClient`'s resume
handshake: it issues `session/load` directly under the original sessionId and
registers the session queue **after** the load response so replayed transcript
frames are dropped rather than counted against the current turn.

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

**Crew answers no credential callback; kiro-cli's relay owns KAS auth.** KAS is
reached as `kiro-cli acp --agent-engine v3 --auth-method cli`, whose relay
forwards unrelated NDJSON frames byte-for-byte and consumes
`_kiro/auth/getAccessToken` itself, resolving tokens from kiro-cli's own store.
Crew therefore never sees that frame and holds no KAS token. One consequence is
recorded in KAS's auth declaration and reaches callers as
`backends_retired_by_host_logout()`: because the relay signs in from kiro-cli's
store, a KAS runtime is retired by an external `kiro-cli logout` on the same terms
as the kiro backend. A second is that the KAS process gets no OS
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
`test/test_acp_runtime.py`, which force the branch through
`platform_compat.IS_WINDOWS`): a branch one CI lane reaches is a branch whose loss
is invisible in every other lane. The shared kill test double drains the Windows
tree by awaiting `process.wait()` for the same reason the real
`terminate_windows_asyncio_tree` does — that await is what populates `returncode`,
so a double returning without it would report the placeholder on Windows and hide
the amendment behind its own unfaithfulness.
