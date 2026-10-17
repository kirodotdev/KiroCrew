# Kiro BYO

**Provider-neutral ACP Harness for KiroCrew**

Kiro BYO is a provider-neutral ACP Harness for KiroCrew. It runs as an alternative
Agent Backend alongside the official Kiro CLI and lets users bring their own model
providers without making Kiro BYO a child of Kiro CLI or a KAS wrapper.

Kiro BYO is a parallel ACP harness for KiroCrew. KiroCrew remains authoritative for
session orchestration, backend-card capabilities, Crewmate governance, Side Chat,
hooks and permission UI. Kiro BYO owns provider transport, the agent/tool loop,
native host tools, MCP bridging, session persistence and provider telemetry.

Current release: **0.1.0**. Kiro BYO 0.1.0 is the branded Provider-neutral ACP Harness release; its version is independent of KiroCrew's product version. The verified host contract remains **KiroCrew 0.9**, with source-audit coverage through **`v0.9.0-insider.6`** (`b4cf148`, re-audited 2026-10-13) and `release/0.9.0`. Newer KiroCrew versions are not rejected merely for changing the version string: they enter semantic preflight and are accepted when every required seam still validates. Versions beyond the audited family are labelled as semantically compatible but not yet source-audited; genuinely changed upstream seams continue to fail closed.

0.1.0 preserves the provider-neutral Harness boundary, carries mid-turn steering through native process and permission waits, adds session-scoped directory permission escalation, ships Xiaomi MiMo V2.6 as a first-class provider, and reorganizes the App UI for denser day-to-day management. Exact installed-package acceptance should still run the verifier/install/runtime/repair/rollback sequence after a KiroCrew update.

## Install / update

Install the folder or zip containing `app.json` as the **Kiro BYO** KiroCrew App.
Then open:

```text
Kiro BYO -> Integration -> Install / Update integration
```

Completely restart KiroCrew after the integration succeeds. Then select:

```text
Settings -> Agent Harness -> Kiro BYO
```

Create a **new** chat/session after switching harness. Existing sessions retain
the backend they were created with.

There is no supported PowerShell source-overlay installer in 0.1.0. Integration
is owned by the App and uses the semantic Crew 0.9 adapter.

### Management status surface

The App exposes one backend-owned overview snapshot that combines Integration,
Provider/model discovery, credential-store identity, runtime session capabilities,
native-tool inventory and metadata-only turn diagnostics. Health/alert policy is
computed in the Python backend and rendered by the UI; the React page does not
independently guess whether the backend is ready. No API key, prompt, response
body or tool arguments/results are included in the snapshot.

On desktop the Kiro BYO sidebar keeps the compact runtime summary visible while
Providers, Diagnostics and Integration are open. Clicking the Kiro BYO header
returns to a compact single-screen Overview; normal desktop-height Overview does
not require a page scrollbar, while genuinely long data pages may scroll.

### Runtime Inspector

Kiro BYO now keeps a second, bounded **runtime event stream** beside the existing
metadata-only turn telemetry. The event stream follows ACP ingress/egress, provider
rounds, streaming deltas, deferred tool search, native/MCP tool execution and turn
completion, which makes the harness inspectable without coupling observability to a
specific Provider.

The `KIRO_BYO_RUNTIME_TRACE` environment variable controls
content retention:

```text
off       no runtime event ledger
metadata  default; event/routing facts and payload shapes only
full      local debugging; includes prompt/response/tool content
```

`full` mode recursively redacts credential-shaped fields such as API keys,
Authorization headers, tokens, cookies and passwords before writing JSONL, but it
can still contain user/workspace content and should be treated as sensitive local
debug data. The App's **Diagnostics -> Runtime Inspector** surface shows the recent
event stream and can clear it independently of ordinary turn telemetry. Runtime
trace data is never shipped in the release archive.

## Crew 0.9 integration architecture

Crew 0.9 routes multiplexed ACP backends through `HarnessAdapter` and shared
`AcpRuntime`. Kiro BYO therefore registers a dedicated `KiroByoHarness`; it does
not patch the legacy per-session `AcpClient` spawn path.

