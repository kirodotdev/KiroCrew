## LLM Provider Abstraction

KiroCrew drives a single LLM backend: `kiro-cli` over ACP. The `LLMProvider`
interface is retained as a thin seam (consumers depend only on the ABC), but
there is exactly one concrete provider — `agent.provider` is fixed to `acp`.

### Architecture

```
┌─────────────────────────────────────────────┐
│  Consumers (handler, gateway, cli, session) │
│  Use LLMProvider interface only             │
└──────────────────┬──────────────────────────┘
                   │
         ┌─────────┴─────────┐
         │   LLMProvider ABC  │
         │   providers/base   │
         └─────────┬─────────┘
                   │
            ┌──────┴──────┐
            │ AcpProvider │
            │ acp.py      │
            │ kiro-cli    │
            └─────────────┘
```

**Note:** the removed Bedrock provider and the removed standalone provider were
**deleted** during de-Amazoning, along with their config fields and the
multi-provider dispatch factory. `agent.provider` stays `acp`. `AcpProvider` can
drive a registry adapter (`claude-agent-acp`, `codex-acp`, …) selected at
`agent.acp_backend`. There is no second `LLMProvider` and no provider selector.
See [`../features/claude-code-provider.md`](../features/claude-code-provider.md).

### LLMProvider ABC (`providers/base.py`)

```python
class LLMProvider(ABC):
    async def start() -> None
    async def shutdown() -> None
    async def stream(message: str) -> AsyncIterator[LLMEvent]
    async def approve_tool(request_id) -> None
    async def reject_tool(request_id) -> None
    def context_usage_pct() -> float
    # Optional (have defaults):
    @property
    def backend() -> str | None
    def available_models() -> list[dict[str, str]]
    @property
    def supports_steer() -> bool
    def has_active_turn() -> bool
    async def steer(message: str) -> bool
    def rate_limit_payload() -> dict | None
    async def stream_command(command: str) -> AsyncIterator[LLMEvent]
    async def compact(context: str = "") -> None
    async def wait_for_compaction(timeout: float = COMPACT_WAIT_TIMEOUT_SECS) -> dict
    async def cancel(*, wait_ack_timeout: float = 0.0) -> CancelOutcome
    def is_alive() -> bool
    def touch_activity() -> None
```

### LLMEvent (`providers/base.py`)

Provider-agnostic event dataclass (aliased from `AcpEvent`):

| Kind | Description |
|------|-------------|
| `text_chunk` | Text output from agent |
| `thinking_chunk` | Extended thinking (Claude 3.7+) |
| `tool_call` | Tool invocation |
| `tool_result` | Tool output |
| `permission_request` | Tool approval request (ACP only) |
| `complete` | End of turn |
| `compaction_status` | Compaction result |
| `clear_status` | Clear display |
| `agent_switched` | Agent mode changed |
| `mcp_oauth_request` | MCP server needs OAuth (has `server_name`, `oauth_url`) |
| `mcp_server_initialized` | MCP server ready after OAuth (has `server_name`) |
| `mcp_server_init_failure` | MCP server OAuth/init failed (has `server_name`, `text`) |

### ACP providers (`providers/acp.py`)

The public provider family speaks JSON-RPC 2.0 over stdio. `AcpProvider` is the
first-class Kiro/KAS path; `SpecAdapterAcpProvider` is the additive
public-spec-adapter path.

**Backend selection:** `AcpProvider`/`AcpClient` take an `acp_backend`
parameter (`""` → kiro-cli; `"claude"` / `"codex"` / `"goose"` / `"opencode"` /
`"pi"` / `"kas"`). OpenCode and pi persist those ids; the registry spelling
`pi-acp` canonicalises onto `pi`.
The public factory still exposes one `LLMProvider`; `agent.acp_backend` selects
which admitted adapter that provider drives. Described registry adapters remain
unpersistable until they join the selectable set. Binary-resolution and
config-isolation details live in [`acp-client.md`](acp-client.md). Do not re-add a second
`agent.provider` value, an API-key path, or a provider selector.
`DefaultProviderRegistry.create_factory` returns the core factory object
directly when Kiro is configured. For an opted-in adapter, it chooses
`SpecAdapterAcpProvider` for a public-spec dialect and `AcpProvider` for the
Kiro family through an exhaustive dialect mapping; a future unmapped dialect
refuses rather than inheriting the Kiro provider class. The core
`create_provider_factory()` is an unconditional direct Kiro-only body and does
not call the adapter-factory helper;
the registry owns backend validation, provider-class selection, and capability
resolution in a separate cached factory. The direct Kiro factory carries a
sidecar builder that the session boundary consults only when a dedicated child's
live parent predates a Settings switch; the factory's ordinary call path does
not become an adapter dispatcher merely to preserve that crossover. The adapter subclass declares
`SpecAdapterAcpClient`, so admission adds no conditional, awaited step, or
failure mode to Kiro startup (H13). Knowledge workers read backend, sandbox, and
the ungated-tools opt-out from one pool-start config snapshot, and every
`AcpWorker` in that pool receives that same snapshot rather than rereading
Settings during its own start. A concurrent Settings save therefore cannot
produce a pool split across backend or security settings; directly constructed
workers still take one lazy snapshot at start. KAS workers enter through
`AcpProvider`/`AcpRuntime`; Kiro keeps its first-class direct client, and
public-spec adapters use `SpecAdapterAcpClient` admission.
Session and messaging consumers read `backend`, model advertisement, and steer
support through the `LLMProvider` defaults rather than probing concrete clients
(H14).

