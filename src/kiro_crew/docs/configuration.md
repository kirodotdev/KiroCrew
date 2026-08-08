# Configuration Reference

Everything Kiro Crew remembers about how it should behave lives in one JSON file,
`~/.kiro/crew/config.json`, created automatically on the first `kirocrew gateway`
run. Most keys are also editable from the dashboard's Settings pages, and this
page is the reference for the ones that are not: what they mean, what they
default to, and which environment variables outrank them.

## Managing Config

```bash
kirocrew config get                    # print full config
kirocrew config get agent.model        # print a specific value
kirocrew config set agent.model auto   # set a value (auto type detection)
kirocrew config set --local agent.model auto   # write config.local.json instead
kirocrew config edit                   # open in $EDITOR
```

Every config change is audit-logged to the security event log.

`config.local.json` holds overrides that survive an upgrade, which is what
`--local` writes to. Its values win over `config.json`.

The dashboard port is **not** a config key: set `KIROCREW_PORT` instead.

## Sandbox

`agent.sandbox` controls whether Kiro Crew wraps the agent process in its own
OS-level sandbox (a user namespace on Linux, `sandbox-exec` on macOS).

| Value | Behavior |
|-------|----------|
| `off` (default) | Defer isolation to kiro-cli's own internal agent sandbox |
| `auto` | Add Kiro Crew's OS-level sandbox on top |

The two layers are mutually exclusive on macOS, because a nested seatbelt
sandbox fails with `EPERM`. That is why the default is `off`: kiro-cli already
isolates the agent, and stacking a second sandbox would break it.

Set via `kirocrew config set agent.sandbox auto`.

## Key Settings

```json
{
  "agent": {
    "provider": "acp",
    "approval_mode": "auto",
    "model": "auto",
    "reasoning_effort": "",
    "sandbox": "off",
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
    "timeout_secs": 3600,
    "autocompact_pct": 90.0,
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
    "enabled": true,
    "provider": "whisper",
    "streaming": false,
    "transcribe_region": "us-east-1",
    "language_code": "en-US"
  },
  "memory": {
    "embedding_provider": "llama_cpp",
    "embedding_dim": 1024,
    "history_idle_hours": 3.0,
    "history_max_days": 365
  },
  "skills": {
    "max_triggered": 0
  },
  "knowledge": {
    "auto_ingest_artifacts": true,
    "auto_add_documents": true,
    "auto_register_project_docs": true,
    "auto_ingest_artifact_kinds": ["markdown", "text", "html", "json"],
    "auto_ingest_chunk_budget": 150,
    "folder_ingest_chunk_budget": 300,
    "dedup_every_n_sweeps": 12
  },
  "auto_update": true,
  "timezone": ""
}
```

### Agent