The bundled `adaptive09` transformer modifies ten recognized Crew seams and
creates one Kiro BYO-owned harness module. Every source seam is inspected before
write. Installation is transactional: all seams preflight first, exact pre-write
bytes are snapshotted, writes are verified, and a later failure rolls every Crew
file back to the transaction-start state. Startup repair uses the same semantic
inspector and never guesses through an unknown upstream change.

Backend inheritance follows the selected whole-Crew harness. Ordinary/root/derived
sessions continue to resolve from `agent.acp_backend`. KiroCrew insider.6 normally
resolves member-DM sessions from the separate `agent.member_acp_backend` axis; the
adaptive integration overrides that one resolution only while `agent.acp_backend` is
`kiro-byo`, so members follow Kiro BYO too. Switching the selected backend away from
`kiro-byo` immediately restores KiroCrew's native member-backend semantics. No
provider or model identity participates in this decision.

Saved-agent authorization remains Crew-owned. The Kiro BYO harness projects only
`allowedTools` grants that survive Crew's live governance ceiling, official KAS
permission classification, and PreToolUse-hook withholding. Those fresh per-session
grants may skip `session/request_permission`; all unmatched calls return to Crew for
approval, and Kiro BYO's local sandbox/hard-deny checks still run first. The grants
are not persisted by Kiro BYO across a later `session/load`.

### Personal Agent / custom-agent fit

Kiro BYO fits KiroCrew's personal-agent architecture at the **ACP backend** layer:
Crew remains the owner of personal/custom agent selection and parses JSON/Markdown
agent specs, while Kiro BYO consumes the live per-session projection. Prompt, tool
visibility, `allowedTools`, `excludedTools`, projected MCP servers and Crew-injected
resources are honored; Crew keeps hooks and higher-level personal-agent lifecycle
governance. Provider/model choice remains Kiro BYO-owned, so an agent template's
model pin is not allowed to silently cross the backend ownership boundary.

This is why Markdown/custom agents are genuinely usable with Kiro BYO while the
stronger legacy `member_saved_agent` capability remains intentionally unclaimed:
BYO supports the personal-agent projection needed to run the session, but does not
pretend to own Crew's complete saved-member spec lifecycle.

### Crew-authoritative legacy capability card

Kiro BYO does not maintain a private score table. The following 9/10 surface is a
**legacy KiroCrew backend-card contract**, not a percentage estimate of official Kiro
Unified Harness parity. The audited Crew 0.9 seams derive this boundary after
installation:

```text
crew_tools                yes
member_thread_tools       yes
member_saved_agent        no
side_chat_tools           yes
subagent_continuation     yes
mid_turn_steer            yes
manual_compact            yes
reasoning_effort          yes
model_switch              yes
markdown_agents           yes
                           = 9 / 10
```

Kiro BYO now participates in Crew's own per-session member-dispatch and member-panel
mounts, so member threads use the selected Kiro BYO harness without a second model
selection path. It intentionally still does **not** claim `member_saved_agent`: that
Crew capability means full saved-member activation with host-observable lifecycle
evidence, which is stronger than per-session agent/prompt/tool projection. Markdown-defined
agents are now supported through the same host-format seam as KAS: Crew parses the
user-level JSON/Markdown spec and Kiro BYO consumes the projected active agent over
`session/new` / `session/load`. Native Kiro CLI may therefore still score differently
on this compact card; the number is not a parity percentage.

## Provider configuration

Preferred path:

```text
Kiro BYO -> Providers
  -> provider preset or custom endpoint
  -> API Key
  -> Save & discover models

Kiro BYO -> Models
  -> Mode: Tiga
  -> Type: Multi Type (default)
```

API keys are stored outside `providers.json` through the operating-system
credential store. Model capability is model-scoped: reasoning controls, tool-use
metadata and context-window information are published only when known or
discovered. Unknown values remain unknown rather than receiving guessed defaults.

### Kiro BYO modes and execution routing

