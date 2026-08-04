# Configuration Reference

## Config File

`~/.kiro/crew/config.json` — main configuration file. Created automatically
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

Kiro Crew supports tiered sandbox levels for agent-backend process isolation:

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
    "reasoning_effort": "",
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
    "embedding_provider": "llama_cpp",
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
| `agent.model` | Default LLM model for new sessions. `"auto"` defers to the agent config, then to Kiro's own default. Editable from Settings → Chat → Model; a per-session model picker overrides it for that session only | `"auto"` |
| `agent.reasoning_effort` | Default reasoning effort for new sessions on reasoning-capable models (Opus, Sonnet, Fable, GPT-5.x). One of `""`, `low`, `medium`, `high`, `xhigh`, `max`; `""` defers to the provider/model default. Editable from Settings → Chat → Model; a per-session effort override wins | `""` |
| `agent.bot_name` | Custom name the bot identifies as | `""` |
| `agent.conductor_skill` | Enable agent delegation conductor | `false` |
| `agent.max_channels` | Max concurrent agent channels (1-5) | `1` |
| `agent.max_channel_agents` | Max agents per channel (1-10) | `3` |
| `agent.soft_stop_budget_secs` | Seconds to wait for cooperative cancel before hard kill | `10.0` |
| `agent.max_subagents` | Maximum concurrent subagents (`0` = auto-size at startup) | `0` |
| `agent.subagent_max_turns` | Default tool-call budget per subagent | `100` |
| `agent.spawn_min_memory_gb` | Minimum available memory (GB) to spawn a subagent (0 disables) | `4.0` |
| `agent.completion_keep` | Which end of the subagent transcript to keep in the completion event injected into the parent session. Three values: `"head"` (first N chars), `"tail"` (last N chars), `"both"` (head + middle marker + tail). | `"head"` |
| `agent.completion_keep_chars` | Maximum characters retained in the completion event after applying `completion_keep`. The full transcript stays in `~/.kiro/crew/subagents/<id>/result.txt` until cleanup; use the `spawn_status` MCP tool to read it before delivery. `0` disables truncation entirely. | `3000` |
| `session.timeout_secs` | Idle session timeout (0 disables idle sweep) | `1800` (30 min) |
| `session.pool_size` | Number of pre-warmed agent processes | `0` (disabled) |
| `session.pool_agent` | Agent for warm pool processes (empty = default) | `""` |
| `session.pool_ttl_secs` | Max age for pooled processes before discard | `1800` |
| `dashboard.url` | Dashboard URL for remote access | `""` (localhost only) |
| `dashboard.restore_sessions` | Restore sessions on restart | `false` |
| `dashboard.restore_window_minutes` | Minutes after restart within which sessions can be restored | `30` |
| `dashboard.merge_queued_messages` | Concatenate follow-up messages while agent is busy | `false` |
| `dashboard.mcp_probe_timeout_secs` | Seconds to wait for MCP server handshake during probe (5-120) | `15` |
| `slack.allowed_users` | Users who can interact with Kiro Crew | `[]` |
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
| `memory.embedding_provider` | Vector embedding backend — always-on, in-process via the bundled llama-cpp-python runtime. Every legacy value (including `"ollama"` and `"none"`) is coerced to `"llama_cpp"`; setting `"none"` no longer disables embeddings. The old `embedding_url` / `embedding_model` / `embedding_runtime` / `embedding_managed` / `embedding_auth` / `embedding_timeout_secs` / `allow_remote_embedding` keys are removed and ignored if present (`embedding_model` in particular is ignored — the model identity comes from the active backend) | `"llama_cpp"` |
| `memory.embed_model_url` | Override HTTPS URL for the embedding-model GGUF download (mirrored/airgapped hosts). Empty uses the public Kiro Crew CDN; the `KIROCREW_EMBED_MODEL_URL` env var wins over both. Downloads are sha256-verified regardless of source | `""` |
| `memory.embed_model_path` | Absolute path to a local GGUF embedding model to run **instead of** the bundled Qwen3-Embedding-0.6B. When set, the default model is never downloaded or installed, so a custom model survives a default-model version change. Set `memory.embedding_dim` to the model's output width — a mismatch is detected at load and refuses to publish the model rather than silently returning no embeddings. Changing the model changes the vector space, so stored embeddings are regenerated automatically. A configured-but-unreadable path fails closed (embeddings unavailable, keyword search still works) rather than reverting to the default and re-embedding your corpus behind your back. Editable from the dashboard (Memory → Embedding Model), which validates the path, refuses protected locations, probes the model's width and re-embeds stored vectors in the background — no restart needed. `KIROCREW_EMBED_MODEL_PATH` wins over this, and while that env var is set the dashboard refuses to change the model (a config write could not take effect) | `""` |
| `memory.embed_model_id` | Optional stable identifier for a custom model's vector space. Defaults to `custom:<filename>:<size>`, which changes when a different model file is used. Set explicitly if you swap between models of identical byte size, which the default derivation cannot distinguish | `""` |
| `skills.max_triggered` | Maximum skills loaded per message (≥1) | `3` |
| `knowledge.auto_ingest_artifacts` | Auto-ingest content-bearing local artifacts into the Knowledge Library (searchable "Artifacts" source); kept in sync and removed when the artifact is deleted (see [Knowledge Library](knowledge-library-how-it-works.md)) | `true` |
| `knowledge.auto_ingest_artifact_kinds` | Artifact kinds eligible for auto-ingest (`widget` excluded as UI/dashboards; `svg` excluded — no reader support) | `["markdown", "text", "html", "json"]` |
| `knowledge.auto_discover_folder` | Watch for a documents folder inside the active workspace and register it as a Knowledge source automatically, so files dropped there become searchable without adding the source by hand. The folder is never created for you — its absence means you have not opted in — and it is picked up within one watcher sweep (default 300s) of being created, with no restart. Deleting the auto-added source records a dismissal, so it stays gone instead of reappearing on the next sweep; pausing it also persists. Off by default because ingestion spends LLM extraction on every supported file | `false` |
| `knowledge.auto_discover_dirname` | Folder name inside the workspace that auto-discovery looks for. A single path segment; separators and traversal are rejected so the source cannot be redirected outside the workspace. Avoid `knowledge` — that is where the Library's own SQLite store lives and it always exists, which would defeat discovery | `"knowledge-docs"` |

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `KIROCREW_HOME` | Override config/data directory | `~/.kiro/crew` |
| `KIROCREW_PORT` | Override dashboard port | `5476` |
| `KIROCREW_PROJECT_DIR` | Override agent config/skills directory | Auto-detected |
| `KIROCREW_SKIP_MODEL_DOWNLOAD` | Set to `1` to skip the background embedding-model download at gateway startup (tests/CI, air-gapped hosts) | unset |
| `KIROCREW_EMBED_MODEL_URL` | Override HTTPS URL for the embedding-model GGUF (mirrors); wins over `memory.embed_model_url` and the CDN default | unset |
| `KIROCREW_EMBED_MODEL_PATH` | Absolute path to a local GGUF to use instead of the bundled model; wins over `memory.embed_model_path`. Suppresses the default-model download entirely | unset |
| `KIROCREW_WORKSPACE` | Override workspace root directory | Platform default |

