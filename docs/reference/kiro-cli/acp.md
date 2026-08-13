# Agent Client Protocol (ACP)

Source: https://kiro.dev/docs/cli/acp/

ACP is an open standard for agent-editor communication (like LSP for language servers). Kiro CLI implements ACP, enabling use in JetBrains IDEs, Zed, and other compatible editors.

## Quick start

```bash
kiro-cli acp                      # run as ACP agent
kiro-cli acp --agent my-agent     # with specific agent config
```

Communicates over stdin/stdout using JSON-RPC 2.0.

## Editor setup

Use full path to `kiro-cli` (IDEs don't inherit shell PATH). Find with `which kiro-cli`.

### JetBrains IDEs

Add to `~/.jetbrains/acp.json`:

```json
{
  "agent_servers": {
    "Kiro Agent": {
      "command": "/full/path/to/kiro-cli",
      "args": ["acp"]
    }
  }
}
```

### Zed

Add to `~/.config/zed/settings.json`:

```json
{
  "agent_servers": {
    "Kiro Agent": {
      "type": "custom",
      "command": "~/.local/bin/kiro-cli",
      "args": ["acp"],
      "env": {}
    }
  }
}
```

## Supported ACP methods

### Core protocol

| Method | Description |
|--------|-------------|
| `initialize` | Initialize connection, exchange capabilities |
| `session/new` | Create new chat session |
| `session/load` | Load existing session by ID |
| `session/prompt` | Send a prompt to the agent |
| `session/cancel` | Cancel current operation |
| `session/set_mode` | Switch agent mode (kiro-cli only) |
| `session/set_model` | Change model for session (kiro-cli only) |
| `session/set_config_option` | Set config option (claude-agent-acp; e.g. configId `model`) |

### Agent capabilities

Advertised during initialization:
- `loadSession: true` — supports loading existing sessions
- `promptCapabilities.image: true` — supports image content

### Session update types (via `session/notification`)

| Type | Description |
|------|-------------|
| `agent_message_chunk` | Streaming text/content |
| `agent_thought_chunk` | Reasoning/thinking content (claude-agent-acp) |
| `tool_call` | Tool invocation with name, params, status |
| `tool_call_update` | Progress updates for running tools |
| `usage_update` | Context window usage (used, size) |
| `plan` | Planning state (plumbing-only) |
| `available_commands_update` | Available slash commands changed |
| `current_mode_update` | Agent mode changed |
| `config_option_update` | Config option changed |
| `session_info_update` | Session metadata changed |
| `user_message_chunk` | Echo of user message |

## Kiro extensions (experimental)

Custom methods prefixed with `_kiro.dev/` (optional, safe to ignore).

### Slash commands

| Method | Type | Description |
|--------|------|-------------|
| `_kiro.dev/commands/execute` | Request | Execute slash command |
| `_kiro.dev/commands/options` | Request | Autocomplete suggestions |
| `_kiro.dev/commands/available` | Notification | Available commands list |

### MCP server events

| Method | Type | Description |
|--------|------|-------------|
| `_kiro.dev/mcp/oauth_request` | Notification | OAuth URL for MCP auth |
| `_kiro.dev/mcp/server_initialized` | Notification | MCP server ready |
| `_kiro.dev/mcp/server_init_failure` | Notification | MCP server init failed |

### Session management

| Method | Type | Description |
|--------|------|-------------|
| `_kiro.dev/compaction/status` | Notification | Context compaction progress |
| `_kiro.dev/clear/status` | Notification | Session clear status |
| `_kiro.dev/agent/switched` | Notification | Agent mode/name changed |
| `_session/terminate` | Notification | Terminate subagent session |

## Example: Initialize connection

```json
// Client → Agent
{
  "jsonrpc": "2.0",
  "id": 0,
  "method": "initialize",
  "params": {
    "protocolVersion": 1,
    "clientCapabilities": {
      "fs": { "readTextFile": true, "writeTextFile": true },
      "terminal": true
    },
    "clientInfo": { "name": "my-editor", "version": "1.0.0" }
  }
}

// Agent → Client
{
  "jsonrpc": "2.0",
  "id": 0,
  "result": {
    "protocolVersion": 1,
    "agentCapabilities": {
      "loadSession": true,
      "promptCapabilities": { "image": true }
    },
    "agentInfo": { "name": "kiro-cli", "version": "1.5.0" }
  }
}

// Create session
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "session/new",
  "params": { "cwd": "/home/user/my-project", "mcpServers": [] }
}

// Send prompt
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "session/prompt",
  "params": {
    "sessionId": "sess_abc123",
    "content": [{ "type": "text", "text": "Explain this codebase" }]
  }
}
```

## Agent configuration

### Config field: `prompt` (replaces `systemPrompt`)

Kiro CLI spec uses `prompt` for the system prompt field in agent config JSON files. The prior `systemPrompt` key is deprecated and should not be used.

```json
{
  "name": "my-agent",
  "model": "auto",
  "description": "Agent description",
  "prompt": "You are a helpful assistant.",
  "tools": ["@kirocrew-core"]
}
```

### Valid hook events

Kiro-cli 2.4.2+ rejects unknown keys in the `hooks` field. Valid events:

- `preToolUse`
- `postToolUse`
- `userPromptSubmit`
- `agentSpawn`
- `stop`

Any other keys (e.g. `auto_approve_tools`) cause kiro-cli to silently fall back to its default agent, losing custom MCP server configuration.

### `protocolVersion` by backend

| Backend | `protocolVersion` value |
|---------|------------------------|
| kiro-cli | `"2025-08-22"` (date string) |
| claude-agent-acp | `1` (integer) |

Sending the wrong shape yields `-32602 Invalid params` or `-32601 Method not found`.

## Session storage

```
~/.kiro/sessions/cli/
├── <session-id>.json   # metadata and state
└── <session-id>.jsonl  # event log (conversation history)
```

## Logging

| Platform | Location |
|----------|----------|
| macOS | `$TMPDIR/kiro-log/kiro-chat.log` |
| Linux | `$XDG_RUNTIME_DIR/kiro-log/kiro-chat.log` |

```bash
KIRO_LOG_LEVEL=debug kiro-cli acp
KIRO_CHAT_LOG_FILE=/path/to/custom.log kiro-cli acp
```