| Key | Description | Default |
|-----|-------------|---------|
| `agent.provider` | LLM provider backend. `"acp"` (KiroACP / kiro-cli) is the only accepted value | `"acp"` |
| `agent.default_agent` | Default agent name for new sessions. Empty resolves from the agent config | `""` |
| `agent.approval_mode` | `"auto"` or `"interactive"` | `"auto"` |
| `agent.model` | Default LLM model for new sessions. `"auto"` defers to the agent config, then to Kiro's own default. Editable from Settings → Chat → Model; a per-session model picker overrides it for that session only | `"auto"` |
| `agent.reasoning_effort` | Default reasoning effort on models that support it. One of `""`, `low`, `medium`, `high`, `xhigh`, `max`; `""` defers to the provider/model default. A per-session override wins | `""` |
| `agent.sandbox` | `"off"` (defer to kiro-cli) or `"auto"` (add Kiro Crew's OS-level sandbox) | `"off"` |
| `agent.streaming` | Stream response text as it is generated | `true` |
| `agent.bot_name` | Custom name the bot identifies as | `""` |
| `agent.conductor_skill` | Enable agent delegation conductor | `false` |
| `agent.max_channels` | Max concurrent agent channels (1-5) | `1` |
| `agent.max_channel_agents` | Max agents per channel (1-10) | `3` |
| `agent.log_level` | Persistent log level for the `kiro_crew` logger, applied at startup. The `--verbose` CLI flag overrides it | `"WARNING"` |
| `agent.soft_stop_budget_secs` | Seconds to wait for a cooperative cancel before hard-killing the session | `10.0` |
| `agent.max_subagents` | Max concurrent subagents. `0` auto-sizes the cap at startup from host memory/CPU and a learned per-agent cost. A pin of 1 or 2 is raised to 3, because a cap below 3 would disable auto-sizing and still run under the default | `0` |
| `agent.subagent_max_turns` | Default tool-call budget per subagent | `100` |
| `agent.spawn_min_memory_gb` | Minimum available memory (GB) to spawn a subagent (0 disables the check) | `4.0` |
| `agent.completion_keep` | Which end of the subagent transcript to keep in the completion event injected into the parent session: `"head"`, `"tail"`, or `"both"` (head + middle marker + tail) | `"head"` |
| `agent.completion_keep_chars` | Max characters retained in the completion event after applying `completion_keep`. `0` disables truncation. The full transcript stays on disk (see `subagent_result_ttl_secs`) | `3000` |
| `agent.subagent_result_ttl_secs` | How long a delivered subagent's `result.txt` is retained before the reaper prunes it, so the parent can read the full transcript on demand instead of re-running the subagent | `3600` (1h) |

### Session

| Key | Description | Default |
|-----|-------------|---------|
| `session.timeout_secs` | Idle session timeout in seconds (0 disables the idle sweep) | `3600` (60 min) |
| `session.autocompact_pct` | Context usage percentage at which auto-compaction triggers (5-90) | `90.0` |
| `session.pool_size` | Number of pre-spawned kiro-cli processes kept ready for instant session start. 0 disables | `0` |
| `session.pool_agent` | Agent for warm-pool processes. Empty uses `agent.default_agent` | `""` |
| `session.pool_ttl_secs` | Max age in seconds for pooled processes, discarded at claim time. 0 disables | `1800` |
| `session.archive_retention_days` | Days to keep compacted/rotated session archives before auto-cleanup. `-1` disables cleanup | `30` |
| `session.watchdog_rss_max_mb` | Recycle a session when its process tree resident memory exceeds this many MiB. 0 disables. A session with a turn in flight is never recycled | `0` |

### Dashboard

| Key | Description | Default |
|-----|-------------|---------|
| `dashboard.url` | Dashboard URL for remote access | `""` (localhost only) |
| `dashboard.restore_sessions` | Restore sessions on restart | `false` |
| `dashboard.restore_window_minutes` | Minutes after restart within which sessions can be restored | `30` |
| `dashboard.merge_queued_messages` | Concatenate follow-up messages while the agent is busy | `false` |
| `dashboard.mcp_probe_timeout_secs` | Seconds to wait for an MCP server handshake during a probe (5-120) | `15` |

### Slack

| Key | Description | Default |
|-----|-------------|---------|
| `slack.allowed_users` | User records (`{slack_id, name}`) recorded for Slack access | `[]` |
| `slack.tracking_channels` | Channels to monitor for new members | `[]` |
| `slack.open_channels` | Channel records retained in config | `[]` |
| `slack.command` | Slash-command name | `"kirocrew"` |
| `slack.reactions` | Override phase reaction emojis (set a value to `null` to suppress that phase) | `{}` |
| `slack.reactions_enabled` | Show phase reactions on Slack messages | `true` |

Only the owner (`KIROCREW_OWNER_ID`) is authorized to interact over Slack.
Multi-user access and open channels are refused regardless of what these lists
contain, so treat them as bookkeeping rather than an access grant.

### Speech-to-text

| Key | Description | Default |
|-----|-------------|---------|
| `stt.enabled` | Enable voice-memo transcription | `true` |
| `stt.provider` | `"whisper"` (local), `"mlx"` (local, Apple silicon), or `"transcribe"` (AWS, needs the `voice` extra) | `"whisper"` |
| `stt.streaming` | Stream partial transcripts live into the dashboard input. Transcribe provider only | `false` |
| `stt.transcribe_region` | AWS region for the Transcribe API (transcribe provider only) | `"us-east-1"` |
| `stt.language_code` | Language for speech recognition, e.g. `en-US`, `fr-FR` | `"en-US"` |

### Memory and embeddings

Embeddings are always on and run in-process through the bundled
llama-cpp-python runtime. There is no server to install and no way to disable
them, so there is no enable switch here: only knobs for *which* model runs.

| Key | Description | Default |
|-----|-------------|---------|
| `memory.embedding_provider` | Vector embedding backend. `"llama_cpp"` is the only accepted value; any other value in an existing config (including a legacy `"ollama"` or `"none"`) is coerced to it on load | `"llama_cpp"` |
| `memory.embedding_dim` | Output width of the embedding model in use. Must match a custom model's real width, or the load is refused | `1024` |
| `memory.embed_model_url` | Override HTTPS URL for the embedding-model GGUF download (mirrored or airgapped hosts). Empty uses the public Kiro Crew CDN. `KIROCREW_EMBED_MODEL_URL` wins over both. Downloads are sha256-verified regardless of source | `""` |
| `memory.embed_model_path` | Absolute path to a local GGUF to run **instead of** the bundled Qwen3-Embedding-0.6B. When set, the default model is never downloaded, so a custom model survives a default-model version change. Set `embedding_dim` to the model's output width. Changing the model changes the vector space, so stored embeddings are regenerated in the background. A configured-but-unreadable path fails closed (keyword search still works) rather than silently reverting to the default and re-embedding your corpus. Editable from the dashboard (Memory → Embedding Model). `KIROCREW_EMBED_MODEL_PATH` wins over this | `""` |
| `memory.embed_model_id` | Stable identifier for a custom model's vector space. Defaults to `custom:<filename>:<size>`, which cannot distinguish two different models of identical byte size, so set it explicitly if you swap between such models | `""` |
| `memory.semantic_confidence_threshold` | Minimum similarity score for a semantic search result | `0.8` |
| `memory.episodic_max_results` | Max episodic memories injected per session | `8` |
| `memory.episodic_max_count` | Max total episodic memories stored | `10000` |
| `memory.history_idle_hours` | Hours of inactivity before history consolidation | `3.0` |
| `memory.history_max_days` | Days of history to retain before pruning | `365` |

### Skills

| Key | Description | Default |
|-----|-------------|---------|
| `skills.max_triggered` | Maximum skills loaded per message (>=0) | `0` |
| `skills.lazy_load` | Inject only a usage-ranked top-K of on-demand skills at session start and leave the long tail discoverable via search, so a large skills set cannot crowd out memory and lessons | `false` |

### Knowledge Library

| Key | Description | Default |
|-----|-------------|---------|
| `knowledge.auto_ingest_artifacts` | Auto-ingest content-bearing local artifacts into the Knowledge Library as a searchable "Artifacts" source, kept in sync and removed when the artifact is deleted (see [Knowledge Library](knowledge-library-how-it-works.md)) | `true` |
| `knowledge.auto_ingest_artifact_kinds` | Artifact kinds eligible for auto-ingest. `widget` is excluded as UI rather than a document; `svg` is excluded because the file reader has no support for it | `["markdown", "text", "html", "json"]` |
| `knowledge.auto_add_documents` | Let the agent add documents it reads while working to the Knowledge Library (one aggregate "Auto-added" source). The agent fetches the content with its own tools under your approval; Kiro Crew fetches nothing, so `doc_ingest_hosts` does not apply. Renamed from `auto_ingest_doc_links`, which is still accepted on read | `true` |
| `knowledge.auto_register_project_docs` | Register the documents of each project you work in as a Knowledge source automatically. Documents only (`.md`/`.pdf`/`.docx`/`.org` above a size floor, excluding agent instructions, generated files and repository boilerplate) — never source code | `true` |
| `knowledge.auto_ingest_chunk_budget` | Chunks an automatically-registered source may ingest per watcher sweep. Each chunk is one LLM extraction call, so this bounds the cost; newest documents land first and the rest follow on later sweeps. 0 removes the bound | `150` |
| `knowledge.folder_ingest_chunk_budget` | Chunks a folder you add by hand may ingest per watcher sweep, including the first scan started by confirming the source. Nothing is skipped — newest files land first and the rest continue on later sweeps — so this paces spend rather than limiting what is ingested. Higher than the auto-ingest budget because you asked for the folder explicitly. 0 removes the bound; a per-source `chunk_budget` property overrides it for one folder | `300` |
| `knowledge.dedup_every_n_sweeps` | Run a full duplicate-collapsing pass every Nth watcher sweep (the per-write gate only catches byte-identical documents). 0 disables | `12` |
| `knowledge.auto_discover_folder` | Watch for a documents folder inside the active workspace and register it as a Knowledge source automatically, so files dropped there become searchable without adding the source by hand. The folder is never created for you, and deleting or pausing the auto-added source persists so it does not reappear on the next sweep. Off by default because ingestion spends LLM extraction on every supported file | `false` |
| `knowledge.auto_discover_dirname` | Folder name inside the workspace that auto-discovery looks for. A single path segment: separators and traversal are rejected so the source cannot be redirected outside the workspace. Avoid `knowledge`, which is where the Library's own store lives and always exists | `"knowledge-docs"` |

### Top level

| Key | Description | Default |
|-----|-------------|---------|
| `auto_update` | Enable automatic update checks | `true` |
| `timezone` | IANA timezone name, e.g. `"America/Los_Angeles"` | `""` (falls back to UTC) |
| `snapshot_dir` | Where `kirocrew snapshot` writes tarballs | `""` (`~/.kiro/crew/snapshots`) |

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `KIROCREW_HOME` | Override the config/data directory | `~/.kiro/crew` |
| `KIROCREW_PORT` | Override the dashboard port | `5476` |
| `KIROCREW_PROJECT_DIR` | Override the agent-config/skills project directory | Auto-detected |
| `KIROCREW_WORKSPACE` | Override the workspace root, used as-is with no subdirectory appended | Saved `workspace_dir`, else a platform default |
| `KIROCREW_SKIP_MODEL_DOWNLOAD` | Set to `1` to skip the background embedding-model download at gateway startup (tests, CI, airgapped hosts) | unset |
| `KIROCREW_EMBED_MODEL_URL` | Override HTTPS URL for the embedding-model GGUF; wins over `memory.embed_model_url` and the CDN default | unset |
| `KIROCREW_EMBED_MODEL_PATH` | Absolute path to a local GGUF to use instead of the bundled model; wins over `memory.embed_model_path` and suppresses the default download entirely | unset |

### Timezone

The `timezone` key affects three things:

- the `[CURRENT DATE]` line injected into every LLM prompt, so "today" is not
  ambiguous on a host whose system clock is UTC
- cron schedule display (`kirocrew cron list`, the Slack Home Tab)
- `skip_dates` evaluation for cron jobs

A per-job `timezone` on a cron job wins over this global value.

## Credentials

`~/.kiro/crew/.env` holds the Slack tokens and owner ID:

```
SLACK_APP_TOKEN=xapp-...
SLACK_BOT_TOKEN=xoxb-...
KIROCREW_OWNER_ID=UXXXXXXXX
```

## Denied Commands

The built-in destructive-command deny rules are enforced at Kiro Crew's own
PreToolUse gate, and are on by default. They are configurable from Settings →
Security: you can disable individual rules, disable them all, or add your own
patterns.

That opt-out state is **not** stored in `config.json`. It lives in a trust-root
file the agent itself cannot read or write, which is what makes the ceiling
un-disableable by the agent. An enterprise security policy can force-pin the
rules so they cannot be opted out of at all.

## File Locations

| Path | Purpose |
|------|---------|
| `~/.kiro/crew/config.json` | Main config |
| `~/.kiro/crew/config.local.json` | Local overrides that survive upgrades |
| `~/.kiro/crew/.env` | Slack credentials |
| `~/.kiro/crew/skills/` | User skills |
| `~/.kiro/crew/crons.json` | Scheduled jobs |
| `~/.kiro/crew/hooks.json` | Script hooks |
| `~/.kiro/crew/lessons.jsonl` | Learned corrections |
| `~/.kiro/crew/notifications.jsonl` | Notification history |
| `~/.kiro/crew/models/` | Embedding model, downloaded in the background at startup |
| `~/.kiro/crew/history/` | Chat history (JSONL) |
| `~/.kiro/crew/workspace/memory/` | Memory files |
| `~/.kiro/crew/session_map.json` | Session resume mapping |
| `~/.kiro/crew/snapshots/` | Default output of `kirocrew snapshot` |
| `~/.kiro/agents/kirocrew.json` | Installed agent config |
| `~/.kiro/settings/mcp.json` | Global MCP server config |
