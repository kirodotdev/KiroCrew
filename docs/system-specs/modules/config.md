# Config Module

Last Updated: 2026-07-25 (Data-home resolution hardened against split-brain: (1) the completion marker `~/.kiro/crew/.data-home-ready` is now AUTHORITATIVE — marker present ⇒ trust the new home and NEVER re-migrate, even when a `~/.kirocrew` reappears alongside it (that dir is resurrection debris under the no-downgrade design, never promoted over the authoritative home; the old "marker + legacy ⇒ legacy always wins" re-migration is removed, closing a data-loss window where debris reverted `sel_hmac.key`/logs/`workspace/` and the recreate/TOCTOU race); (2) when a migration is skipped because a gateway is live, the resolving process JOINS whichever home the live gateway holds (legacy or new) so its `.local_secret` matches for internal IPC — a process pinned to the other home would 403 every internal API call and, writing into legacy, resurrect it. Prior — Data-home migration now overwrites READ-ONLY destination files: the re-migration copy passes a custom `copy_function` (`_copy_overwrite`) that clears a same-path destination's read-only state — adds the owner-write bit — before `copy2`, so a `0o444` git packfile under an app-source checkout no longer makes `shutil.copytree` raise `PermissionError` and abort the whole migration — the bug that trapped an already-populated new home in a permanent split-brain. Prior — Data-home migration simplified to copy-then-verify-then-delete: legacy `~/.kirocrew` is copied DIRECTLY into `~/.kiro/crew` — no staging dir, no quiesce snapshot — legacy files OVERWRITE anything already there (no more no-overwrite merge / byte-identical divergence guard / reconcile-with-backup) — and `~/.kirocrew` is deleted outright once verified, with no `~/.kirocrew.archived` rollback copy, no `~/.kiro/crew.pre-migration` divergent-home backup, no archive secret-expiry sweep. Symlinks are skipped entirely (not preserved, not dereferenced) rather than retargeted, which also removes the regenerable-bulk-dir relocation step (`models`/`cache` are simply never copied, matching a fresh install). `security._CREW_HOME_PREFIXES` dropped `.kirocrew.archived`; the `.kiro/crew.pre-migration` keystone entry is removed. Since an earlier (already-shipped) release could have left one of those now-ungated directories on disk, `config_dir()` added `_sweep_ungated_archive_leftovers` to delete either outright on every default-path resolution — see "Leftover-archive cleanup" below. There is now no downgrade/rollback path — see "No rollback" below; the release gate below is unchanged but now applies unconditionally (no install has an archive fallback). Prior — SessionConfig.empty_response_auto_continue added — default-ON gate for the dashboard chat runner's bounded empty-response auto-continue rung; see session.md "Empty-response recovery ladder". Prior — Divergent-new-home auto-reconcile: a pre-existing/stale `~/.kiro/crew` that diverges from legacy during the no-overwrite merge no longer strands the user on `~/.kirocrew` — reconciliation now completes the switch with legacy authoritative by renaming the divergent home aside to a keystone-gated (`~/.kiro/crew.pre-migration` is on `security._SENSITIVE_HOME_DIRS`), owner-locked `~/.kiro/crew.pre-migration/<ts>` backup, promoting the quiesced legacy snapshot into `~/.kiro/crew` (absolute intra-home symlinks retargeted), and marking complete; live data is unchanged (legacy, as the prior retain behavior), the sidelined home is a recoverable rollback, and a gateway live on the new home is never yanked aside. Prior — Data-home migration hardening: quiesce-before-compare closes the compare→archive TOCTOU (legacy tree atomically renamed to a per-PID `~/.kirocrew.quiescing.<pid>` snapshot before the divergence compare, then that frozen snapshot is compared + archived, so a concurrent legacy-era writer can't make stale state authoritative); regenerable bulk trees (`models`, `cache`) are RELOCATED (moved, not copied) from the snapshot into the new home so the GGUF model survives the upgrade for offline users (archive still carries no duplicate; falls back to strip-and-redownload on EXDEV); pre-copy stderr visibility notice; documented downgrade/rollback procedure; `kirocrew doctor` **Data Home** section surfaces the leftover `~/.kirocrew.archived` size + cleanup command. Prior — 2026-07-23 Data home moved from top-level `~/.kirocrew` to `~/.kiro/crew` — `config_dir()` now resolves to `~/.kiro/crew` by default (still overridable via `KIROCREW_HOME`), and triggers a one-time migration of a pre-move `~/.kirocrew` into the new home on first launch. See "Data Home Location & Migration" below. Prior — 2026-07-15 Removed the SecretaryConfig / TaskKeeperConfig / KeywordHook DTOs and the `secretary`/`taskkeeper` KiroCrewConfig fields — the Secretary/TaskKeeper features were dropped from the public fork (P472753900); config-baseline regenerated. Prior — 2026-07-13 Schema refresh: documented security-bounded load-time clamp — SUBAGENT_AUTO_MAX_CEILING=64 / SUBAGENT_MAX_TURNS_CEILING=200 / POOL_SIZE_MAX=10, `_clamp_security_bounds` + `config_bounds_clamped` SEL event; added clamped AgentConfig fields, SessionConfig.pool_size, MessagingConfig/SkillsConfig/TelemetryConfig/DashboardConfig (theme_mode/theme_color/onboarded) DTOs, `_resolve_named_agent_model`/`kiro_agents_dir`; corrected `_resolve_agent_model` fallback to `config_package_dir()/defaults.json`. 2026-06-22: AgentConfig: added sandbox_allow_no_isolation (SEC-009) field; agent_model_state.json sidecar: model_managed/cc_model moved out of kiro agent specs so kiro-cli deny_unknown_fields no longer drops KiroCrew agents)

Update 2026-07-26: Foreign-agent onboarding adds the independent
`dashboard.import_onboarded` gate, migration from `dashboard.onboarded`, a
strict merge-only settings projection. The loader preserves legacy numeric
strings and integral floats in existing config files while rejecting booleans
and non-integral, malformed, or non-finite values; imported settings are
type-validated before they are written, and the CLI converts typed values
before writing.

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

**The completion marker is authoritative.** Resolution is gated on
`~/.kiro/crew/.data-home-ready`: once it exists (written only after a verified
copy), the migration is done and the new home is authoritative — `config_dir()`
returns it and **never re-migrates**, even if a `~/.kirocrew` directory is
present alongside it. Because the migration force-deletes legacy and there is
no downgrade/rollback path (below), a legacy dir that reappears *after* the
marker can only be resurrection **debris** (stale files an old or legacy-pinned
process wrote back); it is never authoritative and is never copied over the new
home — doing so would revert same-named files (`sel_hmac.key`, logs,
`workspace/`) to stale versions. The debris is left in place and RETAINED for
manual cleanup — it stays under the credential-protected `.kirocrew`
sensitive-path prefix, but is NOT auto-removed (the leftover sweep only clears
`.kirocrew.archived` / `.kiro/crew.pre-migration`, never `.kirocrew` itself). A
legacy dir re-created later is likewise never promoted, so the recreate /
check-to-resolve race is benign. The conflicted state (marker + non-empty
legacy) is not silent: `config_dir()` logs a one-time WARNING and
`detect_data_home_conflict()` surfaces it in `kirocrew doctor`'s Data Home
section with a manual-cleanup hint. Migration therefore runs **only**
when the marker is absent (a genuine pre-move install whose legacy home is the
real data root).

It is **copy-then-verify-then-delete**: the legacy tree is copied directly into
the new home — OVERWRITING any file already present there under the same
relative path, so the legacy copy always wins over whatever pre-existed at
`~/.kiro/crew` (a partial prior migration, a dir a sibling Kiro tool created, or
a `KIROCREW_HOME=~/.kiro/crew` experiment), while a new-home-only entry with no
legacy counterpart is left untouched — every regular file is then verified
present at the destination, and only after that verification succeeds is
`~/.kirocrew` removed outright. **There is no rollback copy and no backup of
whatever the new home held before the overwrite** — once the move completes,
only `~/.kiro/crew` remains on disk. If the copy or verification fails,
`~/.kirocrew` is left fully intact for a retry on the next start. The move is
idempotent, skipped while a gateway is live on either home (the resolving
process JOINS whichever home the live gateway holds — legacy or new — so its
`.local_secret` matches the gateway's for internal IPC, rather than pinning to
the other home and failing every internal API call with 403; the completion
marker is NOT written on a liveness skip — it is reserved for a verified copy,
so a fail-safe `_gateway_is_live` OSError can't brand a partial home as
migrated — and the one-time copy simply completes on the next clean cold
start), and never runs when `KIROCREW_HOME` is set (dev/worktree homes are
not migrated). Before the copy starts it prints a one-line `migrating data
home …` notice to stderr so a slow first-run copy on a large home is not
mistaken for a hang.

**Read-only destination files are overwritten.** The copy passes a custom
`copy_function` (`_copy_overwrite`) instead of `shutil.copytree`'s default
`copy2`. When the new home is already populated (a partial prior migration, or
a directory a sibling Kiro tool created — the marker is ABSENT, so this is the
one-time first migration, NOT a re-migration; under the marker-authoritative
rule a marker-present home is never re-migrated), a same-path destination file
that is read-only would make `copy2`'s
truncate-open fail with `PermissionError`. This is not hypothetical: git writes
packfiles (`*.pack`/`*.idx`/`*.rev` under `.git/objects/pack`) mode `0o444`, and
app-source checkouts under the data home carry them, so an unguarded merge
reliably aborted on the first such file — leaving the user in a permanent
split-brain (legacy authoritative, new home half-populated, gateway pinned to
legacy). `_copy_overwrite` clears the destination's read-only state (adds the
owner-write bit, `st_mode | S_IWUSR`) before delegating to `copy2` (which then
copies the source's own mode bits over, restoring `0o444`), so legacy still wins
the overwrite as intended. The chmod is best-effort and only touches a path that
already exists at the destination — never the read-only source.

**Symlinks are skipped, not preserved.** The copy does not pass
`symlinks=True` to `shutil.copytree`, so any symlink in the legacy tree —
intra-home, pointing outside the home, or dangling — is skipped entirely
(matched by `_make_copy_ignore` alongside sockets/FIFOs/devices) rather than
followed or reproduced. This is a deliberate simplification: preserving
symlinks across a merge has real edge cases (a legacy symlink can't overwrite
a real file already at the destination; an absolute intra-home symlink would
dangle once legacy is deleted; a dangling symlink would abort the whole
`copytree` call if dereferenced), and the data home has no user-facing
symlinks worth carrying forward. The practical effect is limited to internal
convenience links a user or tool may have created inside the data home.

**Excluded bulk trees.** `_EXCLUDED_TOP_LEVEL_DIRS` (`models`, `cache`) are
large and regenerable, so they are never copied — carrying them forward would
make the first-run copy needlessly slow for no benefit. The new home simply
regenerates them on demand (the sha256-pinned GGUF embedding model re-downloads
over HTTPS on next start), exactly as a fresh install does. A same-named dir
NESTED under real data is not excluded (the match is anchored at the legacy
root).

**No rollback.** Because the legacy home is deleted (not archived) and any
pre-existing divergent `~/.kiro/crew` is overwritten (not backed up), there is no
supported downgrade path: a release older than this move knows nothing of
`~/.kiro/crew`, and after the migration completes there is nothing left under
`~/.kirocrew` to restore from. A user who needs to preserve the pre-move state
must back it up themselves (e.g. `cp -a ~/.kirocrew ~/.kirocrew.manual-backup`)
BEFORE upgrading.

**Leftover-archive cleanup (`_sweep_ungated_archive_leftovers`).** An EARLIER
release of this migration (already shipped on `main` before this no-retention
contract) could have left `~/.kirocrew.archived` (a full rollback copy) or
`~/.kiro/crew.pre-migration/<timestamp>` (a sidelined divergent-home backup) on
disk. Neither path is on the security keystone anymore (`_CREW_HOME_PREFIXES`
dropped `.kirocrew.archived`; the `.kiro/crew.pre-migration` entry was removed
outright — nothing creates them, so gating them was dead weight), which means a
leftover one from that earlier release is now UNGATED: its frozen credentials
would otherwise be agent-readable indefinitely with nothing to ever prompt a
cleanup. `config_dir()` therefore deletes either directory outright (matching
this migration's no-retention design — not just shredding the credential
leaves) on every default-path resolution. It never follows a symlink at either
root, is best-effort (a removal failure is logged and retried on the next
start, never blocks startup), and is a quiet no-op once both are gone.

**Uninstaller consideration (Zezhen's open question).** Because the data home now
lives under `~/.kiro/`, a hypothetical Kiro-family uninstaller that removes
`~/.kiro/` would also remove `~/.kiro/crew` and take KiroCrew's data — config,
credentials, memory DB, session history, and the SEL audit chain — with it. This
is a persisted-data one-way door, and — unlike when an archived rollback copy
existed — there is now no `~/.kirocrew.archived` fallback for ANY install
(upgrader or fresh), so such a wipe is unrecoverable total data loss.

Any Kiro-family uninstaller spec **MUST** either explicitly exclude
`~/.kiro/crew` from a `~/.kiro/`-wide wipe, or prompt before deleting it.
Independently, a user who wants the data home entirely outside `~/.kiro/` can set
`KIROCREW_HOME` to relocate it.

**Technical hedge — recovery-pointer breadcrumb.** `config_dir()` writes a small,
non-secret `~/.kirocrew.breadcrumb` pointer file at the top-level home
(`RECOVERY_BREADCRUMB_NAME`), deliberately **outside** `~/.kiro/`, recording the
data-home path (see `_write_recovery_breadcrumb`). It is idempotent (rewritten
only when the recorded path changes), best-effort (never blocks startup), and
written only on the default path (a `KIROCREW_HOME` override carries no `~/.kiro/`
wipe risk). It is **not a backup** — just a durable signpost that survives a
`~/.kiro/`-wide uninstaller wipe so a user or support script can find any
surviving data or understand what was removed. This narrows, but does not
eliminate, the one-way-door risk above; the release gate still stands.

> **Release gate (UNINSTALLER-EXCLUDE-CREW).** This is a pre-release,
> human-sign-off dependency, NOT a code change in this repo: the code cannot
> constrain another product's uninstaller. Before the first release that ships
> data under `~/.kiro/`, the KiroCrew product owner MUST confirm the
> Kiro-family uninstaller either excludes `~/.kiro/crew` or prompts — because
> there is no `~/.kirocrew.archived` fallback for any install, so a
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
    verbosity: str = "default"     # "default" | "concise"; "concise" injects a brevity guideline block into the agent prompt ({{VERBOSITY_BLOCK}}). Read/written via GET/PUT /api/dashboard/config (rejects values other than default|concise). Resolved for all transports in ContextBuilder._resolve_prompt_templates.
    theme_mode: str = ""           # "dark" | "light" | "system"; empty = unset (frontend falls back to localStorage or "system")
    theme_color: str = ""          # color-theme slug (e.g. "kiro", "emerald", "monokai"); empty = unset
    language: str = ""             # dashboard UI language, BCP-47 (e.g. "en", "zh-CN"); empty = auto-detect from the browser. See "Dashboard UI language" below.
    onboarded: bool = False         # whether the "Choose your look" onboarding modal was completed
    import_onboarded: bool = False  # whether foreign-agent import was completed or skipped
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

### Dashboard UI language

`DashboardConfig.language` selects the dashboard interface language. It rides the
same two endpoints as the theme fields — surfaced by `GET /api/theme/boot`
(unauthenticated, so the SPA can pick a language before the token flow completes
and avoid an English flash) and written by `PUT /api/config/theme`
(`{"language": "<tag>"}`). Both responses are built by one helper
(`handlers/core.py::_theme_payload`), so every read site returns the same shape.

Resolution precedence, implemented in `website/src/i18n/detect.ts`:

1. this config value (mirrored into `localStorage['mc-lang']` for a synchronous
   first paint),
2. the browser's `navigator.languages`, matched exact-then-primary-subtag
   (so `zh`/`zh-Hans` resolve to `zh-CN`),
3. `en`.

`""` is a first-class value meaning **auto-detect**, not "missing" — the picker's
Auto option writes `""` to clear a previous explicit choice. An explicit choice
always outranks detection, so a user who selects English on a zh-CN machine is
not re-detected back to Chinese on the next load.

The backend validates **shape only** (`_LANGUAGE_TAG_RE`, a conservative BCP-47
subset), not membership in the set of shipped catalogs. That keeps "which
languages exist" a pure frontend data change (`SUPPORTED_LANGUAGES` + one
`locales/<tag>.json`) and never requires a backend edit to add one; a well-formed
tag with no catalog falls back to detection client-side.

### Foreign-agent import onboarding state

`DashboardConfig.import_onboarded` is a separate workspace-persistent gate from
`dashboard.onboarded`. The import gate controls the first-run foreign-agent
review; `onboarded` continues to control the existing theme/feature onboarding.
The import gate is evaluated first. Completing or skipping import sets only
`import_onboarded`; it does not silently complete the later onboarding.

For backward compatibility, a config that omits `dashboard.import_onboarded`
is migrated from `dashboard.onboarded`. An already-onboarded user therefore
starts with `import_onboarded=true` and retains legacy status past the new first-run
gate, while a new or not-yet-onboarded workspace sees import before the existing
onboarding. `GET /api/theme/boot` exposes the resolved `import_onboarded` boolean
alongside the existing non-secret theme boot fields.

The frontend also recognizes the older browser-only `mc-onboarded` marker when
no `mc-import-onboarded` marker exists. Before applying false server defaults,
it persists both onboarding flags through `PUT /api/config/theme`; an explicit
newer import marker remains a cache only and continues to yield to server state.

Foreign settings are never deep-merged into `config.json`. The importer applies
only its explicit non-security settings allowlist, preserves every existing
KiroCrew value on collision, and reports unsupported or secret-bearing source
settings without copying them. Foreign credentials, security policy,
approval/sandbox settings, agent/runtime state, hooks, and arbitrary unknown
config sections cannot enter configuration through this path.

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
