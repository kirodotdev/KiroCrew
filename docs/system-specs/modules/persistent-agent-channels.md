# Persistent Agent Channels Module

## Overview

`src/kiro_crew/channel.py`, `src/kiro_crew/dashboard/handlers_channel.py`, and `website/src/pages/ChannelPage.tsx` implement persistent multi-agent workspaces where an orchestrator coordinates specialist agents through channel messages, threads, and @mentions.

## Problem

A shared workspace needs durable coordination, explicit delivery rules, and a human-controlled tool boundary so concurrent agents do not act on ambiguous messages or silently gain authority.

## Solution

Persistent agent channels provide:

- An orchestrator that receives unmentioned top-level human messages.
- Specialist agents that receive explicit @mentions, except where thread routing selects the parent sender.
- Per-agent listen modes, channel-scoped message history, and persisted channel configuration.
- A channel approval surface for provider permission requests that are not already subject to global YOLO or a channel trust grant.

## Architecture

### Data Model

```text
ChannelManager
  └── Channel
        ├── topic and orchestrator_id
        ├── members: dict[str, ChannelAgent]
        ├── messages and message-id index
        ├── exchange_counts for directed agent-to-agent delivery
        └── trusted: channel-wide auto-approval grant

ChannelAgent
  ├── session_key
  ├── state: pending, working, listening, done, or failed
  ├── approval_policy: all, writes, or trusted
  ├── listen_mode: all, mention, or silent
  └── inbox: asyncio.Queue[ChannelMessage]
```

`Channel.add_agent` makes the first member an orchestrator when the request supplies none and forces an orchestrator to listen to all messages; this guarantees that an unmentioned human request has a coordinator. `test/test_channel.py::TestChannelRouting::test_human_no_mention_reaches_orchestrator_only` pins the routing invariant.

`ChannelManager.create` and `Channel.add_agent` enforce configured channel and member capacities, and `test/test_channel.py::TestChannelManager::test_create_capacity` plus `TestChannel::test_add_agent_capacity` pin those boundaries. `api_channel_create` and `api_channel_add_agent` return HTTP 429 with a remediation message when either capacity is reached. `Channel.post` retains the newest bounded message buffer and removes matching index entries when it evicts a message, so a stale thread identifier cannot resolve to discarded state.

### Agent Lifecycle

`run_channel_agent` publishes `pending` before it acquires a session, transitions to `listening` after `SessionManager.get_or_create`, uses `working` only while handling an inbox message, and reports `failed` on an execution error. `Channel.subscribe` uses bounded queue waits so a terminal agent cannot leave a task blocked forever; `test/test_channel_subscribe_timeout.py::test_subscribe_exits_when_agent_becomes_done` pins that shutdown behavior.

A new orchestrator posts a ready system message, while a new specialist posts its task and @mentions the orchestrator. A terminal agent can be restarted only through `api_channel_wake_agent`; it returns an error for an active or missing agent.

### Message Routing

1. `Channel.post` delivers an unmentioned top-level human message to the orchestrator.
2. A human or agent message with a valid @mention is delivered only to the named active member; a specialist does not act without an @mention merely because it exists in the channel.
3. A thread reply without an @mention goes to the parent sender; a human reply to a system or otherwise unowned parent falls back to the orchestrator.
4. `ListenMode.SILENT` prevents inbox delivery, including @mentions, and terminal agents do not receive work; a mention of a terminal target produces a system bounce.
5. Agent-to-agent delivery has a directed exchange budget that a human message resets, preventing an autonomous pair from consuming the channel indefinitely. `test/test_channel.py::TestChannelRouting::test_human_message_resets_exchange_counts` and `test_configurable_max_exchanges` pin that rule.

`run_channel_agent` keeps coordination in a thread. The orchestrator posts at the top level only for a top-level human request or when reporting back after threaded agent work, which preserves a readable human-facing summary without suppressing specialist work.

## Approval Boundary

`ApprovalPolicy` accepts `all`, `writes`, and `trusted`, and `run_channel_agent` stores the selected value through `SessionManager.get_or_create`. In this module, all three values have the same initial approval handling: `_stream_task` does not classify read versus write calls or translate `trusted` into an auto-approval grant. An `EVENT_PERMISSION_REQUEST` stays interactive unless global YOLO, persisted channel trust, or a separately granted agent-scoped command literal authorizes it. The policy field names themselves do not provide a stronger per-agent guarantee.

For each permission request that reaches `_stream_task`, the channel waits for a human decision unless global YOLO is active, `Channel.trusted` is already set, or an agent-scoped command grant matches. The approval endpoint accepts `approved`, `rejected`, `trust`, `trust_command`, and `trust_base`; a missing, invalid, denied, or timed-out decision rejects the provider request. `asyncio.wait_for` supplies the timeout and `_stream_task` audits the resulting decision through `sel().log_tool_invocation()`.

A `trust` decision sets and persists `Channel.trusted`, so subsequent permission requests in that channel auto-approve. `trust_command` and `trust_base` are runtime-only, agent-scoped shell grants. The server derives their authority from the pending provider-classified shell event's canonical `tool_input`; the request pattern is only a consent proof and a stale or divergent card fails closed. Exact grants use case-sensitive literal equality, and base grants are available only for one simple, unambiguous invocation. Non-shell, redacted, compound, quoted-executable, environment-prefixed, and unparseable commands remain allow-once/reject only where a safe base cannot be derived.

Global YOLO, channel-wide trust, and agent-scoped command trust all run after the containment denylist: `_blocked_tool_named` rejects direct-to-user messaging and session-control tools before every auto-approval branch. `test/test_channel_blocked_tools.py::test_blocked_tool_rejected_even_on_trusted_channel` pins that ordering.

