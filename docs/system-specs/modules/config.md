# Config Module

Last Updated: 2026-07-24 (SessionConfig.empty_response_auto_continue added — default-ON gate for the dashboard chat runner's bounded empty-response auto-continue rung; see session.md "Empty-response recovery ladder". Prior — Divergent-new-home auto-reconcile: a pre-existing/stale `~/.kiro/crew` that diverges from legacy during the no-overwrite merge no longer strands the user on `~/.kirocrew` — reconciliation now completes the switch with legacy authoritative by renaming the divergent home aside to a keystone-gated (`~/.kiro/crew.pre-migration` is on `security._SENSITIVE_HOME_DIRS`), owner-locked `~/.kiro/crew.pre-migration/<ts>` backup, promoting the quiesced legacy snapshot into `~/.kiro/crew` (absolute intra-home symlinks retargeted), and marking complete; live data is unchanged (legacy, as the prior retain behavior), the sidelined home is a recoverable rollback, and a gateway live on the new home is never yanked aside. Prior — Data-home migration hardening: quiesce-before-compare closes the compare→archive TOCTOU (legacy tree atomically renamed to a per-PID `~/.kirocrew.quiescing.<pid>` snapshot before the divergence compare, then that frozen snapshot is compared + archived, so a concurrent legacy-era writer can't make stale state authoritative); regenerable bulk trees (`models`, `cache`) are RELOCATED (moved, not copied) from the snapshot into the new home so the GGUF model survives the upgrade for offline users (archive still carries no duplicate; falls back to strip-and-redownload on EXDEV); pre-copy stderr visibility notice; documented downgrade/rollback procedure; `kirocrew doctor` **Data Home** section surfaces the leftover `~/.kirocrew.archived` size + cleanup command. Prior — 2026-07-23 Data home moved from top-level `~/.kirocrew` to `~/.kiro/crew` — `config_dir()` now resolves to `~/.kiro/crew` by default (still overridable via `KIROCREW_HOME`), and triggers a one-time migration of a pre-move `~/.kirocrew` into the new home on first launch. See "Data Home Location & Migration" below. Prior — 2026-07-15 Removed the SecretaryConfig / TaskKeeperConfig / KeywordHook DTOs and the `secretary`/`taskkeeper` KiroCrewConfig fields — the Secretary/TaskKeeper features were dropped from the public fork (P472753900); config-baseline regenerated. Prior — 2026-07-13 Schema refresh: documented security-bounded load-time clamp — SUBAGENT_AUTO_MAX_CEILING=64 / SUBAGENT_MAX_TURNS_CEILING=200 / POOL_SIZE_MAX=10, `_clamp_security_bounds` + `config_bounds_clamped` SEL event; added clamped AgentConfig fields, SessionConfig.pool_size, MessagingConfig/SkillsConfig/TelemetryConfig/DashboardConfig (theme_mode/theme_color/onboarded) DTOs, `_resolve_named_agent_model`/`kiro_agents_dir`; corrected `_resolve_agent_model` fallback to `config_package_dir()/defaults.json`. 2026-06-22: AgentConfig: added sandbox_allow_no_isolation (SEC-009) field; agent_model_state.json sidecar: model_managed/cc_model moved out of kiro agent specs so kiro-cli deny_unknown_fields no longer drops KiroCrew agents)

## Overview

The config module (`kiro_crew/config/loader.py`) loads runtime configuration from `~/.kiro/crew/config.json` using stdlib dataclasses with sensible defaults.

## Data Home Location & Migration

KiroCrew's data root nests **under kiro-cli's own `~/.kiro/` base** so all
Kiro-family apps share a single directory a user can secure. `config_dir()`
(in `kiro_crew/config/paths.py`, re-exported from `kiro_crew/config/loader.py`)
is the single accessor and resolves to:

1. `$KIROCREW_HOME` when set (used as-is; refuses system directories like `/`,
   `/usr`, `/System`, `/etc`), else
2. `~/.kiro/crew` (the default).

