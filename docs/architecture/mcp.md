# MCP Server Architecture

How MCP (Model Context Protocol) servers are configured, merged, probed and
loaded, plus the two invariants every new Kiro Crew MCP tool must satisfy: it
ships as an MCP tool (not only a CLI command), and it holds no per-caller state.

Related: the CPP extension-point seam this doc reads from is
[platform-context](../system-specs/modules/platform-context.md); the governance
ceiling that filters auto-approve is
[governance](../system-specs/modules/governance.md); the computer-use server's
own gate model is [computer-use](../system-specs/modules/computer-use.md).

> **Design invariant: Kiro Crew does NOT write to provider globals.**
> `~/.kiro/settings/mcp.json` is user-owned. Kiro Crew reads it and never mutates
> it. Kiro Crew's own additions go into the per-agent file it fully owns,
> `~/.kiro/agents/kirocrew.json`. That keeps tools scoped to Kiro Crew out of every
> interactive kiro-cli and Kiro IDE session the user runs outside Kiro Crew. If
> `kirocrew-core` / `kirocrew-cron` ever appear in a provider global, that is
> leftover state from an older install: clean it from the dashboard MCP panel,
> or run `kirocrew cli-setup`, which calls the narrowly-scoped
> `mcp_cleanup.clean_stale_managed_mcp()` helper.

## Config file hierarchy

| File | Owner | Purpose | Read by |
|------|-------|---------|---------|
| `~/.kiro/agents/kirocrew.json` | Kiro Crew gateway (`agent.rebuild_agent_config`) | The rendered Kiro agent: model + tools + merged `mcpServers` | kiro-cli, when spawned as the `kirocrew` agent |
| `~/.kiro/settings/mcp.json` | User | Kiro global MCP servers | kiro-cli for all agents; merged into Kiro Crew's agent file at render time |
| `~/.kiro/crew/mcp.json` | User, via the dashboard MCP panel | specific to Kiro Crew additions and per-server tool disables | Kiro Crew gateway only |

`rebuild_agent_config()` writes exactly **one** file, `~/.kiro/agents/kirocrew.json`.
There is no second rendered agent file and no agent-file renderer for any other
provider: Kiro Crew is KiroACP-only.

### Provider-global scopes come from the platform seam, not the core

A provider-specific global (Claude Code's `~/.claude.json`, for example) is
**not** read by this build. It is contributed at call time by the CPP
extension point `McpToolingProvider.extra_mcp_scopes()`
(`platform/interfaces.py`), and the public `DefaultMcpToolingProvider`
(`platform/defaults.py`) returns `[]`. So in this repo:

- `agent._extra_mcp_scope_globals()` yields no paths, so the rebuild merges the
  Kiro global only.
- `mcp_discovery._extra_scope_sources()` yields no extra scopes, so discovery
  scans the two core files only.
- `dashboard/handlers/mcp.py`'s apply and uninstall paths write the Kiro scope
  only.

The three stay symmetric on purpose. If discovery scanned a file that apply and
uninstall could not manage, a server would show up in the dashboard that the
dashboard could never remove, and the rebuild would keep re-merging it into
sessions. `agent._CC_MCP_JSON` and `mcp_discovery.SCOPE_CC_GLOBAL` are retained
as the canonical constants for a companion edition and for tests, not as
evidence that the core reads that file.

### Merge order in `rebuild_agent_config()`

The existing `~/.kiro/agents/kirocrew.json` is the merge **base** when one
exists and `clean=False`, so any server the user already customized survives
(`autoApprove` edits, hand-edits, servers added with `kiro-cli mcp add --agent
kirocrew`). Onto that base:

1. **App-contributed servers** (`_collect_app_mcp_servers()`), keyed
   `{app}:{server}`, are assigned first so an app's namespaced entry outranks a
   same-named leftover in a shared file. Assignment, not `setdefault`: the
   manifests are authoritative and are re-derived on every rebuild, so keeping
   the previous rebuild's entry would preserve an `autoApprove` grant this pass
   had just stripped.
2. **`~/.kiro/settings/mcp.json`** (Kiro global) via `setdefault`.
3. **Seam-contributed provider globals** via `setdefault`, so they can only fill
   gaps the Kiro global did not. Empty in this build.
4. **`~/.kiro/crew/mcp.json`** via `update()` on an existing entry, so
   Kiro Crew's `command`/`args`/`env` win while user-set fields such as
   `autoApprove` survive.

Kiro global outranks any seam-contributed provider global because Kiro Crew is
kiro-cli-only. Managed servers are skipped by every merge loop: their
`command`/`args` are set by `_refresh_dynamic_fields()` and must not be
overwritten by a stale global entry.

**Resolution-aware fallback.** The same server can be defined in several sources
with different commands. If the merged winner's `command` does not resolve (a
bare command whose binary is not on the rebuild PATH), the rebuild retries the
same server's spec from the other sources in priority order (kirocrew, then
kiro-global, then provider-global) before dropping it. When it falls back to a
different source it adopts that source's `command`, `args` and `env` **as a
unit**, so one source's command is never paired with another's arguments.
Resolution uses the same `augmented_path()` the probe uses, so a server cannot
probe healthy on the dashboard while being silently dropped from the agent
config.

