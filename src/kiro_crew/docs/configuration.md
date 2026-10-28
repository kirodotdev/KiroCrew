# Configuration Reference

Everything Kiro Crew remembers about how it should behave lives in one JSON file,
`~/.kiro/crew/config.json`, created automatically on the first `kirocrew gateway`
run. Most keys are also editable from the dashboard's Settings pages, and this
page covers the most-used keys that are not: what they mean, what they default
to, and which environment variables outrank them. It does not list every key.
The full set is declared in the config schema (`src/kiro_crew/config/sections.py`
and its sibling section modules), which the gateway serves at
`GET /api/config/schema` and which **Developer → Config** renders.

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

Both files persist across gateway restarts and upgrades; nothing regenerates
`config.json`. `config.local.json` is the overlay `--local` writes to: its values
win over `config.json`, so put a value there when you want it pinned above
whatever the dashboard or `config set` later writes.

The dashboard port is **not** a config key: set `KIROCREW_PORT` instead.

## When a Shipped Default Changes

`config.json` is written as a full materialization of the schema, so every key is
on disk even if you never set it — and a stored value always beats the shipped
default. Changing a default therefore reaches new installs only: yours keeps
whatever was written the last time it saved.

Kiro Crew fixes that for itself on three keys: the two agent timeout budgets (the
subagent timeout and the chat-turn ceiling) and the subagent memory floor
(`agent.spawn_min_memory_gb`, whose old `4.0` kept subagents from starting on a
16 GB laptop). On the first start after an upgrade, a stored value that is exactly
an old shipped default is removed so the current default applies, in that same run. It happens once per key: set one back afterwards and it stays yours.
Affirming a value with `--keep` before that first start also keeps it.

One more key is fixed once, for installs upgrading straight from 0.6.x or earlier:
those releases stored `skills.lazy_load: false`, which then meant the full skills
listing. In current builds `false` selects the short entry that names only eight skills, so on
the first start after such an upgrade the stored `false` is removed and the default
skill index applies. An install that has already run any 0.7 build or later,
insider builds included, keeps its value.
To choose the short entry afterwards: `kirocrew config set skills.lazy_load false`.

Everything else is reported, not changed, because a stored value can be a real
choice: `stt.streaming: false` is how you turn live dictation text off, and on disk
that is identical to the old default. On startup Kiro Crew prints one line naming any
key still holding an old default.

`kirocrew config defaults` shows each one with its stored value, the current
default, and the release that changed it. Two ways to answer it:

```bash
kirocrew config defaults --adopt       # take the current defaults
kirocrew config defaults --keep        # affirm your values, stop the notice
```

Both accept specific keys — `kirocrew config defaults --keep session.autocompact_pct`
if you chose 90 on purpose and want the rest adopted. `--adopt` removes the keys,
and a running gateway picks up the current defaults live; the command asks for a
restart only for a key that needs one, and names it. `--keep` records the exact
values you affirmed, so changing one later brings the notice back. `kirocrew doctor` lists
everything, affirmed values included.

The same command also clears a stored value Kiro Crew has to replace — a retired
`stt.provider` such as `whisper`, which already runs on `local`. That one cannot be
kept, because the stored name has no engine behind it; `--adopt` drops the dead
value and the notice with it.

## Sandbox

`agent.sandbox` controls whether Kiro Crew wraps the agent process in its own
OS-level sandbox (a user namespace on Linux, `sandbox-exec` on macOS), and how
much of your home directory that sandbox hides from the agent's subprocesses.

