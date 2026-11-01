# Kiro BYO

## 关于本仓库

这是 **Kiro BYO 的源码仓库**，当前版本 **0.3.0**。Kiro BYO 是 KiroCrew 的第三方模型后端（App），**非 KiroCrew 官方项目**，也不随 KiroCrew 官方分发。

- **想安装使用**：请获取打包好的 release 压缩包（含安装包 `.whl` 与校验码），不要直接拿本仓库源码安装。本仓库只放源码。
- **兼容性**：0.3.0 对应 KiroCrew **0.11.0-rc.1**。版本不符时集成器会拒绝写入，以保护现有数据。
- **新手教程**：[中文教程.md](中文教程.md)（从安装到聊天，全流程）。

- **中文文档**：[WINDOWS_QUICKSTART.md](WINDOWS_QUICKSTART.md)（安装与升级）、[安装说明.txt](安装说明.txt)（本次热修复步骤）、[修复技术报告.md](修复技术报告.md)（修复原理）、[CHANGELOG.md](CHANGELOG.md)（版本历史）。
- **英文文档**：下方 README 正文、[QUICKSTART.md](QUICKSTART.md)、[RELEASE_NOTES_0.2.5.md](RELEASE_NOTES_0.2.5.md)。

### 仓库结构

| 目录 | 内容 |
|---|---|
| `src/kiro_byo/` | 主程序源码 |
| `backend/`、`ui/` | KiroCrew App 外壳（后端路由与界面） |
| `config/` | 配置样例（`*.example.json`）；真实配置与模型密钥不入库 |
| `integration/` | 对各版 KiroCrew 的集成适配 |
| `scripts/`、`tests/` | 构建/校验脚本与测试 |
| 各类 `*.md` 报告 | 开发过程的审计与阶段报告 |

安装包、release 副本、SHA256 校验码属于打包产物，**不放在本仓库**。

This repository is the **source code of Kiro BYO**, an unofficial third-party model backend for KiroCrew. It is not affiliated with the KiroCrew project. For installation use the packaged release, not this raw source tree. See [WINDOWS_QUICKSTART.md](WINDOWS_QUICKSTART.md) for the Chinese guide.

---


**Provider-neutral ACP Harness for KiroCrew**

Kiro BYO is a provider-neutral ACP Harness for KiroCrew. It runs as an alternative
Agent Backend alongside the official Kiro CLI and lets users bring their own model
providers without making Kiro BYO a child of Kiro CLI or a KAS wrapper.

Kiro BYO is a parallel ACP harness for KiroCrew. KiroCrew remains authoritative for
session orchestration, backend-card capabilities, Crewmate governance, Side Chat,
hooks and permission UI. Kiro BYO owns provider transport, the agent/tool loop,
native host tools, MCP bridging, session persistence and provider telemetry.

Current release: **0.3.0**. Prior: bounded, session-local `update_plan` with 20 steps and read-only Crew MCP refusal diagnostics. The Plan tool is not a Crew schedule/task database and is not durable. See `RELEASE_NOTES_0.3.0.md`.

Current compatibility: This is a **Crew 0.11.0-rc.1 compatibility candidate**, not a source-verified stable integration. The adapter is pinned to that exact prerelease and refuses unknown source seams without writing. Crew 0.10 support is retired. When the main backend is BYO, new member sessions are routed to BYO and conflicting explicit official member overrides are refused; saved Crew member preferences remain untouched when switching back. This is a source-level route guard, not Windows Mate 2.0 end-to-end certification (notably per-Mate Conductor/Worker paths and already-open sessions). Existing Tiga / Dyna policies and BYO runtime semantics are retained. Static source-contract checks do not constitute a real Windows end-to-end acceptance test.