### `includeMcpJson` is pinned false

```json
{
  "includeMcpJson": false,
  "mcpServers": {
    "kirocrew-core":     { "command": "…", "args": ["mcp-core"] },
    "kirocrew-cron":     { "command": "…", "args": ["mcp-cron"] },
    "kirocrew-computer": { "command": "…", "args": ["mcp-computer"] }
  }
}
```

The gateway already merges the Kiro global into the agent file, so the agent
file is the superset. With `includeMcpJson: true` kiro-cli would merge the
global a second time at session start, producing duplicate entries and letting a
stale path in the global shadow the fresh path the gateway just resolved.
Kiro Crew forces `false` on every agent it manages (the primary agent and every
app agent). Plain kiro-cli agents outside Kiro Crew keep kiro-cli's own default.

### Managed servers

`agent._MANAGED_MCP_SERVERS` holds the three servers the gateway owns end to
end: `kirocrew-cron`, `kirocrew-core`, `kirocrew-computer`. Each is refreshed on
every rebuild by `_refresh_dynamic_fields()`, which rewrites `command`/`args`
from the live `kirocrew` binary, strips stale remote-transport fields (`url`,
`headers`) left by older builds, and re-pins `env.KIROCREW_HOME` to the home the
gateway is actually running under while preserving the user's own env keys.
User customizations such as `autoApprove` are preserved.

`kirocrew-computer` carries **no `autoApprove` key and none may ever be added.**
kiro-cli approves an auto-approved MCP tool locally and emits no permission
request, so `hooks.on_tool_call` (the PreToolUse deny floor, sensitive-path
check and governance ceiling) is never reached for it. For a tool that can click
and type into an already-authenticated application, that would be a complete
gate bypass. Its stdio shim answers an empty `tools/list` while the keystone
enable is off, so a disabled feature costs the model no context.

### The final auto-approve pass

`allowedTools` is kiro-cli's blanket auto-approve list, and it is the one path
that never reaches the PreToolUse gate. Builtin grants (`fs_read`,
`execute_bash`, …) arrive straight from the shipped agent template, so no
per-writer path re-touches them. The last thing `rebuild_agent_config` does
before writing is filter the whole assembled `allowedTools` list through one
predicate: a ref the governance ceiling has an opinion about loses its blanket
grant and its calls go through the gate, where the per-argument rule actually
applies; a ref the ceiling is silent about is kept. `mcpServers[*].autoApprove`
gets the same treatment on the final map. `tools` is deliberately left intact,
because mounting a tool is not auto-approving it. Withheld grants are recorded
in SEL as `mcp_auto_approve_withheld` so an operator can see why a template tool
now prompts.

### Two writers, one lock