| Value | Behavior |
|-------|----------|
| `auto` (default) | Add the Kiro Crew OS-level sandbox at the **standard** tier; on macOS it defers to the kiro-cli internal sandbox when that is enabled |
| `strict` | Add the Kiro Crew OS-level sandbox at the **strict** tier: everything `standard` hides, plus `~/.aws` (including `~/.aws/sso/cache`, kiro-cli's grant store for OAuth-connected remote MCP servers), `~/.ssh` (only `known_hosts` stays readable), `~/.kube`, `~/.config/gh`, and the credential files `~/.npmrc`, `~/.pypirc`, `~/.netrc`, `~/.git-credentials` |
| `off` | Skip the Kiro Crew OS-level sandbox |

**What the default leaves visible, and why.** The standard tier hides
`~/.gnupg`, `~/.docker`, `~/.azure`, `~/.config/gcloud`, the crew secret vault
and the governance cache. It deliberately does **not** hide `~/.aws`, `~/.ssh`
or `~/.kube`: the `aws` CLI, boto3 and `credential_process`, git-over-SSH and
`kubectl` all read those directories, and an agent that cannot reach them
cannot debug or deploy the way you would. Kiro Crew's file tools still refuse to
open paths under them, and the AWS/SSH environment-variable, SDK and
exfiltration command shapes are denied at the tool gate, but a plain shell read
(`cat ~/.aws/credentials`) is not fenced at this tier — the OS sandbox is the
enforcement point, and standard does not seal that directory.

**When to use `strict`.** Set it when the host holds credentials the agent must
never read, and you accept that inside the agent the `aws` CLI, boto3,
git-over-SSH, `gh`, `kubectl`, npm/pip registry auth (`~/.npmrc`, `~/.pypirc`),
`.netrc` HTTPS auth, the git credential store (`~/.git-credentials`) and
OAuth-connected remote MCP servers (their grants live in `~/.aws/sso/cache`)
stop working — they cannot see their config, keys or tokens, even though the
gateway-side Connections page, which reads your real home, still shows the grant.
It is opt-in and per host; nothing changes for you until you set it.
Like every value of this key, a change applies to sessions started after it: a
session already running keeps the tier it was spawned with until it ends, so
restart the sessions (or the gateway) you want confined at the new tier.
`strict` only tightens where Kiro Crew's own sandbox is what confines the
spawn: on Windows there is no OS backend, and a macOS spawn delegated to
kiro-cli's internal sandbox is confined by that profile instead.

The two layers are mutually exclusive on macOS because a nested seatbelt sandbox fails with `EPERM`. The default is `auto`: it uses the Kiro Crew sandbox where available and defers to the kiro-cli internal sandbox on macOS when that sandbox is enabled.

Set via `kirocrew config set agent.sandbox auto` (or `strict`, or `off`).

## ACP Backend

`agent.acp_backend` selects the ACP harness Kiro Crew drives. `agent.provider`
stays `acp` either way — the backend is a choice *within* ACP, not a different
provider.

| Value | Harness | Notes |
|-------|---------|-------|
| absent or `""` | Kiro Agent (KAS) | No choice: the default. Falls back to kiro-cli when KAS is unavailable. |
| `kas` | Kiro Agent (KAS) | Served through kiro-cli's `acp --agent-engine v3` relay. |
| `kiro` | kiro-cli | The Kiro CLI choice. kiro-cli now serves its sessions on its v3 (KAS) engine, so once Kiro Crew confirms your kiro-cli offers v3 this choice runs through KAS, and the dashboard offers one click to save `kas`. On a kiro-cli too old for v3 it keeps the older engine, and `kirocrew doctor` names `kiro-cli update`. |
| `claude` | Claude Code | Uses the public `claude-agent-acp` adapter. |
| `codex` | Codex | Uses the Codex ACP adapter. |
| `opencode` | OpenCode | Uses OpenCode's native ACP server. |
| `pi` | Pi | Uses `pi-acp` and its Pi gate extension. |
| `goose` | goose | Uses goose's native ACP server. |
| `deepseek` | DeepSeek Harness | Runs `dsh --profile acp` with Kiro Crew's gate plugin; provider keys come from `agent.deepseek_env`. |
| `copilot` | GitHub Copilot CLI | Runs `copilot --acp --stdio`; signs in with its own login. A project carrying Copilot CLI's own hooks, settings or MCP config is refused. |

Codex runs with `~/.aws/credentials` and `~/.aws/sso/cache` hidden on purpose,
and only `~/.aws/config` readable. Codex on Amazon Bedrock therefore needs a
self-contained `credential_process` in that file, not SSO or static keys. If a
Codex session fails to start with an authentication error, the error says this
too.

- Keep static keys in `~/.aws/credentials`. Do not write them into `~/.aws/config`.
- Do not make `~/.aws/config` a symlink. On Linux the sandbox copies whatever
  the link points at, so a link to a credential file would expose it.

The non-default harnesses are offered only when this build registers them. A
host governance policy can narrow that list further, and the dashboard reports
missing harness components with their install command. `kirocrew doctor` reports
backend-specific setup failures. An unselectable or unrecognized value logs a
warning and falls back to the default backend.

KAS is not a separate executable: Kiro Crew starts a sufficiently recent
kiro-cli ACP relay. When Kiro Crew owns KAS authentication, the relay asks the
gateway for access tokens and the encrypted refresh token stays in Kiro Crew;
otherwise `--auth-method cli` uses kiro-cli's existing login. A sign-in or
sign-out takes effect on the next KAS process.

When the gateway starts with KAS as the selected main or member backend, it runs the
same `kiro-cli acp --help` check `kirocrew doctor` reports, in the background, so
startup does not wait for it. If that kiro-cli has no
`--agent-engine` flag, or does not offer engine `v3`, Kiro Crew logs a warning and
runs kiro-cli instead; the dashboard's KAS row lists `kiro-cli acp --agent-engine
v3` as missing. `config.json` keeps `kas`, so updating kiro-cli and restarting
returns to KAS. A check that cannot run leaves KAS selected.

KAS can also run with **no kiro-cli at all**. `kirocrew kas install` downloads
the `@kiro/agent` version this Kiro Crew release pins, plus its own Node runtime,
from a sha256-pinned manifest (`agent.kas_bundle_url`) into
`<data home>/engines/kas/<version>/`. Each Kiro Crew release runs exactly one
engine version; after a Kiro Crew update run `kirocrew kas install` again. And
`agent.kas_bundle: true` makes the `kas` backend spawn that engine directly. On
this route Kiro Crew is always the credential owner: sign in from the dashboard
(Sign in) — kiro-cli's login is not consulted. The chat model picker lists the
models the engine serves, with their credit multipliers, once a chat has started
on this route (the engine reports them when a session opens; before that the
picker offers `auto`). `kirocrew kas status` shows what is
installed and whether the route is on; `kirocrew doctor` reports it under
"KAS backend". The engine directory is read-only inside the agent sandbox and
fenced from the agent's file tools, so a session cannot rewrite the code its next
session runs; only the install command (your own terminal) writes there.

Harness capabilities differ: agent-spec projection, MCP transport, model
selection, permission routing, resume, compaction, and subagent continuation are
not inferred from the harness name. See [Agent Spec Field
Reference](agent-spec-fields.md) for the per-field behavior and the [agent host
contract](../../../docs/system-specs/modules/agent-host-contract.md) for the
per-harness capability matrix.

Set a registered value with, for example,
`kirocrew config set agent.acp_backend kiro`. `agent.member_acp_backend`, the
harness for crew-member DM sessions, reads the same values, except that `kiro`
there selects KAS: crewmates run on KAS as their Kiro backend, because their
work-dispatch tools need a backend that takes them per session.

One crewmate can run on its own harness: `agents.<name>.acp_backend` takes the
same values as `agent.acp_backend` (`kiro` here means kiro-cli, and runs through
KAS once Kiro Crew confirms your kiro-cli offers v3, as it does there) and applies to
that crewmate's chat and the worker sessions it opens. Empty inherits
`agent.member_acp_backend`. A harness this gateway cannot start refuses the
crewmate's session with `crewmate_harness_unavailable` rather than running it on
another harness, and the crewmate's `model` and `worker_model` are judged against
the pinned harness when they change or the harness does, so an edit to another
field still saves while the pinned harness cannot start. The dashboard shows the
setting as **Agent harness** on a crewmate's profile Settings page once Settings >
Developer > Feature Previews > Agent harness per mate is on; a saved pin applies
whether or not that preview is on.

## Key Settings

```json
{
  "agent": {
    "provider": "acp",
    "approval_mode": "auto",
    "model": "auto",
    "reasoning_effort": "",
    "sandbox": "auto",
    "bot_name": "",
    "max_channels": 1,
    "max_channel_agents": 3,
    "max_subagents": 0,
    "subagent_max_turns": 1000,
    "spawn_min_memory_gb": 2.0,
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
    "provider": "local",
    "streaming": true,
    "transcribe_region": "us-east-1",
    "language_code": "auto"
  },
  "memory": {
    "embedding_provider": "llama_cpp",
    "embedding_dim": 1024,
    "history_idle_hours": 3.0,
    "history_max_days": 365,
    "persistence_enabled": true,
    "inject_memory": true,
    "inject_lessons": true,
    "inject_activity": true
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
| `agent.crew_mode_agent` | The conductor template a chat runs when Crew Mode is turned on. Empty falls back to the default. Takes effect live for new chats; no gateway restart | `"kirocrew-conductor"` |
| `agent.kas_bundle` | Run the `kas` backend from Kiro Crew's own installed engine bundle (`kirocrew kas install`) with its bundled Node, instead of kiro-cli's ACP relay. kiro-cli is not needed; the engine signs in through Kiro Crew's own login only. `true` with nothing installed refuses the spawn and names the install command | `false` |
| `agent.kas_persist_mcp_sign_in` | Keep a remote MCP server's OAuth sign-in across `kas` sessions, subagents, crons and gateway restarts. Kiro Crew stores the server's client registration and tokens in a dedicated encrypted vault under the data home (`kas/mcp-grants/`); it is not the Secrets vault, is not listed there, cannot be granted to a cron, and the agent cannot read it. `false` keeps each sign-in in the engine's memory, so every new session asks again. Takes effect when a KAS process starts | `false` |
| `agent.kas_bundle_url` | `https://` base `kirocrew kas install` fetches the engine manifest from (`<base>/kas/<version>/manifest.json`). Every artifact is sha256-pinned by the manifest and the manifest by the release, so this chooses where the pinned build is fetched from, never what is trusted. Non-https values fall back to the default | the project's release bucket |
| `agent.native_skill_projection` | Launch each `kiro` backend agent under a Kiro Crew managed view (`kirocrew-skill-view-*.json` in the kiro agents directory) that serves skills through Kiro Crew's skill search, with `.kiro/settings/cli.json` in the workspace overlaid while it runs. `false` launches agents under their authored names, writes no view, rolls the workspace overlay back, and lets kiro-cli load skill metadata natively. Turn it off when you also run kiro-cli on its own against the same agents directory. `KIROCREW_NATIVE_SKILL_PROJECTION=0` disables it regardless of this value. Takes effect when a native session is next spawned | `true` |
| `agent.deepseek_env` | Provider keys handed to the DeepSeek Harness (`agent.acp_backend: deepseek`) as environment variables at spawn: a map of environment-variable NAME to a `secret://<vault name>` reference. Store the key under Settings → Secrets first, then map it — e.g. `{"DEEPSEEK_API_KEY": "secret://my-dsh-key"}`. Any provider name the harness knows works. A plaintext value is refused at write time — the `config set` itself fails, so no key is ever stored in `config.json` — and, at spawn, so is a name the harness would forward to its own shell children, a name Kiro Crew sets itself, or one Kiro Crew's agent environment scrub strips: the session is refused before it starts, naming the offending key. Empty means no key — a locally served model needs none. Other backends ignore it | `{}` |
| `agent.approval_mode` | `"auto"` or `"interactive"` | `"auto"` |
| `agent.model` | Default LLM model for new sessions. `"auto"` defers to the agent config, then to Kiro's own default. Editable from Settings → Chat → Model; a per-session model picker overrides it for that session only | `"auto"` |
| `agent.reasoning_effort` | Default reasoning effort on models that support it. One of `""`, `low`, `medium`, `high`, `xhigh`, `max`; `""` defers to the provider/model default. A per-session override wins | `""` |
| `agent.sandbox` | `"auto"` (Kiro Crew OS-level sandbox at the standard tier, which leaves `~/.aws`/`~/.ssh`/`~/.kube` visible for credential tooling; defers to the kiro-cli internal sandbox on macOS), `"strict"` (also hides `~/.aws` incl. `sso/cache`, `~/.ssh` bar `known_hosts`, `~/.kube`, `~/.config/gh`, `~/.npmrc`, `~/.pypirc`, `~/.netrc`, `~/.git-credentials`), or `"off"` (skip the Kiro Crew sandbox). Applies to sessions started after the change. See [Sandbox](#sandbox) | `"auto"` |
| `agent.streaming` | Stream response text as it is generated | `true` |
| `agent.bot_name` | Custom name the bot identifies as | `""` |
| `agent.session_sharing` | Reuse a shared ACP runtime for subagents on the kiro-cli backend; alternate ACP backends ignore it | `true` |
| `agent.tool_search` | Defer MCP tool definitions so the model loads them on demand with `tool_search`. kiro-cli defers once either threshold below is exceeded; On KAS, an agent whose `tools` grants `tool_search` has all of its MCP tools deferred and loaded through KAS's own `tool_load`; an agent that grants no `tool_search` keeps them direct, because Kiro Crew lists each of its MCP tools in KAS's never-defer list (a whole-server `@server` grant is expanded from the tool list Kiro Crew already holds for that server, and a server it holds none for stays deferred). KAS ignores this toggle and both thresholds. Other ACP backends ignore it. Kiro Crew's own MCP servers defer only when the spawn runs the pinned kiro-cli install (or its `kiro-cli-chat`) at 2.27.0 or later; any other executable, or an older or unknown version, keeps them resident. Setting `ASBX_KIRO_MANDATORY_MCPS` (comma-separated server names) in the gateway's environment overrides that never-defer list | `true` |
| `agent.tool_search_min_pct` | Tool-definition context threshold as a percentage; `0` with the token threshold also `0` always defers | `5` |
| `agent.tool_search_min_tokens` | Tool-definition token threshold; `0` with the percentage threshold also `0` always defers | `50000` |
| `agent.fallback_model` | Model used after the active model exhausts its transient-retry budget. `"auto"` defers to availability-aware routing; `""` disables fallback | `"auto"` |
| `agent.refusal_fallback_model` | Model one declined message is retried on when the active model's content filter refuses it (single-message; the primary returns on the next turn). `"auto"` uses the model the provider's refusal recommends; `""` disables the retry | `""` |
| `agent.max_channels` | Max concurrent agent channels (1-5) | `1` |
| `agent.max_channel_agents` | Max agents per channel (1-10) | `3` |
| `agent.log_level` | Persistent log level for the `kiro_crew` logger, applied at startup. The `--verbose` CLI flag overrides it | `"WARNING"` |
| `agent.soft_stop_budget_secs` | Seconds to wait for a cooperative cancel before hard-killing the session | `10.0` |
| `agent.max_subagents` | Max concurrent subagents. `0` = auto: the `agent.subagent_auto_max` ceiling (32), with free memory bounding each start through `agent.spawn_min_memory_gb` long before that on most hosts (sized from host memory when `agent.spawn_min_memory_gb` is `0`, 3 if host memory cannot be read). A pin of 1 or 2 is raised to 3, the floor an explicit cap keeps | `0` |
| `agent.subagent_max_turns` | Default tool-call budget per subagent; stored user values are preserved on upgrade | `1000` |
| `agent.chat_turn_timeout_secs` | Wall-clock limit for one chat turn, in seconds. A turn that reaches it ends with a notice naming the limit. Clamped to 300–86400 (24h) and cannot be turned off; for work spanning many hours, use a monitor loop or a task instead of one long turn. Also set in Settings → Chat → Advanced → **Turn Time Limit**. One dashboard session can run under its own limit in the same range, set from its session menu under **Turn time limit** | `14400` |
| `agent.spawn_min_memory_gb` | Free memory (GB) that must remain available after a subagent start is admitted. A dedicated-process start is priced at what such a runtime settles at: about 1 GB until runs of that agent have been measured, then their learned size capped at 2 GB, never below `agent.subagent_cost_gb`. A start that shares its parent's runtime is priced about 0.35 GB lower. A nested start that shares its parent's runtime is admitted at the floor itself, since its parent already holds its memory. A spawn that does not fit waits in the queue, never refused: in the durable queue, or in memory for a temporary or incognito memory mode. It is re-checked when memory can have moved (a subagent ending, or a reading every few seconds while something waits, which also sees a warming subagent settle), and while another chat's subagents wait, a chat already running its share of dedicated subagents (weighted by `agent.lane_weights`) waits for its next top-level start. On macOS free memory here counts free, purgeable and file-backed inactive pages, not inactive memory that would have to be compressed or swapped, and a start also waits while the kernel reports memory pressure and one of this gateway's dedicated subagents is running. An install still carrying the old `4.0` default moves to `2.0` once; a value set back afterwards is kept. 0 disables the check, that wait included | `2.0` |
| `agent.subagent_queue_max_wait_secs` | Longest a subagent spawn may wait for memory, counted as the time `agent.spawn_min_memory_gb` has held it back, in the durable queue or in memory. Past it the spawn ends without starting: its parent gets the result `never started: waiting for memory (not enough memory freed up in time); retry once memory frees up` like any other subagent result, and the spawn leaves the parent's queued count. Time a spawn spends waiting only for a free slot is not counted. It also bounds the macOS memory-pressure hold: a start it has held that long with no other top-level start from the same chat beginning in the meantime ends without starting (a chat whose queue keeps starting does not expire the spawns waiting behind them, whichever `spawn_run` batch they belong to), and while one pressure episode lasts longer than this, new starts it would hold end at once. Applies live, without a restart. `0` waits without a bound | `1800` (30 min) |
| `agent.completion_keep` | Which end of the subagent transcript to keep in the completion event injected into the parent session: `"head"`, `"tail"`, or `"both"` (head + middle marker + tail) | `"head"` |
| `agent.completion_keep_chars` | Max characters retained in the completion event after applying `completion_keep`. `0` disables truncation. The full transcript stays on disk (see `subagent_result_ttl_secs`) | `3000` |
| `agent.subagent_result_ttl_secs` | How long a delivered subagent's `result.txt` is retained before the reaper prunes it, so the parent can read the full transcript on demand instead of re-running the subagent. Measured from the moment the completion reaches the parent, not from when the run finished | `3600` (1h) |

**Tool Search restart compatibility:** automatic fresh-session replay after a restart currently applies to direct dashboard conversations. Messaging-channel and dashboard-linked channel sessions continue using native session resume; if a deferred tool remains unavailable after one of those sessions resumes, set `agent.tool_search` to `false` until channel dispatchers support the same replay-settlement contract.

### Session

| Key | Description | Default |
|-----|-------------|---------|
| `session.timeout_secs` | Idle session timeout in seconds (0 disables the idle sweep) | `3600` (60 min) |
| `session.idle_exempt_keys` | Session keys the idle timeout never ends, for a chat that automation wakes hours apart and that would otherwise start cold on every wake. A dashboard chat's key is `dashboard:` followed by the `sid` in its URL, unless the tab is linked to a cron job or a channel thread, which uses that link's key instead (for example `cron:<job id>`). Closing a dashboard or cron tab still ends its process; a channel thread's session belongs to the channel, so closing its viewer does not. `session.watchdog_rss_max_mb`, when set, still applies. A listed session with no tab, such as a cron job that runs without one, keeps running even after its job is deleted, so take a deleted job's key off the list. The RSS limit or a gateway restart still ends it. On the kiro-cli or KAS backend, a Kiro sign-out from the dashboard ends it at once, whatever account it signed in with. After any other switch or sign-out of the Kiro account it signed in with, the next dashboard chat turn ends it, and so does the next agent cron run unless Kiro Crew then cannot tell which account is signed in, as with a social login. A session that is mid-turn then ends later instead, when its next turn starts or at a later dashboard chat turn or agent cron run. Re-read every cleanup tick, so a change needs no restart. At most 10 keys of up to 512 characters each are kept, because each listed key can keep a whole kiro-cli process tree running; a longer key is dropped, not cut short, and the gateway log warns once per load when any key is dropped | `[]` |
| `session.empty_response_auto_continue` | After two consecutive empty model responses, send transcript-visible `continue` nudges on the same session | `true` |
| `session.empty_response_max_continues` | How many `continue` nudges may run back to back before the give-up card (clamped 1-10; above 1 the notice shows "recovery N of M") | `1` |
| `session.autocompact_pct` | Context usage percentage at which auto-compaction triggers (5-90). Lower compacts sooner and keeps per-turn cost down; higher retains more conversation before rewriting it. Applies to new installs: an existing `config.json` keeps its stored value. On the KAS backend it does not trigger compaction: KAS decides when to compact on its own | `70.0` |
| `session.context_cap` | Trim a goal conductor's chat (`kirocrew-conductor`) on the KAS backend at the start of a dashboard turn. A chat over 400K tokens, or over 200K tokens when its previous turn ended 5 minutes or more before, starts that turn on a new KAS session. The new session's first prompt carries a note about the trim, the chat's session-ledger snapshot and the newest messages as text, up to 270,000 characters. Tool output and older messages leave the model's context, stay in the chat's transcript, and `search_chat_history` finds them. The chat shows nothing about a trim, and the transcript gains no row. No model call is spent, and KAS's own compaction is unchanged. A trim waits while a question card or approval is pending, sub-agents are attached, a `reset_conversation` is queued or another turn holds the session. Read at each turn start, so a change needs no restart. Other agents and backends are not affected, and neither is a session a channel runs without a dashboard chat; a linked channel's message on a conductor's dashboard chat is a dashboard turn and is checked like one | `true` |
| `session.compact_wait_secs` | Seconds to wait for a compaction to finish: automatic, the task runner's context-overflow compaction, and a manual `/compact` on any surface (dashboard, chat channels). Past it, the session manager's automatic compaction and the task runner's compaction restart the session, a chat channel's near-limit compaction gives up and keeps the session, and a manual `/compact` reports that it timed out. `0` (the default) uses the built-in budget. A positive value below 60 is raised to 60 and a value above 3600 is capped. Raise it on a host where compaction on a large context window regularly needs longer than the built-in budget; a stuck compaction also waits the full budget | `0.0` |
| `session.pool_size` | Number of pre-spawned kiro-cli processes kept ready for instant session start. 0 disables. `session.eager_spawn` hides a new chat's startup only while someone types; a first message that arrives with its chat (a dispatched worker, `session_send`, cron, Slack) pays that startup, several seconds, before the agent sees it. Set `1` on a host that takes that traffic, so such a session of `session.pool_agent` claims a process that is already started. Each pooled process costs about 110 MB | `0` |
| `session.pool_agent` | Agent for warm-pool processes. Empty uses `agent.default_agent` | `""` |
| `session.pool_ttl_secs` | Max age in seconds for pooled processes, discarded at claim time. 0 disables | `1800` |
| `session.eager_spawn` | Create a chat session when its slot is created, switched, or retargeted instead of waiting for the first message | `true` |
| `session.archive_retention_days` | Days to keep compacted/rotated session archives before auto-cleanup. `-1` disables cleanup | `30` |
| `session.file_changes_keep_turns` | How many of a dashboard chat's most recent file-changing turns keep the before and after copies their file-change card diffs. An older turn's card lists its files marked "File only", and the rows that held its copies are written to the session archive first, where `session.archive_retention_days` applies. Rows still in the chat's in-memory window are never changed. Applied when a save re-reads the older part of the transcript: the first save after a restart, after another process appended to the chat, or after the window trims. `0`, the default, keeps every turn's copies, so nothing changes until you set a count | `0` |
| `session.watchdog_rss_max_mb` | Recycle an idle session when its process tree resident memory exceeds this many MiB. 0 disables, which is the default; the internal background runtime still recycles at 1536 MiB. Size a ceiling to your own agents: one with several MCP servers can sit above 1.4 GB. A session with a turn in flight is never recycled. `kirocrew status` and `kirocrew doctor` show the ceiling next to the gateway's own resident memory | `0` |
| `session.reconcile_max_kills` | Unowned root candidates the runtime reconciler may signal the process tree of in one pass — one candidate can signal several processes. The ceiling equals the default, so this setting can only lower the budget, never raise it. Lower it where more than one install shares this data home: the agent slice is keyed on the data home, so a runtime owned by another install has no record here and reads as unowned. `0` leaves the kill arm observing — it still publishes the `unowned_alive` / `owned_dead` leak reading and audits each candidate it would have signalled as `would_kill`, and signals nothing. The same budget caps one **Reclaim** of the dashboard's Leaked agent runtimes report, so `0` there refuses every candidate. Re-read every cleanup tick, so a change needs no restart | `5` |

### Dashboard

| Key | Description | Default |
|-----|-------------|---------|
| `dashboard.url` | Dashboard URL for remote access | `""` (localhost only) |
| `dashboard.restore_sessions` | Restore sessions on restart | `false` |
| `dashboard.restore_window_minutes` | Minutes after restart within which sessions can be restored | `30` |
| `dashboard.idle_slot_sweep_days` | Days a session may sit idle before the hourly sweep archives it to History. The sweep runs only while 450 or more sessions are open (the live cap is 500). It keeps pinned sessions, app sessions, the session in the foreground, sessions with a running turn, a pending approval or sub-agents, crew worker sessions and sessions an auto-nudge loop drives. A restart also restores at most 400 open tabs, newest first, and leaves the rest in History. Both paths post one notification that links to History. Takes effect on restart. `0` = never archive | `7` |
| `dashboard.qr_session_until_restart` | Keep a phone signed in for as long as the gateway process runs. Ordinary idling no longer signs it out; a gateway restart does, and so does going 30 days untouched (the refresh credential's lifetime, renewed on each visit). Turn off for a timed session that expires on a clock whether or not the gateway is still running. | `true` |
| `dashboard.merge_queued_messages` | Concatenate follow-up messages while the agent is busy | `false` |
| `dashboard.session_folder_root` | Give each new dashboard chat its own folder. When set, a chat started from the dashboard with no project of its own gets a newly created subfolder of this directory, named `<YYYY-MM-DD>-<6 hex>` with owner-only permissions, as its project directory. A chat filed into a folder that has a linked project keeps that project. A chat opened on a chosen directory (a worktree, a reopened session) never gets one, and neither does a purpose-built chat: one an app opens, an artifact's companion chat, a Slack thread intake, an error hand-off or **Request a Feature**. The root must be an existing absolute directory the project chooser would accept: not a sensitive path, not a network share, and on macOS not overlapping the data home. It is checked again each time a folder is made, so if it has become unusable the chat opens on the default project and the dashboard shows a notice. Folders are not deleted with their chat. Also set in Settings → Chat → Sessions → **New Session Folders**. `""` = off | `""` |
| `dashboard.default_session_folder` | The sidebar folder new dashboard chats are filed into when they are started from New without choosing a folder: the sidebar New button and its incognito and temporary items, the flyout New, the new-chat shortcut, the command palette's New actions, the Sessions page's New, and a first message from the empty chat page. It groups chats in the sidebar and is not a folder on disk (that is `dashboard.session_folder_root`). The value is a folder ID, so renaming the folder or moving it under another folder keeps it. A chat started in a specific folder still goes there, and a chat that lands in the default folder takes that folder's linked project and agent the same way **New chat in folder** does. Every other create path keeps its own placement, because the default is opt-in at those entry points: apps (including an app's chat launch), the auto-send launches from Projects, Dev Fleet and Prompts, crons, channels, agents (`session_create`), forks, worktree chats, the phone's **New session here**, crew and agent-template chats and an artifact sent to a new session. Existing sessions are never moved. **Clean up empty folders** keeps the default folder and its parents even when they are empty. If the folder is deleted, or its linked project directory has gone missing, new chats are left unfiled instead of being refused. Set in Settings → Chat → Sessions → **Default Sidebar Folder for New Chats**; if the folder list cannot be loaded, that row says so and offers Retry. `""` = unfiled | `""` |
| `dashboard.mcp_probe_timeout_secs` | Seconds to wait for an MCP server handshake during a probe (5-120) | `15` |
| `dashboard.title_refresh_every_turns` | Re-examine an auto-generated session title every N user turns (N, 2N, 3N, ...) and rename the session when the topic moved. `0` keeps the built-in schedule (turns 8 and 24 only); either way, a title that began as a bare link or ticket key also gets one refresh after the first turn. 1-3 is raised to 4; ceiling 1000. Each refresh is one background LLM call. Turns are counted over the messages held for the session. A session reloaded by a gateway restart or reopened from History holds its latest 500 plus the new ones, and the cadence continues from the restored count; if the reload keeps fewer user turns than the latest built-in turn the session had already reached (the first turn, 8 or 24), the cadence resumes after that turn instead. Once 10,000 are held, the oldest drop off as new ones arrive, so refreshes slow down or stop until the next reload. A title renamed by hand is never refreshed. Also set in Settings → Chat → Sessions → **Auto-title refresh interval (turns)**, a stepper whose readout shows "Default" for `0` | `0` |
| `dashboard.title_ticket_prefix` | Lead an auto-generated session title with the ticket key the opening message names: a tracker key such as `PROJ-1234` (upper-case project key, at least two digits) or a GitHub `#123`, as `PROJ-1234: Increase regression shards`. The model still writes only the summary; the key is copied from the message. The first key in the opening message is used, a title that already names the key is left alone, and a refresh keeps the key the title already leads with. Off by default because standard names like `SHA-256` have the same shape. A title renamed by hand is never changed | `false` |
| `dashboard.link_previews` | Fetch and render HTTP(S) link metadata in assistant messages. Off by default because each linked site receives a request from this machine | `false` |
| `dashboard.feature_videos_enabled` | Play a short intro clip for a feature this install has not used yet. Off turns off every clip except the built-in Meet your Mates intro (a dialog the dashboard draws, not a clip); set `dashboard.crewmates_onboarded: true` to turn that one off. See [Feature Videos](feature-videos.md). Off until real clips ship | `false` |
| `dashboard.link_patterns` | Rewrite matching plain text in transcripts into links at display time, through the same autolink rule engine editions register vocabulary on. Each rule pairs a JavaScript regex with an absolute http(s) URL template in which `{match}` inserts the matched text percent-encoded (no userinfo, placeholder outside the host), e.g. `{"pattern": "\\bPROJ-\\d+\\b", "url": "https://tracker.example.com/browse/{match}"}`. Code blocks and existing links are never rewritten; an inline code span whose whole text matches becomes a link chip. At most 50 rules with distinct patterns, each carrying at most one wide quantifier (`*`, `+`, `{n,}` or a wide `{n,m}`; narrow ranges may accompany it), scanning at most 2000 characters per text block | `[]` |
| `dashboard.feature_videos_cache_max_mb` | Disk budget for downloaded clips. Whole release folders are removed oldest-first to fit; the release you are running is never removed. `0` = no cap | `500` |
| `dashboard.crewmates_in_agent_picker` | List mates in the chat composer's agent picker beside the agent templates. Off: the picker lists templates plus any mate no listed template already covers; the default agent stays even when covered, unless a template shares its name. Takes effect when the dashboard is reloaded; no gateway restart | `false` |
| `dashboard.jira_auth` | Per-host Jira connection entries for the Issues panel, each `{"host": ..., "email": ...}`. `user` is accepted as an alias of `email` (`email` wins when both are set). The API token is not stored here: it is read from the secrets vault first, then from `.env` as `JIRA_API_TOKEN` (or a per-host token name when several hosts are configured). A Jira Cloud host (`*.atlassian.net`) with no `email`/`user` fails with `jira_config_error` before any request is sent | `[]` |

### Slack

| Key | Description | Default |
|-----|-------------|---------|
| `slack.allowed_users` | Ignored for access (Slack is owner-only); a name is only a display-name fallback | `[]` |
| `slack.tracking_channels` | Channels to monitor for new members | `[]` |
| `slack.open_channels` | Channel records retained in config | `[]` |
| `slack.command` | Slash-command name | `"kirocrew"` |
| `slack.reactions` | Override the status reaction emojis: `queued` (the ack shown while answering where Slack shows no native thread status) and `error`; set a value to `null` to suppress it | `{}` |
| `slack.reactions_enabled` | Show the status reaction (ack / error) on Slack messages | `true` |
| `slack.dm_single_session` | Treat each 1:1 DM as one continuous session, threaded replies included, instead of one per message. Applies only on the default messaging transport (`messaging.use_transport`, on by default) and never in a DM set to `review` mode | `false` |

Only the owner (`KIROCREW_OWNER_ID`) is authorized to interact over Slack.
Multi-user access and open channels are refused regardless of what these lists
contain, so treat them as bookkeeping rather than an access grant.

Every other messaging channel is configured from the dashboard — the roster is in
[the documentation index](index.md#chat-channels), and each channel's own doc
lists its keys and credentials.

### Speech-to-text

Speech-to-text is on by default and runs on your machine. Dictate into the
dashboard composer, and voice notes that arrive over a messaging channel are
transcribed the same way.

| Key | Description | Default |
|-----|-------------|---------|
| `stt.enabled` | Turn spoken input into text you can send | `true` |
| `stt.provider` | `"local"` (this machine, no account), `"apple"` (the on-device recognizer built into macOS 26 and later), `"transcribe"` (AWS Transcribe, which bills your AWS account), or `"off"` (no recognizer runs, the same as turning speech input off). `kirocrew config set` refuses any other value. A stored unknown value runs as `off` with a log warning, and `kirocrew config defaults --adopt stt.provider` rewrites it to `off` rather than switching to `local`; a retired name reads as `local` (see below) | `"local"` |
| `stt.model` | Which speech model the local provider downloads and runs: `tiny`, `base`, `small`, `large-v3-turbo`, or `custom` to run your own (see below). Bigger is more accurate and a longer first-time download — and on a CPU-only build the largest can recognise slower than you speak (an 11-second clip took 13.6 s on a 16-thread aarch64 CPU, 1.24x the audio), which Settings → Voice says beside the choice | `"base"` |
| `stt.custom_model_path` | File name of a whisper.cpp ggml model in the speech models folder (`models/whisper` under the Kiro Crew home), run instead of a catalog one when `model` is `custom`. Must resolve to a regular file inside that folder; nothing is downloaded | `""` |
| `stt.custom_model_sha256` | The 64-character hex sha256 of the file at `custom_model_path`, checked on every load. Required for `model: "custom"`; with either half missing or malformed, `custom` degrades to the default model with a warning | `""` |
| `stt.language_code` | Language for speech recognition, e.g. `en-US`, `fr-FR`. `"auto"` auto-detects on the local provider | `"auto"` |
| `stt.streaming` | Show words in the message box while you are still speaking rather than only once you stop. Every provider supports it; turning it off spends less CPU on `local` and fewer API calls on `transcribe` | `true` |
| `stt.silence_ms` | How long a pause must last before what you said is treated as a finished phrase. Raise it if you are being cut off mid-sentence, lower it if the text lags behind you. A value outside 200-5000 ms is clamped into that range. Set here only: Settings -> Voice offers no picker, because nobody can tell 700 ms from 750 ms by feel, and the setting most people actually want when dictation cuts them off is `stt.endpointing` | `700` |
| `stt.partial_interval_ms` | How often the live transcript is refreshed while you speak. A value outside 100-5000 ms is clamped into that range. Set here only: Settings → Voice offers no picker for it, because a decode costs a large fixed amount plus a small amount per second of audio (about 0.78 s + 0.08 s per audio-second for `base` on a 32-core CPU build), so on any CPU build the recogniser, not this number, decides the real cadence | `400` |
| `stt.idle_evict_secs` | How long the local model stays in memory after your last recording. It holds roughly 150 MB at the default model and reloads in a fraction of a second, so lower this on a machine short of memory. `0` releases it as soon as you stop speaking | `600` |
| `stt.threads` | CPU threads the local model uses to recognise speech. `0` or `"auto"` uses half the cores this process may run on, at most 16, which stays steady on a host busy with other work. Any other number from 1 to 1024 is used as given, lowered to the cores this process may run on when the model loads. On a 128-core host `large-v3-turbo` took 3.7-4.2 s on an 11-second clip at 16 threads, 2.0 s at 32, 1.25 s at 64 and 1.1 s at 96. `kirocrew config set` and Settings -> Voice refuse other values; a hand-edited unusable value loads as auto with a log warning. `KIROCREW_STT_THREADS` overrides it. A change applies at the next dictation, which reloads the model once; no restart is needed | `0` |
| `stt.endpointing` | While dictating, judge each finished phrase with a fast background model and send the message once it reads as a complete request, without you pressing anything. Needs `streaming` | `false` |
| `stt.submit_on_release` | Send the message once the transcript is ready when you deliberately stop dictating: letting go of hold-to-talk, releasing the push-to-talk key, or pressing the mic or the key again. Shift-click the mic, or slide sideways on the phone's hold bar, to keep a draft; with a push-to-talk binding that does not use Shift, Shift as you release works too. Cancelling, losing focus and the recording time limit never send, and nothing is sent if you typed, sent or switched chats while it was transcribing. Works with streaming and batch; applies to the next recording without a restart | `false` |
| `stt.polish` | After a dictation finishes, hand the TEXT (never the audio) to a fast model that fixes punctuation and spacing, and replace what is in the message box a moment later. Off by default because this is the one part of `local` recognition that sends anything off your machine. It never blocks you — the recogniser's own text is already there and already sendable — and never CHANGES a word: a reply that altered one is discarded, so the worst case is that nothing happens. It also never touches anything you typed after you stopped talking | `false` |
| `stt.dictation_panel` | Show the animated dictation panel while recording instead of the thin status bar. Ignored when the browser lacks WebGL2 or the OS asks for reduced motion, both of which fall back to the bar | `true` |
| `stt.timeout_secs` | Ceiling on transcribing one whole file: the audio decode, and each model load or recognition inside it | `300` |
| `stt.transcribe_region` | AWS region for the Transcribe API (`transcribe` provider only) | `"us-east-1"` |
| `stt.transcribe_profile` | AWS profile for the Transcribe API. Empty uses the default credential chain (`transcribe` provider only) | `""` |
| `stt.transcribe_vocabulary` | Name of an Amazon Transcribe custom vocabulary to recognise your names and terms, from the same region. Its language must match `stt.language_code`, or Amazon Transcribe refuses it and dictation fails. Empty uses none (`transcribe` provider only) | `""` |
| `stt.transcribe_languages` | Languages speakers switch between in one recording, for example `["vi-VN", "en-US"]`. With two or more, a live Transcribe stream identifies the language of each part and transcribes it in that language instead of using `stt.language_code`; with fewer, `stt.language_code` is used. At most five, one dialect per language (`en-US` and `en-GB` cannot be listed together), and a list that breaks either rule is ignored with a log warning. Applies to streaming dictation and meetings; a voice message, or dictation with `stt.streaming` off, still uses `stt.language_code`, because Amazon Transcribe identifies several languages only in a raw PCM stream. A custom vocabulary is then sent in the form identification accepts, and its language should be one of the candidates. Needs `amazon-transcribe` 0.6.3 or newer (`transcribe` provider only) | `[]` |

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
(`pip install 'boto3>=1.34,<2' 'amazon-transcribe>=0.6.3,<1' 'pywhispercpp>=1.5,<2'`):

- **Intel Macs have no prebuilt recognizer.** Every other platform Kiro Crew
  supports (Apple silicon macOS, glibc and musl Linux on x86_64 and arm64, and
  Windows) installs a ready-built wheel. On an Intel Mac `pip` falls back to
  building from source, which needs a C++ toolchain and CMake. Settings reports
  that as its own state rather than as a missing extra, because the two need
  different fixes.

  If you would rather not build it, installing only the cloud half
  (`pip install 'boto3>=1.34,<2' 'amazon-transcribe>=0.6.3,<1'`) gets you the AWS
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
  same pinned bytes into `<data home>/models/ffmpeg/` and verifies them
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

### Voice replies

Spoken replies live in the `voice_reply` block of `config.json`: `enabled`,
`auto_speak`, `provider` (`system`, the default host speech engine, or `polly`),
`voice_id`, `engine`, `rate`, `pitch`, `aws_profile`, `region`, `system_voice` and
`language_code`. `language_code` is a language tag such as `en-US` for spoken
replies; empty, the default, lets each provider use its own default language, and
the built-in providers speak the language of the selected voice.
The `piper` provider was removed; a saved `piper` value speaks with `system` and
logs one warning, and its `piper_*` keys are ignored. **Settings > Voice** is the writer for this block. It is not in
the config schema, so `kirocrew config set voice_reply.<key>` answers
`Unknown key`.

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

Both local defaults (`system` for TTS, `local` for STT) need no AWS account and no
confirmation. `system` additionally needs nothing installed on macOS and Windows,
which is why it is the TTS default.

### An AWS profile per project

`aws.project_profiles` binds an AWS named profile to a project directory. A
session working in that directory starts its agent with `AWS_PROFILE` (and
`AWS_DEFAULT_PROFILE`, which some tools read first) set to the profile, so `aws`,
the SDKs and tools built on them reach that account without a `--profile` on every
command. It works with any profile in your own AWS config:
IAM keys, SSO or `credential_process`.

Set it from the chat composer: click the project folder in the project pill, and
the project picker carries an **AWS profile for this project** row once you have
AWS profiles registered in AWS Control or the project is bound. Pick a profile,
type another name in **Other profile name** and click **Use profile**, or pick
**No profile** to go back to the gateway's own AWS sign-in. The pill itself then
shows the bound profile. You can also edit the map directly:

```bash
kirocrew config set aws.project_profiles '{"/home/me/src/billing": "billing-prod", "/home/me/src/sandbox": "dev"}'
```

How a session's directory is matched:

- **The deepest mapped directory wins.** A key covers the directory itself and
  everything under it, so `/home/me/src` and `/home/me/src/billing` can both be
  bound and `billing` gets its own profile.
- **Matching is on whole path components after resolving symlinks.**
  `/a/proj` does not cover `/a/project2`, and a symlink counts as the
  directory it points to.
- **An unmapped directory keeps today's behaviour:** the agent gets whatever
  credential chain the gateway itself has.
- **A profile the caller sets itself wins**, for example a scheduled job's
  `env` with its own `AWS_PROFILE` or `AWS_DEFAULT_PROFILE`.
- **A change reaches sessions started after it.** A session that is already
  running keeps the environment it started with.
- **A subagent follows its own directory.** It shares its parent's agent process
  only when its directory binds the same profile as the parent's. Otherwise it
  starts a process of its own, with the profile its directory binds or with none.

When a bound profile's sign-in no longer works, the profile in the pill turns amber.
For an SSO profile its tooltip gives the `aws sso login --profile <name>` command to
run.

The profile is set on the agent's harness process itself, not only on the commands
the agent runs: kiro-cli, KAS, Claude Code, Codex, OpenCode, Pi and Goose all start
with it. A harness that signs its own model calls through the AWS credential chain
signs them with the bound profile too. Claude Code set up for Amazon Bedrock
(`CLAUDE_CODE_USE_BEDROCK=1`) is one, and the bound account then needs access to the
Bedrock model. Under the `strict` and `cc` sandbox modes `~/.aws` is hidden from
the agent (`cc` keeps a read-only `~/.aws/config`), so there a profile that needs
`~/.aws/credentials` or the SSO cache does not resolve for the harness either.
kiro-cli and KAS sign in with Kiro's own login, and Codex,
OpenCode, Pi and Goose call whichever provider their own configuration names. Kiro
Crew does not check whether any of them also reads `AWS_PROFILE` for its model calls.

A profile name must be 1 to 128 letters, digits, `_`, `.`, `+` or `-`, and must not
start with `-`. A bad name or a relative directory is refused when you save it, and
ignored with a warning if a hand edit puts it in `config.json`.

The binding is a default, not a boundary. Only the profile **name** is stored and
passed on, and no credential is read or copied. The agent can still name another
profile. The binding is read only from your own `config.json`, never from a file
inside the project, so a cloned repository cannot choose which account its agent
uses. For separation beyond picking the account:

- A [crew](crew-members.md) gives a project its own workspace, memory store and
  approval mode.
- A separate Kiro Crew instance, with its own `KIROCREW_HOME` and port, keeps
  everything apart.

### Memory and embeddings

Embeddings are always on and run in-process through the bundled
llama-cpp-python runtime. There is no server to install and no way to disable
them, so there is no enable switch here: only knobs for *which* model runs.

| Key | Description | Default |
|-----|-------------|---------|
| `memory.embedding_provider` | Vector embedding backend. `"llama_cpp"` is the only accepted value; any other value in an existing config (including a legacy `"ollama"` or `"none"`) is coerced to it on load | `"llama_cpp"` |
| `memory.embedding_dim` | Output width of the embedding model in use. Must match a custom model's real width, or the load is refused | `1024` |
| `memory.embedding_threads` | CPU threads llama.cpp may use per embedding call; explicit settings are clamped to the CPUs the process may run on, which a CPU-set restriction (`--cpuset-cpus`, `taskset`) narrows below the host's cores | `4` (`6` where the process may use 12 or more CPUs; a saved `4` is read as this default there, so set 3 or 5 to stay low) |
| `memory.embedding_bulk_threads` | Threads used for background embedding; `0` inherits `embedding_threads` | `1` |
| `memory.embedding_bulk_duty` | Target fraction of worker time spent on background embedding; interactive queries take priority | `0.2` |
| `memory.embedding_idle_evict_secs` | Unload the embedding model after this many seconds with no embedding; the next search loads it again. `0` keeps it loaded | `3600` |
| `memory.embedding_child_process` | Load the embedding model and run inference in a separate low-priority child process, so embedding work never competes with the gateway process. Off by default: the model runs inside the gateway. Takes effect at the next model load | `false` |
| `memory.embed_model_url` | Override HTTPS URL for the embedding-model GGUF download (mirrored or airgapped hosts). Empty uses the public Kiro Crew CDN. `KIROCREW_EMBED_MODEL_URL` wins over both. Downloads are sha256-verified regardless of source | `""` |
| `memory.embed_model_path` | Absolute path to a local GGUF to run **instead of** the bundled Qwen3-Embedding-0.6B. When set, the default model is never downloaded, so a custom model survives a default-model version change. Set `embedding_dim` to the model's output width. Changing the model changes the vector space, so stored embeddings are regenerated in the background. A configured-but-unreadable path fails closed (keyword search still works) rather than silently reverting to the default and re-embedding your corpus. Editable from the dashboard (Memory → Embedding Model). `KIROCREW_EMBED_MODEL_PATH` wins over this | `""` |
| `memory.embed_model_id` | Optional label for a custom model. The vector-space identity is `<label>:sha256:<digest>` of the model file's bytes, so two different models of identical name and size are always told apart and this key cannot pin or override that identity. A model recorded without a label takes the pooling its GGUF declares into its id, after the digest (`custom:sha256:<digest>@mean`, `custom:sha256:<digest>@cls`); a label you set, whatever it contains, keeps last-token pooling. A custom model applied before Kiro Crew read the declared pooling keeps last-token pooling and its vectors until you apply it again. A file replaced in place under a `custom` id takes the new file's declared pooling (its digest changed, so it is re-embedded anyway); a label you set carries over. Applying a model from the dashboard writes the resulting id together with `memory.embed_model_stamp` (the file's device, inode, size and timestamps); an unchanged file reuses the stored digest at startup instead of re-hashing the weights | `""` |
| `memory.semantic_confidence_threshold` | Minimum similarity score for a semantic search result | `0.8` |
| `memory.episodic_dedup_threshold` | Similarity threshold for deduplicating episodic memories | `0.88` |
| `memory.episodic_max_results` | Max episodic memories injected per session | `8` |
| `memory.episodic_max_count` | Max total episodic memories stored | `10000` |
| `memory.decay_rates` | Per-tag episodic recency decay rates, per day (score factor `exp(-rate * days_old)`). Keys are memory tags (case-insensitive); the reserved `default` key replaces the built-in `0.03` for memories matching no configured tag. A memory carrying several configured tags uses the slowest (smallest) rate, so a broad tag can never age out a long-retention one. `0` never ages out of retrieval ranking; `1` falls out of retrieval within about a day. Ranking only: `episodic_max_count` cap eviction (lowest importance, then oldest) still applies regardless of decay rate. Values are clamped to `0..10`; non-numeric values are ignored with a logged warning. Example: `{"legal_precedents": 0.0, "trading_data": 1.0}` | `{}` |
| `memory.history_idle_hours` | Hours of inactivity before history consolidation | `3.0` |
| `memory.history_max_days` | Days of history to retain before pruning | `365` |
| `memory.backup_enabled` | Periodic rotating backups of every active memory store (the default store, named V1 stores and member V2 stores); retention does not delete active memories | `true` |
| `memory.backup_keep` | Backup copies retained per store, with a minimum of one | `7` |
| `memory.persistence_enabled` | Global switch for persistent memory. Off: no automatic memory writes anywhere — `learn_add` and `kirocrew learn add` refuse, history consolidation pauses entirely (no LLM turn spent), task-runner lesson extraction skips, and app-owned projections such as Ops Mission Control's ledger import are no-ops before opening the memory store — and stored memory/lessons are not injected into new sessions. Within-conversation context is unaffected, and explicit dashboard edits/deletions (the right to forget) stay available. | `true` |
| `memory.writes_enabled` | Read-only memory. Off: the same automatic writers `memory.persistence_enabled` stops are stopped (`learn_add`, `kirocrew learn add`, history consolidation, task-runner lesson extraction, app-owned projections), while stored memory and lessons are still injected into new sessions and `memory_recall` still answers. Incognito and temporary sessions behave as before. Explicit dashboard edits/deletions stay available | `true` |
| `memory.lesson_extraction_enabled` | Off: history consolidation and the task runner save no extracted lessons. The rest of each consolidation pass (history, preferences, semantic and episodic memory) still runs, and `learn_add` still saves lessons | `true` |
| `memory.kind_routing` | What history consolidation does with the `kind` it tags each `project.*` fact with: `fact`, `event` (something that happened) or `state` (a PR, issue, CI, branch or round status that GitHub, git or the work ledger already holds). `off` keeps today's prompt and writes. `shadow` (opt-in) asks for the tag, writes every fact as before and only logs what routing would do, inside fixed caps (one counter row a day; at most 50 samples a day, 2,000 in all and 1 MB, for the first 7 days). `enforce` skips a state item and writes facts and events as before. Stored facts are never changed. `pref.*`, `user.*`, lessons and facts you state yourself are never routed | `"off"` |
| `memory.inject_memory` | Inject the stored memory block (preferences, the memory activity index, recent-session snippets) into new-session context, including the re-injection after a compaction. On-demand `memory_recall` and writes are unaffected | `true` |
| `memory.inject_lessons` | Inject the learned-corrections and user-profile blocks into new-session context. Writes are unaffected | `true` |
| `memory.inject_lessons_per_turn` | On each follow-up message, add up to three stored lessons (2,000 characters) that match it and that the session has not been shown yet: the session-start block holds only what fits its budget. A lesson matches when it shares at least two distinct words with the message that are each rare among stored lessons: carried by at most 1% of them, and never fewer than one. Request words on the lesson stop list and words of two letters or fewer are ignored. Each lesson is sent at most once until a compaction drops it from context, even when the turn that carried it was cancelled or failed; a gateway restart, or turning this on mid-session, can send one once more; it can also be sent once more after 256 newer lessons in that session or after its record is dropped past 256 sessions. Vector stores only. Requires `inject_lessons` | `false` |
| `memory.inject_activity` | Inject the recent activity block (active projects, daily history (14 full days, then decayed summaries and counts to day 180), task facts and relevant past episodes) into new-session context as a budgeted background block the context budget may drop whole. Off: only preferences and the activity index ship at session start, and older material is read through `memory_recall`. Requires `inject_memory` | `true` |
| `memory.startup_recall_timeout_secs` | How long a new dashboard chat's first message waits for its own embedding, which the dashboard starts ahead of the session-start build, beside session startup when the chat has to start its session. That embedding ranks the task facts, past episodes and lessons the session starts with; past the wait they are ranked by keyword. `0` turns the early embedding and the early memory reads off, and the build embeds the message and reads memory itself, as on other surfaces. Clamped to 0–10 | `1.0` |

Decay, episodic capacity eviction and history age pruning apply to V1 only.
V2 keeps memory until explicit correction, replacement, forgetting or restoration.
Global V1 retains its session-start retrieval; V2 injects essential member and
project guidance and recalls memory fragments on demand. The shared embedding
worker and its thread defaults affect both versions.

#### Named memory stores

Explicit member creation assigns a stable `member_id`, one managed `store_id`,
and one SQLite database at `memory_stores/<store>/memory.db`, where `<store>` is
the store's name, which is also its `store_id`. Display names,
templates, projects and workspaces do not change the memory owner. The database
contains learned facts, corrections, experiences, history, full-text indexes and
vectors. Manual member rules and project guidance remain separate documents.

| Key | Description | Default |
|-----|-------------|---------|
| `memory_stores` | Declares each managed store, its version and stable owner identity | `{"default": {}}` |
| `agents.<crew>.member_id` | Stable member identity, independent of its display label | Allocated on member creation |
| `agents.<crew>.memory_store` | The member's single managed store identity | Allocated on member creation |
| `default_memory_store` | Existing V1 default configuration; never repairs a member identity | `"default"` |

A declared V1 store's directory is never created on first use. Run `kirocrew memory create-store <name>` to create it; an existing directory is left untouched, and the default store, an undeclared name and a V2 member store (created with its member) are refused. `kirocrew doctor` names this command for a declared V1 binding whose only defect is the missing directory.

Global V1 keeps its existing files and behavior. Creating a member does not copy
Global learning into that member. Opening a missing, corrupt or wrong-member V2
database reports an error and never creates an empty replacement. Restore a
damaged member database from its own daily backup. Backups use SQLite's consistent
backup API and coordinate restore with active connections. Snapshots include
named memory stores as well as Global memory.

Member memory provides separate learning and working context, not adversarial
confidentiality between agents operated by the same user. Bound memory tools use
the execution's selected database. Prompt and built-in path guidance discourage
raw database edits and accidental cross-member file access; arbitrary code can
read other members' files. Ordinary transport authentication, host sandbox,
credential protection and enterprise policy remain in force. No additional
member-memory sandbox is required.

### Skills

| Key | Description | Default |
|-----|-------------|---------|
| `skills.max_triggered` | Maximum skills loaded per message (>=0) | `0` |
| `skills.lazy_load` | Inject a usage-ranked top-K of on-demand skills at session start, plus one line naming the families it leaves out, and leave the tail discoverable via search, so a large skills set cannot crowd out memory and lessons. Set false for the shorter entry that names eight skills. In both, the user's own skills take up to six of the first eight places and the highest-ranked remaining skills of either kind take the rest, so a new install names the skills its user wrote while a shipped skill with real usage keeps its name | `true` |

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
| `knowledge.doc_ingest_hosts` | Exact hostnames whose pages Kiro Crew itself may fetch for a Web Page source (Knowledge → Add Source). `https://` only, default port only, no subdomain matching; the host must also resolve to public addresses, and a redirect to another host is refused. Empty fetches nothing, and a source naming an unlisted host is refused when added and when synced | `[]` |
| `knowledge.folder_ingest_chunk_budget` | Chunks a folder you add by hand may ingest per watcher sweep, including the first scan started by confirming the source. Nothing is skipped — newest files land first and the rest continue on later sweeps — so this paces spend rather than limiting what is ingested. 0 removes the bound; a per-source `chunk_budget` property overrides it for one folder | `300` |
| `knowledge.dedup_every_n_sweeps` | Run a full duplicate-collapsing pass every Nth watcher sweep (the per-write gate only catches byte-identical documents). 0 disables | `12` |
| `knowledge.extraction_pool_size` | Concurrent LLM workers for document extraction. Applies live: the pool resizes once its in-flight extractions finish | `3` |
| `knowledge.extraction_effort` | Reasoning effort for the document-extraction LLM pool: `""`, `low`, `medium`, `high`, `xhigh` or `max`. Empty runs the default, `high`. Only applies on reasoning-capable models. Restart required | `""` |
| `knowledge.embed_rate_limit` | Maximum embedding generations per minute across all sources. `0` removes the bound | `120` |
| `knowledge.sweep_chunk_budget` | Maximum chunks ingested across all sources in one watcher sweep. `0` removes the bound | `500` |
| `knowledge.import_chunk_budget` | Maximum chunks ingested through the explicit one-shot import paths (single-file add, agent add, direct text ingest, remote sync) within a rolling ~60s window -- the cross-file cost ceiling those paths otherwise lack. When exhausted the next import is refused with a reason rather than silently truncated; a single file stays bounded by the 50-chunk per-file cap independently. `0` (the default) removes the bound; opt in by setting it (e.g. `500`). Limitation if enabled: reservation is worst-case (each in-flight import books the 50-chunk per-file maximum up front and reconciles to the real count only on completion), so concurrent imports throttle below the nominal number until that accounting is refined. | `0` |

### Work ledger

| Key | Description | Default |
|-----|-------------|---------|
| `work_ledger.max_depth` | How many levels of the work ledger may conduct. Depth 0 is the root conductor, so `2` lets it dispatch one level of conductors whose own items are workers, and `3` lets those conductors dispatch conductors of their own. Accepted range 2-10; a value outside it is refused at load with a warning and the default applies, it is never clamped. Each extra level multiplies sessions (three levels of three items is 27), so raise it only where concurrency is already capped per level. Lowering it leaves open ledgers alone, but new ledgers and new items at the levels it removes are refused. | `2` |
| `work_ledger.remote_dispatch` | Preview. Lets a conductor on another machine place work items on this crew. When `true`, `GET /api/chat/headroom` answers this machine's free memory, CPU load and free scratch disk to the owner, every owner session-create reply carries that `headroom` snapshot, and an owner create that carries a `work_item` is refused `crew_busy` (naming `memory`, `cpu` or `disk`) when one is short. When `false`, the route answers 404 and no create is checked. Read on every request, so no restart is needed | `false` |
| `work_ledger.remote_dispatch_memory_gb` | Free memory one dispatched worker needs; a work-item create is refused `crew_busy` on memory when less is free. A value that is not a positive number uses the default | `2.0` |
| `work_ledger.remote_dispatch_disk_gb` | Free disk under the scratch root one dispatched worker needs; a work-item create is refused `crew_busy` on disk when less is free. A value that is not a positive number uses the default | `10.0` |

### Power

| Key | Description | Default |
|-----|-------------|---------|
| `power.on_battery` | What Knowledge Library background sweeps do while this machine runs on battery; nothing else reads it yet. `normal` ignores the power source; `reduced` cuts each sweep to a quarter of `knowledge.sweep_chunk_budget`, at least 10 chunks and never more than the configured budget; `paused` skips the sweeps until the machine is back on mains. A document you add yourself always ingests; a folder you add ingests its first paced batch at once, and the rest of a large folder arrives with the sweeps, so under `paused` it waits for mains. A host that cannot report its power source counts as plugged in. Read on Linux and Windows; macOS always counts as plugged in for now | `"normal"` |

### Top level

| Key | Description | Default |
|-----|-------------|---------|
| `auto_update` | Where the install can apply updates, `true` installs them and restarts the gateway; `false` only notifies. When work is running, a checkout waits before applying, while the `cli.sh` managed venv builds the new version alongside and waits only to restart. Elsewhere it has no effect. On those same installs a policy minimum version applies regardless. A hand-edited recognized boolean spelling (such as `"false"`) is honoured; any other non-boolean value reads as off; an absent key stays on. See [Updates](#updates) | `true` |
| `timezone` | IANA timezone name, e.g. `"America/Los_Angeles"` | `""` (falls back to UTC) |
| `snapshot_dir` | Where `kirocrew snapshot` writes tarballs | `""` (`~/.kiro/crew/snapshots`) |

## Running a custom speech-to-text model

Put a whisper.cpp ggml model file in the speech models folder, `models/whisper` under the Kiro Crew home (`~/.kiro/crew/models/whisper` by default). Then set `stt.model` to `custom`, `stt.custom_model_path` to the file's name, and `stt.custom_model_sha256` to its 64-character hex sha256:

```json
{
  "stt": {
    "model": "custom",
    "custom_model_path": "ggml-my-model.bin",
    "custom_model_sha256": "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe"
  }
}
```

Settings → Voice does the same: pick **Custom**, then enter the file name and the SHA-256. Get the digest with `shasum -a 256 <file>` (`Get-FileHash <file> -Algorithm SHA256` on Windows).

The path must resolve to a regular file inside the models folder. A name with `..`, an absolute path elsewhere, a symlink that points out of the folder, and a built-in model's file name (`ggml-base.bin` and the like, which the built-in download replaces) are all refused, and Settings → Voice says why. Nothing is downloaded for a custom model.

The file is hashed on every load and is not used unless it matches the digest, so a file replaced in the models folder is refused rather than run. If either setting is missing or malformed, `custom` falls back to the default model with a warning.

Only whisper.cpp ggml weights work. This picks a different model for the bundled recognizer, not a different recognizer.

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `KIROCREW_HOME` | Override the config/data directory | `~/.kiro/crew` |
| `KIROCREW_PORT` | Override the dashboard port | `5476` |
| `KIROCREW_PROJECT_DIR` | Override the agent-config/skills project directory | Auto-detected |
| `KIROCREW_WORKSPACE` | Override the workspace root. No subdirectory is appended; one surrounding quote pair is dropped and `~` is expanded. A value that is not absolute is logged and replaced by the platform default | Saved `workspace_dir`, else a platform default |
| `KIROCREW_SKIP_MODEL_DOWNLOAD` | Set to `1` to skip the background embedding-model download at gateway startup (tests, CI, airgapped hosts) | unset |
| `KIROCREW_EMBED_MODEL_URL` | Override HTTPS URL for the embedding-model GGUF; wins over `memory.embed_model_url` and the CDN default | unset |
| `KIROCREW_EMBED_MODEL_PATH` | Absolute path to a local GGUF to use instead of the bundled model; wins over `memory.embed_model_path` and suppresses the default download entirely. A model set only here is never recorded, so it keeps last-token pooling; setting `memory.embed_model_path` to the same file (or applying it in the dashboard) records it with the pooling its GGUF declares | unset |
| `KIROCREW_CC_PERMISSION_MODE` | Set to `auto` to seed every Claude Code session with Claude's own classifier as its permission mode. Only the exact value `auto` takes effect; anything else logs a warning and seeds nothing. Audit caveat: a call the classifier approves asks nothing, so it reaches no Kiro Crew tool-call hook and writes no security event log record. Other backends ignore it | unset |
| `KIROCREW_STT_THREADS` | Decode threads for the local speech model in this process: `auto` or a whole number. Wins over `stt.threads`. An unusable value is logged once and ignored, leaving `stt.threads` in force. Read when the model loads, so changing it needs a gateway restart | unset |
| `KIROCREW_SKIP_APP_STORE_FETCH` | Set to `1` to keep the app store off the network: the listing shows the bundled app list and does not fetch the published catalog (tests, CI, airgapped hosts). Installing an app still needs the network | unset |

Use a dedicated directory for `KIROCREW_HOME`. Startup applies owner-only
permissions or an owner-only Windows ACL to the data home, including homes that
use only named stores. A deliberately group-shared directory will have those
permissions tightened. This is shared data-home hardening and affects V1 as well
as V2 installations.

### Timezone

The `timezone` key affects three things:

- the `[CURRENT DATE]` line injected into every LLM prompt, so "today" is not
  ambiguous on a host whose system clock is UTC
- cron schedule display (`kirocrew cron list`, the Slack Home Tab)
- `skip_dates` evaluation for cron jobs

A per-job `timezone` on a cron job wins over this global value.

## Updates

### When the gateway checks

The gateway checks for a newer release when it starts (its own restarts
included), every 12 hours, and every five minutes while an update is waiting
for running work to finish. Depending on the install, the check is a `git
fetch` or a release-feed fetch. Docker and the desktop app's bundled gateway
skip it (see below); everywhere else it runs whatever `auto_update` is set to.

`auto_update` decides what happens when the check finds something. With `true`
(the default), the gateway applies the update and restarts itself, on the
installs that can apply. With `false`, it only notifies.

A checkout applies only when no turn or background job is running. If work is
in flight, it keeps serving and tries again five minutes later, so steady
activity can postpone even a mandatory update. While a checkout's update
applies, the gateway does not start new turns.

The `cli.sh` managed venv does not wait to apply. It builds the new version
beside the live install while turns keep running, and nothing the running
gateway loads from is changed. Only the restart into the new version waits for
running work to finish, retried on the same five-minute cadence. On a Linux host
where the `kirocrew-userns` AppArmor profile is attached to the launcher, the
gateway does not apply by itself: it posts a notice to run `kirocrew update` and
then `kirocrew service install`, which re-attaches the profile.

If an update removes the dashboard bundle while the gateway cannot be relaunched
(the service manager could not start it, or the update moved the install but
could not sync its dependencies), the gateway stays up on its loaded code. The
dashboard then serves its fallback page, and the log carries a CRITICAL line
naming the reason. See the
[stale-asset watchdog](../../../docs/system-specs/modules/slack-gateway.md) in the
gateway spec.

### What each install does

A security policy that names update commands replaces everything in this
section: its commands then check and apply on every install shape except
Windows, where they never run and the gateway does not update itself. See the
[governance spec](../../../docs/system-specs/modules/governance.md#update-pins-updates--policy-only).

| Install | With `auto_update` on |
|---|---|
| Git or source checkout (any OS) on a primary branch: `main`, `mainline` or `master` <!-- wokeignore:rule=master --> | Applies |
| The `cli.sh` managed venv (macOS, Linux) | Applies |
| pipx (what `cli.sh` uses when pipx is on `PATH`) or plain `pip` | Notifies only |
| Docker | Neither checks nor applies. The About page says to pull a newer image |
| The gateway bundled in the desktop app | Neither. The app's own updater owns it |

Of the non-checkout installs, the gateway applies an update itself only on the
`cli.sh` managed venv, so pip and pipx only notify.

A checkout applies when the tip of its branch carries a newer `__version__` than
the running code, and then hard-resets to that tip and restarts. It does not
apply over local changes, local commits or untracked files the update would
overwrite, and it does not restart a gateway whose checkout you already pulled
by hand. Other branches,
`release/*` included, never auto-apply. `main` is always one minor version ahead
of the release line, so a `main` checkout moves onto nightly code each time a
release branch is cut. Below a policy minimum version, a primary-branch checkout
applies on the same newer-`__version__` test and only when the checkout can take
the update cleanly, so a floor moves it toward a build that satisfies the minimum
rather than resetting to every intermediate commit. When the floor is pinned
above the newest available build, or the checkout has diverged (local commits a
reset would discard), the gateway refreshes the update badge instead of applying.
A mandated apply that finds the required code already on disk leaves a restart
pending and refreshes the badge rather than reporting a restart that does not
happen.

### Turning it off and updating by hand

Set `auto_update` with `kirocrew config set auto_update false`, from
**Developer → Config → Auto Update** in the dashboard, or with **Update the gateway
automatically** on the About page. The About switch appears in a browser, and in the
desktop app when the gateway it connects to installs updates itself.

Updating by hand never restarts a running gateway, so finish with
`kirocrew restart`:

- **Git checkout, `cli.sh` managed venv or pipx:** run `kirocrew update`, then
  `kirocrew restart`. On pipx it re-runs the installer, which replaces the pipx
  install.
- **Plain `pip`:** upgrade with pip in the same environment, from the channel
  index you installed from (Kiro Crew is not on PyPI; see
  [Installing a published wheel with pip](../../../docs/guides/install.md#installing-a-published-wheel-with-pip)),
  then `kirocrew restart`. Here `kirocrew update` installs a separate copy
  instead of upgrading that environment.

`kirocrew --version` prints the version installed on disk. The About page shows
the version the running gateway serves.

### What can still update a host with `auto_update` off

- **A policy minimum version.** An administrator can set `min_version` in the
  `updates` block of `security_policy.json`. On an install whose gateway updates
  itself, a gateway below that version applies the update even with
  `auto_update` off — but only toward a build carrying a newer `__version__`, so
  a floor advances the host instead of churning on commit distance alone. The
  scope per install is in the
  [governance spec](../../../docs/system-specs/modules/governance.md#update-pins-updates--policy-only).
- **The desktop app's own updater.** On a desktop install, **Install app
  updates automatically** on the About page is the switch that stops automatic
  updates. It updates the app and the gateway bundled in it. The **Update the gateway
  automatically** switch that can appear beside it sets only the attached gateway's
  `auto_update`. `auto_update` matters there only if a
  policy names update commands, or if the app is attached to a separately
  installed gateway (from the CLI, as a service, or reached over an SSH tunnel),
  which follows its own `auto_update` per the table above. The app
  keeps its switch in its own settings file (**Open Config File** in the tray
  menu or the **Connection** menu), not in the gateway's
  `~/.kiro/crew/config.json`. How the app's updater downloads and installs is in
  [release.md](../../../docs/build/release.md#client-auto-update).

An edition can hand updates to a package manager, through policy
`check_command` / `apply_command` or a packaged app marker's `checkCommand` /
`updateCommand`. While its updates are on, a pause set in that package manager
holds only if the edition's check command honours it; see
[externally managed installs](../../../docs/build/desktop-app.md#externally-managed-installs-repackagers).

## Credentials

`~/.kiro/crew/.env` holds messaging-channel credentials and the owner ID. For
Slack:

```
SLACK_APP_TOKEN=xapp-...
SLACK_BOT_TOKEN=xoxb-...
KIROCREW_OWNER_ID=UXXXXXXXX
```

Save `.env` as UTF-8. A UTF-8 byte-order mark is fine and is kept when Kiro
Crew rewrites the file. A file saved as UTF-16 or UTF-32 (PowerShell's default
`>` / `Out-File`) is not read: every credential in it reads as unset, the log
warns once, and a dashboard channel save answers 409 until the file is re-saved
as UTF-8.

To add directories to the `PATH` the agent's shell starts with, put a `PATH` line
in the same file. List only the directories to add, as absolute paths separated the
way `PATH` is on your platform (`:` on macOS and Linux, `;` on Windows):

```
PATH=/opt/tools/bin:/opt/other-tools/bin
```

They are appended behind the `PATH` the gateway already has, so a directory here
can supply a tool the gateway cannot find but never replaces one it can. `~`
expands; `$HOME` and other shell syntax do not, and an entry that is not an
absolute directory is dropped with a warning in the log. Restart the gateway after
editing the file.

A variable the gateway already inherits (from your login shell, the desktop app,
a service unit or a container) wins over the same key in `.env`. The `.env` line
does not reach the processes the gateway starts, and the log warns once, naming
the key but not either value. A `secret://` line is not compared. Unset the
variable where it comes from, then stop and start the gateway.

Kiro Crew sets a few variables itself at startup, such as a CA bundle in
`SSL_CERT_FILE` when Python has no default CA file. A `.env` line for one of
those never takes effect and is named the same way; set the variable in the
environment that starts the gateway instead, where Kiro Crew keeps it.

An in-app restart keeps the environment of the process it replaces, including the
`.env` values that process loaded, so a `.env` edit only takes effect after the
gateway is stopped and started; until then the same warning names the edited key.

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
| `~/.kiro/crew/config.local.json` | Local overrides that win over `config.json` |
| `~/.kiro/crew/.env` | Slack credentials |
| `~/.kiro/crew/skills/` | User skills |
| `~/.kiro/crew/crons.json` | Scheduled jobs |
| `~/.kiro/crew/hooks.json` | Script hooks |
| `~/.kiro/crew/lessons.jsonl` | Learned corrections |
| `~/.kiro/crew/notifications.jsonl` | Notification history |
| `~/.kiro/crew/models/` | Embedding model, downloaded in the background at startup |
| `~/.kiro/crew/history/` | Chat history (JSONL) |
| `~/.kiro/crew/workspace/memory/` | Memory files (default store) |
| `~/.kiro/crew/memory_index.db` | Full-text search index (default store) |
| `~/.kiro/crew/memory.db` | Semantic, episodic and lesson memory (default store) |
| `~/.kiro/crew/memory_stores/<store>/` | A managed store (`<store>` is its name, also its `store_id`): one member’s SQLite learning database and manual context files |
| `~/.kiro/crew/session_map.json` | Session resume mapping |
| `~/.kiro/crew/snapshots/` | Default output of `kirocrew snapshot` |
| `~/.kiro/agents/kirocrew.json` | Installed agent config |
| `~/.kiro/settings/mcp.json` | Global MCP server config |
