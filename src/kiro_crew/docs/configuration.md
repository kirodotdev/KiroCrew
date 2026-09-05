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
kirocrew config defaults               # stored values holding a superseded default
```

Every config change is audit-logged to the security event log.

`config.local.json` holds overrides that survive an upgrade, which is what
`--local` writes to. Its values win over `config.json`.

The dashboard port is **not** a config key: set `KIROCREW_PORT` instead.

## When a Shipped Default Changes

`config.json` is written as a full materialization of the schema, so every key is
on disk even if you never set it — and a stored value always beats the shipped
default. Changing a default therefore reaches new installs only: yours keeps
whatever was written the last time it saved. On startup Kiro Crew prints one line
naming any key still holding an old default.

`kirocrew config defaults` shows each one with its stored value, the current
default, and the release that changed it. Two ways to answer it:

```bash
kirocrew config defaults --adopt       # take the current defaults
kirocrew config defaults --keep        # affirm your values, stop the notice
```

Both accept specific keys — `kirocrew config defaults --keep session.autocompact_pct`
if you chose 90 on purpose and want the rest adopted. `--adopt` removes the keys so
the current defaults apply from the next start; `--keep` records the exact values
you affirmed, so changing one later brings the notice back. `kirocrew doctor` lists
everything, affirmed values included.

The same command also clears a stored value Kiro Crew has to replace — a retired
`stt.provider` such as `whisper`, which already runs on `local`. That one cannot be
kept, because the stored name has no engine behind it; `--adopt` drops the dead
value and the notice with it.

## Sandbox

`agent.sandbox` controls whether Kiro Crew wraps the agent process in its own
OS-level sandbox (a user namespace on Linux, `sandbox-exec` on macOS).

| Value | Behavior |
|-------|----------|
| `auto` (default) | Add the Kiro Crew OS-level sandbox; on macOS it defers to the kiro-cli internal sandbox when that is enabled |
| `off` | Skip the Kiro Crew OS-level sandbox |

The two layers are mutually exclusive on macOS because a nested seatbelt sandbox fails with `EPERM`. The default is `auto`: it uses the Kiro Crew sandbox where available and defers to the kiro-cli internal sandbox on macOS when that sandbox is enabled.

Set via `kirocrew config set agent.sandbox auto`.

## ACP Backend

`agent.acp_backend` selects which ACP agent Kiro Crew drives. `agent.provider`
stays `acp` either way — the backend is a choice *within* ACP, not a different
provider.

| Value | Agent | Status |
|-------|-------|--------|
| `""` (default) | kiro-cli | full support |
| `kas` | kiro-agent (KAS) | runs chat; some surfaces still missing |

**What works on `kas`:** normal chat — your configured agent, its prompt, its tool
allowlist, and session resume. The context-usage percentage meter, compaction
(summarization) status, and agent-switch echoes are wired: KAS reports these as
`session/update` discriminants (`session_info_update` with a `context_usage` /
`turn_completion` / `summarization_*` kind, and `current_mode_update`) rather than
the separate `_kiro.dev/*` methods kiro-cli uses, and Kiro Crew maps them back to
the same displays.

**What does not, yet:**

- Native subagent progress reporting (subagents run; their live progress does not
  surface in the UI).
- Slash commands: KAS advertises them (`available_commands_update`), but Kiro Crew
  surfaces no available-commands UI for any backend (kiro-cli's
  `_kiro.dev/commands/available` is likewise unconsumed), and slash-command
  *execution* is not wired.
- Auto-approve (`allowedTools`) is not carried over, so KAS applies its own
  default approval policy.
- `spawn_continue` works for runs started with an explicit keep, but not for
  opportunistically-retained shared subagents.
- Model selection is unverified: KAS advertises no model list on an
  unauthenticated session, and Kiro Crew only sends a model the session
  advertised, so a session may simply run KAS's own default model.

**Signals with no KAS analog** (documented so they are not mistaken for gaps):
KAS has no `clear/status` notification, and its MCP methods (`_kiro/mcp/status`,
`_kiro/mcp/toggle`) are request-side only — it emits no MCP server-init
notification for Kiro Crew to surface. A resumable-session existence probe would
use KAS's `_kiro/session/list` (which returns the full `sessions[]` to search by
id); that is deferred to the session-lifecycle work, not the display path.


**KAS is served by kiro-cli's own ACP relay.** Kiro Crew spawns
`kiro-cli acp --agent-engine v3 --auth-method cli` and speaks ordinary ACP to it;
the relay forwards frames to KAS in both directions. Two consequences worth
knowing:

- **Credentials stay in kiro-cli.** `--auth-method cli` makes the relay resolve
  access tokens from kiro-cli's own store, so Kiro Crew never handles a KAS
  token. This works on any machine where `kiro-cli login` has succeeded; sign in
  with kiro-cli before switching.
- **No KAS assets to locate.** Kiro Crew does not read kiro-cli's extracted KAS
  bundle or its Node runtime, so there is nothing to point at and no override to
  set. What it does need is a kiro-cli new enough to offer `--agent-engine v3`;
  `kirocrew doctor` reports that when `agent.acp_backend` is `kas`.

An unrecognized value logs a warning and falls back to the default backend, so a
typo costs you a line in the log rather than a gateway that will not start.

Set via `kirocrew config set agent.acp_backend kas`.

## Key Settings

```json
{
  "agent": {
    "provider": "acp",
    "acp_backend": "",
    "approval_mode": "auto",
    "model": "auto",
    "reasoning_effort": "",
    "sandbox": "auto",
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
    "autocompact_pct": 70.0,
    "pool_size": 0,
    "pool_agent": "",
    "pool_ttl_secs": 1800
  },
  "dashboard": {
    "url": "",
    "restore_sessions": false,
    "restore_window_minutes": 30,
    "qr_session_until_restart": true,
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
    "auto_ingest_artifacts": false,
    "auto_add_documents": false,
    "auto_ingest_artifact_kinds": ["markdown", "text", "html", "json"],
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
| `agent.sandbox` | `"auto"` (use Kiro Crew OS-level sandbox, or defer to the kiro-cli internal sandbox on macOS) or `"off"` (skip the Kiro Crew sandbox) | `"auto"` |
| `agent.streaming` | Stream response text as it is generated | `true` |
| `agent.bot_name` | Custom name the bot identifies as | `""` |
| `agent.conductor_skill` | Enable agent delegation conductor | `false` |
| `agent.session_sharing` | Reuse a shared ACP runtime for subagents on the kiro-cli backend; alternate ACP backends ignore it | `true` |
| `agent.tool_search` | On the kiro-cli backend, defer MCP tool definitions when either threshold below is exceeded; alternate ACP backends ignore it | `true` |
| `agent.tool_search_min_pct` | Tool-definition context threshold as a percentage; `0` with the token threshold also `0` always defers | `5` |
| `agent.tool_search_min_tokens` | Tool-definition token threshold; `0` with the percentage threshold also `0` always defers | `50000` |
| `agent.fallback_model` | Model used after the active model exhausts its transient-retry budget. `"auto"` defers to availability-aware routing; `""` disables fallback | `"auto"` |
| `agent.max_channels` | Max concurrent agent channels (1-5) | `1` |
| `agent.max_channel_agents` | Max agents per channel (1-10) | `3` |
| `agent.log_level` | Persistent log level for the `kiro_crew` logger, applied at startup. The `--verbose` CLI flag overrides it | `"WARNING"` |
| `agent.soft_stop_budget_secs` | Seconds to wait for a cooperative cancel before hard-killing the session | `10.0` |
| `agent.max_subagents` | Max concurrent subagents. `0` auto-sizes the cap at startup from host memory/CPU and a learned per-agent cost. A pin of 1 or 2 is raised to 3, because a cap below 3 would disable auto-sizing and still run under the default | `0` |
| `agent.subagent_max_turns` | Default tool-call budget per subagent | `100` |
| `agent.spawn_min_memory_gb` | Minimum available memory (GB) to spawn a subagent (0 disables the check) | `4.0` |
| `agent.completion_keep` | Which end of the subagent transcript to keep in the completion event injected into the parent session: `"head"`, `"tail"`, or `"both"` (head + middle marker + tail) | `"head"` |
| `agent.completion_keep_chars` | Max characters retained in the completion event after applying `completion_keep`. `0` disables truncation. The full transcript stays on disk (see `subagent_result_ttl_secs`) | `3000` |
| `agent.subagent_result_ttl_secs` | How long a delivered subagent's `result.txt` is retained before the reaper prunes it, so the parent can read the full transcript on demand instead of re-running the subagent. Measured from the moment the completion reaches the parent, not from when the run finished | `3600` (1h) |

### Session

| Key | Description | Default |
|-----|-------------|---------|
| `session.timeout_secs` | Idle session timeout in seconds (0 disables the idle sweep) | `3600` (60 min) |
| `session.empty_response_auto_continue` | After two consecutive empty model responses, send one transcript-visible `continue` nudge per user message | `true` |
| `session.autocompact_pct` | Context usage percentage at which auto-compaction triggers (5-90). Lower compacts sooner and keeps per-turn cost down; higher retains more conversation before rewriting it. Applies to new installs: an existing `config.json` keeps its stored value | `70.0` |
| `session.pool_size` | Number of pre-spawned kiro-cli processes kept ready for instant session start. 0 disables | `0` |
| `session.pool_agent` | Agent for warm-pool processes. Empty uses `agent.default_agent` | `""` |
| `session.pool_ttl_secs` | Max age in seconds for pooled processes, discarded at claim time. 0 disables | `1800` |
| `session.eager_spawn` | Create a chat session when its slot is created, switched, or retargeted instead of waiting for the first message | `true` |
| `session.archive_retention_days` | Days to keep compacted/rotated session archives before auto-cleanup. `-1` disables cleanup | `30` |
| `session.watchdog_rss_max_mb` | Recycle a session when its process tree resident memory exceeds this many MiB. 0 disables. A session with a turn in flight is never recycled | `0` |

### Dashboard

| Key | Description | Default |
|-----|-------------|---------|
| `dashboard.url` | Dashboard URL for remote access | `""` (localhost only) |
| `dashboard.restore_sessions` | Restore sessions on restart | `false` |
| `dashboard.restore_window_minutes` | Minutes after restart within which sessions can be restored | `30` |
| `dashboard.qr_session_until_restart` | Keep a phone signed in for as long as the gateway process runs. Ordinary idling no longer signs it out; a gateway restart does, and so does going 30 days untouched (the refresh credential's lifetime, renewed on each visit). Turn off for a timed session that expires on a clock whether or not the gateway is still running. | `true` |
| `dashboard.merge_queued_messages` | Concatenate follow-up messages while the agent is busy | `false` |
| `dashboard.mcp_probe_timeout_secs` | Seconds to wait for an MCP server handshake during a probe (5-120) | `15` |
| `dashboard.link_previews` | Fetch and render HTTP(S) link metadata in assistant messages. Off by default because each linked site receives a request from this machine | `false` |

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

Other channels (Discord, Telegram, Teams, Webex, WeCom, WeChat) are configured
from the dashboard — see each channel's doc for keys and credentials.

### Speech-to-text

Speech-to-text is on by default and runs on your machine. Dictate into the
dashboard composer, and voice notes that arrive over a messaging channel are
transcribed the same way.

| Key | Description | Default |
|-----|-------------|---------|
| `stt.enabled` | Turn spoken input into text you can send | `true` |
| `stt.provider` | `"local"` (this machine, no account), `"apple"` (the on-device recognizer built into macOS 26 and later), or `"transcribe"` (AWS Transcribe, which bills your AWS account) | `"local"` |
| `stt.model` | Which speech model the local provider downloads and runs: `tiny`, `base`, `small`, or `large-v3-turbo`. Bigger is more accurate and a longer first-time download | `"base"` |
| `stt.language_code` | Language for speech recognition, e.g. `en-US`, `fr-FR` | `"en-US"` |
| `stt.streaming` | Show words in the message box while you are still speaking rather than only once you stop. Every provider supports it; turning it off spends less CPU on `local` and fewer API calls on `transcribe` | `true` |
| `stt.silence_ms` | How long a pause must last before what you said is treated as a finished phrase. Raise it if you are being cut off mid-sentence, lower it if the text lags behind you. A value outside 200-5000 ms is clamped into that range, because a shorter pause than that falls between two ordinary words | `700` |
| `stt.partial_interval_ms` | How often the live transcript is refreshed while you speak. Lower feels more immediate and costs a little more CPU per second of speech; higher is steadier to read. A value outside 100-5000 ms is clamped into that range | `400` |
| `stt.idle_evict_secs` | How long the local model stays in memory after your last recording. It holds roughly 150 MB at the default model and reloads in a fraction of a second, so lower this on a machine short of memory. `0` releases it as soon as you stop speaking | `600` |
| `stt.endpointing` | While dictating, judge each finished phrase with a fast background model and send the message once it reads as a complete request, without you pressing anything. Needs `streaming` | `false` |
| `stt.dictation_panel` | Show the animated dictation panel while recording instead of the thin status bar. Ignored when the browser lacks WebGL2 or the OS asks for reduced motion, both of which fall back to the bar | `true` |
| `stt.timeout_secs` | Ceiling on transcribing one whole file: the audio decode, and each model load or recognition inside it | `300` |
| `stt.transcribe_region` | AWS region for the Transcribe API (`transcribe` provider only) | `"us-east-1"` |
| `stt.transcribe_profile` | AWS profile for the Transcribe API. Empty uses the default credential chain (`transcribe` provider only) | `""` |

#### The local provider downloads one model, once

Recognition needs weights, and they are too large to ship inside the package, so
the first time you dictate Kiro Crew fetches the model named by `stt.model` and
every session after that loads it from disk. `base` is 148 MB. The others are
78 MB (`tiny`), 488 MB (`small`) and 1.6 GB (`large-v3-turbo`). The dashboard says
a download has started before it begins and reports its progress, because a silent
transfer of that size is indistinguishable from a hang.

Every download is verified against a pinned sha256 digest and is only moved into
place once the digest matches, so a tampered mirror, a truncated transfer or a
captive-portal login page cannot become your speech model. Weights live under
`models/whisper/` in the data home. Deleting one just costs you the download
again.

Desktop users install nothing else by hand: the app already carries the
recognizer, decoder, and AWS client. In a source environment the recognizer and
AWS client are the optional `voice` extra, installed as its own dependencies
(`pip install 'boto3>=1.34,<2' 'amazon-transcribe>=0.6,<1' 'pywhispercpp>=1.5,<2'`):

- **Intel Macs have no prebuilt recognizer.** Every other platform Kiro Crew
  supports (Apple silicon macOS, glibc and musl Linux on x86_64 and arm64, and
  Windows) installs a ready-built wheel. On an Intel Mac `pip` falls back to
  building from source, which needs a C++ toolchain and CMake. Settings reports
  that as its own state rather than as a missing extra, because the two need
  different fixes.

  If you would rather not build it, installing only the cloud half
  (`pip install 'boto3>=1.34,<2' 'amazon-transcribe>=0.6,<1'`) gets you the AWS
  Transcribe client on its own. `pip` resolves an extra all-or-nothing, so on a
  platform without the recognizer wheel the full `voice` extra installs *nothing* —
  including the Transcribe client, which has no such limitation. This is the way to
  get the paid provider on a host that cannot build the free one.
- Compressed audio still passes through ffmpeg internally: a voice note arrives
  as ogg/Opus and a browser recording as webm. Desktop releases bundle and verify
  a pinned decoder, so there is no separate FFmpeg installation step. Source
  environments use a system FFmpeg from the fixed platform paths — never an
  executable inside an agent-writable project venv — and where the host packages
  none, **Settings > Voice offers a one-click decoder download** that fetches the
  same pinned upstream bytes into `<data home>/models/ffmpeg/` and verifies them
  against a built-in SHA-256 digest before anything is executed. The digest is the
  trust anchor, so `~/.local/bin` is still not a place a decoder can be installed
  for Kiro Crew's use. If that download fails, the page offers to hand the failure
  to a chat session, which is given the host details and the trusted locations.

#### Retired providers

The `whisper`, `mlx`, `parakeet` and `faster` providers are gone. Each of them
needed a runtime you had to install yourself (a `whisper` command on `PATH`, or an
`mlx-whisper` / `parakeet-mlx` / `faster-whisper` package), which is exactly the
work `local` removes while recognizing the same speech. On Apple silicon the GPU
acceleration that `mlx` existed for is already in the bundled recognizer.

A config that still names one keeps working: it is read as `local`, and the
gateway log says which value it replaced. The settings those providers used
(`whisper_path`, `mlx_model`, `parakeet_model`, `device`) are ignored if they are
still present, so there is nothing you have to remove by hand.

### Paid AWS services need an explicit confirmation

Two providers reach a **paid** AWS service: `voice_reply.provider: "polly"`
(text-to-speech) and `stt.provider: "transcribe"` (speech-to-text). Selecting
one is not enough to start spending — neither sends a request until you confirm
it in **Settings > Voice**, and the confirmation names the AWS account it
resolves to first.

Three things worth knowing:

- **An empty profile is not "no account".** With `aws_profile` /
  `transcribe_profile` unset, nothing is passed to the provider and its own
  default credential chain resolves — environment variables, the shared config's
  `default` profile, or container/instance metadata. The confirmation shows you
  which account that turns out to be.
- **A confirmation is tied to the profile, region and account it was given for.**
  Changing the profile or region asks again, and the live account is re-checked
  before each call: if the profile is later repointed at a different AWS account,
  the call is refused and the confirmation withdrawn.
- **The check needs to be able to run.** If the account cannot be resolved, the
  call is refused rather than allowed, so an outage withholds a paid request
  instead of risking an unconfirmed charge.

The record lives in `aws_service_consent.json` in the data home rather than in
`config.json`, because it is an authorization rather than a preference: it is on
the read+write keystone floor, so an agent can neither read it nor grant itself
permission to spend. The authenticated dashboard is the only writer — there is
deliberately no CLI verb, because a terminal command that records a grant on
request is a grant an automated caller can take.

Both local defaults (`piper` for TTS, `local` for STT) need no AWS account and no
confirmation.

### Memory and embeddings

Embeddings are always on and run in-process through the bundled
llama-cpp-python runtime. There is no server to install and no way to disable
them, so there is no enable switch here: only knobs for *which* model runs.

| Key | Description | Default |
|-----|-------------|---------|
| `memory.embedding_provider` | Vector embedding backend. `"llama_cpp"` is the only accepted value; any other value in an existing config (including a legacy `"ollama"` or `"none"`) is coerced to it on load | `"llama_cpp"` |
| `memory.embedding_dim` | Output width of the embedding model in use. Must match a custom model's real width, or the load is refused | `1024` |
| `memory.embedding_threads` | CPU threads llama.cpp may use per embedding call; clamped to the machine core count | `4` |
| `memory.embed_model_url` | Override HTTPS URL for the embedding-model GGUF download (mirrored or airgapped hosts). Empty uses the public Kiro Crew CDN. `KIROCREW_EMBED_MODEL_URL` wins over both. Downloads are sha256-verified regardless of source | `""` |
| `memory.embed_model_path` | Absolute path to a local GGUF to run **instead of** the bundled Qwen3-Embedding-0.6B. When set, the default model is never downloaded, so a custom model survives a default-model version change. Set `embedding_dim` to the model's output width. Changing the model changes the vector space, so stored embeddings are regenerated in the background. A configured-but-unreadable path fails closed (keyword search still works) rather than silently reverting to the default and re-embedding your corpus. Editable from the dashboard (Memory → Embedding Model). `KIROCREW_EMBED_MODEL_PATH` wins over this | `""` |
| `memory.embed_model_id` | Stable identifier for a custom model's vector space. Defaults to `custom:<filename>:<size>`, which cannot distinguish two different models of identical byte size, so set it explicitly if you swap between such models | `""` |
| `memory.semantic_confidence_threshold` | Minimum similarity score for a semantic search result | `0.8` |
| `memory.episodic_dedup_threshold` | Similarity threshold for deduplicating episodic memories | `0.88` |
| `memory.episodic_max_results` | Max episodic memories injected per session | `8` |
| `memory.episodic_max_count` | Max total episodic memories stored | `10000` |
| `memory.decay_rates` | Per-tag episodic recency decay rates, per day (score factor `exp(-rate * days_old)`). Keys are memory tags (case-insensitive); the reserved `default` key replaces the built-in `0.03` for memories matching no configured tag. A memory carrying several configured tags uses the slowest (smallest) rate, so a broad tag can never age out a long-retention one. `0` never ages out of retrieval ranking; `1` falls out of retrieval within about a day. Ranking only: `episodic_max_count` cap eviction (lowest importance, then oldest) still applies regardless of decay rate. Values are clamped to `0..10`; non-numeric values are ignored with a logged warning. Example: `{"legal_precedents": 0.0, "trading_data": 1.0}` | `{}` |
| `memory.history_idle_hours` | Hours of inactivity before history consolidation | `3.0` |
| `memory.history_max_days` | Days of history to retain before pruning | `365` |

### Skills

| Key | Description | Default |
|-----|-------------|---------|
| `skills.max_triggered` | Maximum skills loaded per message (>=0) | `0` |
| `skills.lazy_load` | Inject only a usage-ranked top-K of on-demand skills at session start and leave the long tail discoverable via search, so a large skills set cannot crowd out memory and lessons | `false` |

### MCP Gateway

| Key | Description | Default |
|-----|-------------|---------|
| `mcp_gateway.enabled` | Share one MCP backend process between sessions with identical server configuration. Opt-in; when false each session owns its backend | `false` |

### Session summaries

| Key | Description | Default |
|-----|-------------|---------|
| `session_summary.enabled` | Generate intent-level summaries for the chat side panel after turns. This consumes model tokens; unchanged sessions are served from cache | `false` |

### Knowledge Library

| Key | Description | Default |
|-----|-------------|---------|
| `knowledge.auto_ingest_artifacts` | Auto-ingest content-bearing local artifacts into the Knowledge Library as a searchable "Artifacts" source, kept in sync and removed when the artifact is deleted (see [Knowledge Library](knowledge-library-how-it-works.md)). Opt-in: enabling it backfills the artifacts you already have | `false` |
| `knowledge.auto_ingest_artifact_kinds` | Artifact kinds eligible for auto-ingest. `widget` is excluded as UI rather than a document; `svg` is excluded because the file reader has no support for it | `["markdown", "text", "html", "json"]` |
| `knowledge.max_ingest_file_mb` | Per-file Knowledge Library ingestion size cap; oversized files are skipped. `0` disables the cap | `100.0` |
| `knowledge.auto_add_documents` | Let the agent add documents it reads while working to the Knowledge Library (one aggregate "Auto-added" source). The agent fetches the content with its own tools under your approval; Kiro Crew fetches nothing, so `doc_ingest_hosts` does not apply. Renamed from `auto_ingest_doc_links`, which is still accepted on read | `false` |
| `knowledge.folder_ingest_chunk_budget` | Chunks a folder you add by hand may ingest per watcher sweep, including the first scan started by confirming the source. Nothing is skipped — newest files land first and the rest continue on later sweeps — so this paces spend rather than limiting what is ingested. 0 removes the bound; a per-source `chunk_budget` property overrides it for one folder | `300` |
| `knowledge.dedup_every_n_sweeps` | Run a full duplicate-collapsing pass every Nth watcher sweep (the per-write gate only catches byte-identical documents). 0 disables | `12` |
| `knowledge.extraction_pool_size` | Concurrent LLM workers for document extraction; requires restart | `3` |
| `knowledge.embed_rate_limit` | Maximum embedding generations per minute across all sources. `0` removes the bound | `120` |
| `knowledge.sweep_chunk_budget` | Maximum chunks ingested across all sources in one watcher sweep. `0` removes the bound | `500` |

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

`~/.kiro/crew/.env` holds messaging-channel credentials and the owner ID. For
Slack:

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