0.2.0 is the first **productized Agent Harness UI** release on top of the Tiga Adaptive Routing / Unified Agent Runtime line. It keeps the bounded internal execution runtime, durable Run recovery and canonical session-event model, while replacing the debug-oriented management surface with a compact one-screen workbench: **Overview / Providers / Models / Modes / Accounts / Records / Settings**. Modes explains the selected Policy Pack and shows real recent routing/model-use evidence; Records presents requests, token/cache usage, route decisions and recovery in user-readable form, with raw runtime events available only as drill-down evidence. KiroCrew integration and ACP conformance live under Settings. Removed Tasks, standalone Integration, external-agent management and workspace-management pages are not part of the normal 0.2.0 product surface. Every Type still compiles into the same provider-neutral execution seam, Policy Pack schema v4 remains the authority for orchestration constraints, and Selection Truth records whether a session is actually on a Type or a concrete-model pin. Background scheduling remains owned by KiroCrew; BYO owns provider execution, durable recovery and its ACP/runtime boundary.

The adaptive model-ranking layer remains deterministic and evidence-bounded. Recent TTFT/turn latency changes the effective Speed signal, while recent success/error outcomes and same-task history add a bounded Reliability signal; Quality and Economy remain curated/provider catalogue priors. Hard capability gates run before scoring, schema-v1/v2/v3 custom Policy Packs remain compatible, and no Router LLM, random exploration, or user-editable routing weights are introduced. The Modes page is read-only observability: it renders Policy-Pack-owned Type behavior, the most recent real task-shape signals, and the selected Type's actual model usage over the latest 60 hours. Unknown evidence is rendered as unknown rather than filled with guessed values.

0.1.6 introduced the **Reasoning Language Policy**. User-visible Thinking/reasoning presentation can follow the user's current conversational language instead of each Provider's default language. `Auto` uses the current user message plus recent user turns and is independent of the KiroCrew UI locale. Technical terms and identifiers remain in their natural/original spelling when that is clearer (for example ACP, LLM, Token, API, Provider, Router, model names, `configOptions`, paths and commands). This remains a generation-time presentation hint for reasoning text/summaries an upstream already exposes; it does not request or expose hidden/private chain-of-thought, add a post-hoc translation pass, change Thinking/effort routing, or override an explicit final-answer-language request.

0.1.5 is the **Type-owned reasoning policy** release. Tiga Types no longer expose a session-wide reasoning-effort slider: Sky/Multi/Power/Glitter choose effort only after routing to a concrete model, using that model's actual advertised vocabulary, and models with no adjustable effort remain fully eligible. Switching from a concrete model back to a Type clears any stale manual effort lock. Concrete-model sessions still expose their real effort selector. Deep-thinking Auto/On/Off remains a separate per-model hard boundary: user On/Off wins, fixed-on models stay on, unsupported/unknown models are never forced, and Type Auto suggestions apply only when the concrete model can express them.


0.1.4 is the **Reasoning Surface** release. Reasoning effort is an ACP session control owned by KiroCrew's conversation UI, while the model-specific deep-thinking Auto/On/Off preference remains in the Kiro BYO Models page. Official-endpoint capability enrichment recognizes DeepSeek V4.1 Flash aliases and can fill verified capability facts from an official display name when a catalogue uses a different stable wire id. Hotfix 2 keeps the boundary strict: Kiro BYO advertises a live `effortLevel` config option only when the selected concrete model has verified selectable levels, and KiroCrew's own live session/config-option surface is the sole owner of whether the native effort selector is shown. Binary thinking toggles and unknown/new models therefore do not inherit low/high/max, while valid DeepSeek-style low/high/max controls are not hidden by a second browser-side guard. Type-to-concrete model switches push fresh config options immediately and clear incompatible stale effort locks; Kiro BYO never exports its deep-thinking toggle as a Crew config option. Hotfix 2 also makes the BYO conversation footer measure the full visible turn, including pre-dispatch setup, instead of only the provider-stream slice. Capability authority remains local override > explicit live provider facts > verified official profile > cache/name inference, so a future vendor/model is not guessed from its brand or model id.