### Timezone

The `timezone` config key (IANA format, e.g. `"America/Los_Angeles"`) affects:
- `[CURRENT DATE]` injection in every LLM prompt
- Cron schedule display (`cron list`, Home Tab)
- `skip_dates` evaluation for cron jobs

When empty (default), falls back to UTC.

## Credentials

`~/.kiro/crew/.env` — Slack tokens and owner ID:

```
SLACK_APP_TOKEN=xapp-...
SLACK_BOT_TOKEN=xoxb-...
KIROCREW_OWNER_ID=UXXXXXXXX
```

## File Locations

| Path | Purpose |
|------|---------|
| `~/.kiro/crew/config.json` | Main config |
| `~/.kiro/crew/.env` | Slack credentials |
| `~/.kiro/crew/skills/` | User skills |
| `~/.kiro/crew/crons.json` | Scheduled jobs |
| `~/.kiro/crew/lessons.jsonl` | Learned corrections |
| `~/.kiro/crew/models/` | Embedding model (downloaded in background at startup) |
| `~/.kiro/crew/history/` | Chat history (JSONL) |
| `~/.kiro/crew/workspace/memory/` | Memory files |
| `~/.kiro/crew/session_map.json` | Session resume mapping |
| `~/.kiro/agents/kirocrew.json` | Installed agent config |
| `~/.kiro/settings/mcp.json` | Global MCP server config |