Kiro BYO exposes its own execution mode instead of presenting a fake provider model named `auto`. The built-in mode is **Tiga**. Its default **Multi Type** is the balanced mode intended to match the role of an official Auto system: it re-evaluates every turn after the current prompt and context are known and may use one or multiple models when that improves expected value. Manual concrete-model selection bypasses BYO mode routing completely.

Internally, Tiga is implemented by the built-in **Tiga Policy Pack**, with **Multi Type** selected by default:

- **Sky Type** — minimize latency and cost. Usually single-pass; exceptionally hard/high-reasoning turns may use a low-overhead verifier.
- **Multi Type** — maximize expected value; recommended default. It stays single-pass for ordinary work and adds a value-oriented verifier only when complexity/risk justifies the extra pass.
- **Power Type** — maximize capability and reliability while retaining practical latency/cost bounds. Complex, coding, tool-heavy, or high-reasoning turns proactively gain a strong independent verifier.
- **Glitter Type** — long-task multi-model collaboration. A strong lead owns planning, changes and the final answer; fast same-provider teammates may continue read/search/fetch work, and an independent equal-or-stronger final review is added when available.

These four names are no longer hard-coded router branches. They are data in the
**Tiga Policy Pack**. The core Execution Planner consumes a generic Policy Pack
schema, so other modes (for example Dyna, Gaia, Cosmos, or a user-defined work
profile) can provide different Types without changing router code. In the UI the user sees **Mode / Type**; “Policy Pack” is the internal extension contract. User packs
are JSON files under `<KIRO_BYO_HOME>/policy_packs/` and are re-read on the next
config/UI access; no process restart is required. A custom file may also replace a
built-in pack id intentionally. See `POLICY_PACKS.md`.

Policy packs describe execution intent only. They do **not** change model-owned
capabilities. For a model whose deep-thinking switch is user-set to **On** or **Off**,
that choice is a hard lock; **Auto** delegates the per-invocation decision to the current
Type. Reasoning effort is a separate control: an explicit user effort level is also a
hard lock, while Auto/default lets the Type choose only among levels that the concrete
model really exposes. Fixed-on/fixed-off thinking and models with no effort control
cannot be changed by a Type.

Agent execution is not cut off by a small fixed round count. Productive long tool chains
may continue. A provider-neutral execution guard watches repeated identical actions and
short stagnating cycles, asks the model to re-plan once, and stops only if the same lack
of progress continues; a high emergency backstop exists only for catastrophic runaway.

Routing first removes models that explicitly cannot satisfy the turn (for example
image input, tool use or context capacity), then scores the eligible pool using task
complexity, reasoning effort, code/tool pressure and model routing hints. Providers
may supply explicit `routing.quality`, `routing.speed` and `routing.economy` values;
otherwise neutral capability/tier priors are used. The selected model, ordered
candidates, scores, signals and safe first-request fallback are recorded in Runtime
Trace. Provider cards therefore do not have a separate "default model" control.

0.1.0 keeps an **OpenAI-compatible** fallback transport, but verified official
endpoints use the highest-fidelity documented provider path when it materially
preserves reasoning/tool continuation or provider-side state/cache behavior. DeepSeek,
OpenAI, xAI/Grok, Volcengine Ark, Baidu Qianfan and Groq use Responses-family
transports; Anthropic uses Messages; Gemini uses Interactions; Cohere uses Chat V2.
Moonshot/Kimi, Xiaomi MiMo and Zhipu/GLM retain their official Chat wire extensions so
`reasoning_content` and tool-call state survive agent loops. Xiaomi has separate first-party
connection templates for pay-as-you-go (`sk-`, `https://api.xiaomimimo.com/v1`) and Token
Plan (`tp-`/`ttp-`, `https://token-plan-{cn,sgp,ams}.xiaomimimo.com/v1`). Pay-as-you-go
discovers the current Pro / Flash / Pro-Ultraspeed agent models from Xiaomi's mixed `/models`
catalogue; Token Plan uses Xiaomi's documented subscription entitlement for V2.6 Pro/Flash.
Both paths retain the same native MiMo reasoning/tool continuation adapter.
MiniMax uses its officially recommended Anthropic-Messages-compatible endpoint with
interleaved thinking and prompt caching. Qwen uses a provider-aware Chat adapter for its official compatibility
endpoint and automatically upgrades documented Qwen 3.5–3.8 models to Responses;
Tencent Hunyuan likewise upgrades the documented Hy3/Hy4 models to Responses.
Mistral uses a dedicated Chat adapter that preserves `ThinkChunk` reasoning and a
stable per-session `prompt_cache_key`. StepFun intentionally remains on its documented
OpenAI Chat interface: Kiro BYO does not invent an unpublished proprietary protocol.