**One-time migration.** On the first launch after upgrading an existing install,
`config_dir()` triggers a one-time relocation of the pre-move top-level
`~/.kirocrew` into `~/.kiro/crew` (implemented in `kiro_crew/home_migration.py`).
It is **copy-then-verify-then-quiesce-then-archive**: the legacy tree is copied
into the new home and every regular file is verified present before the source is
touched, then — before the final byte comparison — the legacy tree is
**quiesced** by atomically renaming it out of its live path to a private, per-PID
snapshot (`~/.kirocrew.quiescing.<pid>`), and it is that frozen snapshot that is
compared against the new home and then renamed to `~/.kirocrew.archived` (a
rollback copy — never auto-deleted). Quiescing before the compare closes a
compare→archive **TOCTOU**: the migration lock only serializes *migrations*, but a
normal legacy-era writer (an older release's CLI, or a cron-fired `kirocrew` on
the old version) does not hold it — comparing the *live* tree and only then
archiving it would leave a window in which such a writer mutates a file after the
comparison but before the archive, so the newest bytes would survive only in the
rollback archive, absent from the live home. Renaming the tree away first means no
writer using the canonical legacy path can touch the bytes that are
compared-then-archived. On a divergence the snapshot is restored to the canonical
`~/.kirocrew` path (and the run falls back to it); if legacy cannot be quiesced at
all, the migration retains it intact, does not archive, and does not mark complete
(retried on the next cold start). An interruption at any stage leaves the current
data intact under either `~/.kirocrew` or the snapshot (no data-loss window). The
move is idempotent, skipped while a gateway is live on the legacy home (retried on
the next cold start), and never runs when `KIROCREW_HOME` is set (dev/worktree
homes are not migrated). Before the copy starts it prints a one-line `migrating
data home …` notice to stderr so a slow first-run copy on a large home is not
mistaken for a hang.

**Excluded bulk trees (copied? no — relocated).** `_EXCLUDED_TOP_LEVEL_DIRS`
(`models`, `cache`) are large and regenerable, so they are never *copied* (that
would be a slow first-run copy and a permanent second on-disk copy of hundreds of
MB). But they are not destroyed either: after the divergence compare passes they
are **relocated** (atomically renamed, not copied) from the quiesced legacy
snapshot into the new home (`_relocate_excluded_dirs_into_new_home`), so the
sha256-pinned GGUF embedding model survives the upgrade. This matters because
embeddings are always-on in this fork — a migrating **offline / air-gapped /
metered-connection** user who lost `models/` would silently lose memory/knowledge
search until an HTTPS re-download succeeds (for an air-gapped host, possibly
never). The relocate only fills a GAP — a `models/`/`cache/` already present in
the new home (a fresh re-download or partial) is authoritative and kept, and the
snapshot's redundant copy is then stripped from the archive. On a cross-device
rename (`EXDEV`, new home on a different filesystem than the legacy home) or any
other rename failure the dir is left in the snapshot and `_strip_excluded_dirs`
removes it — falling back to the prior strip-and-redownload behavior (no worse
than before). Either way the archive carries no duplicate of the model bytes. A
same-named dir NESTED under real data is not excluded (the match is anchored at
the legacy root).

**Archive hardening + secret end-of-life.** The archive is a frozen snapshot of
the pre-move home, so it holds copies of the credential leaves (`.env`,
`token_signing.key`, `sel_hmac.key`, `refresh_chains.json`, browser cookies,
`profiles/`). The keystone gates those from the *agent* under the
`.kirocrew.archived` prefix, but to also shrink exposure to backup/sync tools and
other local processes: at archive time the archive tree is locked to the owner
(`0o700` dirs + `restrict_to_owner`/`0o600` on the secret leaves), and the
credential leaves are given an automatic **end-of-life** — once the completion
marker is older than `_ARCHIVE_SECRET_GRACE_SECONDS` (7 days), a later
`config_dir()` resolution shreds only the **replaceable credentials**
(`_EXPIRABLE_CREDENTIAL_LEAVES`: `.env`, `token_signing.key`,
`refresh_chains.json`, browser cookies, playwright state, `.local_secret`) from
the archive (`shred_archive_secrets_if_stale`). The governance/security **ceiling**
(`security_policy.json`, `profiles`, `admission_policy.json`,
`denied_commands.json`) and the tamper-evident **audit chain** (`sel_hmac.key`,
`security_events.jsonl`, `app_admission.json`) are DELIBERATELY retained — if the
archive is later restored as `~/.kirocrew` (the downgrade path), those files carry
the release's security ceiling, so expiring them would let a downgrade boot
WITHOUT its ceiling (a permission widening, worse than a stale credential).
Non-secret config/history is likewise kept as a rollback copy. Both the lockdown
and the shred are best-effort and never block startup.