**Key APIs:**
- `start()` → `AcpClient.ensure_ready()` (spawns process, handshake, session/new)
- `stream()` → maps events from `stream_events()`
- `stream_command()` → native slash command execution
- `approve_tool()`/`reject_tool()` → JSON-RPC response
- `context_usage_pct()` → reads `last_prompt_stats.context_pct`
- `context_window_tokens()` → reads `last_prompt_stats.context_window_tokens` (the real served window from `usage_update.size`, 0 if unknown). Used by the dashboard token text instead of re-deriving the window from the model id. A mid-session `set_model` (live switch on both `AcpClient` and `AcpSessionHandle`) rebases these stats via `AcpPromptStats.rebase_to_window`: the window is re-derived from `model_registry.model_window` (0 on a registry miss), `context_used_tokens` is kept, `context_pct` is recomputed and clamped, and `context_tokens_from_usage` is cleared so the next metadata `contextUsagePercentage` can backfill against the NEW model instead of being gated forever by the old model's `usage_update`. The dashboard model-switch endpoint then broadcasts one `context_usage` WS event with `reset: true` (both live-switch and session-reset paths, single and bulk), which lets the frontend reducer replace or delete its stored per-slot token counts — per-turn events without `reset` never delete. The post-compaction pct-0 broadcast carries the same flag.
- `rate_limit_payload()` → `last_prompt_stats.rate_limit.to_payload()`, or `None` when the harness reports no plan quota (the ABC default, which is most of them). A capability on the ABC with a safe default rather than a `hasattr` probe at the call site (H8): `_context_usage_payload` gates on `isinstance(client, LLMProvider)` and attaches the dict to the existing `context_usage` WS frame under `rate_limit`. The KEY'S PRESENCE is the frontend's "this harness has a quota" signal, so an empty reading is omitted rather than sent as `{}`; a `reset` on the same frame does not clear it (compaction changes the transcript, not the account). The dashboard renders it as a section of the context popover. One known gap: the reading is held only in the live provider's stats, so a page reload shows no quota row until the next turn's frame restores it — the slot-detail context seed does not carry it.
- `compact()` → sends `/compact` via `send_command()`
- `cancel()` → sends `session/cancel` notification
- `supports_effort()` / `change_effort(level)` / `clear_effort()` → reasoning-effort control (see below)
- `is_alive()` → `AcpClient.is_responsive()` (600s stale threshold)
- `is_process_alive()` → OS-level process check

**Reasoning effort** (Opus/Sonnet/Fable **and GPT-5.x** — shared vocabulary in `effort.py`: levels `low|medium|high|xhigh|max`, capability via `model_supports_effort`, resolution via `resolve_effort_for_model` with priority slot-override > workspace default > None). Capability is a conservative allowlist of known-capable families (`opus`/`sonnet`/`fable`/`gpt`, minus a hard `haiku` exclusion), verified against kiro-cli 2.12/2.13 over ACP — kiro rejects `/effort` on the other third-party models (deepseek/minimax/glm/qwen/auto) with "Effort configuration is currently not available on <model>". A new model family lands as unsupported until confirmed (safe default: the slider hides). Applied via a workspace `cli.json` overlay at `<work_dir>/.kiro/settings/cli.json` → `chat.modelDefaults.<model>.<key>.effort`, written before every spawn (`_write_cli_overlay`) and recovered on init (`_read_cli_overlay`) for server-restart resilience. The `<key>` sub-object is **family-specific** (`effort_settings_key`): `output_config` for Claude models, `reasoning` for GPT models — kiro silently ignores the wrong key, so a mismatched shape would survive a live push but drop on respawn. `_write_cli_overlay` removes stale effort from the other family key while preserving unrelated settings; `_clear_cli_overlay_effort`/`_read_cli_overlay` sweep both keys. Live change pushes `/effort` with the TuiCommand args form (`send_command(args={"level": …})`). The factory threads `reasoning_effort_override` → `effort_per_model[current_model]`; when a valid requested effort cannot be threaded (the resolved model is empty or not effort-capable) the factory's gate logs one warning naming the level, the session, and the resolved model (or `auto` when unresolved, matching the spawn-side `effort_dropped` verdict) — the single drop authority reporting its own decision, covering every surface (spawn, dashboard slot, cron) that funnels through it, an explicit `reasoning_effort_override` always warns (a caller's own dropped request is the event the gate exists to surface), while a drop sourced only from the config default (`agent.reasoning_effort`) is deduped once per (model, level) for the factory's lifetime so one static configuration fact does not repeat on every construction. A `reasoning_effort_override` also bypasses the warm pool (`bypass_effort`): a pre-warmed provider was built without the override and post-claim fixups never touch effort, so the override must reach a fresh factory call to be delivered at all. The dashboard handler routes through `change_effort`/`clear_effort` and only resets the session when there is no live provider. Non-effort-capable models persist the slot value without a live apply or reset.

