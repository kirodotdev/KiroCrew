## LLM Provider Abstraction

Kiro Crew drives one concrete LLM provider over ACP. `agent.provider` remains
fixed to `acp`; `agent.acp_backend` selects the harness: kiro-cli by default,
KAS, or OpenCode. The `LLMProvider` interface remains the consumer boundary.

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
            │ ACP harness │
            └─────────────┘
```

**Note:** the removed Bedrock provider and the removed standalone provider were
**deleted** during de-Amazoning, along with their config fields and the
multi-provider dispatch factory. The public provider factory selects kiro-cli,
KAS, or OpenCode through the `acp_backend` seam. `ACP_BACKEND_CLAUDE` remains a
dormant seam that an internal companion can re-register; it is not selectable
in the public build.
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

### AcpProvider (`providers/acp.py`)

The sole provider. It speaks JSON-RPC 2.0 over stdio to a selected ACP harness.
Kiro and KAS use the multiplexed `AcpRuntime`; OpenCode and the dormant Claude
adapter use one `AcpClient` process per session.

`AcpProvider`/`AcpClient` accept `acp_backend`: `""` selects kiro-cli, `"kas"`
selects KAS, `"opencode"` selects OpenCode, and the dormant `"claude"` seam is
reserved for a companion build. Binary resolution, protocol differences, and
session semantics live in [`acp-client.md`](acp-client.md).

**Key APIs:**
- `start()` → `AcpClient.ensure_ready()` (spawns process, handshake, session/new)
- `stream()` → maps events from `stream_events()`
- `stream_command()` → native slash command execution
- `approve_tool()`/`reject_tool()` → JSON-RPC response
- `context_usage_pct()` → reads `last_prompt_stats.context_pct`
- `context_window_tokens()` → reads `last_prompt_stats.context_window_tokens` (the real served window from `usage_update.size`, 0 if unknown). Used by the dashboard token text instead of re-deriving the window from the model id. A mid-session `set_model` (live switch on both `AcpClient` and `AcpSessionHandle`) rebases these stats via `AcpPromptStats.rebase_to_window`: the window is re-derived from `model_registry.model_window` (0 on a registry miss), `context_used_tokens` is kept, `context_pct` is recomputed and clamped, and `context_tokens_from_usage` is cleared so the next metadata `contextUsagePercentage` can backfill against the NEW model instead of being gated forever by the old model's `usage_update`. The dashboard model-switch endpoint then broadcasts one `context_usage` WS event with `reset: true` (both live-switch and session-reset paths, single and bulk), which lets the frontend reducer replace or delete its stored per-slot token counts — per-turn events without `reset` never delete. The post-compaction pct-0 broadcast carries the same flag.
- `compact()` → sends `/compact` via `send_command()`
- `cancel()` → sends `session/cancel` notification
- `supports_effort()` / `change_effort(level)` / `clear_effort()` → reasoning-effort control (see below)
- `is_alive()` → `AcpClient.is_responsive()` (600s stale threshold)
- `is_process_alive()` → OS-level process check

**Reasoning effort** (Opus/Sonnet/Fable **and GPT-5.x** — shared vocabulary in `effort.py`: levels `low|medium|high|xhigh|max`, capability via `model_supports_effort`, resolution via `resolve_effort_for_model` with priority slot-override > workspace default > None). Capability is a conservative allowlist of known-capable families (`opus`/`sonnet`/`fable`/`gpt`, minus a hard `haiku` exclusion), verified against kiro-cli 2.12/2.13 over ACP — kiro rejects `/effort` on the other third-party models (deepseek/minimax/glm/qwen/auto) with "Effort configuration is currently not available on <model>". A new model family lands as unsupported until confirmed (safe default: the slider hides). Applied via a workspace `cli.json` overlay at `<work_dir>/.kiro/settings/cli.json` → `chat.modelDefaults.<model>.<key>.effort`, written before every spawn (`_write_cli_overlay`) and recovered on init (`_read_cli_overlay`) for server-restart resilience. The `<key>` sub-object is **family-specific** (`effort_settings_key`): `output_config` for Claude models, `reasoning` for GPT models — kiro silently ignores the wrong key, so a mismatched shape would survive a live push but drop on respawn. `_write_cli_overlay` removes stale effort from the other family key while preserving unrelated settings; `_clear_cli_overlay_effort`/`_read_cli_overlay` sweep both keys. Live change pushes `/effort` with the TuiCommand args form (`send_command(args={"level": …})`). The factory threads `reasoning_effort_override` → `effort_per_model[current_model]`; when a valid requested effort cannot be threaded (the resolved model is empty or not effort-capable) the factory's gate logs one warning naming the level, the session, and the resolved model (or `auto` when unresolved, matching the spawn-side `effort_dropped` verdict) — the single drop authority reporting its own decision, covering every surface (spawn, dashboard slot, cron) that funnels through it, an explicit `reasoning_effort_override` always warns (a caller's own dropped request is the event the gate exists to surface), while a drop sourced only from the config default (`agent.reasoning_effort`) is deduped once per (model, level) for the factory's lifetime so one static configuration fact does not repeat on every construction. A `reasoning_effort_override` also bypasses the warm pool (`bypass_effort`): a pre-warmed provider was built without the override and post-claim fixups never touch effort, so the override must reach a fresh factory call to be delivered at all. The dashboard handler routes through `change_effort`/`clear_effort` and only resets the session when there is no live provider. Non-effort-capable models persist the slot value without a live apply or reset.

OpenCode's ACP config advertises its own effort selector, but Kiro Crew does not
map it yet. `supports_effort`, workspace-overlay writes, and live `/effort`
calls fail closed for OpenCode rather than applying Kiro semantics to it.

**MCP Tool Search** (kiro backend only — see https://kiro.dev/docs/cli/mcp/tool-search/): loads MCP tool specs on demand ("search-and-call") instead of sending every tool definition each turn, keeping the context window clear when many MCP servers are configured. Gated by the `agent.tool_search` config toggle (default **on**; auto-surfaces as a Settings toggle since the schema is generated from the dataclass).
- Applied via the **same** workspace `cli.json` overlay used for effort (`<work_dir>/.kiro/settings/cli.json`), written deterministically before every spawn and on each restart by `_write_tool_search_overlay` (called from `AcpProvider.__init__` and `start()`). When enabled it writes the flat keys `toolSearch.enabled=true` plus `toolSearch.minPct`/`toolSearch.minTokens`, taken from `agent.tool_search_min_pct` / `agent.tool_search_min_tokens` (defaults `5` / `50000`, mirroring kiro-cli's own thresholds; clamped to 0-100 and >= 0, non-numeric falls back to the default); when disabled it writes `toolSearch.enabled=false` and drops both thresholds.
- **Why the thresholds are not forced to 0:** deferral costs a round-trip — a deferred tool's spec is absent from the model's tool list, so the first direct call fails with `A tool with the name '<name>' does not exist` and has to be recovered with `tool_search`. That only pays once the specs are genuinely large, which is what the thresholds express (kiro-cli defers when EITHER is exceeded). An earlier build hard-coded both to `0`, imposing the round-trip on every install including ones far below the threshold. Setting both to `0` still restores unconditional deferral for operators who want it. The thresholds are written **explicitly** rather than omitted, so a machine carrying the old forced zeros is actually migrated instead of silently keeping them.
- Writing both `true` and `false` makes the KiroCrew toggle authoritative over any value in the user's global `~/.kiro/settings/cli.json`. The write is merge-safe with the effort `chat.modelDefaults` keys in the same file.
- **Adapted per-session backends** — no-op. Tool Search is a kiro-cli/KAS setting; `_apply_tool_search_overlay` only runs for explicit `ACP_BACKENDS_ACP_RUNTIME` members.

- **Resume guard:** Kiro `session/load` is attempted only when its transcript exists at `~/.kiro/sessions/cli/<sid>.json`. OpenCode owns a different store, so its resume id is sent directly and no Kiro file or private metadata is consulted.
- **Working dir:** `AcpProvider.cwd` overrides the `LLMProvider` ABC default so `session_map` persists the real workspace path. AcpProvider's work_dir lives on the inner client (`_client._work_dir`), so the prior `getattr(provider, "_work_dir", "")` persisted `""` for all ACP sessions — `provider.cwd` fixes resume-cwd-override.

### Config (`config/loader.py`)

```json
{
  "agent": {
    "provider": "acp",
    "acp_backend": "opencode",
    "model": "auto"
  }
}
```

- `agent.provider` is fixed to `"acp"` (enum `["acp"]`); the harness is selected at `agent.acp_backend`.
- `agent.acp_backend` accepts the registered public values `""`, `"kas"`, and `"opencode"`; an unusable persisted value degrades to Kiro with a logged reason.
- `create_provider_factory()` returns a `Callable` that creates the selected `AcpProvider`.

An agent spec's model is consumed by kiro-cli before Kiro Crew reaches
`session/new`, so the live-session entitlement guard cannot diagnose a wrong
wire spelling at spawn time. Agent create/update validate a pin before
persisting it: they reuse the role-model validator for advertised ids and
`model_registry.acp_id_correction` for the offline positive case where the
registry recognizes a non-ACP spelling and can name its ACP id. Unknown ids are
allowed because they may be valid regional or newly released ids; empty and
`auto` continue to defer. Doctor applies the same correction audit to every
discoverable user- and project-scoped spec.

### MCP Server Registration

Registration is backend-specific:

- **kiro-cli / KAS:** the rendered Kiro agent remains authoritative. The direct
  client adds only shared-gateway broker stubs to `session/new` or
  `session/load`; the shared runtime applies the equivalent session overlay.
- **OpenCode:** the adapter reads the rendered agent through the capped,
  traversal-safe resolver and passes only fully exposed Kiro Crew-managed
  servers in ACP's portable local-server shape. Per-tool-only mounts,
  `disabledTools`, disabled entries, user-installed servers, and app-contributed
  servers are omitted rather than widened. An eligible pooled stub replaces the
  direct managed entry by name.
- **Dormant Claude seam:** the companion-owned registration hook remains the
  authority, with pooled stubs appended as before.

The managed ownership catalog contains `kirocrew-core`, `kirocrew-cron`,
`kirocrew-computer`, and the opt-in `kirocrew-dashboard`; emission still obeys
each rendered agent's mounts and feature gates.

### SessionManager (`session.py`)

- Provider-agnostic via factory (one `AcpProvider`, selected ACP harness)
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

The default backend requires `kiro-cli` and `kiro-cli login`. OpenCode requires
the `opencode` executable plus an authenticated provider (`opencode auth login`);
the resolver accepts `OPENCODE_BIN` when it is not on the daemon's `PATH`.


## AcpProvider: shared-runtime startup

`AcpProvider.start()` branches on the backend. Every shared-runtime branch below
enters the same `AcpRuntime.spawn()` cold-start coordinator (default 2 concurrent
spawn+initialize handshakes per gateway loop); admission is backend-neutral, so an
adapted runtime harness neither bypasses the bound nor changes the Kiro path.

- **AcpRuntime members (`is_acp_runtime_backend` True)** → `_start_kiro_runtime()`. This spawns an
  `AcpRuntime` (carrying the provider's sandbox mode, extra env, and MCP-gateway
  overlay/socket), resumes via `runtime.load_session()` when a prior transcript
  exists or otherwise `runtime.create_session()`, applies the configured model,
  and replaces `self._client` with an `AcpSessionProvider` (which implements the
  same interface as `AcpClient`, so downstream callers are unchanged). Any
  failure after `spawn()` kills the runtime so a half-initialised session never
  leaks an orphaned `kiro-cli`.
- **Adapted per-session backend (Claude or OpenCode)** → `AcpClient.ensure_ready()`.

`AcpProvider.is_session_sharing_eligible` is membership in
`ACP_BACKENDS_SESSION_SHARING` (harness-parity H6), not `not is_claude_backend`:
a capability granted by the absence of one backend is inherited by every backend
added later. It is what `SessionManager.is_session_sharing_eligible()` consults
to decide whether a parent session can host multiplexed subagent sessions. The
invariants governing what an added harness may and may not change are in
[harness-parity.md](harness-parity.md).