Provider-native behavior is enabled only on the preset's verified official endpoint. If
a preset is pointed at a relay/custom Base URL, Kiro BYO fails safe to OpenAI
Compatible and does not inject vendor-specific capability metadata or wire formats.

## ACP control plane

Kiro BYO supports multiplexed `session/new`, durable `session/load` / resume,
streaming, cancellation, mid-turn steer, model/effort config options and manual
compact. Saved Crew agent projection is persisted per session and re-projected on
load, without advertising the stronger Crew `member_saved_agent` capability.

`session/new`, load and resume validate the ACP working directory and
`additionalDirectories`. Client-supplied additional directories are not silently
resurrected when the client omits them later.

The prompt implementation is split into a small request coordinator plus an
independent provider/tool loop and dedicated native/MCP/deferred-search helpers;
this keeps transport, tool execution and lifecycle recovery from sharing one
monolithic dispatch function.

## Native host tools and permission boundary

Core tool surface:

```text
read              read one text file / compatibility directory read
read_files        read several known text files in one tool call
list_directory    explicit directory listing
glob              path-pattern discovery
grep              regex content search
write             atomic full-file create/replace
edit              unified-diff/apply-patch
delete_file       regular-file-only deletion
shell                    bounded one-shot PowerShell / shell command execution
execute_shell_process     start a session-owned long-running process
control_shell_process     read/status/stdin/terminate/kill a long-running process
code_symbols              structural symbols / code overview (Python AST fallback)
code_query                raw Tree-sitter structural query (advertised only when installed)
web_fetch                 public HTTP/HTTPS text fetch
web_search                public web result search
```


### Long-running process model

`execute_shell_process` promotes an OS process to a first-class, session-owned
Harness resource. Each process receives a stable `process_id`; stdout/stderr use
bounded ring buffers; stdin can be written later; and terminate/kill target the
whole process group/tree rather than only the shell parent. Completed-process
history is also bounded so a long-lived ACP session cannot accumulate unlimited
output handles. Replacing, closing or deleting the ACP session closes its native
runtime and terminates all remaining session-owned processes. `session/cancel` continues to cancel the current turn without implicitly killing intentionally persistent background servers. Mid-turn `steer` is different: it interrupts the current one-shot shell/process action, preserves the session, injects the newer user instruction and lets the Agent replan.

### Code intelligence / Tree-sitter adapter

`code_symbols` is always available as a read-only structural tool. When the
optional Tree-sitter runtime plus Python/JavaScript/TypeScript grammars are
installed, it uses Tree-sitter syntax trees; otherwise Python files use an
explicit stdlib `ast` fallback. JavaScript/TypeScript structural analysis requires
Tree-sitter. `code_query` is **not advertised at all** unless the complete
Tree-sitter adapter is importable, so the model is never shown a capability the
runtime cannot execute.

For a standalone Python installation:

```text
pip install -e ".[code-intel]"
```

KiroCrew Apps execute backend hooks inside Crew's Python environment, so the App
integration does not silently run `pip install` or mutate that interpreter. To
enable Tree-sitter there, install the optional dependencies into the exact Crew
backend interpreter through an operator-controlled deployment step. Python
`code_symbols` remains usable without that optional install.

