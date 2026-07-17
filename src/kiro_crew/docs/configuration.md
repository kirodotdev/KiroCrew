# Configuration Reference

## Config File

`~/.kirocrew/config.json` — main configuration file. Created automatically
on first `kirocrew gateway` run.

### Managing Config

```bash
kirocrew config get                    # print full config
kirocrew config get agent.model        # print a specific value
kirocrew config set agent.model auto   # set a value (auto type detection)
kirocrew config edit                   # open in $EDITOR
```

All config changes are audit-logged.

### Sandbox Modes

KiroCrew supports tiered sandbox levels for agent-backend process isolation:

| Mode | Behavior |
|------|----------|
| `auto` (default) | Standard isolation — enables git-over-SSH and AWS CLI via `credential_process` while hiding non-workflow credential stores |
| `strict` | Maximum isolation — blocks all external network and credential access |
| `off` | No sandbox — full system access (use with caution) |

Set via `kirocrew config set sandbox.mode auto`.

### Key Settings

```json
{
  "agent": {
    "default_agent": "kirocrew",
    "approval_mode": "interactive",
    "model": "auto",
    "bot_name": "",
    "conductor_skill": false,
    "max_channels": 1,
    "max_channel_agents": 3,
    "max_subagents": 0,
    "subagent_max_turns": 100,
    "spawn_min_memory_gb": 4.0,
    "soft_stop_budget_secs": 10.0,
    "completion_keep": "head",
    "completion_keep_chars": 3000
  },
  "session": {
    "timeout_secs": 1800,
    "pool_size": 0,
    "pool_agent": "",
    "pool_ttl_secs": 1800
  },
  "dashboard": {
    "url": "",
    "restore_sessions": false,
    "restore_window_minutes": 30,
    "merge_queued_messages": false,
    "mcp_probe_timeout_secs": 15
  },
  "slack": {
    "allowed_users": [],
    "tracking_channels": [],
    "open_channels": [],
    "command": "kirocrew",
    "reactions": {},
    "reactions_enabled": true
  },
  "stt": {
    "enabled": false,
    "provider": "whisper",
    "streaming": false,
    "transcribe_region": "us-east-1",
    "language_code": "en-US"
  },
  "memory": {
    "history_idle_hours": 3.0,
    "history_max_days": 365
  },
  "skills": {
    "max_triggered": 3
  },
  "knowledge": {
    "auto_ingest_artifacts": true,
    "auto_ingest_artifact_kinds": ["markdown", "text", "html", "json"]
  }
}
```