0.1.3 is the **Core Refinement** release. The live Agent loop now advances through explicit provider-neutral Turn phases, provider rounds/tool batches/interruption handling sit behind narrow capability seams, and durable session state is projected from typed events instead of accumulating parallel mutable truths. Policy Pack v2 separates user-facing Type semantics from machine execution policy: the assistant can still explain Sky/Multi/Power/Glitter when asked, while the Turn Engine consumes only a compiled `ExecutionIntent`. Routing metadata now keeps field-level source/confidence so local overrides, live discovery, curated facts, cache and last-resort name inference have explicit authority. The model/provider intelligence added in earlier 0.1.x releases remains intact. Exact installed-package acceptance should still run the verifier/install/runtime/repair/rollback sequence after a KiroCrew update.

## Install / update

Install the folder or zip containing `app.json` as the **Kiro BYO** KiroCrew App.
Then open:

```text
Kiro BYO -> Settings -> KiroCrew -> Install / Update integration
```

Completely restart KiroCrew after the integration succeeds. Then select:

```text
Settings -> Agent Harness -> Kiro BYO
```

Create a **new** chat/session after switching harness. Existing sessions retain
the backend they were created with.

There is no supported source-overlay installer in 0.2.2. Integration is owned by the App,
uses the semantic Crew 0.10 adapter, and locates the active KiroCrew Python
package instead of assuming a Windows/macOS/Linux installation path. The App manifest
truthfully declares macOS, Linux and Windows; provider secrets use Windows Credential
Manager, macOS Keychain or Linux Secret Service respectively.

### Unified Agent Runtime and Subagents

Tiga Types compile to the same provider-neutral `ExecutionPlan`; the Turn Engine does not branch on Sky/Multi/Power/Glitter names. Broad, parallelisable, low-write turns may admit isolated read-only child agents within the Type's declared budget. Multi routes those children through its fast worker pool, Power through its strong pool, and Glitter through its fast gathering pool while the strong Leader retains all write/final-answer authority. Sky remains single-agent by default.

Each child has a stable in-process `task_id`, its own transcript snapshot, sticky concrete model, cancellation token and structured result. Only the structured hand-back is merged into the parent conversation, so child search/tool chatter does not inflate the Leader transcript. Built-in foreground planning remains intentionally bounded; shared read-only native-tool execution is serialised while model inference can run in parallel. Third-party/MCP authority is still not inherited by ordinary child agents. A writer child is a separate trust mode: it receives a private Git worktree and a private NativeToolRuntime rooted in that worktree, so multiple candidate writers can run without concurrently mutating the parent checkout.

The private control-plane methods `_kiro/agent/list`, `_kiro/agent/send`, `_kiro/agent/cancel` and `_kiro/agent/resume` remain internal runtime/control-plane contracts rather than normal management-UI concepts. Continuation preserves child context and the pinned model while the BYO process/session runtime remains alive; after a process restart stale child projections are marked paused instead of being falsely resumed.

### Agent Team, isolated writers and ACP workers

An `AgentTeam` is a bounded coordination layer over the existing `AgentTask` runtime rather than a second execution engine. It owns a Leader id, member roster, shared task board, limited team messages and explicit `max_agents` / `max_parallel` / `max_depth` budgets. `_kiro/team/spawn` may add a read-only worker/specialist or an isolated writer between parent turns; `_kiro/team/send`, `_kiro/team/board` and `_kiro/team/recycle` coordinate and reclaim terminal members. Nested spawn is bounded and the built-in Tiga policies do not recursively create unbounded swarms.

Writer workspaces are fail-closed Git worktrees. `_kiro/workspace/compare` exposes independent candidate diffs/check results and `_kiro/workspace/apply` applies one explicitly selected candidate only when the parent repository is clean and `git apply --check` succeeds. There is no automatic conflict resolution, implicit merge or implicit commit. Workspace metadata is runtime state under `data/agent_workspaces` and is excluded from release/source archives.