Codex advertises picker rows as `<base-model>[<effort>]`, while its session
config accepts those components separately: `model=<base-model>` and
`reasoning_effort=<effort>`. The exact composite row remains the persisted slot
model so the picker survives a gateway restart. A live single-slot pick applies
the base model and then its encoded effort; the bulk/reset path persists the
same effort before the next cold start. On cold start, `AcpClient` strips only
the wire model value, keeps the composite `_model` key for effort resolution,
and records the stripped value as the served `_resolved_model_id`. An explicit
per-slot effort override remains newer than the model row and wins when a
provider is reconstructed.

**MCP Tool Search** (kiro backend only — see https://kiro.dev/docs/cli/mcp/tool-search/): loads MCP tool specs on demand ("search-and-call") instead of sending every tool definition each turn, keeping the context window clear when many MCP servers are configured. Gated by the `agent.tool_search` config toggle (default **on**; auto-surfaces as a Settings toggle since the schema is generated from the dataclass).
- Applied via the **same** workspace `cli.json` overlay used for effort (`<work_dir>/.kiro/settings/cli.json`), written deterministically before every spawn and on each restart by `_write_tool_search_overlay` (called from `AcpProvider.__init__` and `start()`). When enabled it writes the flat keys `toolSearch.enabled=true` plus `toolSearch.minPct`/`toolSearch.minTokens`, taken from `agent.tool_search_min_pct` / `agent.tool_search_min_tokens` (defaults `5` / `50000`, mirroring kiro-cli's own thresholds; clamped to 0-100 and >= 0, non-numeric falls back to the default); when disabled it writes `toolSearch.enabled=false` and drops both thresholds.
- **Why the thresholds are not forced to 0:** deferral costs a round-trip — a deferred tool's spec is absent from the model's tool list, so the first direct call fails with `A tool with the name '<name>' does not exist` and has to be recovered with `tool_search`. That only pays once the specs are genuinely large, which is what the thresholds express (kiro-cli defers when EITHER is exceeded). An earlier build hard-coded both to `0`, imposing the round-trip on every install including ones far below the threshold. Setting both to `0` still restores unconditional deferral for operators who want it. The thresholds are written **explicitly** rather than omitted, so a machine carrying the old forced zeros is actually migrated instead of silently keeping them.
- Writing both `true` and `false` makes the KiroCrew toggle authoritative over any value in the user's global `~/.kiro/settings/cli.json`. The write is merge-safe with the effort `chat.modelDefaults` keys in the same file.
- **claude backend** — no-op. Tool Search is a kiro-cli feature; `_apply_tool_search_overlay` returns early for the claude backend and when no toggle value was threaded in (`tool_search is None`).

- **Resume guard:** `session/load` (resume) is only attempted when the prior session transcript exists on disk (`~/.kiro/sessions/cli/<sid>.json`). A stale persisted sid with no transcript falls back to `session/new`, preventing a fresh conversation from replaying old turns (which inflated base context).
- **Working dir:** `AcpProvider.cwd` overrides the `LLMProvider` ABC default so `session_map` persists the real workspace path. AcpProvider's work_dir lives on the inner client (`_client._work_dir`), so the prior `getattr(provider, "_work_dir", "")` persisted `""` for all ACP sessions — `provider.cwd` fixes resume-cwd-override.

### Backend registry (`acp/backends.py`)

ACP adapters selected at `agent.acp_backend` are a shipped goal. `agent.provider`
stays fixed to `acp`; there is no API-key path and no provider selector. An
adapter whose tool calls Kiro Crew cannot govern is refused unless the operator
names the opt-out. The conditions that remain, and what is still gone, are in
[`docs/task-specs/2026/08/pluggable-acp-backends/README.md`](../../task-specs/2026/08/pluggable-acp-backends/README.md).

`acp/types.py` owns the backend *vocabulary* — the id constants,
`ACP_BACKENDS_KNOWN` (what the code understands), `ACP_BACKENDS_SELECTABLE` (what
an operator may persist), and the `PROVIDER_LABEL_*` values. `acp/backends.py`
adds the layer above it: one frozen `BackendDescriptor` per backend carrying its
label, `experimental` flag, protocol `Dialect`, tool-gate `Routing`, sign-in
command, credential leaves, process markers, and a capability map.
Synthesized registry descriptors deliberately have no process marker. Registry
metadata is launch/display input, not kill authority. Current PID records carry
the OS process-start identity captured at spawn, which lets the orphan reaper
clean a crashed dynamic adapter even when it is hosted by a generic `node.exe`;
only legacy records without an identity may fall back to code-owned markers.

Registry refreshes publish `<data-home>/acp-registry.json` atomically with
owner-only permissions, so a concurrent config read sees either the prior or
new complete registry and cannot transiently normalize a persisted adapter back
to Kiro. The discovery endpoint refreshes before resolving its active state for
the same reason, and threads that fetched snapshot through active-state
normalization and descriptor serialization when disk publication fails. Because
the cache supplies executable package/argv metadata, it
is write-protected from agent file and shell tools; registry-declared environment
keys that control executable lookup, language-runtime startup hooks, dynamic
loaders, shells, or npm configuration are discarded. A launchable distribution
must carry a concrete semver, a syntactically valid package with that exact
version, and bounded string argv with no NUL. This is checked both while parsing
the cache and again by `RegistryAdapter.is_launchable`; invalid entries remain
unselectable and never become a runner command. Valid uvx metadata remains
discoverable but is unselectable because offline uvx may execute a cache-only,
ephemeral environment without a persistent operator install. Npm adapters
enumerate the inherited shell toolchain first and all augmented manager-path
toolchains after it. Ordinary toolchains query each global npm root, validate the
exact installed manifest and one unambiguous Node entry point, and spawn that
entry through the toolchain's absolute `node` path. Executable manager shims run
directly; shell-only Windows `.cmd` / `.bat` shims are unwrapped to
`npm-cli.js`. Volta toolchains instead verify the exact package and bin
registration metadata, resolve the package-pinned Node image, and execute that
Node binary with the verified package entry point. The generic Volta shim is not
used because workspace state can redirect it. Successful ordinary roots are
cached, while
misses and Volta images remain retryable so a new global install needs no
restart. Startup therefore consumes the package installed by the displayed
`npm install -g <exact-package>` command without consulting npx's cache. Missing,
version-mismatched, non-Node, ambiguous-bin, or traversing installs fail closed.

| Field | Purpose |
|---|---|
| `dialect` | `KIRO` (date `protocolVersion`, `set_mode`, `set_model`, empty `mcpServers`) or `SPEC` (integer version, `mcpServers` in session params). Spec adapters do not share one config surface: Claude uses `set_config_option`; goose uses `session/set_mode` for its permission pin. |
| `routing` | How the backend is made to ask before running a tool: `AGENT_SPEC`, `SEEDED_SETTINGS` (Claude `permissions.defaultMode`; OpenCode project `permission: ask`), `SESSION_CONFIG`, `PERMISSION_REQUEST` (pi: privileged tools arrive as `session/request_permission`; goose: same, but only after the client pins session mode `approve` — goose defaults to `auto`, which auto-approves tools and is withheld from the dashboard), or fail-closed `UNVERIFIED` |
| `permission_config_id` / `permission_config_value` | The ACP v1 session config option and the exact value a `SESSION_CONFIG` backend must accept before its first prompt (codex-acp: `mode` = `read-only`); empty for every other routing |
| `capabilities` | Per-capability `SUPPORTED` / `DEGRADED` / `UNAVAILABLE` / `UNVERIFIED` |

`supports()` answers `False` for `DEGRADED`, `UNAVAILABLE`, and `UNVERIFIED`, so a
code gate never treats partial or unmeasured behavior as working. Disclosure
surfaces read `level()` to preserve the distinction.

The registry factory accepts a per-call `acp_backend` override
(`None` = the factory snapshot, `""` = kiro-cli). Its ordinary Kiro call delegates
to the direct Kiro-only factory without importing or validating adapters; an explicit
foreign override dispatches through the adapter factory even when the snapshot is
Kiro. Dedicated subagent children use this to stay on the live parent harness.
Capability lookups (registry model
ids, tool search) follow the effective backend of that call, not the snapshot. The
warm pool is bypassed when the override differs from `SessionManager.acp_backend`.
Every context/usage producer reads `provider_label(live_provider)` after session
acquisition rather than the invariant `agent.provider="acp"`. That label is what
makes spec-adapter resource/skill injection, Claude-only branding, persistence,
and spend attribution follow the harness that actually served the turn. A long-
lived `ContextBuilder` refreshes its default persona name from that runtime label,
so a backend switch does not retain the previous harness's branding. Spec adapters
also inject the selected custom agent's markdown `file://` resources; kiro-dialect
sessions continue deferring those resources to `--agent` and do not duplicate them.
Spec adapters with backend-owned model ids inherit only an explicit request or a
concrete `agent.model`; they never resolve the Kiro agent JSON model fields, and
`auto` remains the selected adapter's default. The dashboard backend-save
transaction resets global, role, crew-agent, and live-slot namespaced model pins
atomically, reloads the config under the same lock, and verifies that the selected
backend survived registry normalization. If registry state changed during the
save, it conditionally restores the prior backend and persisted model pins and
returns a machine-readable 409 before any session defaults or slot state are
refreshed.

**A descriptor records evidence, not inheritance.** KAS uses the Kiro dialect,
but capabilities not independently measured against KAS are `UNVERIFIED` rather
than copied from kiro-cli. Known absences remain `UNAVAILABLE`; both states stay
fail-closed in code while the Settings page and doctor report why.

Selectability is deliberately NOT in the descriptor. It stays in
`ACP_BACKENDS_SELECTABLE` so there is exactly one answer to "may an operator
persist this value", and a descriptor cannot drift from it. The initial preview
admits only Kiro CLI and KAS. Claude, Codex, goose, OpenCode, pi, and registry-only
adapters remain described and discoverable but cannot be persisted until their
validation evidence admits them to the set. Claude's routing seed can merge into
an existing project settings file, while its current reset path unlinks that whole
file; it stays withheld until cleanup preserves operator-owned state. Codex's
`mode=read-only` blocks writes but does not permission-route passive reads; because
the standard sandbox leaves credential homes readable, that is not enough evidence
for admission. KAS is fully described and is selectable (cli-fronted via `kiro-cli`).
Mid-turn steer is measured and granted (`ACP_BACKENDS_STEER`); session sharing
stays fail-closed until keep-aware teardown lands. Spec-adapter steer degrades to
follow-up.

Call sites outside a backend's own dialect adapter ask `supports(backend, CAP_X)`
or `dialect_of(backend)`; they do not compare ids. The module also exports
`bills_kiro_credits(backend)`, which is a membership question rather than a
capability level — see § "Credit-billing surface" for why the two cannot be
collapsed. The eleven `not is_claude`
inferences that previously meant "kiro" are converted — see
[`acp-client.md`](acp-client.md) § "Backend Selection".

Shipped modules: `acp/codex.py` (paths, resolution ladder, MCP shaping,
model-id translation), `acp/claude.py` (permission-mode probe and
seeding), `acp/goose.py` / `acp/pi.py` (owned resolution ladders;
`PERMISSION_REQUEST` routing; goose pins `session/set_mode` to
`approve` by default because its own `auto` auto-approves tools;
the dashboard offers Auto only when that live session advertised it),
`acp/opencode.py` (owned resolution plus
`SEEDED_SETTINGS` `permission: ask` seed), `acp/tool_gate.py` (routing verdicts
and enforcement), `acp/spec_agent_guard.py` (agent-profile fail-closed guard),
`acp/doctor.py` (doctor rows). The withheld spec adapters that resolve ROUTED
(goose, OpenCode, pi) are ready to start without the ungated-tools opt-out once
they are admitted to the selectable set. Crew MCP is
delivered only when routing is established before `session/new`: OpenCode and pi
receive it; goose does not because its `approve` pin is acknowledged only after
the session exists. Codex remains withheld even though its post-session
`mode=read-only` acknowledgement is enforced on direct integration paths: the
mode does not route passive reads through Kiro Crew's sensitive-path gate.
Adapter resolution honours Windows
`PATHEXT`; native adapters must resolve to a platform-runnable file, while Node
entry scripts may be paired with a supported Node runtime. OpenCode is seeded in
the session `work_dir` the same shape as Claude: write only when nothing is configured, never overwrite
`allow`, never replace malformed/unreadable operator content, never follow a
symlink/junction in the settings path, and never write `~/.config/opencode`.
Claude's seed has the same preservation and no-link rules. Pi may leave delivered
Crew servers inert until `pi-acp` forwards MCP.

### Model list surface (`GET /api/models`)

Every model picker in the dashboard reads one list, through
`useAvailableModels` → `AcpAdapter.fetchAvailableModels` → this endpoint. The
backend branch is checked BEFORE the `kiro-cli --list-models` spawn, because on
another backend that subprocess is both impossible (the binary may be absent) and
wrong (its ids are kiro-namespace and the other backend rejects them). The
advertised list from a live session (`session/new`'s `models`) is the only correct
source there, newest session wins, and lists are never merged across sessions — a
merge would offer ids the active backend rejects.

Three response shapes, and the shape itself is the discriminator:

| Backend | Shape |
|---|---|
| kiro | a **bare JSON array** |
| non-kiro, advertised | `{"models": [...], "backend": "<id>", "serves_auto": <bool>}` |
| non-kiro, nothing advertised yet | 503 with `code: "acp_backend_models_unavailable"`, plus the same `backend` and `serves_auto` |

`backend` and `serves_auto` ride the **failure** as well as the success, because
that 503 is the steady state of an adapter with no live session — it is precisely
when the client has the least information and has to decide what to render. A
degraded answer still identifies the namespace, so the client can refuse a cached
list written for a different one.

**`"auto"` is a kiro-namespace model id, not a protocol concept.** The kiro-agent
family advertises it as a row of its own list; a spec adapter has no such id
(claude-agent-acp advertises `default`, codex-acp advertises `openai.*` ids) and
rejects it at the wire as `-32603`. So `serves_auto` is reported from
`ACP_BACKENDS_AUTO_MODEL` (harness-parity H6) rather than left for the dashboard
to infer from the id: "is `backend` non-empty" reads as a kiro test only because
`ACP_BACKEND_KIRO` is `""`, and it withheld the row from KAS, which speaks kiro's
dialect and does serve the id. The set is kept separate from
`ACP_BACKENDS_ACP_RUNTIME` because running on the shared runtime and serving a
model id are independent claims.

Membership governs only the surfaces that must name a model BEFORE any live list
exists — the picker's cold-start placeholder and its degraded fallbacks. Once a
session has advertised, `resolve_usable_model` / `model_is_unusable` gate `"auto"`
on the advertised set instead, which needs no per-backend knowledge (H12).

Client side (`website/src/providers/adapters/acp.ts`,
`hooks/useAvailableModels.ts`): the flag is remembered in `localStorage` under
`kc.acp.servesAuto.v1`, and **absence resolves to "serves it"** — `agent.acp_backend`
defaults to kiro and kiro is the floor (H1/H4), so a browser that has never seen a
response must paint what it painted before any adapter existed. The one place that
default does *not* apply is the namespace-unavailable branch itself: reaching it
proves the backend is not kiro, so only an explicit `serves_auto: true` in that very
body keeps the row, and a gateway too old to send the field withholds it. A picker
offering an unusable `auto` fails in the direction that costs a turn — it renders
as the only row, so it gets picked, and the failure surfaces as a bare wire error
with no hint that the row was never real. Showing nothing is the honest degraded
state; `withAutoFirst` orders an Auto row first but never invents one.

### Credit-billing surface (`bills_kiro_credits`)

`acp/backends.py::bills_kiro_credits(backend)` answers one question: **does a turn
on this harness draw down the signed-in Kiro account's credit plan?** Membership in
`ACP_BACKENDS_KIRO_CREDITS` (`{kiro, kas}`, harness-parity H6), not a descriptor
lookup — an id with no cached descriptor answers `False` instead of raising, because
both callers are readouts and hiding a number is a correct degraded state where
showing another account's balance is not.

**`CAP_BILLING` is not a substitute and must not be reused as one.** That level says
whether Kiro Crew can READ a cost signal at all; claude-agent-acp sits at DEGRADED
there because it reports a real cumulative dollar figure — from Anthropic's account.
Whose balance moved is a property of the account a harness authenticates to, not of
the wire dialect, which is also why KAS is a member while its billing capability is
UNVERIFIED.

Two consumers, both gated by the gateway so no frontend carries a copy of the set:

| Surface | Behaviour |
|---|---|
| `GET /api/status` → `harness` | `{backend, label, kiro_credits}`, or `null` when the configured backend cannot be read. `label` comes from the descriptor, falling back to the raw id. Owner-only: `ws.py` strips it from the Tier-0 frame alongside `branch` and `commit`. |
| `GET /api/sessions/usage` | Answers `{"usage": {"available": false}}` immediately for a non-member, ahead of the kiro-readiness check, and does **not** schedule the background refresh. The cache is left intact so switching back to Kiro still serves the last good value. |

The endpoint gate is the one that matters for cost: populating the pill can spend a
**billed** `kiro-cli chat --no-interactive --agent kirocrew-lite /usage` turn, on a
30s timer. The pre-existing `available: false` marker does not cover this — it is
set when kiro-cli is absent from the HOST, which is a host-presence test, so an
operator with kiro-cli installed and an adapter selected kept paying for a balance
no turn was drawing down.

The dashboard gates the pill on `harness.kiro_credits !== false` *as well*, which is
not redundant: the usage cache can still hold a Kiro reading for up to one refresh
interval after a harness switch. An **absent or null** block means UNKNOWN — an older
gateway, or an unreadable config — and both sides then behave exactly as they did
before the field existed, per H4 (kiro is the floor) and H12's "unknown means allow".

The top-bar harness segment is a passive readout. Connection, metrics, and usage
already consume the compact row's action budget; adapter navigation lives in the
dedicated preview-gated Developer tab. `ACP_BACKEND_ROUTE`
(`/developer?tab=acp-adapters#acp-adapter`) remains the canonical deep link for
surfaces that have room to navigate. The scroll lives in `AcpBackendCard`, not in
`useSettingHighlight`. That hook
  resolves its target synchronously on its first effect, while the backend rows
  arrive only after `GET /api/acp-backends` answers — a registry refresh — so a
  `highlight=key:agent.acp_backend` link would strip its own param and scroll
  nowhere. The card scrolls on hash arrival once its payload has landed, and only
  then. The selector owns a dedicated preview-gated Developer tab rather than
  sharing the general Config page, so the card keeps that tab occupied with a
  skeleton during the probe and shows a recoverable error for non-403 failures;
  an owner-only 403 still hides the control.
`AcpBackendCard` and the preview-gated Services row share the same React Query
  key and `?probe=1` payload. Selecting an adapter and refetching the card
  therefore updates the Services label from the shared cache instead of leaving
  stale local state behind. Services disables the query entirely while the
  preview flag hides the row, so ordinary dashboard loads do not walk the
  adapter resolvers for an invisible label. The gateway registers a lightweight
  lazy route for `GET /api/acp-backends`; its descriptor handler module is first
  imported when that endpoint is requested, so a preview-disabled gateway does
  not execute adapter discovery code before binding its socket.

Dashboard slash-command classification follows the live slot backend when one
exists and otherwise reads `SessionManager.acp_backend`, the refreshed config
snapshot already held by the session store. It does not reload and revalidate
`config.json` synchronously on each turn merely to decide whether a slash command
belongs to the Claude adapter.

The local session-analytics usage endpoint falls back to Kiro Crew's own token
shards when a non-Kiro backend has no kiro-cli sessions directory. Its cold-cache
probe checks both directory presence and the configured backend off the event
loop; loading and validating config does not perform registry network I/O.

### Config (`config/loader.py`)

```json
{
  "agent": {
    "provider": "acp",
    "model": "auto"
  }
}
```

- `agent.provider` is fixed to `"acp"` (enum `["acp"]`); there is no provider to choose.
- `agent.acp_backend` selects WHICH ACP backend the `acp` provider drives.
- A dashboard change to `agent.acp_backend` is owner-only; app-scoped and
  non-owner identities are refused before registry resolution or config I/O.
  Saving Kiro into a legacy config that omits the key is not a backend switch,
  because omission and `ACP_BACKEND_KIRO` are the same effective selection.
  A real dashboard change refreshes the captured provider
  factory and drains prewarmed providers immediately. Existing sessions keep
  their original backend; sessions created after the change use the new one.
  Validated against `ACP_BACKENDS_SELECTABLE`; an unknown or unselectable
  persisted value degrades to Kiro with a logged reason at startup (H3), so the
  refusal lands where a human is looking rather than on the operator's first
  message. Building the import-time JSON schema performs no registry I/O;
  config validation uses the reviewed static selection set, and
  `GET /api/config/schema` resolves the same set off the event loop at request
  time. The dashboard config PATCH also resolves its backend allowlist off the
  event loop so future descriptor-backed admission cannot add synchronous work
  to the request loop.
  The ACP discovery endpoint instead validates and normalizes against its
  fetched in-memory snapshot and uses that same snapshot for the active routing
  verdict, so failed cache publication cannot discard or misreport the adapter.
  The endpoint lists every hand-described backend plus every valid registry
  record, including distribution kinds Kiro Crew cannot launch, while deriving each
  row's `selectable` flag only from the reviewed admission set. A newly
  discovered adapter can therefore appear as described but withheld without
  becoming a valid config value. Backend switches
  clear every persisted backend-namespaced model pin only after a reload through
  `KiroCrewConfig` confirms the requested backend survived validation/normalization;
  a rejected reload restores the old backend and leaves the model choices intact.
- `agent.acp_backend_allow_ungated_tools` (default `false`) is the single named
  opt-out that lets a session start when its tool calls would not reach the
  PreToolUse gate. Goose permission Auto has no dashboard surface and the
  mode-change endpoint refuses it; that harness-owned grant cannot yet inherit
  the governance clamp or expiry.
  See [`security.md`](security.md) § "ACP
  backend tool-gate routing".
- The factory resolves capability once per build, not per session:
  `to_acp_id` model normalisation runs only under `CAP_REGISTRY_MODEL_IDS`, and
  `tool_search` is passed as `None` (write no overlay) rather than `False` (write
  an explicit disable) for a backend that reads no `cli.json`.
- `create_provider_factory()` returns a `Callable` that creates the `AcpProvider`.
  Factory construction resolves the configured backend descriptor immediately,
  so a programmatic unknown value fails before any session starts; persisted
  unknown or unselectable values are normalized to kiro-cli during config load.

An agent spec's model is consumed by kiro-cli before Kiro Crew reaches
`session/new`, so the live-session entitlement guard cannot diagnose a wrong
wire spelling at spawn time. Agent create/update validate a pin before
persisting it: they reuse the role-model validator for advertised ids and
`model_registry.acp_id_correction` for the offline positive case where the
registry recognizes a non-ACP spelling and can name its ACP id. Unknown ids are
allowed because they may be valid regional or newly released ids; empty and
`auto` continue to defer. Doctor applies the same correction audit to every
discoverable user- and project-scoped spec.

A public-spec adapter has no general `session/set_mode` equivalent, so it
refuses an agent spec that demonstrably withholds shell access rather than
silently widening its tools. Resolution follows the shared declared-name and
project-shadow discovery rules. If an exact-name project file is present but
the hardened reader omits it as unreadable or invalid, that project shadow is
still treated as unverifiable and refused; it never falls through to a readable,
same-name global spec whose broader permissions would mask the project ceiling.
The exemption for Kiro Crew-authored agents is tied to an owned file in the
global agent directory, not to the `kirocrew` name; a project shadow of the
default name is still checked.

### MCP Server Registration

MCP servers are passed directly in the `session/new` params. The two managed
servers (`kirocrew-core`, `kirocrew-cron` — see `agent.py:_MANAGED_MCP_SERVERS`)
are always present; user-configured servers from the agent config are merged in.

### SessionManager (`session.py`)

- Provider-agnostic via factory (one provider: kiro-cli `AcpProvider`)
- Calls `repair_agent_configs()` on gateway startup and periodically
- context_info() reports model/agent
- Resume: calls `set_resume_session_id()` before `start()`

### Subagent Approval Mode Inheritance (`subagent.py`)

Subagents inherit the global `approval_mode=auto` config as a final fallback when:
1. No parent session key exists (spawned independently), OR
2. Parent session key exists but the session is no longer in the store (garbage-collected)

If the parent session is alive but returned no policy, deny-by-default applies — the session is intentionally non-auto. This ensures subagents spawned from dashboard sessions still get auto-approval even if the parent session is GC'd before the subagent executes.

### Automatic recovery

Provider-level recovery mechanisms that fire automatically without user intervention:

**Interactive transient-5xx retry** (a270bd1f; post-token recovery c6fe60a): The interactive dashboard/Slack `chat_runner` stream loop retries a transient backend 5xx (InternalServerError / DispatchFailure / ConnectionReset, JSON-RPC `-32603`) through the shared `llm_helpers` transient classifier + backoff, **without** resetting the still-alive session. Auth/validation errors are excluded (fail-fast); on retry-budget exhaustion a clean error surfaces on a still-resumable session. This extends the unattended `stream_and_collect` retry path (previously deferred for the interactive loop) to interactive callers.

A transient 5xx that arrives *after* the turn already emitted output (the `_turn_emitted` guard is set once any assistant token streams or a tool call fires) no longer drops the turn. Instead it **RECOVERS ONCE**: the streamed partial is preserved as a finalized assistant message, a brief recovery notice is appended, and a *continue* instruction (not the original prompt) is re-queued onto the SAME live ACP session — which still holds the interrupted turn's context (original prompt, streamed partial, and any completed tool results) — so the model resumes from where it stopped rather than restarting. The recovery is one-shot per genuine user turn: the allowance is consumed only when a recovery is actually enqueued and is refreshed at the start of the next real user turn, never on the synthetic recovery turn, so a repeated post-token 5xx during recovery surfaces a clean error instead of looping. When Stop is active or the turn is nested (`_prompt_depth != 0`) the partial + notice are still shown but nothing is re-queued (the allowance is left unconsumed). This recovery **also applies to turns that already fired a tool call** — an ACCEPTED TRADEOFF (owner decision), rather than failing fast: a mid-stream 5xx is rare, and the continue instruction tells the model to resume and not re-run tools that already completed. A residual double-execution risk remains only for a side-effecting/destructive tool that was still *in flight* when the 5xx hit; the owner accepts that narrow risk in favor of recovering the turn.

**Compaction-failure notice backoff** (dashboard-chat; `dashboard/chat_utils.py:_broadcast_compaction_result`): Per-turn compaction failures no longer spam the chat. Per slot, `_compaction_fail_streak` counts consecutive failures and the first `_COMPACTION_NOTICE_SHOW_FIRST_N` (=2) are shown verbatim ("❌ Compaction failed: …"); further failures within the `_COMPACTION_FAIL_COOLDOWN_SECS` (60s) `_compaction_fail_cooldown_until` window are suppressed, and when the cooldown elapses a single collapsed "failed Nx in a row … Consider `/compact` manually" message is shown with `/compact` guidance. A `completed` status resets the streak/cooldown. `acp/client.py:_handle_compaction_status` logs the raw failed-compaction notification params at WARNING (kiro-cli carries no dedicated error field on failure). This is a UX/spam guard only — the underlying compaction still runs every turn on kiro-cli's schedule — and is distinct from SessionManager's proactive auto-compact cooldown.

**Compaction resets — then accurately re-reports — the context meter**: a `completed` `_kiro.dev/compaction/status` drops the stale token stats at the provider chokepoints — `AcpClient._handle_compaction_status` (every dispatch loop plus `wait_for_compaction`) and the mirrored sites in `AcpSessionHandle` (prompt dispatch loop and its `wait_for_compaction` queue-drain path) — via `AcpPromptStats.reset_after_compaction()`: `context_used_tokens`/`context_pct` zero out and `context_tokens_from_usage` clears (so fresh metadata can re-derive instead of being gated by the pre-compaction `usage_update`), while `context_window_tokens` is kept (the model did not change, so the served window still holds). kiro-cli then emits a fresh `_kiro.dev/metadata` with the real post-compaction `contextUsagePercentage` about a second after the completed status (live-probe confirmed), so `wait_for_compaction` grace-drains up to `_POST_COMPACTION_METADATA_GRACE_SECS` (5s) for it on `AcpClient`, `AcpSessionHandle`, and `AcpProvider`'s cached mid-turn result path (which delegates to the inner client via the `AcpSessionProvider` pass-through); the drain only ends on a metadata frame actually carrying a `contextUsagePercentage` (a credits-only frame is consumed but does not end it), re-queues non-metadata frames before any poison sentinel, and lets process death (`AcpError`) propagate; `_backfill_context_window` prefers the **kept served window** over the model registry when deriving tokens from that percentage, since the served size can differ from the static entry (e.g. opus served at [1m] vs a 200K registry row). The dashboard's manual `/compact` path then broadcasts the REAL post-compaction numbers when the drain captured them, and only falls back to `context_usage {pct: 0, reset: true}` (the same contract as the threshold auto-compact callback and the in-turn `_broadcast_compaction_result` chokepoint) when no metadata arrived — the meter then self-corrects on the next turn's telemetry. A failed/timed-out compaction leaves the counts untouched and re-sends them as-is. `_context_usage_payload` treats `used == 0` with a known window as "not measured yet" and omits the token fields, so the unconditional end-of-turn broadcast cannot overwrite a reset with a false "0 / W tokens" claim.

### Installation

KiroCrew drives `kiro-cli` over ACP — install it per its own docs, ensure it is
on `PATH`, and run `kiro-cli login`. `kirocrew doctor` reports its status.


## AcpProvider: shared-runtime startup

`AcpProvider.start()` branches on named backend capabilities. Every shared-runtime branch below
enters the same `AcpRuntime.spawn()` cold-start coordinator (default 2 concurrent
spawn+initialize handshakes per gateway loop); admission is backend-neutral, so an
adapted runtime harness neither bypasses the bound nor changes the Kiro path.

- **ACP runtime member (`ACP_BACKENDS_ACP_RUNTIME`: Kiro or KAS)** →
  `_start_kiro_runtime()`. This spawns an
  `AcpRuntime` (carrying the provider's sandbox mode, extra env, and MCP-gateway
  overlay/socket), resumes via `runtime.load_session()` when a prior transcript
  exists or otherwise `runtime.create_session()`, applies the configured model,
  and replaces `self._client` with an `AcpSessionProvider` (which implements the
  same interface as `AcpClient`, so downstream callers are unchanged). Any
  failure after `spawn()` kills the runtime so a half-initialised session never
  leaks an orphaned `kiro-cli`.
- **Public-spec adapter** → admitted `AcpClient.ensure_ready()`.

`AcpProvider.is_session_sharing_eligible` is membership in
`ACP_BACKENDS_SESSION_SHARING` (harness-parity H6), not `not is_claude_backend`:
a capability granted by the absence of one backend is inherited by every backend
added later. It is what `SessionManager.is_session_sharing_eligible()` consults
to decide whether a parent session can host multiplexed subagent sessions. The
invariants governing what an added harness may and may not change are in
[harness-parity.md](harness-parity.md).
