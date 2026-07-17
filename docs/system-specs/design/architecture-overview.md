# Architecture Overview

> ⚠️ **Do not enter Critical or Restricted classified data into KiroCrew. Your cloud desktop and laptop are not approved to handle this data classification. See the [Data Handling Standard](https://policy.a2z.com/docs/99/publication).**

Last Updated: 2026-05-18

## System Overview

KiroCrew is an open-source personal AI agent. Thin adapter between user interfaces (CLI, Slack, Dashboard) and `kiro-cli acp` (LLM calls, tool execution, MCP servers). kiro-cli over ACP is the only provider.

## Message Flow

```
User → CLI / Slack / Dashboard → KiroCrew
  → Hooks (auto-reply / transform / context inject)
  → Cron command interception (if applicable)
  → SessionManager.get_or_create(key) → (LLMProvider, is_new)
  → ContextBuilder.build_message(text, is_new)
      → memory + skills + lessons + conversation history
  → LLMProvider.stream(message) → response to user
  → ConversationLog.append() → detect_correction() → consolidation
```

## Package Structure

| Package | Purpose |
|---------|---------|
| `agents/` + `skills/` | Project-level config and skills (edit without rebuilding) |
| `KiroCrewWebsite/` | React 18 + TypeScript + Vite + Redux + Tailwind CSS SPA (separate package) |
| `acp/` | JSON-RPC 2.0 client for kiro-cli |
| `config/` | Dataclass config, `~/.kirocrew/config.json` loader |
| `providers/` | LLMProvider ABC + AcpProvider (kiro-cli — the only provider) |
| `slack/` | Socket Mode gateway + handler |
| `dashboard/` | Web dashboard backend (aiohttp + SSE + WebSocket) at localhost:5476 |
| `session.py` | Thread-keyed provider pool with idle expiry + compaction |
| `memory.py` | Structured memory files + FTS5 search |
| `history.py` | JSONL conversation log + LLM consolidation + title persistence |
| `context.py` | Assembles memory + skills + hooks + lessons + history |
| `skills.py` | Skill loader (project-level → bundled fallback) |
| `hooks.py` | Config-driven message/tool hooks |
| `learn.py` | Self-learning from corrections |
| `cron.py` | Scheduled jobs with cross-process locking |
| `mcp_cron.py` | MCP server for cron tools (kiro-cli calls directly) |
| `mcp_core.py` | MCP server for spawn, learn, task tools (kiro-cli calls directly) |

## Key Design Decisions

- **kiro-cli as backend** — handles LLM, tools, and MCP; KiroCrew is the orchestrator
- **JSON-RPC 2.0 over stdio** — line-delimited JSON
- **Dataclass config** — no Pydantic; stdlib dataclasses with JSON loader
- **Minimal dependencies** — stdlib core; `slack-sdk` + `aiohttp` only external deps
- **Project-level config** — `agents/` and `skills/` editable without code changes
- **Isolated workspace** — LLM sessions and tasks operate in per-session subdirectories under `workspace_root()` (`/Volumes/workplace/kirocrew-workspace` on macOS, `~/workplace/kirocrew-workspace` on Linux)
- **React SPA frontend** — Vite builds to `static/dist/`, served by aiohttp; SPA fallback middleware for React Router; if the dist bundle is absent the gateway serves a static "not found" guidance page (the legacy `dashboard.html` fallback was removed — Talos V2285871874)
- **Frontend security** — DOMPurify sanitizes all `dangerouslySetInnerHTML`; `sudo tee -a` for `/etc/hosts` (no shell injection)
- **Custom domain** — `kirocrew setup` uses `kirocrew.localhost` (RFC 6761, no /etc/hosts needed)

## Shutdown

`kiro_crew.shutdown_event` (asyncio.Event) is the process-wide signal. All background loops use `wait_for(shutdown_event.wait(), timeout=N)` for instant wake on Ctrl-C. Cleanup order: cron → heartbeat → sessions → socket → WebSocket connections (`close_all_ws`) → dashboard.

## Session Lifecycle by Caller

| Caller | Pattern |
|--------|---------|
| Slack handler | Long-lived per thread, `release()` in finally, idle expiry |
| Dashboard chat | Long-lived per slot, `release()` in finally, manual close |
| Cron / Heartbeat / Subagent | One-shot, `release()` + `reset()` in finally |