The approval card exposes sanitized, truncated tool input and only renders action buttons while the dashboard is in normal approval mode. Shell cards whose command remains fully visible offer exact-command and base-command tiers; other cards offer only channel-wide trust. Failed decision requests restore the controls and focus rather than displaying an optimistic success. A channel agent must communicate through channel posts; it is prompted not to use direct messaging or subagent spawning, and blocked tool names are enforced rather than treated as prompt-only guidance.

## Presets and Configuration

`api_channel_presets` reads `channel_presets` from `config.json` and falls back to built-in presets when the file is absent or malformed. `_load_presets` caches against the configuration file's stat signature, so an edited preset is observed without a gateway restart.

`api_channel_create` validates supplied agent fields and injects an orchestrator when none is marked. `ChannelPage.tsx` sends the agents for the selected preset ID rather than resolving roles from display text; its local preset list is only a fallback when the endpoint fails.

## Persistence and Recovery

`Channel.serialize` persists member configuration, message history, exchange counts, the channel trust grant, and routing metadata. `ChannelManager._save_channel` uses `atomic_write`, which protects a channel file from concurrent partial replacement; `ChannelManager._load_all` restores valid channel records at startup.

`Channel.deserialize` restores members in a terminal state. Dashboard startup then marks each restored member `pending` and launches `run_channel_agent` with a fresh session, so persisted configuration resumes without pretending an old process or tool approval is still live. `test/test_channel.py::TestChannelPersistence::test_serialize_deserialize` pins the terminal deserialization state.

Closing a channel cancels live agent tasks, broadcasts the close, and removes its persisted record through `ChannelManager.close`.

## Context Management

`api_channel_clear_context` resets either one agent session or every channel-agent session. An agent-scope reset preserves shared messages and exchange counts; an all-scope reset also clears both, persists the channel, and broadcasts `channel_context_cleared` so other browser clients discard stale messages.

The handler does not take a per-channel lock. A post concurrent with an all-scope reset can be cleared by the reset, and an in-flight approval future is not cancelled by the handler; it resolves through the agent task after the session reset. This is the current concurrency gap, not a guarantee of serialized channel mutation.

## Security

- `_stream_task` redacts credentials and exfiltration URLs from streamed agent output, tool status, and approval content before channel publication.
- `CHANNEL_AGENT_BLOCKED_TOOLS` and `_blocked_tool_named` contain direct-to-user messaging and session-control operations so a channel agent cannot move channel content into a private dashboard session or take control of one.
- `api_channel_approve_agent` validates the decision allowlist. Per-command trust requires a pattern matching the server-bound pending command, records grants as opaque literals, audits both grants and refusals, and resolves the current request as a one-time approval. `_stream_task` repeats the result allowlist check before acting on the approval future.
- `_json_object` rejects invalid JSON and non-object request bodies before channel handlers read fields.
- Channel approval and trust decisions are logged through `sel().log_tool_invocation`; context-clear requests are logged through `sel().log_api_access`.

## Frontend

`ChannelPage.tsx` loads channel summaries and full active-channel detail, then applies `kirocrew-channel` WebSocket events. It deduplicates a create response and its matching `channel_created` event by ID, so one newly created channel is not listed twice.

The page renders pending, working, listening, done, failed, and tool-running states; supports thread replies and @mention completion; exposes per-agent listen-mode updates, dismiss, context reset, and channel close; and displays approval cards for channel approval messages. URL path parameters pass through `encodeURIComponent` in `website/src/api/client.ts`.

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/channels/presets` | Return configured or built-in channel presets. |
| GET | `/api/channels` | List channel summaries. |
| POST | `/api/channels` | Create a channel and its requested agents. |
| GET | `/api/channels/{id}` | Return channel detail and its recent message window. |
| DELETE | `/api/channels/{id}` | Close a channel and cancel its live agents. |
| POST | `/api/channels/{id}/clear-context` | Reset one agent context or all agent contexts and shared channel state. |
| POST | `/api/channels/{id}/messages` | Post a human message, optional mentions, and an optional thread parent. |
| POST | `/api/channels/{id}/agents` | Add and start an agent. |
| PATCH | `/api/channels/{id}/agents/{aid}` | Update an agent approval policy or listen mode. |
| DELETE | `/api/channels/{id}/agents/{aid}` | Dismiss an agent. |
| POST | `/api/channels/{id}/agents/{aid}/wake` | Restart a terminal agent. |
| POST | `/api/channels/{id}/agents/{aid}/approve` | Resolve a pending provider permission request; command/base trust decisions include a consent-proof `pattern`. |

`src/kiro_crew/dashboard/routes/connections.py::register_connection_routes` registers these routes.

## Files

| File | Purpose |
|---|---|
| `src/kiro_crew/channel.py` | Channel model, routing, persistence, containment, and agent execution loop. |
| `src/kiro_crew/dashboard/handlers_channel.py` | Validated channel REST handlers, agent lifecycle calls, approvals, presets, and context resets. |
| `src/kiro_crew/dashboard/routes/connections.py` | Dashboard route registration. |
| `website/src/pages/ChannelPage.tsx` | Channel workspace UI, WebSocket reconciliation, threads, presets, and agent controls. |
| `website/src/api/client.ts` | Encoded channel API client methods. |
| `test/test_channel.py` | Model, routing, capacity, and persistence coverage. |
| `test/test_channel_blocked_tools.py` | Containment ordering coverage. |
| `test/test_channel_subscribe_timeout.py` | Terminal-agent subscription shutdown coverage. |