`~/.kiro/agents/kirocrew.json` has two independent writers: this whole-file
regenerator and the app-MCP registration path
(`apps.bridges._register_mcp_servers`), which does a read-modify-write of the
same file under `bridges._mcp_lock`. A register landing between the rebuild's
app-server snapshot and its write would be silently erased by the full-file
regeneration, so the rebuild takes that same lock across a final re-read and
merge of the app-namespaced servers. An app server is dropped only when its app
is confirmed no longer enabled; absence from the on-disk map is not by itself
proof (a clean rebuild starts from an empty map, and dropping on that basis made
an enabled app's tools vanish).

## Discovery and probing

Source: `mcp_discovery.py`.

`list_servers()` reads `~/.kiro/agents/kirocrew.json`, then each scope file with
provenance, re-resolves stale managed commands, and overlays cached probe
results. Every returned `McpServerInfo` carries a `presence` dict so the
dashboard can render per-scope badges. The `kirocrew` badge is the **effective**
state after the merge minus explicit `disabled: true` overrides in
`~/.kiro/crew/mcp.json`; the other badges are raw membership in that scope's
file.

Probes run from `POST /api/mcp/probe`:

- **stdio** servers are spawned and driven through an MCP `initialize` handshake
  followed by `tools/list`.
- **HTTP** servers get the same two JSON-RPC calls over POST.
- Timeout is `dashboard.mcp_probe_timeout_secs` (default 15s;
  `_PROBE_TIMEOUT_SECS` is the fallback if config is not loaded yet). Results
  are cached for `_PROBE_TTL_SECS` (1800s), after which status reads as
  "outdated".
- A probed stdio child that ignores a closed stdin costs
  `_PROBE_TEARDOWN_WAIT_SECS` twice (graceful wait, then again after SIGKILL)
  before the process-group reap, which is why that budget is a named constant
  tests can shrink.
- **A probe that could not RUN is reported as a probe limitation, not a server
  fault.** `SandboxUnavailableError` is caught ahead of the generic handler:
  `probe_server` spawns through `sandboxed_spawn_argv(mode="standard")`, which
  fail-closes on a host with no OS sandbox backend (any Windows host, macOS >= 26).
  But kiro-cli launches these servers from the agent config **without going through
  this probe**, so the servers work while the probe cannot spawn them. Reported as
  an ordinary error, every row rendered red with "0 tools" and sent the user
  debugging a server that was fine. `server.error` therefore leads with the
  machine-readable `mcp_probe_sandbox_unavailable:` prefix (mirroring the `code`
  field on dashboard JSON error bodies), states that the server itself may be fine,
  and names the `agent.sandbox_allow_unsandboxed_exec` remedy. Because the cause is
  the HOST, it recurs identically for every server on every discovery cycle, so the
  remedy paragraph warns once per server name
  (`_warn_probe_sandbox_unavailable_once`) and demotes repeats to DEBUG.
  - **A managed server FALLS BACK to its declared tool list when — and only when —
    the sandbox refuses.** `kirocrew-core` / `-cron` / `-computer` declare their
    tools statically in this package (`mcp_core._list_tools()` and friends, the very
    functions the stdio shim answers `tools/list` from), so
    `_managed_tools_in_process` can serve the listing with no subprocess at all.
    That is what removes the `agent.sandbox_allow_unsandboxed_exec` opt-in for a
    read-only listing on a backendless host.
    - **Fallback, never primary.** When a backend exists the real spawn still runs,
      because it is the only thing that proves the server can **start**.
      `_fix_stale_managed_command` exists precisely because that invocation goes
      stale ("command not found: kirocrew; the built-in cron/core tools then never
      load"), and the probe is the one surface that catches it. Short-circuiting on
      the server *name* would report `ok` for a managed server that cannot run,
      silently changing what `ok` means in the shared `_cache_probe` store.
    - **Why the import is acceptable only here.** Reading the declaration imports
      package code **into the gateway process**, which the gateway does not
      otherwise do (these modules are absent from `sys.modules` at boot). The
      package directory is writable by the same uid the agent runs as and is not on
      the sensitive-path floor, so on a host where the sandbox *works*, importing
      would beat the isolation the spawn provides — which is why an earlier revision
      that made this the primary path was wrong. Reaching the fallback means the
      sandbox could not confine anything anyway, so the import concedes nothing the
      refused spawn had not already conceded.
    - The substitution is logged at **WARNING**, once per server: `ok` here means
      "this package declares these tools", not "the server answered", and the
      default log level is WARNING, so at info it would be invisible on exactly the
      hosts where it always happens. Third-party servers have no declaration to
      read and keep the honest `mcp_probe_sandbox_unavailable` error.
    - Modules are imported **lazily** (they pull in the validation/artifacts graph,
      which cannot be imported at `mcp_discovery` import time). Any failure returns
      `None` and the original refusal is reported, so a bad read never invents a
      result. An **empty** list is a real result: `mcp_computer._list_tools()`
      returns `[]` by design while the keystone enable is off.
- An MCP command that does not resolve is a **stable** fact, so it warns once
  per `(server, command)` and demotes the repeats to DEBUG. Timeouts and
  handshake errors stay at WARNING every time: a server that newly starts timing
  out is news, one whose binary is absent is not. The ledger self-heals, so the
  warning returns if the command later resolves and then breaks again.

`GET /api/mcp` also kicks off a background re-probe when it sees a server that
is not in the probe cache yet, so a freshly added server transitions from
"Unknown" on the next page load rather than waiting out the TTL.

`_fix_stale_managed_command()` re-resolves the `kirocrew` binary on every
`list_servers()` call, because the stored absolute path goes stale after an
update: first `agent._resolve_kirocrew_bin()`, then `shutil.which("kirocrew")`
on the augmented PATH.

## Dashboard MCP management

The Integrations page aggregates the scope files into one view with per-scope
badges. Clicking a badge **stages** an intent; the page accumulates staged
changes and exposes Apply / Discard. Only Apply performs writes.

`POST /api/mcp/apply` takes a batched payload and applies it in a fixed order:

1. **Uninstalls first.** `_purge_server_config()` removes the entry from
   `~/.kiro/crew/mcp.json`, the Kiro global, every seam-contributed scope, and
   directly from `~/.kiro/agents/kirocrew.json`. That last targeted delete is
   required: the rebuild uses the existing agent file as its merge base, so
   without it the additive merge would resurrect the server. Every step is a
   read-modify-write that no-ops when the entry is already absent, so re-running
   the purge changes nothing.
2. **Scope adds** write the spec into the target scope file.
3. **Scope removes** strip it. If the server would no longer be inherited into
   Kiro Crew but the user kept the Kiro Crew badge on, the full spec is first
   copied into `~/.kiro/crew/mcp.json` (the **preservation rule**), which is why
   "I removed it from the Kiro global and it came back" is correct behavior.
4. **Per-tool overrides** update `disabledTools` on the entry.
5. **One rebuild** at the end re-renders the agent file from the new on-disk
   state.

No scope metadata is persisted. Apply does one-shot edits and forgets; state is
re-read from disk on the next page load, so external edits (`kiro-cli mcp
remove`, hand-edits) are picked up naturally.

Apply does **not** restart sessions. Scope changes take effect at the next
session spawn; the header's Apply & Restart calls `POST /api/sessions/restart`
to drain the warm pool of pre-spawned processes carrying the old config.

## How app agents reach MCP servers

An app declares MCP servers in its manifest, and
`apps.bridges._register_mcp_servers()` writes them into Kiro Crew's agent config
under a `{app}:{server}` namespace rather than into the shared Kiro global,
because that global is read by Kiro IDE and every other kiro-cli agent, so an
app's private tools would leak into surfaces that never installed it.

An HTTP MCP server whose backend port cannot be resolved live is **not written
at all**, and any stale entry for it is scrubbed. A manifest's illustrative
fixed port written verbatim while the backend is down is a reachable-looking but
dead URL, and kiro-cli connects to every server in the agent config on each
request, so one dead entry surfaces as a transient 5xx and then a hard error for
**all** requests, not just that app's. The enable path re-registers with the
real port once the backend is up.

An app agent that references a host-managed server (`@kirocrew-core`,
`@kirocrew-cron`) in its `tools` gets the launch spec copied in by
`_materialize_managed_refs()`. kiro-cli resolves a `@server` ref against the
agent's own `mcpServers` plus the global `mcp.json`, and managed specs live in
the host agent's config only, so without that copy the ref dangles and the tool
silently never mounts.

Containment for app agents has three layers:

| Layer | Mechanism | Where enforced |
|-------|-----------|----------------|
| Agent config | `managedToolPolicy` renders as `disabledTools`; a `neutralize` entry re-declares a server with every tool disabled and does not add it to `tools` | Written at registration, no network |
| kiro-cli | Reads `disabledTools` and filters before the model sees the list | In-process, no network |
| MCP server | `GET /api/session-tool-policy` returns the calling session's `managedToolPolicy.exclude`, and the server filters `tools/list` and `tools/call` | Gateway round-trip |

`managedToolPolicy` and `includeMcpJson` are in
`bridges._FRAMEWORK_OWNED_AGENT_KEYS`, so they are refreshed from the template on
every boot rather than preserved as user preferences. Preserving them is wrong in
both directions: a template that later tightens `exclude` would never reach an
already-enabled install, and anything that edits the agent file could drop the
exclude list, which the framework would then faithfully preserve forever.

`neutralize` uses explicit tool lists rather than a wildcard, because the app
discovers the real tool names, so a server that grows a tool cannot quietly slip
past a stale pattern.

The third layer is defense in depth for hosts that ignore `disabledTools`, and it
fails **open** by design: kiro-cli calls `tools/list` once at session start, so
returning an empty list on a transient gateway failure would leave that session
permanently believing the server has no tools, unrecoverable without a restart.
A missing session key is not cached (a startup race must be retryable); a
resolved key whose policy call fails gets a 30s negative cache so a persistently
unreachable gateway does not add a 5s timeout to every tool call. The gateway
side is deny-by-default in the opposite sense: a caller that cannot prove its
identity gets a 400/404, never an empty policy.

## The MCP-first rule

**A new LLM-facing capability MUST ship as an MCP tool, not only as a CLI
command.** kiro-cli calls MCP tools reliably and may refuse to run a CLI command
via bash. CLI commands stay for human use; the model uses the MCP twin.

Do NOT add regex to match natural-language variants of a command. The LLM does
the interpreting. Handler keywords are only for instant user-typed commands that
need no model round-trip (`cron list`, `spawn list`).

### Server and tool inventory

Managed servers, registered by `agent._MANAGED_MCP_SERVERS` and installed into
`~/.kiro/agents/kirocrew.json`:

| Server | Process | Tools |
|--------|---------|-------|
| `kirocrew-cron` | `kirocrew mcp-cron` (`mcp_cron.py`) | `cron_add`, `cron_list`, `cron_update`, `cron_remove`, `cron_remove_all`, `cron_pause`, `cron_resume`, `cron_trigger` |
| `kirocrew-core` | `kirocrew mcp-core` (`mcp_core.py`) | spawn/subagent, learn, task, messaging, artifact, workflow, knowledge and session-directive tools (see below) |
| `kirocrew-computer` | `kirocrew mcp-computer` (`mcp_computer.py`) | `computer_list_apps`, `computer_get_state`, `computer_click`, `computer_drag`, `computer_type_text`, `computer_press_key`, `computer_set_value`, `computer_scroll`, `computer_perform_action`, `computer_end_turn` |

CLI commands and their MCP twins:

| CLI command | MCP tool | Server |
|-------------|----------|--------|
| `kirocrew cron add` | `cron_add` | `kirocrew-cron` |
| `kirocrew cron list` | `cron_list` | `kirocrew-cron` |
| `kirocrew cron update` | `cron_update` | `kirocrew-cron` |
| `kirocrew cron remove` | `cron_remove` | `kirocrew-cron` |
| `kirocrew cron remove-all` | `cron_remove_all` | `kirocrew-cron` |
| `kirocrew cron pause` | `cron_pause` | `kirocrew-cron` |
| `kirocrew cron resume` | `cron_resume` | `kirocrew-cron` |
| `kirocrew cron trigger` | `cron_trigger` | `kirocrew-cron` |
| `kirocrew spawn run` | `spawn_run` | `kirocrew-core` |
| `kirocrew spawn list` | `spawn_list` | `kirocrew-core` |
| `kirocrew learn add` | `learn_add` | `kirocrew-core` |
| `kirocrew learn list` | `learn_list` | `kirocrew-core` |
| `kirocrew learn remove` | `learn_remove` | `kirocrew-core` |
| `kirocrew run TASK.md` | `task_run` | `kirocrew-core` |
| `kirocrew computer apps` | `computer_list_apps` | `kirocrew-computer` |

`kirocrew-core` tools with no CLI twin, grouped by concern (authoritative list:
the `tools/list` payload in `mcp_core.py`):

- **Subagents:** `spawn_status`, `spawn_continue`, `spawn_steer`,
  `spawn_release`, `spawn_sub_agents`, `wait`
- **Messaging and notification:** `send_message`, `send_notification`,
  `delete_message`, `file_send`, `read_slack_profile`
- **Session-bound directives** (`session_directive.DIRECTIVE_TOOLS`):
  `ask_question`, `suggest_followup`, `monitor_start`, `monitor_update`,
  `autonudge_stop`, `set_project`
- **Crew routing:** `select_crew`
- **Sessions and history:** `list_sessions`, `get_chat_session`,
  `search_chat_history`
- **Artifacts:** `artifact_list`, `artifact_get`, `artifact_save`,
  `artifact_update`, `artifact_delete`, `artifact_move`, `artifact_versions`,
  `artifact_revert`, `artifact_folder_list`, `artifact_folder_create`,
  `artifact_folder_rename`, `artifact_folder_move`, `artifact_folder_delete`,
  `artifact_get_comments`, `artifact_post_comment`, `artifact_reply_comment`,
  `artifact_delete_comment`, `artifact_mark_review`, `deploy_artifact`
- **Knowledge and skills:** `local_knowledge_search`, `knowledge_dedup`,
  `skill_discover`, `skill_search`, `skill_fetch`, `browse_outline`,
  `browse_search`
- **Workflows and hooks:** `workflow_author`, `workflow_list`,
  `workflow_cancel`, `workflow_rerun_subtree`, `register_hook`
- **Diagnostics:** `resource_status`, `issue_radar_record_investigation`

External servers a user may install (a Playwright proxy under the canonical
`playwright-mcp` alias, a Slack server, anything else) are ordinary user-added
servers: they live in one of the scope files and are merged into the agent config
at render time. They are not managed, so a `mcp_server_alias` normalization pass
rewrites slash-containing keys to kiro-safe aliases and
`browser.setup.converge_playwright_servers()` folds every Playwright-proxy entry onto the one
canonical key, keyed by resolved launch target, so a legacy slash-free key
re-injected from `~/.kiro/crew/mcp.json` cannot spawn a second backend.

### The one deliberate exception

`kirocrew computer call <tool>` has **no MCP twin, on purpose.** It is not a
capability; it is a human debug and repro harness that runs the ten existing
`computer_*` tools through the same gated chokepoint (optionally a JSON array of
them in one process, so `element_index` values stay resolvable across calls). The
MCP-first rule exists so the model gets a structured tool instead of shelling
out, and the model already has all ten. A tool that runs other tools would let a
model launder one per-call gate decision into many, so do NOT add
`computer_call`.

## MCP tools MUST be stateless

**A new `kirocrew-core` or `kirocrew-cron` tool MUST NOT keep per-caller or
per-session state in the MCP-server process. Resolve the caller's identity on
every call and keep authoritative state in the gateway.**

### Why: the shared-backend invariant

The managed servers are long-lived stdio subprocesses, and **one server process
serves many sessions.** In the pooled topology a single warm backend is reused
across sessions, and a sub-agent spawned via `spawn_run` runs inside the parent
slot's process tree and talks to the same MCP server. Anything the process
remembers is therefore shared by every session and sub-agent that touches it.
Two failure modes follow.

**1. Identity is not the process, it is the call.** `KIROCREW_SESSION_KEY` and
`os.getppid()` identify the *process*, which is wrong by construction in a shared
backend: the warm pool spawns with an empty key, and a sub-agent inherits its
parent's tree. `mcp_core.py` offers two resolvers:

- `_resolve_session_key_strict()` for **anything that mutates or targets a
  specific session** (post to a slot, change its state, deliver a callback). It
  accepts only the gateway-injected caller context (`mcp_caller.current_caller()`,
  which gatewayd stamps on every forwarded frame after stripping any
  client-forged `kirocrew.caller` block), the injected `KIROCREW_SESSION_KEY`, or
  a `KIROCREW_HOST_PID` lookup whose HMAC sidecar verifies against the
  keystone-protected `sel_hmac.key`. It deliberately **drops** the `/proc`
  ancestor walk and the bare `session_pid_<pid>.txt` fallback: the `.txt` file is
  agent-writable and therefore forgeable, and a sub-agent walking ancestors from
  its own MCP child resolves to its **parent** slot, which would let it mutate
  the wrong conversation.
- `_resolve_session_key()` (lenient, still walks ancestors) is only for read-only
  and telemetry callers where misattribution is harmless.

An unresolved key is not automatically a refusal. `mcp_computer.py` forwards an
empty key and lets the call proceed, because neither strict source exists for a
GUI-launched kiro-cli on macOS, so gating on identity would make the feature
unusable on its only supported platform. What is lost there is audit
*attribution*, not a control: the trail records an empty key, which is honest,
where the lenient walk would have recorded a forgeable one.

**2. State belongs in the gateway.** The tool should be a thin forwarder: resolve
the session, then `POST` to a gateway HTTP endpoint that owns the state (usually
in `DashboardState`), addressed by session key plus a per-request id, blocking on
that round-trip if it needs a result.

### Reference implementations

Two shapes are both correct; pick by whether the tool needs a value back inside
the same turn.

**`POST` to a gateway endpoint that holds the pending future.** The
`/api/ask-question` handler (`dashboard/handlers/ask_question.py`) is the model:
the pending question lives in `DashboardState._pending_questions` /
`_question_futures`, keyed by `ask_id`, and is addressed to one slot resolved from
the posted `session_key`. The handler refuses an unknown slot with 404 rather
than blocking for the full window on a card nobody will render, and the answer is
routed back by `ask_id` from `POST /api/ask-question/{ask_id}/answer`. A stateful
version, parking the pending question in a module global and trusting env-var
identity, would hand the answer to whichever session the shared process last saw
and let a sub-agent's card land in its parent's slot.

**Return a session directive and let the session-aware consumer apply it.** This
is what the `ask_question` MCP tool itself now does, along with `monitor_start`,
`monitor_update`, `autonudge_stop`, `set_project` and `suggest_followup`
(`session_directive.DIRECTIVE_TOOLS`). The tool validates its arguments and
returns a human-readable confirmation plus a marker line carrying the validated
payload and **no session key**. `dashboard/chat_runner`'s tool-result handler
decodes the marker, applies the effect against **its own** `slot.key`, then
strips the marker from the stored transcript. Sub-agent isolation is therefore
structural rather than cryptographic: a sub-agent's tool result flows through the
sub-agent's own runner, so it can only bind to the sub-agent's session. There is
no walk to get wrong.

The directive marker is model-visible, since it comes back as tool-result text,
so the consumer defends against forgery by honoring a directive only when the
tool call it arrived under was recorded, from kiro-cli's out-of-band `_meta`
channel, as an MCP-served call whose canonical name (`_meta.kiro.toolName`, with
`_meta.kiro.mcpServerName` equal to `kirocrew-core`) is in `DIRECTIVE_TOOLS`. The
LLM-authored `title` is explicitly not accepted, because a shell command titled
`monitor_start` whose stdout forges the marker must not be honored. The gate fails
closed when `_meta` identity is absent, and refuses native-sub-agent tool calls,
which surface as flat events in the parent's loop but have no independently
bindable slot. The marker is ASCII-only: an earlier invisible-separator prefix was
destroyed by `validation.build_tool_response`, which strips Unicode category `Cf`
from every tool response, so every directive silently failed. A machine-facing
framing token must not depend on characters that sanitizers and normalizers
legitimately rewrite. `encode()` refuses above `MAX_DIRECTIVE_CHARS` (3800), under
the ACP tool-result truncation bound, so an oversized payload fails loudly
instead of losing its trailing marker.

### The one allowed exception: caller-agnostic process caches

A module-level cache is fine when it is keyed on an **external** signature and is
identical for every caller. `mcp_core._KNOWLEDGE_CACHE` is keyed on the
knowledge-DB and config file signature and is shared safely across calls. Never
key a cache, or any retained object, on caller identity, session, or "the last
request I saw".

### Checklist for a new tool

- No module global holds per-call or per-session data.
- Identity comes from `_resolve_session_key[_strict]()`, never a bare env read.
- Anything mutating or targeting a session uses the **strict** resolver.
- Durable state lives behind a gateway endpoint keyed by session.
- The tool behaves identically whether it is the only caller or one of many
  sharing the backend.

## Troubleshooting

**MCP tools not working.** Check that `~/.kiro/agents/kirocrew.json` contains
`kirocrew-core` and `kirocrew-cron`, that `includeMcpJson` is `false`, then run
`kirocrew doctor` (which checks probe status) and read the live probe results in
the dashboard MCP panel.

**Status stays "Unknown".** The handler auto-triggers a probe for a server it has
no cache entry for, but the result only appears on the next refresh. If it stays
Unknown, the server is failing its handshake: read the dashboard error text or
the gateway log.

**Tools present in Kiro Crew but absent in interactive kiro-cli.** That is correct.
`kirocrew-core` / `kirocrew-cron` / `kirocrew-computer` are agent-scoped and must
not appear in interactive kiro-cli or Kiro IDE sessions. If they do, something
wrote them into a provider global.

**A newly added server does not appear in sessions.** The warm pool holds
pre-spawned processes carrying the old config. Use Apply & Restart, or
`kirocrew config set`, which triggers a restart.