The filesystem tools share the same canonical allowed-root and sensitive-path
boundary. `delete_file` never removes directories. Windows `shell` uses PowerShell
with cwd, timeout, cancellation and process-tree termination. `web_fetch` and
`web_search` are separate Crew permission capabilities and reject localhost,
private/link-local destinations, embedded URL credentials, oversized responses
and unsafe redirects. The bundled search backend is intentionally lightweight and
can be less reliable than a commercial search API; MCP remains available for
provider-specific search services.

Policy file:

```text
<KIRO_BYO_HOME>\config\native_tools.json
```

Important boundaries:

- filesystem paths are canonicalized and checked against allowed roots;
- symlink/junction escape and explicit sensitive locations are rejected;
- `read_only` can disable filesystem mutation and shell execution globally;
- `web_enabled` can hide/disable both native web tools;
- web timeout/response-size limits are app-owned hard execution boundaries;
- hard-denied sensitive paths and impossible requests fail before user approval;
- an otherwise valid filesystem target outside `cwd` / `additionalDirectories` can request a narrow Crew-hosted grant: allow-once is revoked after the call and allow-always is scoped to the current Kiro BYO session only;
- every executable call that survives local hard-deny checks is still governed by KiroCrew permission/trust policy: fresh safe `allowedTools` grants may run no-prompt, while unmatched calls traverse `session/request_permission`;
- Kiro BYO has no second per-tool user-approval system that can bypass Crew and dynamic directory grants never rewrite the global policy file.

PowerShell still executes with the current Windows user's OS authority. Root
checks, approval, timeout and cancellation are not an OS sandbox; strong shell
filesystem confinement requires a Windows isolation boundary such as Restricted
Token/AppContainer/ACL design.

## MCP

Kiro BYO supports MCP over **stdio** and **Streamable HTTP** across the current and
legacy wire eras used by supported servers.

For the modern protocol it probes `server/discover`, negotiates an actually
supported common protocol version, validates discovery/tool-list cache metadata,
and handles HTTP routing/session headers per request. `-32022
UnsupportedProtocolVersion` is not treated as automatic proof of a legacy server:
legacy fallback occurs only when the server's advertised support provides valid
legacy evidence. Deprecated standalone SSE fails closed.

Legacy stdio negotiation reuses the same child process/connection rather than
spawning a second process after probing. Interactive modern results that require
unsupported host projection fail closed rather than being silently converted to
success.

## Usage and diagnostics

When the provider reports token usage, prompt responses preserve input/output,
reasoning tokens and cache signals. Cache **read** and cache **write/creation** tokens
are tracked separately when the provider exposes both (for example Anthropic-style
prompt caching); an unreported cache field remains unknown rather than being shown as
zero. When the real model context window is known, Kiro BYO emits
`usage_update {used,size}` so Crew can display a real context meter. No fake context
window is emitted for unknown models.

Successful-turn telemetry is committed before the ACP prompt result returns,
preventing an immediate diagnostics refresh from showing a completed turn as
zero.

## Verification

Run the full source suite:

```powershell
$env:PYTHONPATH="src"
python -m pytest -q
```

Run the no-write Crew semantic verifier against an installed Crew package:

```powershell
$env:PYTHONPATH="src"
python scripts\verify_crew09_integration.py --crew-package "C:\Users\<you>\AppData\Local\Programs\KiroCrew\resources\backend-dist\kirocrew-backend\Lib\site-packages\kiro_crew"
```

After installing the adapter into a semantically compatible Crew package, the
expected legacy backend-card summary is:

```text
Crew-derived score: native Kiro 8/10; Kiro BYO 9/10
PASS: semantic seams compile; source-shape-appropriate Crew selectability contract is active
```

See `WINDOWS_QUICKSTART.md` for the real-machine acceptance sequence,
`AUDIT_CURRENT.md` for the current third-party-style audit evidence,
`ARCHITECTURE_BOUNDARY.md` for Crew/Kiro BYO ownership, and
`PARITY_IMPLEMENTATION.md` for the historical 0.9.10 implementation record.
Historical release notes belong in `CHANGELOG.md`, not in this README.
