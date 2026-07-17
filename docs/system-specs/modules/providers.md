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

**Note:** `BedrockProvider` (`providers/bedrock.py`) and the standalone
`ClaudeCodeProvider` (`providers/claude_code.py`) were **deleted** during
de-Amazoning, along with the `cc_*` / `bedrock_*` config fields and the
multi-provider dispatch factory. `acp/client.py` keeps a dormant
`ACP_BACKEND_CLAUDE` seam (`AcpProvider` can in principle drive
`claude-agent-acp`) so an internal companion can re-register Claude Code, but
the public provider factory never selects it — `kiro-cli` is the only backend.
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
    async def wait_for_compaction(timeout: float = 120.0) -> dict
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

The sole provider. Spawns a long-lived `kiro-cli acp --agent <name>` subprocess
and speaks JSON-RPC 2.0 over stdio.

**Dormant backend seam:** `AcpProvider`/`AcpClient` retain an `acp_backend`
parameter (`"" ` → kiro-cli; `"claude"` / `ACP_BACKEND_CLAUDE` → `claude-agent-acp`)
so an internal companion can re-register a Claude-Code backend over the same
client. **The public provider factory only ever selects kiro-cli** — the claude
branch is unreachable in this build. Its binary-resolution + config-isolation
details live in [`acp-client.md`](acp-client.md); do not re-add the registration
glue or a provider selector (see the repo-root `CLAUDE.md`).

**Key APIs:**
- `start()` → `AcpClient.ensure_ready()` (spawns process, handshake, session/new)
- `stream()` → maps events from `stream_events()`
- `stream_command()` → native slash command execution
- `approve_tool()`/`reject_tool()` → JSON-RPC response
- `context_usage_pct()` → reads `last_prompt_stats.context_pct`
- `context_window_tokens()` → reads `last_prompt_stats.context_window_tokens` (the real served window from `usage_update.size`, 0 if unknown). Used by the dashboard token text instead of re-deriving the window from the model id.
- `compact()` → sends `/compact` via `send_command()`
- `cancel()` → sends `session/cancel` notification
- `supports_effort()` / `change_effort(level)` / `clear_effort()` → reasoning-effort control (see below)
- `is_alive()` → `AcpClient.is_responsive()` (600s stale threshold)
- `is_process_alive()` → OS-level process check

**Reasoning effort** (Opus/Sonnet/Fable only — shared vocabulary in `effort.py`: levels `low|medium|high|xhigh|max`, capability via `model_supports_effort`, resolution via `resolve_effort_for_model` with priority slot-override > workspace default > None): applied via a workspace `cli.json` overlay at `<work_dir>/.kiro/settings/cli.json` → `chat.modelDefaults.<model>.output_config.effort`, written before every spawn (`_write_cli_overlay`) and recovered on init (`_read_cli_overlay`) for server-restart resilience. Live change pushes `/effort` with the TuiCommand args form (`send_command(args={"level": …})`). The factory threads `reasoning_effort_override` → `effort_per_model[current_model]`; the dashboard handler routes through `change_effort`/`clear_effort` and only resets the session when there is no live provider. Non-effort-capable models persist the slot value without a live apply or reset.

