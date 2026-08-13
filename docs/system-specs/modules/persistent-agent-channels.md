# Persistent Agent Channels Module

## Overview

`channel.py` + `handlers_channel.py` + `ChannelPage.tsx` — multi-agent
collaboration spaces where specialized agents work on assigned roles,
communicate via @mentions, and persist across sessions.

## Problem

KiroCrew's subagent model is fire-and-forget: spawn a task, get a result,
done. There's no way for multiple agents to collaborate on a shared problem
over time, see each other's progress, or coordinate through a human
orchestrator.

## Solution

Persistent agent channels — shared communication spaces with:
- An always-on orchestrator agent that coordinates work
- Specialist agents that wake on @mention
- Human approval for mutating tool calls
- Disk persistence for channel state across gateway restarts

## Architecture

### Data Model

```
ChannelManager (singleton)
  └── Channel (max 1 active, configurable)
        ├── topic: str
        ├── orchestrator_id: str
        ├── members: dict[str, ChannelAgent] (max 3 per channel, configurable)
        ├── messages: list[ChannelMessage] (max 200, O(1) index)

ChannelAgent
  ├── state: pending → listening → working → done/failed
  ├── is_orchestrator: bool
  ├── approval_policy: ALL | WRITES | TRUSTED
  ├── listen_mode: ALL | MENTION | SILENT
  └── inbox: asyncio.Queue[ChannelMessage]
```

### Agent Lifecycle

```
[create] → pending (cold-start) → listening (session ready)
         → working (processing message) → listening
         → done (dismissed or channel closed)
```

- Agents stay alive until explicitly dismissed or channel closed
- Pending state shown as ⏳ until session cold-start completes

### Message Routing

1. Human messages → orchestrator (top-level, no @mention needed)
2. @mention → targeted agent only (bounce message if done/failed)
3. Agent-to-agent → capped at 3 exchanges per pair (prevent loops)
4. Thread replies → routed to parent message sender (fallback to orchestrator)
5. System messages → orchestrator ready, agent joined notifications

### Approval Flow

1. Agent requests tool use → `_stream_task` intercepts `EVENT_PERMISSION_REQUEST`
2. Approval message posted to channel with tool details (sanitized)
3. `asyncio.Future` created before broadcast (prevents race condition)
4. Human clicks Approve/Reject/Trust via REST endpoint
5. Trust mode auto-approves subsequent calls for that agent
6. SEL audit logged for trust escalation and approval decisions

## Limits

| Limit | Value | Enforced at |
|-------|-------|-------------|
| Max active channels | 1 (configurable) | `ChannelManager.create()` |
| Max agents per channel | 3 (configurable) | `Channel.add_agent()` |
| Max messages per channel | 200 | `Channel.post()` — oldest trimmed with index cleanup |
| Max A2A exchanges per pair | 3 | `Channel.post()` — prevents infinite ping-pong |

Backend returns HTTP 429 with descriptive error messages when limits are hit.
Frontend shows a modal dialog (⚠️ Limit Reached) with the specific limit and
suggested action (e.g., "Close an existing channel first").

## Presets

| Preset | Agents | Use case |
|--------|--------|----------|
| Incident Response | Orchestrator, Logs Agent, Code Agent | Investigate production issues |
| Code Review | Reviewer (orchestrator) | Review code changes |
| Research | Orchestrator | Research and synthesize findings |

If no preset agent has `is_orchestrator: true`, the backend auto-injects
an Orchestrator agent as the first member.

## Security

- **LLM output sanitization**: `redact_exfiltration_urls` + `redact_credentials` on all agent output
- **Tool input sanitization**: Credentials and URLs redacted in approval messages
- **SEL audit**: All trust escalations and approval decisions logged via `sel().log_tool_invocation()`
- **Approval validation**: Decision allowlist (`approved`, `rejected`, `trust`) enforced in both handler and channel
- **JSON parse error handling**: All `request.json()` calls wrapped in try/except

## Persistence

Channels saved to `~/.kiro/crew/channels/{id}.json` on every state change.
Restored on gateway startup via `ChannelManager._load_all()`. Agents
restored as `done` and relaunched with fresh sessions.

## Frontend (ChannelPage.tsx)

- WebSocket real-time updates (single source of truth for channel list)
- Thread panel with nested replies
- @mention autocomplete with keyboard navigation and ARIA attributes
- Agent sidebar with state icons (⏳ pending, 🟢 working, 👂 listening, ✅ done, 💥 failed)
- Listen mode dropdown per agent
- Error modal for limit violations
- Design tokens only (no hardcoded colors), `encodeURIComponent` on all URL params

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/channels` | List all channels |
| POST | `/api/channels` | Create channel (429 if limit) |
| DELETE | `/api/channels/{id}` | Close and remove channel |
| POST | `/api/channels/{id}/messages` | Post message |
| POST | `/api/channels/{id}/agents` | Add agent (429 if limit) |
| PATCH | `/api/channels/{id}/agents/{aid}` | Update agent (listen mode) |
| DELETE | `/api/channels/{id}/agents/{aid}` | Dismiss agent |
| POST | `/api/channels/{id}/agents/{aid}/approve` | Approve/reject/trust tool call |
| GET | `/api/channels/presets` | List team presets |

## Files

| File | Lines | Purpose |
|------|-------|---------|
| `src/kiro_crew/channel.py` | ~720 | Core data model, routing, persistence, agent execution loop |
| `src/kiro_crew/dashboard/handlers_channel.py` | ~250 | REST API handlers with input validation |
| `frontend/src/pages/ChannelPage.tsx` | ~620 | Full UI with WebSocket, threads, @mention, approval |
| `frontend/src/api/client.ts` | +15 | API methods (patch helper + 13 channel endpoints) |
| `src/kiro_crew/dashboard/server.py` | +24 | Route registration |
| `test/test_channel.py` | ~240 | 26 tests across 5 classes |

## Out-of-Plan Fixes (discovered during testing)

These issues were found and fixed during live testing, not in the original implementation plan:

1. **Pending state not shown** — Agent showed "listening" during cold-start; now shows "pending" until session ready
2. **Duplicate channel in list** — Optimistic add + WebSocket event caused double entry; removed optimistic add
3. **Error modal not visible** — TypeScript type missing `pending` states caused silent frontend build failure; `setup.py` swallows frontend errors
4. **Auto-inject orchestrator** — Backend ensures every channel has an orchestrator even if preset omits one
5. **Orchestrator ready message** — Posts "✅ Ready" system message so channel doesn't look stuck when orchestrator is alone
6. **Preset resolution bug** — Frontend re-resolved preset by role matching instead of using preset ID directly
7. **Redundant inline import** — `run_channel_agent` imported both at module level and inline
8. **User-friendly limit messages** — Backend returns specific limit numbers and suggested actions instead of generic "limit reached"