External ACP Agents can be registered locally in `config/acp_agents.json` (see `config/acp_agents.example.json`) and selected as a child backend. They run through the same `AgentTask` / `AgentResult` projection as raw-model children. The current boundary is intentionally conservative: external ACP Agents are read-only, permission requests are denied by default, and an external ACP backend cannot be promoted to a writer merely by changing a role field. This makes “Agent as Worker” available without silently transferring the parent's filesystem/tool authority.

### ACP host compatibility and conformance

Generic ACP-v1 semantics and KiroCrew host metadata are separated by `AcpHostAdapter` / `KiroCrewHostAdapter`. Generic clients do not inherit KiroCrew `_meta.kiro` model/effort/thinking semantics, while KiroCrew retains its explicit adapter. `scripts/acp_conformance.py` provides a process-level initialize/session smoke harness and `src/kiro_byo/acp_conformance.py` publishes the standard-v1 and Kiro-extension method surfaces. Named generic/KiroCrew/Zed/JetBrains/Qwen Code profiles are protocol-level profiles only; they are **not** a claim that every proprietary client/version was physically certified in this build environment. Real-client acceptance should still run the harness against the exact installed client/host version.

### Durable Run / Checkpoint foundation

Every real provider-backed prompt now creates a durable `Run` containing one restartable `Step` and a concrete `Attempt`. The runtime checkpoints the turn-ready boundary, completed Subagent groups, tool batches and terminal outcome. A checkpoint stores control-plane metadata, Agent identities and a pointer to the canonical session-event sequence; it does **not** duplicate the user/assistant transcript, tool payloads or credentials.

If the process disappears while a Run is still non-terminal, the next `session/load` or `session/resume` classifies its recovery state from the persisted checkpoints. Clean/read-only interruptions can now be explicitly recovered through `_kiro/run/retry`, `_kiro/run/resume` or `_kiro/run/replan`. Recovery reconstructs the original prompt from the canonical persisted `user/message` event, rewinds the active transcript to the Run's saved `history_start`, closes any orphaned Step/Attempt as `interrupted`, and starts a fresh recovery Step/Attempt on the **same Run id**. Type-owned `replan` also clears the old sticky route pin so Tiga performs a fresh route; concrete-model selections remain pinned and therefore use retry/resume rather than pretending to replan.

Potentially mutating tool batches are checkpointed **before** execution. If the process disappears after that admission boundary without a trustworthy terminal result, restart classification becomes `unknown_side_effect` and replay is rejected until the real-world side effect is verified. This makes durable recovery fail-closed instead of blindly repeating writes. `_kiro/run/list` and `_kiro/run/get` expose the ledger and recovery eligibility, while `_meta.kiro.durableRecovery` exposes safe recoverable Run ids to the host without leaking prompt/transcript content. BYO still does not implement its own cron/webhook/background scheduler; KiroCrew remains responsible for deciding when a long task runs, while BYO is responsible for preserving and safely recovering the execution state.

### Tiga adaptive routing

Open **Kiro BYO -> Modes** to inspect the four Tiga Types. The router remains deterministic and provider-neutral: capability/context/tool gates run first, Policy Pack goals define the Type character, and recent runtime evidence only nudges eligible-model ranking. Runtime evidence is recency-weighted and bounded; low-sample models keep most of their static prior. The page shows Policy-Pack execution characteristics, real recent task-shape evidence and the selected Type's latest 60-hour model-use distribution, while intentionally offering no sliders or weight mutation API.

The adaptive layer currently learns recent latency and reliability by coarse task profile (`Code`, `Vision`, `Tool`, `Long Context`, `Research`, `Casual`). It does not claim to infer model intelligence from success rate, and it does not convert token counts into fake cost estimates. A future trustworthy price/quality signal can be added as another evidence source without changing the Type/Provider boundary.

### Reasoning language

Open **Kiro BYO -> Settings -> 推理与语言** to choose the presentation language used for user-visible Thinking/reasoning text. The default is **Auto**, which follows the current user conversation rather than the application UI language. Manual choices include Simplified/Traditional Chinese, English, Japanese and Korean; the config accepts other valid locale tags for forward compatibility.