**Downgrade / rollback.** A release older than the move knows nothing of
`~/.kiro/crew` or `~/.kirocrew.archived`: on downgrade it finds no `~/.kirocrew`
and starts empty (looks like data loss, is not). The documented recovery (see
`INSTALL.md`) is to stop KiroCrew and `mv ~/.kirocrew.archived ~/.kirocrew`; the
old release re-downloads the excluded `models/` on next start. Roll back within
the 7-day grace window to keep the archived credentials; after that the secret
leaves are shred (the live secrets remain in the new home) and Slack tokens must
be re-entered on the downgraded release. On a subsequent re-upgrade,
`_maybe_migrate_legacy_home()` does **not** blind-trust the completion marker
while a legacy dir exists — it re-runs the merge + byte-identical divergence
guard so interim downgrade data is reconciled, not stranded. `kirocrew doctor`
surfaces the archive (size + `rm -rf` cleanup command) under **Data Home** so the
permanent third keystone prefix has a user-driven end-of-life too.

**Uninstaller consideration (Zezhen's open question).** Because the data home now
lives under `~/.kiro/`, a hypothetical Kiro-family uninstaller that removes
`~/.kiro/` would also remove `~/.kiro/crew` and take KiroCrew's data — config,
credentials, memory DB, session history, and the SEL audit chain — with it. This
is a persisted-data one-way door.

The `~/.kirocrew.archived` rollback copy is **only a partial mitigation, not a
general safety net**, and the spec must not overstate it:

- It exists **only for upgraders** — a machine migrated from a pre-move
  `~/.kirocrew`. A **fresh install** (soon the majority of users) has no
  `~/.kirocrew.archived` at all, so the archive protects nothing for them.
- Even for an upgrader it is **stale by design** — a point-in-time snapshot taken
  at migration; every config change, new session, and credential rotation after
  that day is absent from it.

The real requirements this raises (tracked, not solved by the archive): any
Kiro-family uninstaller spec **MUST** either explicitly exclude `~/.kiro/crew`
from a `~/.kiro/`-wide wipe, or prompt before deleting it. Independently, a user
who wants the data home entirely outside `~/.kiro/` can set `KIROCREW_HOME` to
relocate it.

**Technical hedge — recovery-pointer breadcrumb.** `config_dir()` writes a small,
non-secret `~/.kirocrew.breadcrumb` pointer file at the top-level home
(`RECOVERY_BREADCRUMB_NAME`), deliberately **outside** `~/.kiro/`, recording the
data-home path (see `_write_recovery_breadcrumb`). It is idempotent (rewritten
only when the recorded path changes), best-effort (never blocks startup), and
written only on the default path (a `KIROCREW_HOME` override carries no `~/.kiro/`
wipe risk). It is **not a backup** — just a durable signpost that survives a
`~/.kiro/`-wide uninstaller wipe so a user or support script can find any
surviving data or understand what was removed. This narrows, but does not
eliminate, the one-way-door risk below; the release gate still stands.

> **Release gate (UNINSTALLER-EXCLUDE-CREW).** This is a pre-release,
> human-sign-off dependency, NOT a code change in this repo: the code cannot
> constrain another product's uninstaller. Before the first release that ships
> data under `~/.kiro/`, the KiroCrew product owner MUST confirm the
> Kiro-family uninstaller either excludes `~/.kiro/crew` or prompts — because a
> fresh install (soon the majority) has no `~/.kirocrew.archived` fallback, so a
> `~/.kiro/`-wide wipe would be unrecoverable total data loss. Until confirmed,
> the placement decision is acknowledged-but-owned here under this name so it is
> not lost. **Tracked as release-blocking in
> [issue #355](https://github.com/kirodotdev/KiroCrew/issues/355)** (label
> `release-blocker`); the sign-off must be recorded there and the issue closed
> before tagging the first release containing this change.

## Workspace Root

`workspace_root()` returns the base directory for all LLM working directories (kiro-cli cwd, task runner output, etc.):

Resolution order:
1. `KIROCREW_WORKSPACE` env var — used as-is (no `kirocrew-workspace` subdirectory appended)
2. Saved path in `~/.kiro/crew/workspace_dir` (written by `kirocrew setup`; re-running setup preserves the existing value as the prompt default)
3. Platform default:

| Platform | Path |
|----------|------|
| macOS | `/Volumes/workplace/kirocrew-workspace` (falls back to `~/workplace/kirocrew-workspace` if `/Volumes/workplace` doesn't exist) |
| Linux | `~/workplace/kirocrew-workspace` |

Each session/task gets an isolated subdirectory under this root via `_session_work_dir(key)`:
- Chat sessions: `kirocrew-workspace/cli_chat`, `kirocrew-workspace/{thread_ts}`
- Background: `kirocrew-workspace/_bg`
- Cron: `kirocrew-workspace/cron_{job_id}`
- TaskRunner: `kirocrew-workspace/taskrunner_main`
- Background session: `kirocrew-workspace/_bg`

The parent directory is created on first call if it doesn't exist.

## Project Directory Resolution

`KIROCREW_PROJECT_DIR` env var controls where agent config and skills are loaded from:

1. Env var `KIROCREW_PROJECT_DIR` (if set and valid)
2. CWD walk-up — CLI walks up from CWD looking for `skills/` + `src/kiro_crew/` (the `agents/` dir was removed in commit bbbc1f6e when agent config moved into `src/kiro_crew/config/`)
3. Saved path in `~/.kiro/crew/project_dir` (written by `kirocrew setup`)
4. Bundled fallback — `config/defaults.json` and `builtin_skills/` inside the package

The CLI (`cli.py:main()`) auto-detects and sets the env var at startup.

## Config Overlay (config.local.json)

User overrides can be placed in `~/.kiro/crew/config.local.json`. This file is
deep-merged on top of `config.json` at load time and is never touched by
`kirocrew setup` or package upgrades.

Resolution order:
1. Load `config.json` (managed by KiroCrew, may be regenerated on upgrade)
2. Deep-merge `config.local.json` on top (user-owned, never touched by setup/migration)
3. Return merged result

### CLI Usage

```bash
# Save a setting to config.local.json (persists across upgrades):
kirocrew config set --local agent.yolo true

# Save to config.json (may be overwritten on upgrade):
kirocrew config set agent.yolo true
```

### `config_local_path() -> Path`
Returns `~/.kiro/crew/config.local.json` (or `$KIROCREW_HOME/config.local.json`).

### `_deep_merge(base: dict, overlay: dict) -> dict`
Recursively merges overlay into base. Dict values merge recursively; all other
types in overlay replace base values.

## APIs

### `KiroCrewConfig.load() -> KiroCrewConfig`
Loads config from disk. Merges `config.local.json` overlay if present.
Returns defaults if file is missing or invalid.

**Hot-path cache.** `load()` is called per message / per request on several hot
paths. The expensive work — reading `config.json` (+ `config.local.json`),
`json.loads`, `_deep_merge`, and the full `jsonschema.validate` — is cached as
the validated, merged `data` dict, keyed on a fingerprint of both files
(`st_mtime_ns`, `st_size`, `st_mode`). On a cache hit, `load()` still builds
**fresh dataclasses from a deep copy**, so the many callers that mutate the
returned config in place (settings handlers, the write-back migration) never
corrupt the shared cache. The cache is mtime-keyed (not a blind TTL), so a
runtime edit is reflected on the next `load()`; `save()` also invalidates it
eagerly via `_invalidate_config_cache()`. The defaults-only path (neither file
present) is not cached.

### `KiroCrewConfig._resolve_agent_model() -> str`
Reads model from installed agent config (`~/.kiro/agents/kirocrew.json`),
falling back to the bundled `config_package_dir()/defaults.json` (i.e.
`src/kiro_crew/config/defaults.json`), then `DEFAULT_MODEL`.

### `KiroCrewConfig._resolve_named_agent_model(agent, agents_dir=None) -> str`
Returns a named agent's own kiro `model` field, or `""` if none. Used by
`SessionManager.get_or_create` so an explicit global `agent.model` ranks *below*
a per-agent model pin (per-agent pin > global default). Reads only the kiro
`model` slot. `agents_dir` is a dependency-injection seam for tests; defaults to
`kiro_agents_dir()`.

### `kiro_agents_dir() -> Path` (`config/paths.py`)
Leaf helper returning `~/.kiro/agents`. Lives in the leaf module so `loader.py`
(and `_resolve_named_agent_model`'s `agents_dir` DI seam) can locate installed
agent JSONs without importing `kiro_crew.agent` — which imports `config.loader`
and would create an import cycle.

### `KiroCrewConfig.create_provider_factory() -> Callable`
Returns a factory for LLMProvider instances. Resolves `"auto"` model
before creating the provider.

### `KiroCrewConfig.to_dict() -> dict`
Serializes config to the JSON structure used by `config.json`. Uses `_configured_port`
(the file value) instead of `dashboard_port` (which may be overridden by `KIROCREW_PORT`
env var) to avoid clobbering the saved port on write-back.

### `KiroCrewConfig.save() -> None`
Writes current config to `~/.kiro/crew/config.json` via `to_dict()`. Invalidates
the `load()` validated-data cache so the next load reflects the write immediately.

### `config_dir() -> Path`
Returns `~/.kiro/crew/` (nested under kiro-cli's `~/.kiro/` base). Overridden by
`KIROCREW_HOME` env var (refuses system directories like `/`, `/usr`, `/System`,
`/etc`). On the default (non-override) path, a pre-move `~/.kirocrew` is migrated
once into `~/.kiro/crew` — see "Data Home Location & Migration" above.

### `config_path() -> Path`
Returns `~/.kiro/crew/config.json` (or `$KIROCREW_HOME/config.json` if overridden).

### Agent Bookkeeping Sidecar (`agent_model_state.json`)

KiroCrew tracks two pieces of per-agent state that are **not** part of the
kiro-cli agent schema: `model_managed` (whether an agent's `model` tracks the
shipped default or is a frozen user pick) and `cc_model` (a per-agent Claude
Code model). kiro-cli validates `~/.kiro/agents/*.json` with serde
`deny_unknown_fields` and rejects the *entire* spec on any unknown key, then
silently falls back to the default agent (`--agent <name>` resolves to default
with only a stderr "no agent with name X found" line). To keep every spec
schema-valid, this state lives in a KiroCrew-owned sidecar
`~/.kiro/crew/agent_model_state.json` (honoring `KIROCREW_HOME`), keyed by agent
name:

```json
{
  "kirocrew":           {"model_managed": true},
  "kirocrew-heartbeat": {"cc_model": "claude-sonnet-4.6"}
}
```

- Read/written via `kiro_crew/agent_state.py` (atomic, lock-guarded near-leaf
  module: stdlib + `config.paths` + `atomic_write` only).
- `build_agent_config()` is pure (writes no spec key); `rebuild_agent_config()`
  seeds managed-state on a fresh/clean install (never clobbering a frozen pick).
- `_refresh_dynamic_fields()` sources managed-state from the sidecar and strips
  any stray `model_managed`/`cc_model` from the spec (steady-state self-heal).
- `migrate_agent_specs()` runs at startup (top of `rebuild_agent_config`): lifts
  the keys out of every `~/.kiro/agents/*.json` into the sidecar and removes
  them (idempotent), fixing installs polluted by older builds.
- The dashboard model PATCH writes the sidecar, never the spec; agent DELETE
  prunes the sidecar entry.

Note: KiroCrew is KiroACP (kiro-cli) only — the deleted `claude_code` provider
was the sole reader of spec `cc_model`, so `cc_model` is now dead config. The
lite/heartbeat installers still write it to the sidecar (harmless bookkeeping)
purely to keep the kiro spec schema-clean; nothing in the fork resolves it.

**Invariant:** `~/.kiro/agents/*.json` must contain only kiro-cli schema keys at
all times — after install, refresh, and any dashboard edit — or kiro-cli drops
the agent and silently falls back to default.

## Schema

```python
@dataclass
class AgentConfig:
    approval_mode: str = "auto"    # "auto" or "interactive"
    streaming: bool = True
    model: str = "auto"            # resolved from agent config
    provider: str = "acp"          # fixed to "acp" (kiro-cli) — the only provider
    sandbox: str = "off"           # default "off" (defer to kiro-cli's internal agent sandbox); "auto" (namespace on Linux, seatbelt on macOS), "strict", or "off"
    sandbox_allow_no_isolation: bool = False  # SEC-009: acknowledge running un-isolated when no sandbox backend exists; false = loud SECURITY warning, true = info-level
    enforce_denied_commands: str = "all"  # "all" or "kirocrew"
    soft_stop_budget_secs: float = 10.0  # seconds to wait for cooperative cancel before hard kill [0.5, 60.0]
    yolo: bool = False             # permanent YOLO mode (skip tool approval); tracked via _yolo_from_config flag
    max_subagents: int = 3         # concurrent subagent cap; 0 = auto-size from host memory/CPU. Load-time: 0 (auto) or [3, 64] — a fixed pin of 1/2 is raised to 3
    subagent_auto_max: int = 16    # ceiling on the auto-sized cap (max_subagents=0 only). Load-time clamped to [3, 64]
    subagent_max_turns: int = 100  # default per-subagent tool-call budget. Load-time clamped to [1, 200]
    subagent_result_ttl_secs: int = 3600  # seconds a delivered subagent's result.txt is retained before the reaper prunes it

@dataclass
class SessionConfig:
    timeout_secs: int = 3600       # 60 min idle timeout (DEFAULT_SESSION_TIMEOUT)
    empty_response_auto_continue: bool = True  # after TWO consecutive empty model responses, auto-send ONE synthetic "continue" nudge on the same live session (transcript-visible notice; bounded to once per user message; the config gate fails OPEN to the default so a config-load hiccup cannot disable self-healing). See session.md "Empty-response recovery ladder".
    autocompact_pct: float = 90.0  # context usage % at which auto-compaction triggers (5-90)
    pool_size: int = 2             # pre-warmed kiro-cli processes kept ready for instant session start; 0 disables. Load-time clamped to [0, 10]
    watchdog_rss_max_mb: int = 0   # recycle a session when its process tree RSS exceeds this many MiB; 0 disables (default). Busy sessions (turn in flight) are never recycled.

@dataclass
class TaskRunnerConfig:
    max_parallel_steps: int = 2    # max concurrent step sessions in parallel groups

@dataclass
class MemoryConfig:
    history_idle_hours: float = 3.0  # consolidate history after N hours idle
    history_max_days: int = 365      # prune daily history files older than this

@dataclass
class KnowledgeConfig:
    # Knowledge Library ingestion toggles. Embedding/retrieval settings live
    # under MemoryConfig (shared via create_embedder_from_config).
    auto_ingest_artifacts: bool = True                  # on by default; ingest local artifacts into the KB (aggregate "Artifacts" source)
    auto_ingest_artifact_kinds: list[str] = ["markdown", "text", "html", "json"]  # reader-extractable kinds (widget/svg excluded)
    embed_timeout_secs: float = 10.0                    # per-request embed timeout; 0/unset -> built-in TIMEOUT (10s)
    embed_content_budget: int = 0                       # chunk-content fold budget (chars); 0/unset -> built-in _EMBED_CONTENT_BUDGET

@dataclass
class ChannelConfig:
    activation: str = "mention"    # "always", "mention", "observe", or "off"
    agent: str = ""                # per-channel agent override (empty = use default)

@dataclass
class SttConfig:
    enabled: bool = True           # enabled by default; gated by whisper availability
    whisper_path: str = ""         # auto-detected if empty
    model: str = "turbo"           # turbo (~1.6 GB, 809M params, ~8x faster than large)
    device: str = "cpu"            # "cpu" or "cuda"
    timeout_secs: int = 300

@dataclass
class MessagingConfig:
    use_transport: bool = True     # route inbound Slack through SlackTransport → TurnDriver → SlackRenderer (the canonical path); false falls back to the native handle_message monolith

@dataclass
class SkillsConfig:
    max_triggered: int = 3         # max skills loaded per message (>=1)
    lazy_load: bool = False        # inject only a usage-ranked top-K of on-demand skills (long tail via skill_search / $skillname / triggers); off = legacy full skills dump
    # ... auto_create_from_sessions / auto_refine_on_deviation / extra_paths

@dataclass
class TelemetryConfig:
    enabled: bool = False          # main switch; off = metric call sites are no-ops, nothing written
    local_dir: str = ""            # local JSONL shard dir; empty = ~/.kiro/crew/metrics
    export_interval_seconds: int = 60  # local-exporter flush interval (>=1)

@dataclass
class DashboardConfig:
    url: str = ""                  # public URL for the dashboard (used in Slack links)
    # ... restore_sessions / bot_name / avatar / widget_density / auto_open_browser / etc.
    theme_mode: str = ""           # "dark" | "light" | "system"; empty = unset (frontend falls back to localStorage or "system")
    theme_color: str = ""          # color-theme slug (e.g. "kiro", "emerald", "monokai"); empty = unset
    onboarded: bool = False        # whether the "Choose your look" onboarding modal was completed
    tips_enabled: bool = True      # feature-discovery tips (GET /api/tips/next); live-read
    tips_cadence_hours: float = 6.0    # min hours between surfaced tips (server-side gate; clamped >= 0)
    tips_snooze_hours: float = 48.0    # hours before a snoozed tip is eligible again (clamped >= 0)
    tips_recency_decay: float = 0.6    # weighted-random newer-bias decay (clamped to [0, 1])
    tips_model: str = "claude-haiku-4.5"  # model for tips generation (pinned to Haiku for cost)
    tips_explore_ratio: float = 0.2    # probability of random catalog pick vs personalized (clamped to [0, 1])

@dataclass
class TelegramConfig:
    enabled: bool = False              # start the Telegram Bot API channel (long-polling) at gateway startup
    bot_token: str = ""                # @BotFather token; prefer the TELEGRAM_BOT_TOKEN credential
    allowed_user_ids: list[int] = []   # numeric user IDs allowed to drive the bot; empty = deny all (fail closed)
    soft_threshold_pct: int = 80       # prompt to /compact or /new when context passes this %
    allow_forum: bool = False          # serve supergroup forum Topics as per-Topic sessions (Slack-thread style). Fail-closed: also requires the supergroup's chat_id in allowed_forum_chat_ids, and only real Topics (message_thread_id present) are served — ordinary groups and the supergroup General chat are denied
    allowed_forum_chat_ids: list[int] = []  # numeric supergroup chat_ids permitted to run forum-topic sessions; empty = deny all groups (fail closed)

# Additional top-level DTOs (not fully expanded here — see loader.py):
# OrchestratorConfig, CronHistoryConfig, TunnelConfig, InstancesConfig, HeartbeatConfig,
# WorkspaceConfig, MemoryStoreConfig, ExternalRegistryConfig,
# KiroCrewAgentConfig, SlackConfig.

@dataclass
class KiroCrewConfig:
    agent: AgentConfig
    session: SessionConfig
    taskrunner: TaskRunnerConfig
    memory: MemoryConfig
    knowledge: KnowledgeConfig
    stt: SttConfig
    hooks_data: dict               # raw hooks from config.json
    dashboard_url: str = ""        # e.g. "http://my-host.example.com:8080"
    auto_update: bool = True
    snapshot_dir: str = ""         # snapshot output dir (default ~/.kiro/crew/snapshots)
    slack_channels: dict[str, ChannelConfig]  # per-channel config keyed by channel ID
    slack_dm_activation: str = "always"       # activation mode for DMs (D-prefix channels)
```

### Security-Bounded Config Clamp

Three resource-limit knobs are clamped to hard ceilings **at load time**, not just
at the dashboard write gate. The ceilings are the single source of truth in
`loader.py`:

| Constant | Value | Field |
|----------|-------|-------|
| `SUBAGENT_AUTO_MAX_CEILING` | 64 | `agent.subagent_auto_max`, `agent.max_subagents` |
| `SUBAGENT_MAX_TURNS_CEILING` | 200 | `agent.subagent_max_turns` |
| `POOL_SIZE_MAX` | 10 | `session.pool_size` |

`_SECURITY_BOUNDED_FIELDS` lists each `(section, key, min, max)`; the mins match
the existing runtime floors (0/1) so a legitimate in-range value is never
altered. `_clamp_security_bounds(data)` runs **once on the disk-read (cache-miss)
path, before the validated dict is cached** — so subsequent cache hits already
serve clamped values. It clamps out-of-range real integers in place (a JSON
`true`/`false` bool or any non-int is skipped and left to dataclass
coercion/defaults), logs a WARNING, and emits a best-effort `config_bounds_clamped`
SEL security event (never fatal — config loading must not raise).

Why load-time (not just the API): the REST API rejects out-of-range writes, but a
direct edit of `config.json` (any process running as the same OS user — including
a prompt-injected agent with file-write access) bypassed that gate entirely. Each
knob controls a resource-consumption dimension (concurrent subagent processes,
per-subagent turn budget, pre-warmed pool processes), so an inflated on-disk value
could exhaust host memory/CPU/the process table (DoS). The dashboard write gate
(`dashboard/handlers/core.py`) and the runtime pool cap **import these same
constants**, so write-gate / load-clamp / runtime-cap cannot drift apart —
closing the direct-config-edit DoS gap.

### Dashboard theme persistence

`DashboardConfig.theme_mode` / `theme_color` / `onboarded` are workspace-persistent
(shared across ports and devices) rather than browser-local. The frontend reads
them at boot via `GET /api/theme/boot`; empty `theme_mode`/`theme_color` mean
unset (the frontend falls back to `localStorage` or the built-in default).

### `ChannelConfig.from_dict(data: dict) -> ChannelConfig`
Parses a channel config entry from JSON. Invalid activation values fall back to `"mention"`.

### `KiroCrewConfig.channel_config(channel_id: str) -> ChannelConfig`
Returns the effective config for a channel:
1. Explicit entry in `slack_channels` → returned as-is
2. DM channel (`D`-prefix) → `ChannelConfig(activation=slack_dm_activation)`
3. Group/public channel (`C`/`G`-prefix) → `ChannelConfig(activation="mention")`

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `KIROCREW_HOME` | Override config/data directory | `~/.kiro/crew` |
| `KIROCREW_PORT` | Override dashboard port (dev mode — run dev + prod side by side) | `5476` |
| `KIROCREW_WORKSPACE` | Override workspace root directory | Platform-dependent |
| `KIROCREW_PROJECT_DIR` | Override agent config/skills directory | Auto-detected |
```

## Config File Format

```json
{
  "agent": {
    "approval_mode": "auto",
    "streaming": true,
    "provider": "acp"
  },
  "session": {
    "timeout_secs": 3600
  },
  "taskrunner": {
    "max_parallel_steps": 2
  },
  "memory": {
    "history_idle_hours": 3.0,
    "history_max_days": 365
  },
  "knowledge": {
    "auto_ingest_artifacts": true,
    "auto_ingest_artifact_kinds": ["markdown", "text", "html", "json"],
    "embed_timeout_secs": 10.0,
    "embed_content_budget": 0
  },
  "hooks": {},
  "slack": {
    "command": "kirocrew",
    "allowed_users": [],
    "tracking_channels": [],
    "dm_activation": "always",
    "channels": {
      "C0123ONCALL": { "activation": "always", "agent": "ops" },
      "C0456REVIEWS": { "activation": "mention", "agent": "reviewer" },
      "C0789GENERAL": { "activation": "off" }
    }
  },
  "dashboard": {
    "url": "http://my-host.example.com:8080"
  },
  "snapshot_dir": ""
}
```

The `dashboard.url` field controls where the dashboard is reachable. From it, the system derives the port to bind on, the bind address (`0.0.0.0` for non-loopback hosts, `127.0.0.1` otherwise), and the allowed origins for CSRF/WebSocket checks. When omitted, defaults to `localhost:5476`.

A **malformed** `dashboard.url` (e.g. an unterminated IPv6 literal `http://[::1` or a non-numeric port `http://host:notaport`) does **not** abort startup: `parse_dashboard_url` degrades to the defaults (`""` host, port `5476`) and logs a warning, so a single typo in the config can never take the gateway down on boot. `KIROCREW_PORT` still overrides the port regardless.

## Model Resolution Chain

When `agent.model` is `"auto"` (default):

1. `~/.kiro/agents/kirocrew.json` → `model` field (installed agent config)
2. `config_package_dir()/defaults.json` → `model` field (bundled `src/kiro_crew/config/defaults.json`)
3. Falls back to `DEFAULT_MODEL` (passed through to provider)

## Error Handling

- Missing file → defaults
- Invalid JSON → defaults (warning logged)
- Missing fields → individual defaults