**MCP Tool Search** (kiro backend only — see https://kiro.dev/docs/cli/mcp/tool-search/): loads MCP tool specs on demand ("search-and-call") instead of sending every tool definition each turn, keeping the context window clear when many MCP servers are configured. Gated by the `agent.tool_search` config toggle (default **on**; auto-surfaces as a Settings toggle since the schema is generated from the dataclass).
- Applied via the **same** workspace `cli.json` overlay used for effort (`<work_dir>/.kiro/settings/cli.json`), written deterministically before every spawn and on each restart by `_write_tool_search_overlay` (called from `AcpProvider.__init__` and `start()`). When enabled it writes the flat keys `toolSearch.enabled=true` plus `toolSearch.minPct=0`/`toolSearch.minTokens=0` to force deferral always-on (KiroCrew sessions always carry several MCP servers); when disabled it writes `toolSearch.enabled=false` and drops the zeroed thresholds.
- Writing both `true` and `false` makes the KiroCrew toggle authoritative over any value in the user's global `~/.kiro/settings/cli.json`. The write is merge-safe with the effort `chat.modelDefaults` keys in the same file.
- **claude backend** — no-op. Tool Search is a kiro-cli feature; `_apply_tool_search_overlay` returns early for the claude backend and when no toggle value was threaded in (`tool_search is None`).

- **Resume guard:** `session/load` (resume) is only attempted when the prior session transcript exists on disk (`~/.kiro/sessions/cli/<sid>.json`). A stale persisted sid with no transcript falls back to `session/new`, preventing a fresh conversation from replaying old turns (which inflated base context).
- **Working dir:** `AcpProvider.cwd` overrides the `LLMProvider` ABC default so `session_map` persists the real workspace path. AcpProvider's work_dir lives on the inner client (`_client._work_dir`), so the prior `getattr(provider, "_work_dir", "")` persisted `""` for all ACP sessions — `provider.cwd` fixes resume-cwd-override.

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
- `create_provider_factory()` returns a `Callable` that creates the kiro-cli `AcpProvider`.

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

**Interactive transient-5xx retry** (a270bd1f, Mesh-1157): The interactive dashboard/Slack `chat_runner` stream loop retries a transient backend 5xx (InternalServerError / DispatchFailure / ConnectionReset, JSON-RPC `-32603`) through the shared `llm_helpers` transient classifier + backoff, **without** resetting the still-alive session. A `_turn_emitted` partial-output guard prevents a re-prompt from double-streaming tokens or re-running a side-effecting tool. Auth/validation errors are excluded (fail-fast); on retry-budget exhaustion a clean error surfaces on a still-resumable session. This extends the unattended `stream_and_collect` retry path (previously deferred for the interactive loop) to interactive callers.

**Compaction-failure notice backoff** (dashboard-chat; `dashboard/chat_utils.py:_broadcast_compaction_result`): Per-turn compaction failures no longer spam the chat. Per slot, `_compaction_fail_streak` counts consecutive failures and the first `_COMPACTION_NOTICE_SHOW_FIRST_N` (=2) are shown verbatim ("❌ Compaction failed: …"); further failures within the `_COMPACTION_FAIL_COOLDOWN_SECS` (60s) `_compaction_fail_cooldown_until` window are suppressed, and when the cooldown elapses a single collapsed "failed Nx in a row … Consider `/compact` manually" message is shown with `/compact` guidance. A `completed` status resets the streak/cooldown. `acp/client.py:_log_compaction_status` logs the raw failed-compaction notification params at WARNING (kiro-cli carries no dedicated error field on failure). This is a UX/spam guard only — the underlying compaction still runs every turn on kiro-cli's schedule — and is distinct from SessionManager's proactive auto-compact cooldown.

### Installation

KiroCrew drives `kiro-cli` over ACP — install it per its own docs, ensure it is
on `PATH`, and run `kiro-cli login`. `kirocrew doctor` reports its status.


## AcpProvider: shared-runtime startup

`AcpProvider.start()` branches on the backend:

- **kiro (`is_claude_backend` False)** → `_start_kiro_runtime()`. This spawns an
  `AcpRuntime` (carrying the provider's sandbox mode, extra env, and MCP-gateway
  overlay/socket), resumes via `runtime.load_session()` when a prior transcript
  exists or otherwise `runtime.create_session()`, applies the configured model,
  and replaces `self._client` with an `AcpSessionProvider` (which implements the
  same interface as `AcpClient`, so downstream callers are unchanged). Any
  failure after `spawn()` kills the runtime so a half-initialised session never
  leaks an orphaned `kiro-cli`.
- **Claude Code (`is_claude_backend` True)** → legacy `AcpClient.ensure_ready()`.

`AcpProvider.is_session_sharing_eligible` returns `not is_claude_backend`; it is
what `SessionManager.is_session_sharing_eligible()` consults to decide whether a
parent session can host multiplexed subagent sessions.