The policy is deliberately terminology-aware rather than a machine-translation layer. Protocol/API/model/product names, acronyms, code symbols, parameter names, file paths, commands and mathematical notation stay in their natural/original form whenever that is more precise. It is also deliberately independent of deep-thinking Auto/On/Off and reasoning effort. Providers that expose no visible reasoning remain unchanged, and Kiro BYO does not synthesize or translate hidden/private chain-of-thought.

### Management status surface

The App exposes one backend-owned overview snapshot that combines Integration,
Provider/model discovery, credential-store identity, runtime session capabilities,
native-tool inventory and metadata-only turn diagnostics. Health/alert policy is
computed in the Python backend and rendered by the UI; the React page does not
independently guess whether the backend is ready. No API key, prompt, response
body or tool arguments/results are included in the snapshot.

On desktop the Kiro BYO sidebar keeps the compact runtime summary visible while
Providers, Models, Modes, Accounts, Records and Settings are open. Clicking the Kiro BYO header
returns to a compact single-screen Overview; normal desktop-height Overview does
not require a page scrollbar, while genuinely long data regions use local scrolling.

### Session event log

0.1.3 persists Kiro BYO backend conversation state as a typed append-only event
stream. `Session.messages`, sticky routing pins and provider continuation state are
runtime projections of canonical events; schema-v3 session files therefore avoid a
second durable routing/transcript truth. Rollback, steering, compaction and
final-review replacement remain reconstructable instead of rewriting history.
Schema-v1/v2 records are accepted and migrated in memory, then written as schema-v3
on the next save. See `SESSION_EVENT_MODEL.md` for the event vocabulary and migration
rules.

### Runtime trace and Records

Kiro BYO keeps a bounded **runtime event stream** beside the metadata-only turn telemetry.
The event stream follows ACP ingress/egress, provider rounds, streaming deltas, deferred
tool search, native/MCP tool execution and turn completion, which makes the harness
inspectable without coupling observability to a specific Provider. The normal App surface
is **Records**: it presents request outcomes, token/cache usage, routing evidence and
recovery in readable cards/tables; raw event payloads are hidden behind per-record details.

The `KIRO_BYO_RUNTIME_TRACE` environment variable controls content retention:

```text
off       no runtime event ledger
metadata  default; event/routing facts and payload shapes only
full      local debugging; includes prompt/response/tool content
```

`full` mode recursively redacts credential-shaped fields such as API keys,
Authorization headers, tokens, cookies and passwords before writing JSONL, but it
can still contain user/workspace content and should be treated as sensitive local
debug data. The App's **Records** surface loads recent runtime events only when that page is opened and exposes raw event data only on demand. Runtime trace data is never shipped in the release archive.

## Crew 0.10 integration architecture

Only **KiroCrew 0.10 source-compatible builds** are supported; the audited baseline is
`v0.10.0-insider.4` (`e9bfada`). KiroCrew 0.9 is no longer installable through
this release. The semantic `adaptive10` transformer patches inspected upstream
ACP/runtime/dashboard seams and creates one BYO-owned harness module. It retains
transactional preflight, reversible installation and fail-closed startup repair.

The 0.10 transformer internally shares several ACP transformation helpers that
originated in earlier versions, but no Crew 0.9 adapter, CI job or installer path
is shipped. Saved Agent authorization remains owned by Crew, and BYO receives only
live per-session permissions.

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
Unified Harness parity. The audited Crew 0.10 seams derive this boundary after
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
- **Glitter Type** — long-task multi-model collaboration. A strong-tier lead strictly owns planning, changes and the final answer; each pure gathering subtask can be re-routed to the best eligible fast-tier worker (same-provider affinity is only a preference), and final review stays in the strong tier. If no capable strong leader exists, Glitter fails closed instead of silently downgrading.