| Key | Description | Default |
|-----|-------------|---------|
| `agent.provider` | LLM provider backend: `"acp"` (KiroACP / kiro-cli) | `"acp"` |
| `agent.default_agent` | Default agent name | `"kirocrew"` |
| `agent.approval_mode` | `"auto"` or `"interactive"` | `"interactive"` |
| `agent.model` | LLM model override | `"auto"` |
| `agent.bot_name` | Custom name the bot identifies as | `""` |
| `agent.conductor_skill` | Enable agent delegation conductor | `false` |
| `agent.max_channels` | Max concurrent agent channels (1-5) | `1` |
| `agent.max_channel_agents` | Max agents per channel (1-10) | `3` |
| `agent.soft_stop_budget_secs` | Seconds to wait for cooperative cancel before hard kill | `10.0` |
| `agent.max_subagents` | Maximum concurrent subagents (`0` = auto-size at startup) | `0` |
| `agent.subagent_max_turns` | Default tool-call budget per subagent | `100` |
| `agent.spawn_min_memory_gb` | Minimum available memory (GB) to spawn a subagent (0 disables) | `4.0` |
| `agent.completion_keep` | Which end of the subagent transcript to keep in the completion event injected into the parent session. Three values: `"head"` (first N chars), `"tail"` (last N chars), `"both"` (head + middle marker + tail). | `"head"` |
| `agent.completion_keep_chars` | Maximum characters retained in the completion event after applying `completion_keep`. The full transcript stays in `~/.kirocrew/subagents/<id>/result.txt` until cleanup; use the `spawn_status` MCP tool to read it before delivery. `0` disables truncation entirely. | `3000` |
| `agent.enforce_denied_commands` | Scope for deniedCommands: `"all"` or `"kirocrew"` | `"all"` |
| `session.timeout_secs` | Idle session timeout (0 disables idle sweep) | `1800` (30 min) |
| `session.pool_size` | Number of pre-warmed agent processes | `0` (disabled) |
| `session.pool_agent` | Agent for warm pool processes (empty = default) | `""` |
| `session.pool_ttl_secs` | Max age for pooled processes before discard | `1800` |
| `dashboard.url` | Dashboard URL for remote access | `""` (localhost only) |
| `dashboard.restore_sessions` | Restore sessions on restart | `false` |
| `dashboard.restore_window_minutes` | Minutes after restart within which sessions can be restored | `30` |
| `dashboard.merge_queued_messages` | Concatenate follow-up messages while agent is busy | `false` |
| `dashboard.mcp_probe_timeout_secs` | Seconds to wait for MCP server handshake during probe (5-120) | `15` |
| `slack.allowed_users` | Users who can interact with KiroCrew | `[]` |
| `slack.tracking_channels` | Channels to monitor for new members | `[]` |
| `slack.open_channels` | Channel IDs where all users are authorized | `[]` |
| `slack.reactions` | Override phase reaction emojis (set a value to `null` to suppress that phase) | `{}` |
| `slack.reactions_enabled` | Show phase reactions on Slack messages | `true` |
| `stt.provider` | STT provider: `"whisper"` (local) or `"transcribe"` (AWS, requires the `voice` extra) | `"whisper"` |
| `stt.streaming` | Enable streaming transcription in dashboard | `false` |
| `stt.transcribe_region` | AWS region for Transcribe API (only when provider=`transcribe`) | `"us-east-1"` |
| `stt.language_code` | Language for speech recognition | `"en-US"` |
| `memory.history_idle_hours` | Hours idle before history consolidation | `3.0` |
| `memory.history_max_days` | Days to keep history before pruning | `365` |
| `memory.episodic_max_results` | Max episodic memories injected per session | `8` |
| `memory.embedding_provider` | Vector embedding backend: `"none"` or `"ollama"` | `"none"` |
| `memory.embedding_model` | Ollama model name for embeddings | `"qwen3-embedding:0.6b"` |
| `memory.embedding_runtime` | How Ollama runs: `"native"` or `"docker"` (AL2 glibc fallback) | `"native"` |
| `skills.max_triggered` | Maximum skills loaded per message (≥1) | `3` |
| `knowledge.auto_ingest_artifacts` | Auto-ingest content-bearing local artifacts into the Knowledge Library (searchable "Artifacts" source); kept in sync and removed when the artifact is deleted (see [Knowledge Library](knowledge-library-how-it-works.md)) | `true` |
| `knowledge.auto_ingest_artifact_kinds` | Artifact kinds eligible for auto-ingest (`widget` excluded as UI/dashboards; `svg` excluded — no reader support) | `["markdown", "text", "html", "json"]` |

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `KIROCREW_HOME` | Override config/data directory | `~/.kirocrew` |
| `KIROCREW_PORT` | Override dashboard port | `5476` |
| `KIROCREW_PROJECT_DIR` | Override agent config/skills directory | Auto-detected |
| `KIROCREW_WORKSPACE` | Override workspace root directory | Platform default |

### Timezone

The `timezone` config key (IANA format, e.g. `"America/Los_Angeles"`) affects:
- `[CURRENT DATE]` injection in every LLM prompt
- Cron schedule display (`cron list`, Home Tab)
- `skip_dates` evaluation for cron jobs

When empty (default), falls back to UTC.

## Credentials

`~/.kirocrew/.env` — Slack tokens and owner ID:

```
SLACK_APP_TOKEN=xapp-...
SLACK_BOT_TOKEN=xoxb-...
KIROCREW_OWNER_ID=UXXXXXXXX
```

## File Locations

| Path | Purpose |
|------|---------|
| `~/.kirocrew/config.json` | Main config |
| `~/.kirocrew/.env` | Slack credentials |
| `~/.kirocrew/skills/` | User skills |
| `~/.kirocrew/crons.json` | Scheduled jobs |
| `~/.kirocrew/lessons.jsonl` | Learned corrections |
| `~/.kirocrew/history/` | Chat history (JSONL) |
| `~/.kirocrew/workspace/memory/` | Memory files |
| `~/.kirocrew/session_map.json` | Session resume mapping |
| `~/.kiro/agents/kirocrew.json` | Installed agent config |
| `~/.kiro/settings/mcp.json` | Global MCP server config |