The built-in Tiga orchestration ceilings are intentionally distinct: Sky / Multi / Power / Glitter allow at most **1 / 2 / 3 / 4** parallel read-only Workers. The ceiling is not a target: narrow or mutation-heavy work still stays smaller, and the Leader remains the single writer.

These four names are no longer hard-coded router branches. They are data in the
**Tiga Policy Pack**. The core Execution Planner consumes a generic Policy Pack
schema, so other modes (for example Dyna, Gaia, Cosmos, or a user-defined work
profile) can provide different Types without changing router code. In the UI the user sees **Mode / Type**; “Policy Pack” is the internal extension contract. User packs
are JSON files under `<KIRO_BYO_HOME>/policy_packs/` and are re-read on the next
config/UI access; no process restart is required. A custom file may also replace a
built-in pack id intentionally. See `POLICY_PACKS.md`.

Policy Pack v2 deliberately splits `semantics` from `execution`. KiroCrew/UI and the assistant may consume the semantic projection when the user asks what a Type means; the Turn Engine compiles only the execution half into `ExecutionIntent` and therefore has no dependency on the product-facing Tiga Type names. There is still no model/tool API that can switch Type: user selection remains authoritative in KiroCrew.

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
image input, **required and currently available** tool use, or context capacity), then
scores the eligible pool using task complexity, reasoning effort, code/tool pressure
and model routing hints. Tool visibility is tracked separately from current-turn tool
intent, so an Agent merely having tools configured does not force casual turns onto a
tool-capable model. Providers may supply explicit `routing.quality`, `routing.speed` and `routing.economy` values plus a many-to-many `routing.tiers` role list. A model can therefore participate in several pools (for example `strong` + `balanced`, or `strong` + `fast`), and every Tiga Type remains free to use one or multiple eligible models according to its execution policy. Each routing fact also carries source/confidence metadata. Authority is local operator override > live provider discovery > verified curated profile > historical cache > generic model-name inference; name inference is low-confidence and remains a backward-compatible last resort. The selected model, ordered candidates, scores, signals and safe first-request fallback are recorded in Runtime Trace. Provider cards therefore do not have a separate "default model" control.

0.1.3 keeps an **OpenAI-compatible** fallback transport, but verified official
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

## Usage and records

When the provider reports token usage, prompt responses preserve input/output,
reasoning tokens and cache signals. Cache **read** and cache **write/creation** tokens
are tracked separately when the provider exposes both (for example Anthropic-style
prompt caching); an unreported cache field remains unknown rather than being shown as
zero. When the real model context window is known, Kiro BYO emits
`usage_update {used,size}` so Crew can display a real context meter. No fake context
window is emitted for unknown models.

Turn telemetry is committed before the ACP prompt result returns. Records distinguish
success, cancellation and typed failures (`transport`, provider/runtime and control-plane
categories), record bounded transport retry/fallback counts, and aggregate recent Provider/model
health without storing prompt/reply/tool bodies. The lower-level runtime trace also records incoming
`session/cancel` source and elapsed time, making client-side cancellation boundaries observable.

Premature HTTP/SSE EOF and connection failures are normalized at the provider boundary. A
pristine transport failure can receive one bounded retry; failures after visible output do not
replay automatically. Failed read-only turns roll back to their pre-turn transcript boundary,
while a turn that admitted a potentially mutating tool preserves the relevant transcript and
adds a recovery marker so the next turn verifies uncertain side effects before retrying. A
single upstream/runtime failure therefore must not require creating a new ACP conversation.

## Verification

Run the full source suite:

```powershell
$env:PYTHONPATH="src"
python -m pytest -q
```

Run the no-write Crew semantic verifier against an installed Crew package:

```powershell
$env:PYTHONPATH="src"
python scripts\verify_crew10_integration.py --crew-package "C:\Users\<you>\AppData\Local\Programs\KiroCrew\resources\backend-dist\kirocrew-backend\Lib\site-packages\kiro_crew"
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
