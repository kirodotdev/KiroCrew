# Config Module

## Overview

The config package loads runtime configuration from `~/.kiro/crew/config.json`
using stdlib dataclasses with sensible defaults. `config/sections.py` and
`config/loader.py` are the two facades callers import. Each composes the owner
modules below and re-exports their names as the same objects, so an existing
`config.loader.X` or `config.sections.X` import keeps resolving.

A patch reaches the code that looks the name up in the patched module, and only
that code. `config/loader.py` is a mirrored facade (`kiro_crew._facade_mirror`):
its code lives in the `config/loader_*.py` owners listed below, and a write or
delete of a name through `config.loader` lands in every owner that binds it, so the
names the loader family reads are call-time seams on the loader: `config_path`, `config_dir`, `config_local_path`, `env_path`,
`workspace_root`, `_default_workspace_base`, `write_config_atomically`,
`update_config_locked`, `atomic_write`, `_config_write_lock`,
`_config_fingerprint`, `_validate_config_data`, `_persist_config_migration`,
`_apply_document_migrations`, `_log_config_clamp_event`,
`_DEFAULT_CHAT_TURN_TIMEOUT_SECS`, `DEFAULT_POOL_SIZE`, `unsandboxed_exec_declared`,
`acp_backend_declared`, `publish_config_timezone`, `record_adoptions` (the loader passes it to the
migration transform at call time), each `_build_*` name as `build_config`
calls it, and the published-snapshot globals. A helper or constant read INSIDE a
relocated builder or migration rule is patched on the module that reads it:
`config.section_builders` for the value coercers, the STT, computer-use and
instance bounds, `coerce_runtime_ceiling` (which reads the monitoring bounds in
`monitoring.limits`), the `_resolve_stub_*` roster
readers and the section DTO classes a builder constructs (a replacement must be a
dataclass, and its field defaults are what an omitted key reads); `config.migration` for
`auto_adoptable`, `drop_drifted_keys`, `stored_value_or_none`,
`superseded_default_drift` and `drift_summary` (neither module is a loader owner,
so the loader's mirror does not reach them). The loader facade and
`config.migration` hold the same warn-once set `_REPORTED_SUPERSEDED_KEYS`: clear
it with `.clear()`, never rebind it. `test_config_refactor_contract.py` pins
representative seams of each kind; `test_facade_mirror_guards.py` holds every name
the test trees patch through the loader to reaching every owner that binds it.

| Owner | Owns |
|---|---|
| `config/fields.py` | `_meta` field metadata, the `_safe_*` value coercers every section shares, and `field_default`, the one reader of a DTO field's declared default (`SectionReader` reads through it). An integer coercer keeps a legacy numeric string or integral float already in a config file (`"5"` -> 5, `5.0` -> 5) and rejects booleans and non-integral, malformed or non-finite values in favour of the default. A leaf: it imports nothing from `kiro_crew`. |
| `config/sections.py` | The facade other specs and tests name for the section DTOs below and for the three domain owners after them: it re-exports every name they define as the same object, and defines nothing of its own. |
| `config/sections_defaults.py` | `SectionReader`, which resolves a builder's omitted key and coercer fallback to the DTO field's declared default, and the named defaults a DTO field and a load fallback share (`DEFAULT_MODEL`, `DEFAULT_POOL_SIZE`, ...). |
| `config/sections_agent_env.py` | The environment an agent process receives: the `agent.env` name policy and its refusals, `agent.child_env_defaults` and `agent.deepseek_env`. |
| `config/sections_agent_fields.py` | Field coercion for `agent` and the crew records: model pins, role maps, effort, the context-token map, approval modes, the avatar record and bot-name filter, the jail, ACP-backend, YOLO-duration and completion-keep readers, and the KAS bundle url. |
| `config/sections_agent.py` | `AgentConfig`, and the readers that resolve against its declared defaults. |
| `config/sections_session.py` | `session`, the crew record `KiroCrewAgentConfig`, `workspaces`, `session_environments` (its DTOs; validation lives in `config/session_environments.py`) and a session's `ResolvedBindings`. |
| `config/sections_limits.py` | The bounds constants the loader's clamps and the dashboard's write gate share, and `resource_limits`. |
| `config/sections_tailnet.py`, `config/sections_dashboard.py`, `config/sections_dashboard_hosts.py` | `dashboard`: `TailscaleConfig` and its parser; the DTO with its Jira and link-rule records; the host-list and link-pattern coercers. |
| `config/sections_slack.py`, `config/sections_telegram.py`, `config/sections_channels.py` | The messaging channels: Slack with its per-channel records and the activation modes; Telegram; and the others with the coercers they share. |
| `config/sections_stt.py`, `config/sections_decisions.py`, `config/sections_telemetry.py` | Speech-to-text and its degradation rules; `decisions`; `telemetry` and `wakatime`. |
| `config/memory_sections.py` | `memory`, `knowledge`, `skills`, `session_summary` and the named `memory_stores` records. |
| `config/integration_sections.py` | `mcp`, `mcp_gateway` (with the MCP stub roster readers the gateway seed shares), `instances`, `tunnel`, `publish`, `computer_use`, `aws` and the external app `registries`. |
| `config/service_sections.py` | `taskrunner`, `messaging`, `cron_history`, `monitoring`, `heartbeat`, `watchdog`, `work_ledger` and `power`. |
| `config/section_builders.py` | The `_build_*` helper of 28 sections, grouped by the module that owns each section's DTO. Each reads its section through `sections.SectionReader`, so an omitted key builds the DTO field's default and a `read()` coercer falls back to it; the values a builder keeps of its own and the deliberate departures are listed under "Defaults come from the DTO fields". Four `_build_*` helpers stay in the loader (agent, session, telemetry, dashboard). Sections with no helper are built inline in the loader's `build_config` (`heartbeat`, the external app `registries`, `memory_stores`, the `agents` crew roster, `workspaces`) or by their DTO (`DecisionsConfig.from_raw`, `ResourceLimitsConfig.from_raw`, `ChannelConfig.from_dict` for `slack_channels`). |
| `config/migration.py` | The write-back migration ids, the document transform `apply_document_migrations`, the one-shot `connections_ui` marker name, the legacy `skills.lazy_load` cohort test, superseded-default reporting, and the in-memory half of an adoption. |
| `config/resolution.py` | Raw overlay merging, top-level section classification, and degraded-input tracking. |
| `config/validation.py`, `config/schema.py` | Schema validation with the validated-data cache, and the JSON schema and restart registry built from the DTOs. |
| `config/paths.py`, `config/live.py`, `config/superseded_defaults.py` | Pure path primitives, the one live-config watcher and applier registry, and the superseded-default registry with its acknowledgment ledger. |
| `config/loader.py` and `config/loader_*.py` | The loader facade and its nine owners: `KiroCrewConfig` (load, serialize, save, model and provider resolution) and the load core, one responsibility per owner (see "The loader owners" below). |

Imports run one way: `fields`, then the section owners (the `sections_*`
owners import each other lowest layer first, in the facade's `_OWNERS` order),
then `sections`, then `section_builders` and `migration`, then the loader owners
in the order of the table below, then the `loader` facade. Each module imports
only modules earlier in that order plus the existing leaves (`sections` and
`sections_tailnet` import `resolution`, `migration` imports
`superseded_defaults`, and neither leaf imports back), and none of the section
owners, `section_builders` or `migration` imports `loader`, `schema` or
`validation`; `test_config_module_boundaries.py` pins the graph. No loader owner
imports the facade. Every module
split out of the loader logs as `kiro_crew.config.loader`, so a relocated warning
keeps the record name operators filter on.

`sections.py` stays the import path because other specs name it for these DTOs:
[messaging](messaging.md) (the channel restart flags), [slack-gateway](slack-gateway.md),
[crew-mode](crew-mode.md), [history](history.md), [stt-streaming](stt-streaming.md),
[metrics](metrics.md), [decisions](decisions.md), [security](security.md),
[model-selection](../common/model-selection.md), [model-fallback](model-fallback.md),
[subagent](subagent.md), [acp-client](acp-client.md) and
[crew-log-projection](crew-log-projection.md). It is a `_facade_mirror` facade: a patch
through it reaches every owner that binds the name, and the STT warn-once markers an
owner rebinds are read from that owner. Four source scans allow a construct only in its
owner: `ResourceLimitsConfig.from_raw` (`sections_limits.py`), `_tailscale_config_from`
(`sections_tailnet.py`), the `_AVATAR_MOTIONS` literal (`sections_agent_fields.py`) and
the `AgentConfig.acp_backend` declaration (`sections_agent.py`).

### The loader owners

`config/loader.py` stays the import path and the patch seam: it keeps every name
it bound before its owners existed, and serves each owner-defined name from the
owner that defines it. Its own code is the effective-model resolution
(`resolve_effective_model`, `resolve_app_spawn_model`, `app_model_pin`, the
kiro-agent reference check) and `build_provider_factory`, the platform seam in
front of `KiroCrewConfig.create_provider_factory`. That method builds
`AcpProvider` through `agent_sdk.drivers.acp.build_acp_provider`, so no config
module imports the ACP layer. The rest lives in one owner per responsibility, lowest layer first:

| Owner | Owns |
|---|---|
| `config/loader_credentials.py` | Credential keys (`CRED_*`, `CREDENTIAL_KEYS`, named by [code-style](../common/code-style.md)), the `.env` reader and its decoder, `secret://` env resolution, the `kiro-cli` API-key injection, the dashboard port. |
| `config/loader_locations.py` | The data-home and workspace path helpers (`config_path`, `config_local_path`, the per-feature state paths, `workspace_root`, `workspace_dir_from_entry`, `materialize_workspace_dir`) and `read_local_secret`. |
| `config/loader_document.py` | Document I/O: `_raw_config`, `read_config_for_update`, the publish floor, `write_config_atomically`, `update_config_locked` and its lock, the meta stamp, the unsandboxed-exec policy and `acp_backend_declared` read from the raw document, and the validated-document cache fingerprint. The config-writer scans exempt it with `loader_config.py` (`save`). |
| `config/loader_builders.py` | The load-time field readers, the security clamp and its SEL event, the agent, session, telemetry and dashboard `_build_*` helpers, the harness-choice mapping (`_read_backend_choice`, `_stored_agent_dict`, `_restore_stored_harness_fields`), and the loop-stall and managed-launch readers. |
| `config/loader_snapshots.py` | Published snapshots (the materialized agents with their scan, refresh, dispatch and default-agent healer; the alias table; the compaction threshold; the timezone) and the resolvers that answer from them. Every `global` it rebinds has its one home here, served through the facade. |
| `config/loader_agents.py` | Agent resolution: `resolve_crew_identity`, `resolve_agent_identity`, `resolve_agent_bindings`. |
| `config/loader_writeback.py` | Write-back persistence: `_persist_config_migration`, the backup, and the `_apply_document_migrations` seam that passes `record_adoptions` to the transform. |
| `config/loader_config.py` | `KiroCrewConfig`: fields, `load`, `to_dict`, `save`, model, effort and credential resolution, and the provider factory. |
| `config/loader_pipeline.py` | The load pipeline (`ConfigDocument`, `read_config_document`, `build_config`, `persist_write_back`) and the one-call loaders built on the class (`workspace_dir_for`, `default_project_dir`, `load_config_with_content_stamp`, `load_loop_stall_exit_after`). |

**The harness-choice record.** `agent.acp_backend` and `agent.member_acp_backend` absent or `""` mean no harness choice and resolve to the default; a stored `"kiro"` is the Kiro CLI choice ([harness-parity](harness-parity.md) H1), except in `agent.member_acp_backend`, where `_read_backend_choice` (`config/loader_builders.py`) reads it as KAS before the selection gate, so every reader of the field sees the backend crewmate threads run on and the mapped value is gated like any other pick. In `agent.acp_backend` `_read_backend_choice` reads that choice as KAS (`kiro_cli_pick_backend`; the selection gate itself answers Kiro CLI for Kiro CLI) once this process confirmed the host's kiro-cli offers v3 and while KAS is selectable (`KIRO_SELECTION_BACKEND`; `docs/decisions/2026-10-28-a-kiro-cli-choice-runs-on-kas.md`) — the gateway's KAS floor confirms it at boot, and a non-gateway process that spawns an ACP agent (the CLI, the eval and task runners, history consolidation) confirms it via `ensure_kiro_cli_kas_verdict` before its config load — so the field and the dashboard response read `"kas"` there while `config.json` keeps `"kiro"`; a process that never confirms it (an MCP server, a process whose kiro-cli lacks v3, the bundle route) reads `"kiro"`; `acp_backend_stored_as_kiro()` (`config/loader_document.py`, overlay first) is the one reader of the stored choice, for the KAS floor, the per-process probe, `kirocrew doctor`, the once-only bell note and the Agent Harness panel's save-KAS offer. The loader maps the stored vocabulary to runtime ids on the way in (`_read_backend_choice`), and `to_dict()` maps back (`_stored_agent_dict`), so `config get`, the dashboard response and every full-document writer see `"kiro"` for a Kiro CLI harness that runs as itself, never the runtime `""`. `to_dict()` carries the RESOLVED value; `save()` writes both keys back as `config.json` stores them (`_restore_stored_harness_fields`, read from disk like `unsandboxed_exec_declared`), so it never invents a choice and never replaces a policy-denied pick with the floor. An in-memory change to either key is therefore not persisted by `save()`; the delta writers (`config set`, the dashboard PATCH) are the write path. `acp_backend_declared()` reports a stored choice, overlay first.

Two call cycles run through the owners, and each is broken by one function-local
import of the higher owner, the idiom this package already uses for the ACP
provider: the default-agent healer (`reset_dangling_default_agent`,
`_locked_workspace_dir`) loads the `KiroCrewConfig` that dispatches through the
snapshot it heals, and `KiroCrewConfig._load_resolved` runs the pipeline that
builds instances of the class. `test_loader_owners_contract.py` pins each of the
three imports and shows a patch made through the facade reaches it.

Three seams behave differently from the one-module loader:

- The mirror indexes no dunder, so a patch of `config.loader.__version__` reaches the
  facade alone. The meta stamp reads `loader_document`'s own binding.
- `typing.get_type_hints` raises on the seven owner functions that annotate
  `KiroCrewConfig` under `TYPE_CHECKING` (`resolve_crew_identity`, `resolve_agent_bindings`,
  `crew_autocompact_pct`, `_workspace_name_for_dir`, `publish_agent_alias_snapshot`,
  `publish_autocompact_pct`, `publish_config_timezone`): the cycle forbids a runtime import
  there.
- `importlib.reload(config.loader)` re-runs the facade only. Owner bodies run again only when
  the owners are purged and imported with it.

New section constants, including local speech's automatic-language default, are
read from `config.sections` directly; they do not expand that historical facade.

The loader reads `_sections.SessionEnvironmentsConfig` without expanding the
frozen compatibility reexports; schema-property tests resolve each annotation
with `typing.get_type_hints` on its defining class and traverse every child. Its
live applier is `GatewayOrchestrator.session_environments`.

New work goes to the owner of its responsibility, not to a facade: a field to
the module that owns its section's DTO, a new section's DTO to the `sections_*`,
`memory_sections.py`, `integration_sections.py` or `service_sections.py` owner of
its domain, a shared coercer to `config/fields.py`, a section's `_build_*` helper to
`config/section_builders.py`, a migration rule to `config/migration.py`, and
overlay, validation, schema, live-applier and path work to its owner in the
table above. Loader work goes to the `config/loader_*.py` owner of its row;
`config/loader.py` itself takes only facade glue, effective-model resolution and
`build_provider_factory`, and `sections.py` only re-exports what an owner defines.

A feature whose section spends tokens on the user's behalf defaults to off and
documents its knobs in its own spec — `session_summary` is the current example
(see [session-summary.md](session-summary.md)), following the shape
`SkillsConfig` established: every field carries `_meta` label/help for the config
surfaces, out-of-range values are clamped with a warning rather than raising, and
a malformed section degrades to defaults so a hand-edited file cannot prevent the
gateway from starting.

`dashboard.dynamic_dashboard_cards` follows the same cost rule: default false,
hot-applied through the live watcher, with a native control on Dynamic Dashboard
surfaces. It enables event-driven per-session HTML cards without enabling
`session_summary`. Runtime status and native decisions remain available when it
is off. Route and watcher registration do not import or construct the optional
producer. Both server entrypoints activate it in the deferred post-listen watcher
task when enabled, or on the first live enable; later toggles reuse that instance
and its charged budgets. Fixed call, byte, cache and iframe limits and the disable/cancellation
contract are documented in
[learn-cron-dashboard](learn-cron-dashboard.md#automatic-session-status-cards).

Dynamic capabilities is not a `dashboard.*` key: it is a per-crewmate field,
`agents.<name>.dynamic_capabilities` (default false), set on the crewmate's
Profile card and read per request. A config that still holds an old
`dashboard.crewmate_dynamic_capabilities` key loads unchanged; the key is not
read. Owned by [crew-mode](crew-mode.md#dynamic-capabilities).

## Orchestration prompt contract

`config/prompt.md` guides direct work and delegation using a concrete-value
policy. Parent-plus-child parallelism depends on the spawn receipt's delivery
capability. Runtime checks and compatibility are
owned by [subagent.md](subagent.md), not inferred from prompt wording.

## Embedding rebuild request publication

`memory.embed_rebuild_generation` is an explicit-apply request identity, not a
model label or a completion counter. Model apply commits it with the validated
model settings before invalidating vectors. Managed vector publications hold the
same config sidecar lock while checking that request and committing their SQLite
write. They cannot publish an old result after a newly committed request, even
when the store has not yet been reconciled. Store-local signatures and handled
requests remain checked inside SQLite write admission. Conditional model rollback
preserves the request and unrelated edits, and refuses a competing model/request
change. An untouched upgrade retains the empty request default.

This field is install-local runtime obligation state even though it is published
atomically in the model-settings transaction in `config.json`. It is not a
portable preference or a completed-work flag. Back up and restore the model
settings, this request, and memory databases together. Copying a non-empty request
to a different installation intentionally invalidates stores that have not
acknowledged that request, including aligned ones; do not distribute it in a
fleet configuration template. Clearing/resetting it while keeping existing
memory databases loses the outstanding same-label rebuild obligation. The
ordinary model-space signature check remains, but cannot replace that lost
request. Such a reset is not a supported way to cancel or complete rebuilding:
after a reset or mismatched restore, explicitly apply the intended model again
(`Rebuild memory vectors` for an unchanged path) before relying on vector search.
That publishes a fresh obligation for open and later-opened stores. This design
accepts config/state coupling to retain one atomic publication point; it does
not claim to recover obligations that an operator deletes out of band.

Managed vector publication resolves a symlinked config through `_lock_target`,
exactly as the config writer does, before taking its sidecar lock. Within a
store operation the order is config sidecar, the store's process-local lock,
then SQLite write admission. A writer waiting for the config sidecar therefore
holds no database lock that could stall a concurrent reader. Both locks remain
held through vector validation, commit or rollback. Model apply releases its config mutation before
aligning stores; it never holds that sidecar while waiting for a store lock.
Native inference runs before those publication locks. Store close releases its
SQLite and lifetime handles without saving a vector index or acquiring config
admission again, including when rollback failure closes an uncertain connection.

## Data Home Location

Kiro Crew's data root nests **under kiro-cli's own `~/.kiro/` base** so all
Kiro-family apps share a single directory a user can secure. `config_dir()`
(in `kiro_crew/config/paths.py`, re-exported from `kiro_crew/config/loader.py`)
is the single accessor and resolves to:

1. `$KIROCREW_HOME` when set (expanded and resolved; refuses any filesystem or
   drive root and system directories such as `/usr`, `/System`, `/etc` and the
   macOS-resolved `/private/etc`), else
2. `~/.kiro/crew` (the default).

The override is resolved and validated once per distinct raw value
(`_override_home_pin`, keyed on the env string): a changed value is resolved
afresh, while a relative or `~` value stays anchored to the cwd and `HOME` of the
first call, and a repoint of the same string's symlink target is not seen until
the value changes or the process restarts.

**No migration — net-new users only.** All supported installs start directly in
`~/.kiro/crew`; there is no `~/.kirocrew` to relocate, so `config_dir()` simply
resolves and `mkdir`s the home above. The one-time `~/.kirocrew` → `~/.kiro/crew`
data-home migration that earlier releases carried has been **removed** (see
`docs/system-specs/post-launch-removals.md`). A leftover top-level `~/.kirocrew`
from an old install is never read, migrated, or deleted; it is left in place —
still credential-gated by the `.kirocrew` security-path spelling — and `kirocrew
doctor` reports it, warning rather than advising deletion when it still holds a
virtual environment (`venv`/`.venv`/`venvs`), since that may be the running
interpreter. The same section warns, without failing, when the workspace root or
`KIROCREW_PROJECT_DIR` is the data home or one of its parents (`$HOME` is the
usual case): that puts the managed browser launcher inside the agent-writable
tree, where the launcher fence refuses it, so the one-click browser install
refuses up front (see the browser module).

**Repository-controlled uninstall contract.** Every uninstall path owned by this
repository preserves the Kiro Crew data home by default. `kirocrew service
uninstall` removes only its service definition; the Python/npm packages define
no uninstall lifecycle hook; and the desktop shell's generated NSIS uninstaller
removes only installed program state: its install directory, shortcuts,
channel-scoped updater cache, and any legacy “start with Windows” registry entry
(`deleteAppDataOnUninstall` stays false), without
resolving or removing the Kiro Crew home. App Kit uninstall also preserves the
app's `data/` subtree unless the dedicated `purge_data=true` API action (CLI
`--purge-data`, or an explicit dashboard choice) is supplied. The API checks
for the literal boolean `true`; absent, legacy, or malformed values fail closed
to preservation. A whole-home purge is never coupled to uninstall.

**Uninstaller consideration (external dependency).** Because the data home now
lives under `~/.kiro/`, a hypothetical Kiro-family uninstaller that removes
`~/.kiro/` would also remove `~/.kiro/crew` and take Kiro Crew's data — config,
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
data-home path (see `_write_recovery_breadcrumb`). It is idempotent where the
platform can check safely (on POSIX the prior content is read via `O_NOFOLLOW`
and rewritten only when the recorded path changes; where that flag is missing —
Windows — the check is skipped and the file is atomically rewritten once per
process), best-effort (never blocks startup), and
written only on the default path (a `KIROCREW_HOME` override carries no `~/.kiro/`
wipe risk). It is **not a backup** — just a durable signpost that survives a
`~/.kiro/`-wide uninstaller wipe so a user or support script can find any
surviving data or understand what was removed. This narrows, but does not
eliminate, the one-way-door risk above; the release gate still stands.

> **Release gate (UNINSTALLER-EXCLUDE-CREW).** This is a pre-release,
> human-sign-off dependency, NOT a code change in this repo: the code cannot
> constrain another product's uninstaller. For every release that ships data
> under `~/.kiro/`, the Kiro Crew product owner MUST confirm the
> Kiro-family uninstaller either excludes `~/.kiro/crew` or prompts — because
> there is no `~/.kirocrew.archived` fallback for any install, so a
> `~/.kiro/`-wide wipe would be unrecoverable total data loss. Until confirmed,
> the placement decision is acknowledged-but-owned here under this name so it is
> not lost. **Tracked as release-blocking in
> [issue #355](https://github.com/kirodotdev/KiroCrew/issues/355)**; the sign-off
> must be recorded there.

**Paths are resolved per call, never captured at import.** Because
`config_dir()` re-reads `$KIROCREW_HOME` on every call and resolution/maintenance
is deliberately lazy, the resolved value is only correct at the moment it is
needed. Modules therefore MUST NOT bind a path factory result to a module-level
constant:

```python
_SOME_DIR = config_dir() / "some"        # WRONG -- frozen at import
```

An import-time binding captures whatever home was active when the module was
first imported, which breaks two things at once: pod isolation (a pod exports
its own `KIROCREW_HOME`) and test isolation — `conftest.py`'s autouse `_isolate_kirocrew_home`
fixture runs *after* collection has already imported the module under test, so
it cannot reach a frozen constant, so a test run could write fixture rows into an
operator's real usage store.

The required shape keeps the module-level name as an explicit opt-in override
(`None` = resolve live), so existing `monkeypatch.setattr(mod, "_SOME_DIR", tmp)`
call sites keep working:

```python
_SOME_DIR: Path | None = None

def _some_dir() -> Path:
    return _SOME_DIR if _SOME_DIR is not None else config_dir() / "some"
```

Annotating the override as `Path | None` is load-bearing: any consumer that
still reads the constant directly becomes a **mypy error** rather than a silent
`None` at runtime. This is enforced repo-wide by
`test/test_lazy_data_home_paths.py`, which walks the AST of `src/kiro_crew` for
module-level assignments calling any factory declared in `config/paths.py` and
fails on every hit. The factory list is derived from `paths.py` itself, so a
newly added factory is covered without editing the test.

**`config_dir()` maintains; `data_home()` only resolves.** `config_dir()` is
*resolve + maintain*: besides resolving the home it `mkdir`s it and refreshes the
recovery breadcrumb (a stat + a read). That work belongs to process start —
`ensure_data_home()` is the startup hook — and the distinction did not matter
while callers froze the result in a module constant, because the maintenance
then ran exactly once, at import.

Resolving per call makes it load-bearing: a request handler would otherwise
refresh the breadcrumb **on the event loop** as a side effect of asking where a
directory is. So the accessors above call **`data_home()`**:

| branch | behaviour |
| --- | --- |
| a **valid** `KIROCREW_HOME` override | delegates to `config_dir()` every call, so an override set *after* import is honoured. That branch performs no breadcrumb refresh — only a cheap `mkdir`. |
| default home already resolved | returns the cached `_resolved_home` directly — no `mkdir`, no breadcrumb. |
| not yet resolved | delegates to `config_dir()`, so the **first** resolution in a process creates the home and refreshes the breadcrumb once. |

The first row tests `_valid_override_home()` — the **same predicate `config_dir()`
gates on**, not merely "is the env var set". An override naming a system
directory (`/`, `/usr`, …) is rejected there and resolution falls through to the
default home, so gating on the raw env var would send every call down the
maintenance path for anyone with a bad override. The two predicates must not
drift apart; a regression test pins both directions.

`data_home()` keeps no cache of its own — the override branch must stay live, and
the cached branch reads the same `_resolved_home` that `config_dir()` populates,
so there is one source of truth for the location.

Direct `config_dir()` callers keep the maintenance behaviour; a hot or async path
should call `data_home()` instead.

## Workspace Root

`workspace_root()` returns the base directory for all LLM working directories (kiro-cli cwd, task runner output, etc.):

Resolution order:
1. `KIROCREW_WORKSPACE` env var — no `kirocrew-workspace` subdirectory appended
2. Saved path in `~/.kiro/crew/workspace_dir` (written by `kirocrew setup`; re-running setup preserves the existing value as the prompt default)
3. Platform default:

| Platform | Path |
|----------|------|
| macOS | `/Volumes/workplace/kirocrew-workspace` (falls back to `~/workplace/kirocrew-workspace` if `/Volumes/workplace` doesn't exist) |
| Linux | `~/workplace/kirocrew-workspace` |
| Windows | `~/workplace/kirocrew-workspace` |

Values from (1) and (2) lose one surrounding quote pair and have `~` expanded; a non-absolute result is logged and replaced by (3).

Each session/task gets an isolated subdirectory under this root via `_session_work_dir(key)`:
- Chat sessions: `kirocrew-workspace/cli_chat`, `kirocrew-workspace/{thread_ts}`
- Background: `kirocrew-workspace/_bg`
- Cron: `kirocrew-workspace/cron_{job_id}`
- TaskRunner: the session key `taskrunner:main`. Both `TaskRunner` constructions
  (`cli_server.py` and the gateway boot) pass `_session_work_dir("taskrunner:main")`,
  and `_safe_dir_name` turns the colon into an underscore, so the folder on disk is
  kirocrew-workspace/taskrunner_main. It holds `runs.json` and one subdirectory per
  run: `<spec stem>` for a spec run, `plan_<task_id>` for a planned one. Setting
  `taskrunner.workspace_dir` moves all of it into that folder, and a run started
  with its own `workspace_dir` works there directly; see [taskrunner](taskrunner.md).
- One-run directories for a subagent and a stateless cron run:
  `kirocrew-workspace/subagent_<id>` and `kirocrew-workspace/cron_<job>_<run>`
  (`session_work_dir._DERIVED_SESSION_SHAPES`). These carry a provenance marker and
  are reclaimed when only residue is left, at shutdown and by the predecessor sweep;
  the contract is in [acp-client](acp-client.md).

`workspace_root()` creates the selected root on first call and realpath-normalizes
it before session subdirectories are derived; `workspace_root(create=False)`
resolves it without creating it (the doctor's census uses that).

## Project Directory Resolution

`KIROCREW_PROJECT_DIR` env var controls where agent config and skills are loaded from:

1. Env var `KIROCREW_PROJECT_DIR` (if set and valid)
2. CWD walk-up — CLI walks up from CWD looking for `skills/` + `src/kiro_crew/`. Agent config ships in `src/kiro_crew/config/`; when a user creates `<project>/agents/defaults.json` or `<project>/agents/prompt.md`, `agent._shipped_defaults` / `_shipped_prompt` prefer those over the bundled copies
3. Saved path in `~/.kiro/crew/project_dir` (written by `kirocrew setup`)
4. Bundled fallback — `config/defaults.json` and `builtin_skills/` inside the package

The CLI (`cli.py:main()`) auto-detects and sets the env var at startup.

## Folder steering directories (`dashboard/chat_folders_paths.py`)

A chat folder record in `folders.json` may carry an optional `steering_dirs`
list of absolute (or `~`-prefixed) directories, alongside its existing
`project_dir`/`default_agent`/`color`/`tags`. Every chat in the folder's subtree
loads each directory's `**/*.md` files whose inclusion is `always` or absent as
steering (`manual`, `auto` and `fileMatch` files are skipped), in addition to global and project
steering. The key is omitted when empty (like `color`/`tags`), so "absent means
none" is the single on-disk representation and a PATCH with `[]` clears it.

- **Validation** (`dashboard/chat_folders_paths.py` `_validate_steering_dirs`) checks
  each entry: at most `MAX_FOLDER_STEERING_DIR_LEN` (4096) characters before and
  after canonicalization; a UNC path refused before resolution; refused outright
  on a platform without a pinned tree walk (`pinned_fs.supports_pinned_tree_walk`);
  canonicalized and screened by `hooks.validate_file_path` (a refusal is
  SEL-logged); refused when the memory-silo fence cannot be computed or the path
  crosses a memory silo; and proven to exist by a pinned, no-follow open rather
  than `isdir`. On top of that come a `MAX_FOLDER_STEERING_DIRS` (16) list cap and
  rejection of duplicates within one folder (compared by resolved path). The
  fence, platform and inclusion rules are specified in
  [providers](providers.md). Failures return
  `400` with code `steering_dirs_invalid`.
- **Principal gate** (`_refuse_principal_steering_dirs`) runs at both write
  sites BEFORE validation touches any path: only the person may declare a
  non-empty list. A `POST`/`PATCH` carrying one from an app or crew-member
  principal is refused `403` with code `steering_dirs_forbidden` and SEL-logged
  as denied, because folder permission is not host-file permission -- the
  gateway reads these files unsandboxed on the folder's behalf, and an app that
  could point its own folder at an arbitrary readable Markdown tree would have
  that read laundered into its own model session with no tool grant and no
  signal. Clearing to `[]` stays allowed for every principal (it only removes
  reads). The person may still declare steering on a folder an app or member
  owns; delivery then routes it to that principal's chats as described below.
  A call on the internal credential every MCP tool call carries
  (`request["internal_auth"]`) is refused the same way, at both write sites.
- **Agent surface** is a `folder.steering` change card
  (`change_card_catalog.py`): `propose_change(kind="folder.steering",
  params={folder, steering_dirs})` resolves each directory the way the route
  stores it (`_validate_steering_dirs`, refusing what the route would refuse),
  then shows the person the folder, its current list and the resolved new one,
  and the person's own browser applies it as
  `PATCH /api/chat/folders/{id}` `{"steering_dirs": [...]}` through the card
  hook, so the gates above are the only authority. Adding a directory is
  `widen`, only removing is `tighten`; Undo writes the previous list back while
  the folder still holds what the card stored. The card hook arms each Apply
  and Undo step with the list it replaces (a request attribute, never a body
  field), which the route compares under the store lock
  (`409 steering_dirs_changed` when another save landed first).
  `chat_folder_tree` renders each folder's declared list (`steering=[…]`).
  To empty the list the agent passes `{folder, clear: true}`: a client that
  drops empty containers delivers `steering_dirs: []` as a missing key. The
  catalog normalizes `clear` to `steering_dirs: []`, so everything after it
  reads one shape, and refuses it beside a non-empty list.
- **Resolution** (`_resolve_folder_steering_dirs`) is ACCUMULATIVE up the
  `parent_id` chain (root ancestor first, then descendants), unlike the
  nearest-wins `project_dir` resolver: an org-standards folder above a per-repo
  folder contributes both sets. The walk is cycle-guarded, re-validates each
  stored path (never trusting `folders.json`, which can list a directory since
  moved or made sensitive), and dedups by resolved realpath keeping the first
  occurrence.
- **Live resolution, no slot field.** The effective list is never cached on the
  chat slot: `_ChatSlot` carries no steering field, and neither slot create nor
  agent switch resolves one. The dashboard turn's prompt build (`_turn_build_prompt`,
  `dashboard/chat_turn/prompt_assembly.py`) resolves it live from the slot's current
  `folder_id` against the committed folder tree, on every fresh session and
  every post-compaction reinjection turn; a V2 member chat resolves it on every
  turn, because its essentials envelope is rebuilt each turn
  (`dashboard/chat_turn/turn_context.py` `_folder_steering_turn` is the gate). So
  editing a folder's `steering_dirs` reaches a template chat already filed in it at
  its next fresh session and a member chat on its next turn, with no cache to
  invalidate; a slot with no `folder_id` contributes nothing. A
  resolver error logs a warning naming the slot and the turn proceeds with no
  folder steering, and a directory that has since disappeared is skipped at read
  time rather than failing the turn. Delivery is performed once by the context
  builder (`kiro_crew.context.ContextBuilder`) reading through
  `kiro_crew.folder_steering` — the one layer every backend passes through, so
  no provider can silently drop it. See [providers](providers.md).

## Named Memory Stores (`memory_stores.py`)

The reserved `agents.default` assistant uses the existing Global Memory **V1**.
Explicit creation of a new Crew Member allocates one **V2** memory store.
Automatic discovery and existing V1 members retain their V1 bindings. Changing
`default_agent` picks the roster's default mate (the badge, the undeletable
row) with its existing memory version and binding; it never converts member
memory to Global, and it does not change what a session that picked no mate
reads: that session is a template session on Global Memory V1 (see
`resolve_agent_bindings` below). A materialized provider template which is not a Crew Member
continues to use V1.

### Separate files preserve V1

| Resolver | Global `default` | Member V2 store |
|---|---|---|
| `memory_store_dir_for` | `<home>/workspace/` | `<home>/memory_stores/<name>/` |
| `resolve_store_path` | `<home>/memory.db` | `<home>/memory_stores/<name>/memory.db` |
| `memory_index_path_for` | `<home>/memory_index.db` | `<home>/memory_stores/<name>/memory.db` |

V1 retains its markdown, history and separate index layout. Explicit V2 creation
adds only manual `memory/preferences.md` and `memory/projects.md` beside its
single SQLite database; learned history and search use that database. No path is
renamed and no V1 data is migrated, copied or algorithmically converted on member
creation. Member stores begin empty. Explicit selected-content inheritance and
its provenance are owned by [memory-skills-hooks](memory-skills-hooks.md).

### Member identity and creation

A V2 member has an immutable persisted `member_id`, independent of its editable
config/display label. `MemoryStoreConfig.owner_member_id` and the database's
`member_database` singleton record identify the same owner and store ID.
`memory_version: 2` selects V2; legacy declarations default to V1. `owner_member`
is descriptive display metadata, never execution authority. Templates and
projects cannot select a member's memory.

The template-picker roster withholds `member_id`; the full configuration view
applies the same credential and exfiltration redaction to this hand-editable
string as other member fields. Stored identity values remain unchanged.

`provision_member_memory(config, member)` allocates an exclusive random directory
and creates a new SQLite database through `create_member_database`, plus the
explicit manual preferences/projects documents. The member/config binding is
published only after initialization succeeds. It never adopts an existing
unidentified database, resets a store, or copies earlier V1/V2 data. An existing
V2 member must retain its ID and store. No manifest, retirement registry, private
payload copy, OS admission gate, or memory provisioning feature flag is involved.

`POST /api/agents` and `kirocrew agent create` allocate new stores automatically.
A supplied named store is rejected. They also allocate the member's own workspace
(`member_workspace.provision_member_workspace`: `member-<slug>` bound in `agents`,
`workspace-member-<slug>` under the data home declared in `workspaces`) unless a
workspace is named; `persist_member_config(create=True, new_workspace=…)`
publishes the entry and makes the owner-only directory inside the same locked
mutation as the record. Owner edits may echo the current store but
cannot silently rebind it. Legacy members keep their explicit Global or named V1
binding. Member updates and automatic discovery never initialize a V2 database;
only explicit creation of a new member may allocate one. Existing V1 memory
and transcripts remain unchanged.

`persist_member_config` publishes the member and store under `update_config_locked`,
rechecking duplicate creation, immutable member ID, expected binding, and exclusive
store ownership while preserving unrelated concurrent edits. When creation fails
or is cancelled after allocating a store, the creation paths call
`retire_unpublished_allocation`: it re-reads `config.json` under the same lock and
removes the fresh directory only when no member and no store entry references it,
then restores the in-memory binding so a retry allocates fresh. A publication that
landed is kept; pre-existing bindings and stores are never deleted or retired.
A create also purges the member name from the crew-teams record inside the same
locked mutation (`crew_teams.release_for_create`); a purge that cannot be made
(`TeamsUnavailable`) refuses the creation, 409 `teams_unavailable` on the dashboard
and exit 1 from the CLI.
Degraded or unreadable configuration refuses creation instead of guessing defaults.
Non-string `member_id` or `owner_member_id` values refuse allocation with
`UnknownMemoryStore` before forming the reserved-ID set, preserving the raw values,
existing databases and V1 bindings instead of replacing damaged identities.

A `memory_version: 2` record whose `owner_member_id` is empty is a member store
written before identities existed; the loader keeps it verbatim and every
resolver refuses it. `repair_legacy_member_stores()` runs in the `cli.main`
prologue after `boot_platform` for every CLI subcommand except `gateway`,
`doctor` and the `mcp-*` servers, and for the gateway in its post-readiness
memory preparation worker (never on the boot path), so both
surfaces publish the missing `member_id` / `owner_member_id` pair before any
member is resolved. It writes through `update_config_locked`, skips a config
whose memory section degraded, and never raises. The same repair restores a
member store whose declaration an older build's save stripped back to V1
(`memory_version`, `owner_member_id`, `owner_member` and the member's `member_id`
gone) when its `memory.db` still records a `member_database` identity for that
store and exactly one identity-less member is bound to it. Criteria, refusals and the
database half are owned by
[memory-skills-hooks](memory-skills-hooks.md#pre-identity-member-stores-are-upgraded-at-start).

### Exact resolution and explicit failures

Admission resolves one frozen `ExecutionContext` containing member ID, store ID,
selection namespace, template, app attribution, and privacy mode. The snapshot is
part of the owning session/run/job record, captured before awaiting asynchronous
work. Background workers, continuations, schedules and reruns inherit it directly;
closing the originating chat or editing a display name does not retarget execution.
An explicit `target_member` selects an existing configured member under ordinary
execution permissions. There is no automatic provisioning or fallback to Global.

`resolve_declared_store` and `require_member_memory_store` reject malformed,
missing and contradictory declarations. `resolve_agent_bindings(...,
validate_memory_files=False)` captures configuration identity without opening
learned memory. Persona/project/manual context can therefore load while the
learned database is unavailable. Actual memory operations validate the captured
store's database identity with `open_member_database`; an absent, corrupt or wrong
member database is an explicit error and is never recreated at read time.

Store names use lowercase letters, digits and hyphens, 1–80 characters, with no
leading/trailing hyphen, path separators or Windows device basename. Managed paths
must remain under `memory_stores/` and may not redirect to another store.
`memory_store_version` reads the exact configured declaration; it does not infer
ownership from labels, paths or database contents. V1 opens refuse a database
bearing the explicit V2 identity table rather than treating it as Global memory.

Incognito and Temporary modes are inherited monotonically. New restricted sessions
keep their canonical record in memory and suppress Crew body/checkpoint writes.
Tightening an existing persisted execution updates only retention metadata so a
restart cannot broaden it. Ordinary transport authentication, app/owner permissions,
enterprise governance, host sandbox and credential protection remain independent
of memory routing. Same-host arbitrary-code confidentiality between member stores
is not a goal. See [memory-skills-hooks](memory-skills-hooks.md) and
[security](security.md) for the storage and ordinary host-security contracts.

## Workspace fall-through is logged

`workspace_dir_for(name)` also reads the LOADED config's `workspaces` table rather
than the raw bytes, so it and `resolve_agent_bindings` cannot answer the same
question two ways. An unmapped name falls back to `default_workspace` and then to
`WorkspaceConfig().dir`, and the fall-through is logged: two DISTINCT names both
resolving to `<home>/workspace` warns, because that is how a workspace split becomes
a shared tree nobody notices. An install that simply has no `workspaces` section is
the ordinary fresh state and logs at debug. It **never raises** —
`default_project_dir` and the workspace-identity block are built on it, so a raise
would break a fresh install and take both with it.

A legacy FLAT `{"name": "dir"}` workspaces entry is a schema type mismatch that
the validator keeps (`_is_flat_workspace_string`): `_migrate_workspaces` turns it
into `WorkspaceConfig(dir=...)` in the loaded table, and the `MIGRATE_WORKSPACES`
write-back persists it as `{"dir": ...}`. Both `resolve_agent_bindings` and
`workspace_dir_for` answer from that table.

## Superseded Defaults (reported; a named few adopt themselves once)

`config.json` is a full materialization of the schema -- every field is written to
disk, including fields the operator never set -- and each field is resolved as
`data.get(key, DEFAULT)`. A stored value therefore always beats the dataclass
default, so **changing a shipped default reaches only installs created after the
change**; a pre-existing install keeps whatever value was materialized last.

`config/superseded_defaults.py` holds an append-only registry
(`SUPERSEDED_DEFAULTS`) of default changes existing installs should be told about,
each entry naming the dotted key, the old default, the new default, and the
release that changed it. `superseded_default_drift(base_data)` returns the entries
whose stored value equals the old default, comparing type as well as value so a
stored `0` is not read as `False`.

Every registered row is listed once, in the table under *Auto-adoption* below,
with its change and the reason it adopts or only reports. **Three** carry
`auto_adopt` -- the agent timeout budgets and the spawn memory floor -- and every
other row is report-only.
`test_the_spec_table_lists_every_registered_row` keeps that table equal to the
registry.

A row may also carry a `note`: one sentence that `doctor`,
`kirocrew config defaults` and the `--keep` confirmation append to it. It exists
when choosing between `--adopt` and `--keep` needs a fact the old and new values
do not carry. `skills.lazy_load`'s says what `false` means now (below).
`decisions.history_budget_chars` says adopting is bounded by the consented history
ceiling. The two tool-stall windows say they are coupled (see the table).

A row marked `meaning_moved` also puts its note on the one-line load-path
warning. That mark is for a stored value whose MEANING moved along with the
default, and `skills.lazy_load` is the one such row. On 0.6.0 and earlier
`false` was the default full skills dump, and today it selects the shorter entry
naming only the eight hottest skills. A 0.6.0 install may hold a materialized
`false`, and an operator who chose `false` there chose the full dump. The row
stays report-only because `false` is also the supported switch to the short
entry. Its note says what `false` means now, on every surface, so keeping it is
never mistaken for keeping the 0.6.0 behaviour, and `--adopt` takes the index.

A row may also carry `applies`, a predicate that says where the old and new
defaults behave differently; elsewhere the row is not drift. `stt.language_code`
uses it: only the local recogniser auto-detects, and every other provider
resolves `"auto"` to `en-US`, so a stored `en-US` there already behaves like the
default and is not reported. The row's own value is still detected on the base
document alone, but the predicate reads the EFFECTIVE `stt.provider`, with
`config.local.json` merged over the base, because the recogniser that runs is the
effective one.

The subagent turn budget follows 1000 automatically when its key is absent,
including in an existing installation after an update. Every valid stored value
is retained, even 100: a materialized old default and an explicitly selected
100-turn cap are indistinguishable without historical per-key provenance.
The defaults report exposes the change and the existing adopt/keep commands;
it does not claim that ambiguous legacy files can be upgraded without risking
an operator's deliberate cap. The three-hour execution timeout remains separate.

### Auto-adoption, and the line it does not cross

Reporting is the right answer only while the two readings of a stored value are
indistinguishable AND holding the old value is survivable. On the two agent timeout
budgets neither holds: an install carrying `agent.subagent_timeout_secs: 1800` reaps
every subagent at 30 minutes on a build whose default is 10800, and its operator
sees timeouts instead of results having never chosen 1800. Nor on the spawn
memory floor: a materialized `agent.spawn_min_memory_gb: 4.0` keeps 4 GB free after
every start, which a 16 GB laptop rarely has, so its subagents wait in the queue
and essentially never start. The existing mechanism's
only answer was a CLI command they have no reason to know exists.

So `SupersededDefault.auto_adopt` opts ONE entry into a one-shot rewrite. What keeps
the set small is **not** a judgment about how wide the value's range is. That
criterion was tried and is wrong: `instances.warm_set_cap` is numeric with a range,
and an operator running five crews who types 5 stores exactly the old default. The
line that holds is whether the old value is something an operator sets on purpose.
For four rows another suite pins it as a supported configuration, and the table
names that test; every other report-only row gives its plain reason:

| Key | Change | | Pinned by, or why |
|---|---|---|---|
| `agent.subagent_timeout_secs` | 1800 -> 10800, #8891 | adopts | -- |
| `agent.chat_turn_timeout_secs` | 7200 -> 14400, #8949 | adopts | -- |
| `agent.spawn_min_memory_gb` | 4.0 -> 2.0, #15890 | adopts | -- (the 4.0 inputs in the admission tests set a floor, they do not pin a stored 4.0 as supported; the opt-out is `0`, not the old default) |
| `session.autocompact_pct` | 90.0 -> 70.0, #4388 | reports | `test_a_persisted_ceiling_value_is_left_alone` |
| `dashboard.loop_stall_exit_after_secs` | 25 -> unset, #6651 | reports | `test_explicit_desktop_default_is_preserved_for_managed_service` |
| `stt.streaming` | false -> true, 0.5.0 | reports | `test_put_persists_streaming` |
| `mcp_gateway.forward_declared_env` | false -> true, #4566 | reports | `test_a_real_false_still_turns_it_off` |
| `stt.model` | "turbo" -> "base", 0.5.0 | reports | a picker value; adopting changes transcription accuracy |
| `instances.warm_set_cap` | 5 -> 0, #7248 | reports | 5 is an ordinary deliberate cap |
| `agent.subagent_max_turns` | 100 -> 1000, #12203 | reports | an explicitly stored 100 is a supported cost/turn cap |
| `agent.session_control` | false -> true, #8375 | reports | false is the supported withdrawal of peer-session tools from ordinary agents; crew members and the owner-tab Conductor keep a creator-fenced dispatch bypass unless `agent.member_dispatch` is also false |
| `skills.lazy_load` | false -> true, #12131 | reports | false is the supported switch to the short skill entry; its `note` reaches the load line too (`meaning_moved`, above) |
| `agent.subagent_spawn_stagger_secs` | 2.0 -> 0.25, #12203 | reports | the knob to raise when the host or provider is the bottleneck |
| `session.watchdog_rss_max_mb` | 1536 -> 0, #16393 | reports | 1536 is an ordinary deliberate ceiling; holding it only recycles idle sessions, which keep their history |
| `stt.language_code` | "en-US" -> "auto", #9246 | reports | a locale picked on purpose; adopting changes what the recogniser listens for. Only where the effective provider, overlay included, is local (`applies`): elsewhere "auto" resolves to en-US |
| `decisions.history_budget_chars` | 0 -> 2000, #12928 | reports | 0 is a supported setting below the consented history ceiling; adopting raises it only up to that ceiling (carries a `note`) |
| `watchdog.stale_window_secs` | 300.0 -> 600.0, #8949 | reports | a tuning knob; holding it probes a live think sooner, which regenerates it |
| `watchdog.tool_stall_suspect_secs` | 3600.0 -> 5400.0, #8949 | reports | a tuning knob; holding it cancels a tool the oracle cannot attest after an hour, with no re-run, so that tool's work is lost. Coupled with the hard cap (`note`): the window is the smaller of the two, so both must be adopted to get 5400 |
| `watchdog.tool_stall_hard_cap_secs` | 3600.0 -> 7200.0, #8949 | reports | a tuning knob; holding it caps the same forbearance at an hour, with the same cost. Coupled with the suspect window (`note`): adopting either one alone leaves the window at an hour |
| `watchdog.model_silent_probe_secs` | 900.0 -> 1800.0, #8949 | reports | a tuning knob; holding it probes a long silent think sooner, which regenerates it |
| `agent.session_start_concurrency` | 2 -> "auto", #17055 | reports | the knob to lower when the host or provider is the bottleneck; holding 2 only serialises a burst of starts |

A row whose old value is a supported configuration is not stale noise, whatever
its type. `test_only_unpinned_broken_budgets_adopt_themselves` pins both sets by
name -- the rows that adopt, and the rows that only report -- so a new row has to
be placed in one of them on purpose, and moving a row across edits that test in
the same change. `auto_adopt` defaults to False, so a new row is report-only until
someone states otherwise.

Two further properties make the rewrite safe on the rows that remain, without the
per-key provenance the config layer still lacks:

- **One-shot.** `auto_adoptable()` excludes any key already in the sidecar's
  `adopted` map, so a key is adopted at most once per install and a value the
  operator sets back afterwards is theirs forever. Without that record the loader
  would re-remove a restored value on every load -- the one behaviour worse than
  saying nothing.
- **Marker first, chosen for its worst case.** `record_adoptions` writes the ledger
  entry BEFORE the removal, inside the config write lock, and a failing record aborts
  the whole migration write. `config.json` and the sidecar are two files with no
  shared transaction, so exactly one of two windows exists and the ordering decides
  which one:

  | Ordering | Window | Consequence |
  |---|---|---|
  | marker first (this) | durable marker, failed removal | the operator KEEPS their value and the adoption is not retried. A **missed improvement**, recoverable with `kirocrew config defaults --adopt` and still named by the startup line. |
  | removal first | landed removal, lost marker | nothing suppresses a later adoption, so a value the operator deliberately RESTORES is deleted a second time. A **destroyed choice**, unrecoverable. |

  No retry, rollback, compensating write or two-phase pending/committed scheme removes
  the window -- each only moves it onto another write that can fail the same way, and
  a rollback that fails recreates the hazard it exists to prevent. So the marker goes
  first and the failure lands on the recoverable side.
  `test_a_failed_write_keeps_the_value_and_does_not_re_adopt_later` pins that path
  end to end, including that a later load does not take the value on a retry, and
  `test_the_unapplied_adoption_is_still_reported_so_it_is_recoverable` pins that the
  marker suppresses the retry but not the report.
- **An adoption that did not reach disk drops the validated-data cache.** Only a load
  that READS the base document can decide an adoption (`adoptable` is empty on a cache
  hit, by design), so a read-and-skip that left its document cached would have every
  later load serve the stale value and never retry. A contended lock and an exception
  caught by the best-effort handler both skip the write, so `persist_write_back` tracks
  `adoption_landed` separately from `persisted` (which starts True so the
  `connections_ui` marker still lands on a load that needed no migration) and
  invalidates in a `finally` both share. Both variables are bound before the `try`,
  or an early exception would turn a logged write-back failure into a `NameError`
  out of `load()`. The **degraded-sections branch is deliberately excluded**: its
  retry condition is "the operator fixes the file and restarts the gateway" (a
  degradation observation is sticky for the life of a process), not "the next
  load", so an invalidation there would only re-read and re-parse `config.json` on
  every load for as long as a malformed section coexists with a stored stale
  timeout. After the restart the fixed file's fingerprint misses the cache and the
  adoption retries on that first load
  (`test_a_degraded_load_keeps_its_document_cached_instead_of_re_reading_forever`).
- **An unreadable ledger adopts nothing.** `_read_ack_document_status` returns
  `(document, readable)`, and `auto_adoptable` returns `[]` when a sidecar exists but
  cannot be parsed: reading it as empty would re-arm the one-shot over a value the
  operator restored. The ACK half stays fail-soft, because a missed ack costs one
  report line rather than a deleted setting.

`record_adoptions` takes the sidecar lock **single-shot** (`wait_for_lock=False`),
because the config load path runs on the asyncio event-loop thread in places and a
blocking acquire there stalls the gateway for as long as another writer holds it. A
contended acquire raises `BlockingIOError`, which the migration treats as "defer to
the next load" -- the same deferral `_persist_config_migration` already takes on a
contended config lock. A CLI caller keeps the wait: no loop to stall, no later retry.

`--keep` still wins: an acknowledged value is not drift, so affirming a key before
it is adopted keeps it, which is the answer for an operator who did choose 1800.

Adoption is an **un-materialization**, not a write of the new number:
`drop_drifted_keys` removes the stored key, so the field resolves through
`data.get(key, DEFAULT)`. That holds only until the next FULL rewrite of
`config.json` -- any settings save re-materializes the current number, as
`drop_drifted_keys`' own docstring says -- so a later default move on the same key
still needs its own registry row and does not ride along.

It is applied in memory as well, because the gateway reads these budgets once at
startup -- a disk-only fix would leave the run that performed it still holding the
old value, which is exactly the "upgraded and nothing changed" complaint. Two guards
on that half: `_adopt_in_memory` reads the field's OWN dataclass default rather than
the row's `new_default` (on a key whose default moved twice, the matching row's
`new_default` is an intermediate value), and it replaces the value only when the
parsed field still EQUALS `old_default`, so a value the loader clamped or coerced
keeps the loader's correction. A key the `config.local.json` overlay supplies is
cleared on disk but left alone in memory: the overlay is the operator's live choice.

After the config write succeeds, the loader warns at the default log level for each
adopted key, naming the removed value and the `kirocrew config set` command that
restores it. A deferred or failed write emits no adoption notice. The warning
describes the stored value without claiming to know whether the operator chose it.

That warning is one line in one gateway log, so the same two facts are replayed on
demand: `kirocrew doctor`'s `Stored Defaults` section and a bare `kirocrew config
defaults` both render the sidecar's `adopted` map through `adoption_summary` -- one
`adopted:` line per key, naming the value removed from `config.json` and the exact
restore command -- and both render it BEFORE opening `config.json`, so a missing or
unreadable config does not hide what an earlier load removed from it. The line says
"removed from `config.json`", not "the default now applies", because
`config.local.json` may still carry the key; and it allows for the marker-first
window (an entry whose config write failed describes a value that is still stored
and still listed as drift). Both fields come from the sidecar, a file the agent
sandbox can write, so they are untrusted output: every character is rendered
terminal-safe (control characters escaped, never executed), and the pasteable
restore command is built only from `SUPERSEDED_DEFAULTS` literals after matching the
entry by key and exact value -- no quoting scheme is portable across every shell an
operator might paste into, so an entry the registry does not vouch for is shown,
escaped, with no command. An adopted key holds no stored value any more, so it is
neither drift nor an `--adopt`/`--keep` target; naming one there is refused like any
other non-drifted key.

**Downgrade residual.** A build older than the adoption ledger serializes the
sidecar as `{"acked": ...}` only. Running that build's `--keep` or `--adopt` after a
downgrade therefore rewrites the file WITHOUT the `adopted` map, which re-arms the
one-shot: on the next upgrade a value the operator restored to the old default is
adopted a second time. Current builds carry both maps through every write
(`_update_map`) and refuse to rewrite a sidecar they cannot parse, so the window
exists only across that specific downgrade-then-write sequence, and the second
adoption still announces itself at WARNING with the restore command.

`stt.provider` is deliberately absent from `SUPERSEDED_DEFAULTS` even though its
default moved to `local`: `_validated_stt_provider` coerces a retired value at
parse time, so the stored value never wins and there is no *default* for an
operator to adopt. It is instead a **coerced value**, tracked separately in
`COERCED_VALUES` — see below.

Both sides of an entry are **history**, so both are literals: a later change to
the same key APPENDS a new entry rather than editing an existing one, which keeps
the older row a true record of the change it describes. What must stay current is
the END of each key's chain --
`test_every_registered_key_ends_at_the_live_default` asserts the newest entry per
key names the default the loader actually applies, so moving a default without
appending a row fails rather than leaving the report telling operators to adopt a
value that no longer exists.

Two surfaces render it. Neither writes config; the load path's adoption above is
the only thing that does, and a key it adopts is excluded from the warning rather
than pointing the operator at a command for something already fixed:

- The load path emits **one** warning naming every drifted key plus the command to
  resolve it, evaluated on the **stored base document before the
  `config.local.json` merge** -- an overlay value is the operator's live choice and
  says nothing about what the base materialized, so a base drift is still reported
  when an overlay masks it, and an overlay-only value is not reported at all. One
  line rather than one per key: the registry is append-only, so a per-key line
  grows without bound on exactly the long-lived installs with the most real drift,
  and it lands on every short-lived `kirocrew` invocation, where the
  once-per-process guard buys nothing because there the process IS the invocation.
  The per-key text is still emitted at debug, so `-vv` keeps it in the log. The one
  per-key text the line does carry is the note of a `meaning_moved` row, because
  that stored value now selects a different behaviour from the one its operator
  chose, and the line is what someone who never opens `config defaults` reads
  before running `--keep`.
- `kirocrew doctor` prints a `Stored Defaults` section reading `config.json`
  directly. Drift is informational and does NOT become an issue; an unreadable or
  malformed config does.

### The legacy `skills.lazy_load` rewrite

0.6.x and earlier materialized `skills.lazy_load: false` into every `config.json`
they saved, and `false` was then the default full skills listing. Since 0.7.0
`false` selects the short skill entry and the default is `true`, so an upgraded
install silently runs the narrowest mode. This is NOT an `auto_adopt` row: on a
0.7+ install `false` is the supported switch to the short entry, so value equality
cannot tell noise from a choice. What can is WHEN the value was written, the same
boundary the `connections_ui` launch migration uses.

`migration.legacy_lazy_load_rewrite_due` decides on the BASE document, on a load
that read it (never on a cache hit). It returns the writer's stamp only when all of
these hold; every other case leaves the value exactly as stored:

- the base stores exactly the boolean `false` (an explicit `true`, `0` or a string
  is never touched);
- `meta.lastTouchedVersion` parses as `major.minor.patch` with at most a short
  suffix and names 0.6.x or older. An absent, non-object or unparsable `meta` is an
  unknown writer, not a provable one, and declines;
- `connections_ui_migrated.json` does not exist. It first shipped in
  0.7.0-insider.1 and every clean later load writes it, so its absence proves no
  0.7 build has loaded this home;
- the adoption ledger is readable and does not name the key. This keeps the rewrite
  one-shot even if the marker is later deleted; an unreadable ledger declines.

**What stays unmigrated.** The registry's report-only `skills.lazy_load` row
(`meaning_moved`) reports a declined `false`; the loader skips that key in the
superseded-default line only in the load that removes it. The marker
predates the meaning change: 0.7.0-insider.1 to insider.5 wrote it while `false`
still meant the full listing, so an install that ran one of those keeps its
materialized `false`. That is the conservative side of the boundary: the proof
cannot tell those installs from a 0.7 operator who chose the short entry.

The stamp is the proof, so nothing may re-stamp the document before the rewrite
uses it. The load decides before it writes, and `refresh_config_meta_stamp` (the
gateway's post-bind refresh) holds its refresh while the rewrite is still due, so a
degraded first load leaves the proof for the next clean one. A writer that runs
before any load and re-stamps -- `kirocrew config set --file`, which replaces the
whole document with the operator's own -- ends the cohort, which is correct for a
document the operator just supplied. So does any other real write by this build
during a degraded session (a settings save, `config set`): those writes are this
build's bytes, and holding the stamp across them would let the rewrite undo a value
set on this build, which the in-lock re-check exists to prevent.

The rewrite then rides `MIGRATE_SKILLS_LAZY_LOAD` through the same write-back as an
adoption: re-detected inside the config write lock (still an exact `false`, still
stamped 0.6.x or older; `kirocrew config set` and every settings save re-stamp, so
a value set since the load's read is never undone), recorded in the adoption
ledger as `{"skills.lazy_load": false}` BEFORE the key is removed, in the SAME
record as any superseded-default adoption of that pass (two records would let a
failed second one strand the first as adopted with nothing removed), the key
un-materialized rather than written as `true`, the in-memory value moved only once
the write is confirmed and only where `config.local.json` does not supply the key,
the validated-data cache dropped when the write did not land, and the
`connections_ui` marker deferred with it. One WARNING per rewrite names the key, the
writer's version, why, which value now applies (the default, or the overlay's when
`config.local.json` sets the key), and the `kirocrew config set skills.lazy_load
false` that chooses the short entry. The `skills.lazy_load` registry row has the
ledger entry's key and old value (`superseded_defaults.LEGACY_LAZY_LOAD_ADOPTION`),
so `adoption_summary` vouches for it and `doctor` and `kirocrew config defaults`
replay it with a restore command (a bool spelled as JSON); the marker-first
residual leaves a stored value that row still lists as drift. A load that declines
on an unreadable ledger or stamp still writes the marker, so that install keeps its
value for good; a degraded or deferred load writes neither and the next clean load
decides again.
`test_config_lazy_load_legacy_migration.py` pins the truth table.

## Acknowledging a superseded default

Value equality alone cannot falsify a report, so before #7559 an operator who
deliberately chose a value equal to a superseded default was told about it on every
load forever, with no way to answer -- and that unanswerable line competed for
attention with the genuine drift on the same install.

`kirocrew config defaults` is the surface that resolves the ambiguity the load path
must not resolve for anyone:

- no flag lists each drifted key with its stored value, the current default, and
  the release that changed it, marking anything already affirmed;
- `--adopt [KEY...]` REMOVES the stored keys, so `data.get(key, DEFAULT)` resolves
  the current default from the next load and the next full rewrite materializes it.
  Rewriting is safe here where it is not on the load path because the operator
  asked by name, and only a key whose stored value IS the superseded default is
  ever removed. Detection runs again inside the write lock, so a value changed
  since it was listed is left alone. It prints each key it removed, and asks for
  a gateway restart only for the keys `requires_restart` marks, the schema's one
  statement of which fields a running gateway cannot adopt (see *`restart=True` is
  the single source of restart truth*). That makes a row whose consumer reads its
  key only at boot a row that must carry the mark:
  `dashboard.loop_stall_exit_after_secs` does, because the gateway sizes its
  loop-stall watchdog from it once at start;
- `--keep [KEY...]` records the stored values as intentional, which suppresses the
  load-path line for exactly those values. A kept row that carries a `note`
  prints it again in the confirmation.

An acknowledgment records `<dotted key> -> the acked VALUE`, not the key alone, so
it covers the choice rather than the key: change the value later and the report
returns. Acks live in `~/.kiro/crew/superseded_acked.json` (`{"acked": {...}}`),
**not** in `config.json` -- a `to_dict()` rewrite carries only schema fields, so
the same materialization behaviour this whole mechanism reports on would silently
drop an ack stored in the config document. Three properties of that file matter:

- **Reads cannot block indefinitely.** The read runs on the config-load path, which
  is an event-loop path, and the file sits at a path the agent can name -- where
  `open()` on a FIFO waits for a writer forever and would wedge the gateway rather
  than merely delay it. `_read_ack_document` therefore `lstat`s and refuses anything
  that is not a REGULAR file (links included) or is over `ACK_MAX_BYTES` (64 KiB),
  opens with `O_NONBLOCK | O_NOFOLLOW` where the platform has them so a leaf swapped
  after the `lstat` fails instead of waiting, re-checks the OPENED object with
  `fstat`, then finishes with one capped `os.read`.
- **Every refusal fails soft.** Missing, non-regular, oversized, unreadable,
  malformed, not an object, a non-string key: an ack suppresses one line and changes
  no runtime behaviour, so the worst consequence of ignoring a broken file is being
  told again. That soft read is also why the file carries no schema version -- any
  shape it cannot understand is already handled, so the field would have no reader.
- **Writes never RESOLVE the leaf.** The config writers deliberately FOLLOW a link,
  because symlinking `config.json` into a dotfiles repo is a supported setup; here
  that would let a link planted at this path redirect the write onto an arbitrary
  file. `_update_acked` refuses a link it can see AND writes through `atomic_write`,
  which renames a fresh temp file OVER the leaf -- so a link swapped in after the
  check is replaced rather than followed. The check reports the condition; the rename
  is what makes it unexploitable.
- **Every mutation is a locked read-modify-write.** `_update_acked` holds the ack
  file's own lock across read, merge and write, so two concurrent `--keep` calls
  cannot both read the same map and have the second replacement drop the first
  operator's acknowledgment.
- **`record_acks` re-reads the config under its lock**, rather than trusting the
  caller's snapshot, and re-checks that each key is still drifted. A value changed
  between the listing and the call would otherwise be acknowledged at its superseded
  snapshot, which then suppresses the report for a value the operator never affirmed.
  The ack write happens inside that same config lock hold; lock order is
  config-then-ack wherever they nest.

A settings save is itself an answer, exactly as `--keep` is. When a config PATCH
writes the old default of an `auto_adopt` row, `api_kirocrew_config_patch` calls
`ack_explicit_write` under the config write lock and BEFORE the write (the same
config-then-ack order), so the value is acked and the next load does not
un-materialize the choice the operator just made. A refused ack raises `OSError`
and aborts the write, rather than persisting a value the next load would delete.
Only `auto_adopt` rows are acked this way; a report-only row loses nothing on
load and is left untouched.

`--adopt` also drops the ack for a key it removed, since the acked value is no
longer stored and keeping it would silence a genuinely deliberate choice made
later. When `config.local.json` also carries an adopted key the report says the
overlay still overrides it, because the EFFECTIVE value did not change. Every
filesystem refusal on these paths surfaces as a controlled non-zero CLI error,
never a traceback.

`doctor` LISTS an acknowledged entry rather than hiding it: an ack answers the
unsolicited load-path line, while `doctor` answers "what does this install still
hold?", and hiding an affirmed value would make that answer wrong.

## Coerced values (removable, never affirmable)

`COERCED_VALUES` in the same module tracks a second, distinct kind: a stored value
the loader **replaces** at parse time rather than merely overriding. The difference
decides what an operator may do about it. A superseded default still wins, so it
may be a deliberate choice and must not be rewritten. A coerced value cannot win,
so there is nothing to preserve — which makes removing it unambiguously safe and
makes affirming it meaningless, and `--keep` refuses it by name rather than
promising a setting that never takes effect. Left in place it is inert bytes that
cost a warning on every load, forever, because a load never writes.

One entry today: `stt.provider`. Its retired values (`whisper`, `mlx`, `parakeet`,
`faster`) degrade to `local`; any other unknown value degrades to `off`, so a
value nobody can account for never selects the in-process native recogniser
(kirodotdev/KiroCrew#13179). Because the two resolutions differ, `resolves_to` is
a callable of the stored value (delegating to `sections.stt_provider_resolution`,
the loader's own pure resolver) and the entry carries the section `default`. `--adopt` uses
both: it REWRITES a coerced key as what it resolves to, or deletes it only when
that resolution IS the default (an absent key resolves to the default). So a
retired name is adopted by removal and runs as `local` before and after, and an
unknown name is adopted as the literal `"off"` and runs as `off` before and after.
Adoption never moves the effective provider — deleting an unknown value would have
made the default `local` apply, handing the incident's user back the engine they
were escaping — and `coercion_summary` names the resolution instead of claiming
removal changes nothing. The `is_coerced` predicate rides on the ENTRY, not in the detector's loop,
so appending a retirement is genuinely sufficient — a detector switching on
`dotted_key` would leave an appended entry silently unreported, with no test red and
an operator stuck with a warning nothing can clear. That predicate delegates to
`sections.stt_provider_is_coerced()` rather than restating a provider list, so the
surface offering to remove a value cannot come to disagree with the loader about
which ones are dispatchable. The retirement notice in `_validated_stt_provider`
names the command, for the same reason the drift line does.

**Why nothing is corrected automatically.** At least one registered key also has a
documented escape hatch (`mcp_gateway.forward_declared_env`, whose stored `false`
is pinned as honoured by `test_a_real_false_still_turns_it_off`). On disk that
escape hatch and a stale materialized default are the same bytes, so a rewrite
cannot correct one without overriding the other. Telling them apart needs per-key
provenance -- a record of which keys the operator actually set -- which this layer
does not have.

## Config Overlay (config.local.json)

User overrides can be placed in `~/.kiro/crew/config.local.json`. This file is
deep-merged on top of `config.json` at load time and is never touched by
`kirocrew setup` or package upgrades. Because `save()` keeps an overlay-owned
leaf OUT of `config.json`, such a value exists only here, so the overlay rides
the dashboard export and the snapshot `config` component next to `config.json`
(see [Settings import](#settings-import-dashboard-merge)).

`config.json` is the persistent settings file, not a generated one: no upgrade
or restart regenerates or resets it. The routine writers (dashboard PUTs, keyed
`kirocrew config set`, setup, boot migrations) are locked delta
read-modify-writes of the keys they own (`update_config_locked`), and the
whole-document `KiroCrewConfig.save()` refuses to publish a snapshot of a file
it could not read (see its API entry). Two explicit, user-invoked paths replace
the document instead: `kirocrew config set --file` installs the given file
whole (under the same lock, with `on_corrupt="reset"`, so it also overwrites an
unreadable file), and the dashboard import's **Replace** installs the archive's
`config.json`. Its **Merge** never overwrites a settings document this install
has (see [Settings import](#settings-import-dashboard-merge)). The overlay is for a
value you want PINNED above whatever those writers later put in the base.

Resolution order:
1. Load `config.json` (the persistent settings file every writer updates)
2. Deep-merge `config.local.json` on top (user-owned, never touched by setup/migration)
3. Return merged result

### CLI Usage

```bash
# Pin a setting in config.local.json (wins over config.json):
kirocrew config set --local agent.log_level debug

# Save to config.json (the persistent settings file):
kirocrew config set agent.log_level debug
```

Both forms check the dotted key (`_refuse_unknown_key`): a key the
loaded config does not hold and the schema does not declare exits 1 with
`Unknown key` before anything is written. The check matters most for `--local`:
`config.local.json` outranks `config.json`, so a key nothing reads stored there
(`memory.enabled`) would look pinned while doing nothing. A key plain
`config set` refuses is refused on `--local` too, such as a field of a map member
not stored yet (`agents.<name>.model`). `--local` also accepts a key `to_dict()`
writes from outside its section (`_SECTION_KEYS_EMITTED_ELSEWHERE`, e.g.
`slack.dm_activation`), because the loader reads it there. Its value is still
checked: against the registry row for the field's dataclass spelling
(`slack_dm_activation`, `observe_max_messages`), and for `slack.dm_activation`
against the activation modes. The enum, beacon and
tailnet gates also run before the local/base split, so they apply to both.

A list-typed key (`agent.apps_trusted`, `slack.allowed_users`, ...) takes a JSON array
(`kirocrew config set agent.apps_trusted '["a","b"]'`); anything else is refused with nothing
written. Details: [cli](cli.md) (`config set`).

### `config_local_path() -> Path`
Returns `~/.kiro/crew/config.local.json` (or `$KIROCREW_HOME/config.local.json`).

### `_deep_merge(base: dict, overlay: dict) -> dict`
Recursively merges overlay into base. Dict values merge recursively; all other
types in overlay replace base values.

## Browser UI preferences (ui-prefs.json)

`~/.kiro/crew/ui-prefs.json` is a backup of the dashboard settings that live in
the renderer's `localStorage`, not in `config.json`. It exists because
`localStorage` is keyed by ORIGIN and, in the desktop app, stored inside
Electron's `userData` directory, so a moved dashboard port, a relocated
`userData` directory, a switch between the stable and nightly builds, or a
browser storage eviction wipes every setting the user chose — and reads to the
user as "the upgrade lost my settings".

Owned by `kiro_crew/ui_prefs.py`, served by `GET`/`PUT /api/ui-prefs`, and
consumed by `website/src/lib/uiPrefs.ts`. Deliberately NOT a section of
`config.json`:

- `KiroCrewConfig.save()` re-emits the whole dataclass, so a key the running
  build does not model is dropped on the next save. A bag of client-owned UI
  keys is exactly the shape that loses that fight.
- `config.json` is operator-facing; renderer layout keys do not belong in it.

Contract:

- Values are opaque strings the server never parses. For most keys the value is
  exactly what `localStorage` holds (a UTF-8 string). ONE durable key —
  `mc-chat-config`, a JSON object of ~20 independent chat settings — is the
  exception: it is NOT stored whole. Each of its fields travels under its own
  wire key `mc-chat-config.<hex(field)>`, whose value is that one field's JSON
  fragment, so the server's existing per-KEY merge becomes a per-FIELD merge and
  a profile uploads only the fields it actually changed (issue #15236; before
  this, a second origin holding one stale field uploaded the whole blob and
  overwrote fields it never touched). The field name is hex-encoded (`[0-9a-f]`,
  4 digits per code unit) for two reasons: a raw name could contain the `.`
  separator, and `showContextTokens` — a real field — contains `token`, which
  the credential denylist below would reject, 400-ing every flush. Hex provably
  contains no denied substring and no separator and reverses exactly. The server
  stays oblivious: it just holds more, smaller opaque keys. The file is
  `{"prefs": {...}}` and nothing else: an earlier revision carried a `version`
  and an `updated_at` that no code read, and the loader is tolerant of any shape
  it does not recognize, so a future reshape needs no version field to be safe.
- A second shape of exception is a key that exists only on the wire. The
  board-view folder collapse overrides live in `localStorage` one key per
  (column, folder) pair under `kc-board-folder-collapsed:`, which keeps two tabs
  from overwriting each other's entries. They are backed up as the single
  durable key `kc-board-folder-collapsed`, a JSON object the client projects
  from the per-key entries at every snapshot, with the pairs in sorted order so
  the same overrides always produce the same value. The projection is
  read-only. The key itself never reaches `localStorage`. The host copy is one
  value a flush replaces whole: a toggle on one origin uploads that origin's
  entire family, including pairs the toggle never touched. Where this contract
  lets the host win for it the restore writes the host value as the per-key
  family: every pair it lists becomes its own entry and every entry it does
  not list is removed. The host wins on a cold restore into a profile with no
  overrides, on the reconcile pass that first adds the key to a profile with
  no overrides, on a Settings import, and on the retry after a failed restore.
  A host value with no usable pair, or one whose writes the quota-safe writer
  refuses, leaves the family as it was and counts as a dropped write like any
  other key: the restore does not count it, the host value is not baselined,
  and a family the profile does not hold stays out of the baseline so the
  first flush never reports it as a deletion. A family the profile does hold
  is baselined as a kept local value, so it goes up on the next toggle and
  replaces the host copy then. One key for the whole family because of the
  key bound below: a board with a few columns and a dozen folders would
  otherwise spend a large share of the 200 keys on its own.
- `PUT` is a merge patch; a `null` value deletes its key. BOTH methods are
  owner-gated: the write so a viewer cannot overwrite the owner's settings, and
  the read because some values name real paths on the host (the file explorer's
  saved state, the cloud launch defaults). Gating the read costs nothing, since a
  non-owner can never have written a backup.
- Keys whose name looks like a credential (`token`, `secret`, `password`,
  `credential`, `api_key`, `apikey`) are refused on write and filtered on read, so the
  dashboard bearer token can never land here.
- Bounds: 200 keys, 128-char keys, 64 KiB per value, 512 KiB total. A patch that
  would breach them is rejected WHOLE; nothing partial is written. The client
  answers a rejection by retrying the patch one key at a time, so one unstorable
  value cannot discard the valid changes bundled with it.
- An unreadable or malformed file means "no backup", never an error: the client
  falls back to whatever `localStorage` holds.
- The client reads the backup when this profile has never successfully reached
  the host (`mc-ui-prefs-synced` absent) and only fills keys that are absent, so
  it can never clobber a value the running profile already has. Keyed on
  never-synced rather than no-settings-present so a boot whose fetch failed
  retries on the next one instead of forfeiting the restore. Otherwise the client
  is write-only: the backup is a backup, not a live cross-tab sync channel.
- When the restore actually wrote something the page RELOADS instead of
  rendering. Restoring before the first render is not sufficient on its own:
  static imports are evaluated before the entry module's first statement, so a
  store that reads its key at module scope (`hooks/useBottomTerminal.ts`) has
  already captured the pre-restore value and its first write would persist that
  stale copy back over the restored one. The reload is correct for every
  module-scope reader without a per-store re-init hook, costs one extra load on a
  fresh profile, and cannot loop because the synced marker is written first.
- Every key the host holds is baselined with whatever is in `localStorage` for it
  after the restore — including a local value that DIFFERS from the host's and
  was kept. The hydrating origin is by definition the one that has not been
  syncing, so uploading its value on first flush would overwrite the newer backup
  with a possibly months-stale one; baselined, it stays in use locally and is
  uploaded the moment the user changes it. A value the quota-safe writer had to
  drop is NOT baselined, or the first flush would read it as a deletion and null
  out a good host backup.
- A FAILED first restore writes `mc-ui-prefs-hydrate-pending` holding the list of
  durable keys the profile held AT THAT MOMENT. On the next successful restore a
  key in that list — and any change the user made to it since — is the user's,
  so local wins as usual; a key NOT in the list was written after the failure by
  a page that rendered without its settings (a login screen counts; mount-time
  hooks persist defaults), so for it the HOST wins — treating those defaults as
  the user's choice would upload them over the real backup. A repeat failure
  never widens the list. A never-failed first restore keeps the normal rule.
  Letting the host win for every key instead had the mirror-image defect: a
  returning user whose GET failed once and then changed a preference saw the
  stale host value overwrite the change. The marker is cleared only after the
  synced marker is written, so a crash between the two leaves the profile
  pending rather than synced-with-untrusted-locals.
- `mc-ui-prefs-synced` also holds the NAMES this profile last synced, which is
  how a deletion made before a reload is still reported as a `null` while a key
  this profile never synced is never nulled — that is what stops a second browser
  from deleting the first one's settings.
- Which keys are durable is the client's decision (`DURABLE_PREF_KEYS`).
  Session-scoped and derived state (height caches, panel tabs, drafts, touched
  files) is excluded, as are the settings `config.json` already owns (theme
  mode/colour, language, onboarding flags) and keys a migration deliberately
  deletes (`mc-zoom`, `mc-font-scale`), so no setting has two homes and nothing
  resurrects a key a migration removed. The per-surface prefs that used to
  silently reset across origins -- notification sound (`mc-notification-sound`),
  interface mode (`mc-ui`), reading width (`mc-reading-width`), the sessions
  sidebar width and the width to give back when leaving board view
  (`mc-sidebar-width`, `mc-sidebar-width-pre-board`), and the board folder
  collapse overrides described above -- are durable, and so are the Settings >
  Shortcuts choices: the on/off switch (`mc-keyboard-shortcuts`), the macOS
  digit modifier (`mc-mac-ctrl-digits`), the Search everywhere chord
  (`mc-quicksearch-shortcut`), and the panel toggle chords
  (`mc-panel-toggle-shortcuts`), and the push-to-talk key (`mc-ptt-config`)
  on Settings > Voice. The push-to-talk binding is positional and the
  platform defaults differ, so a client that inherits another machine's
  binding (a Mac client's bare right Option does not arm on most Windows and
  Linux layouts) changes it once; that one change costs less than
  re-recording the key after every destructive upgrade. Two neighboring keys
  are deliberately excluded: the microphone choice (`mc-mic-device-id`) is a
  device id the browser issues for that machine, so a restored one names no
  device, and the rebound-shortcut overrides (`mc-shortcut-overrides`) have
  no writer yet, so the change that adds the writer must also add the key to
  the list.
- A SETTINGS IMPORT rewrites this file under a profile that has already synced,
  which the cold-profile rule above would never re-read. The client therefore
  pauses the sync before sending the import (`pauseUiPrefsSync`: no flush,
  including the `pagehide` one, may land after the import and put this page's
  values back), and when the response says `ui_prefs_restored` it calls
  `adoptHostUiPrefsOnNextLoad` and reloads: that removes `mc-ui-prefs-synced`
  and writes an EMPTY `mc-ui-prefs-hydrate-pending`, so the next load
  cold-hydrates and the HOST wins for every key it holds (by the failed-restore
  rule, a key not in the list is the host's). A key the host does not hold
  keeps its local value. Server side a Merge installs the archive's copy only
  where the host has no `ui-prefs.json` (`ui_prefs.install_imported_ui_prefs`,
  which decides under the module lock that serializes it against the PUT
  handler); a host that keeps one keeps it whole, since the browser adopts the
  host copy on its next load. Either mode holds each entry to the same
  deny-list and bounds as a PUT (an unstorable entry is dropped and counted,
  not fatal).
- GROWING the durable set is guarded by a reconcile pass (growth-gap issue
  9491). A warm profile never runs the cold restore, so a key added to the
  allowlist by an upgrade would otherwise be flushed at its local DEFAULT --
  often written by a hook on mount (`mc-ui` is the live example) -- overwriting
  the value another origin backed up. Before the first flush after an upgrade
  that added keys, the client reads the host copy once for the keys this
  profile has never synced: a key absent locally adopts the host value (with
  the same reload-if-restored rule as the cold path), a key present locally is
  baselined so the first flush does not upload it (it goes up when the user
  next changes it), and a key the host does not hold is seeded from local. A
  host-wins restore the quota-safe writer refuses leaves the local value, when
  there is one, in use and baselined, as the cold path does; with no local
  value the key stays unbaselined, so the first flush neither uploads it nor
  reports it as a deletion. The reconciled roster is recorded as a reserved
  entry inside `mc-ui-prefs-synced` -- deliberately not its own key, so a
  downgraded (pre-roster) build's next
  fingerprint rewrite sheds it and a re-upgrade reconciles again instead of
  trusting a stale roster -- making the pass one GET per allowlist growth, not
  per boot. A profile with no roster predates the mechanism and is baselined
  against the frozen pre-mechanism allowlist, so only genuinely new keys are
  reconciled: a key the profile merely never held is NOT bulk-imported from
  another origin. A failed reconcile suppresses
  the sync for the session -- flushing unreconciled keys is the exact clobber
  the pass exists to prevent -- and the next boot retries; the failed-restore
  marker applies as on the cold path, so a default written by a settings-less
  render between the failure and the retry loses to the host.
- The composite (`mc-chat-config`) rides that SAME reconcile and failed-restore
  machinery at per-FIELD granularity, with four wrinkles worth knowing before
  touching `uiPrefs.ts`:
  - MIGRATION from a host backup written by a pre-split build (one whole-blob
    `mc-chat-config` key): the whole blob is expanded into child wire keys once,
    on both the cold restore and the warm reconcile, so an existing user's chat
    settings are not lost on the first load after upgrade. A field the host
    already holds as a child key wins over the same field inside the legacy
    blob; neither copy is retired (that is more machinery than the issue needs,
    and a child key is authoritative the moment any fixed build writes it).
  - CLEARING a reconciled child: a plain key clears when it is in the synced
    fingerprints OR the reconciled roster. A child gets BOTH — a child the host
    held is fingerprinted; a child the host did NOT hold has nothing to
    fingerprint, so the reconcile records it in the roster regardless of
    outcome. Without the roster entry such a child would read as unreconciled on
    every boot — withheld from flush forever while the first flush of any other
    key fingerprinted it from a value never sent, silently dropping the user's
    choice. The reconcile trigger therefore fires only while a child lacks BOTH
    a fingerprint and a roster entry (or a legacy whole-blob fingerprint is
    still present), so a fully reconciled profile pays no per-boot reconcile GET
    and a transient GET failure cannot cost a whole session's backup.
  - DOWNGRADE: the failed-restore marker records per-field child entries PLUS
    the parent `mc-chat-config` key. A pre-split build iterates the whole-blob
    allowlist and reads ownership by the parent, so the parent entry keeps its
    stale host blob from overwriting local chat settings. A split-aware build
    does NOT treat the parent as a blanket grant over its children — it honours
    the parent only on a legacy marker that carries no child entries at all;
    when child entries are present the exact child key is required, so a default
    a settings-less render wrote AFTER the failure cannot masquerade as owned.
  - DIRTY marker: `saveChatConfig` records the fields the user edited in
    `mc-chat-config-dirty` (`markCompositeFieldsDirty`). A dirty child is never
    withheld and always uploads, even at its default value; it counts as owned
    during restore and reconcile; its marker clears only for a child the host
    acknowledged and only once the synced fingerprints persisted; and host adoption
    (`adoptHostUiPrefsOnNextLoad`) removes the key.
  - PLAIN DIRTY marker: a plain key written only by an explicit user action
    records the save in `mc-ui-prefs-plain-dirty` (`savePrefExplicitly`, which writes the mark first and rolls it back if the value is refused) at its
    writer's save seam: the Shortcuts switches (`components/ShortcutsModal.tsx`),
    the Search everywhere chord, the panel toggle chords and the push-to-talk
    key. Without it a value saved after a failed restore or reconcile is outside
    the frozen owned-at-failure snapshot, and the next successful pass puts the
    host value over the user's choice. A marked key counts as owned on both
    restore paths and is forced into the next upload; its mark clears once that
    upload is baselined; host adoption removes the key. Only allowlisted plain
    keys are accepted.
- Also excluded: any value that GATES A SAFETY CONFIRMATION. `mc-yolo-ack` is the
  instance — its presence makes the approval-mode picker skip the confirmation
  and enable full auto-approval — and the reason is that this file sits in the
  agent-writable data home, so a restorable ack is an ack an agent can forge for
  the user's next fresh origin. Convenience does not outrank a human gate.

## Settings import (dashboard Merge)

**Module layout.** `portability.py` is the import path for every `portability` name
this section cites, and it keeps the import itself: `apply_import_zip` and the settings
and session merges it runs (`_vet_archive_settings`, `_merge_settings`,
`_merge_sessions`). It is a `kiro_crew._facade_mirror` facade over four owners, and new
work goes to the one that owns it: `portability_archive.py` (what an archive holds,
member names and limits both ways), `portability_templates.py` (agent templates, bundled
on export and installed on import), `portability_export.py` (what an export reads: the
walk, the SQLite backups, the session planner and writer) and `portability_vetting.py`
(`validate_import_zip` and the staged cron and store checks). A patch of
`portability.<name>` reaches every owner holding the name. The archive builders
(`create_export_zip`, `stream_export_zip`, `plan_export`) live in `portability_stream.py`,
which the facade re-exports and which reads these names as `portability.<name>`.

**HTTP refusals** of `/api/portability/*` (`dashboard/handlers/portability.py`)
each carry a machine-readable `code`: `file_field_required` (400),
`invalid_import_mode` (400), `import_archive_invalid` (400, with the validator's
detail), `invalid_components` (400, a `?components=` value other than `memory`),
`memory_only_merge_only` (400, a Replace asked for memory only or given a memory
bundle), `import_archive_too_large` (413), `auth_required` (401), a 409 with the
refusal's own code, and `export_failed` / `import_failed` / `preview_failed`
(500, opaque prose). A 4xx carries an actionable detail; a coded 5xx does not, so
the dashboard's `PortabilityTab` `refusalText` shows its own localized fallback for
a coded 5xx and the server's text otherwise.

**Memory only.** `GET /api/portability/export?components=memory` builds an
archive holding only the snapshot `memory` component (`memory.db`,
`memory_index.db`, `workspace/memory/`, `workspace/knowledge/`, `memory_stores/`),
never chats. Its manifest declares `components: ["memory"]` and carries
`portability.MEMORY_EXPORT_MANIFEST_VERSION`, which lies outside the
`1..EXPORT_MANIFEST_VERSION` range a whole-install import accepts, so a reader
that does not know memory bundles refuses one rather than applying it as a whole
install. `POST /api/portability/import?mode=merge&components=memory` extracts and
merges only the memory members of any archive; an archive whose manifest declares
memory only is imported that way without the parameter. The manifest is the one
member at `<prefix>/MANIFEST.json` (`portability._top_manifest`); both the
validator and the import read only that one, so a deeper file with that name is
ordinary content. No top-level manifest, or more than one, is refused. Memory is
not redacted: the dashboard shows a confirm step listing what the file holds
before it builds the export.

**Member names.** Because the memory filter reads member NAMES, both
`validate_import_zip` and `apply_import_zip` refuse a name an extractor could
place elsewhere (`portability._unsafe_member_name`): `..`, an absolute name, a
drive prefix (`C:/x`, which Windows `zipfile` strips), and, only on a host where a
backslash is a path separator, a backslash-spelled form of those. On POSIX a
backslash is an ordinary filename character and is kept. After each extract the
import also refuses a member that did not land under its own top directory. The
manifest is capped at `_MAX_SETTINGS_DOCUMENT_BYTES` (8 MiB, the budget the export
sizes its own manifest to, session times included) before it is decoded.

The dashboard export (`portability.create_export_zip`) carries every document a
Settings choice is persisted in: `config.json`, `config.local.json`,
`ui-prefs.json` and `notification_settings.json`. The snapshot `config`
component carries the same set.

Every archive settings document is vetted before either mode applies one
(`portability._vet_archive_settings`): a document that is not its reader's shape
(a config that is not a JSON object, a `ui-prefs.json` that is not
`{"prefs": {...}}`, a `channel_settings` that is not an object) is refused,
removed from the extraction so neither mode can install it, reported
`<label> (skipped: <why>)` and listed in `refused_merges`, which the handler
audits as a `partial` import. `ui-prefs.json` and `notification_settings.json`
are rewritten to their filtered form (`ui_prefs.parse_imported_ui_prefs`,
`notifications.settings.parse_imported_settings`: a credential-shaped key, an
unstorable value, a mute or lowered priority on `system.approval`, an unknown
field is dropped and counted).

The import's **Merge** (the dashboard default) never overwrites, like every
other merge in the product, and it is honest about it
(`portability._merge_settings`):

- A document this install LACKS is installed from the vetted archive copy and
  reported `<label> (restored)`: `config.json` through `update_config_locked`
  (absence re-checked under the lock; owner-only; this install's `meta`
  stamped; the live watcher woken), `ui-prefs.json` through
  `ui_prefs.install_imported_ui_prefs` (see
  [Browser UI preferences](#browser-ui-preferences-ui-prefsjson)),
  `notification_settings.json` through the running gateway's
  `ChannelSettings.install_imported`, so a restored mute applies at once.
- A document this install HAS is left untouched, byte for byte, and reported
  `<label> (kept this install's; import with Replace to restore the archive's)`.
- `config.local.json` is NEVER installed by a Merge, even where this install has
  none: the overlay outranks `config.json` at load, so an installed copy would
  set every key it names over this install's own config. `kirocrew restore
  --mode merge` follows the same rule.
- Every document the archive carried that the Merge kept the destination's copy
  of is named in `settings_kept`; a document the Merge refused is named in
  `refused_merges` instead, and an empty ui-prefs patch is reported as
  `ui-prefs (nothing to restore)` and named in neither -- the plain file names, so the list is bounded by construction
  (at most four) -- and the dashboard shows them in one notice that points at
  Replace. `ui_prefs_restored: true` is set only when `ui-prefs.json` was
  installed; it tells the dashboard to reload and re-read the browser
  preferences.

**Replace** is the path that restores the archive's settings over this install's.
It installs all four documents through `_do_replace`, which first backs up what
it replaces into a `pre-restore-<ts>/` directory. The staged documents are
owner-only (except `notification_settings.json`, whose live writer uses the
umask), so a replace never installs a config or ui-prefs document wider than its
own writer writes. The swap runs inside `ui_prefs.replacing_file()` and
`ChannelSettings.replacing_file()`, which hold each store's writer lock across the
swap (the notification store also re-reads its file before letting go), so a write
from another tab or channel cannot publish its pre-import copy over the restored
file. It then sets `ui_prefs_restored`, only when the archive carried a vetted
`ui-prefs.json`.

## Shared Crew (`dashboard.shared_crew`)

`config/sections_shared_crew.py` owns the section. Every integer is clamped to
`SHARED_CREW_BOUNDS` and a wrong-typed value reads as its default, so an edit can
narrow or widen a limit but never remove one. The two exposure switches read
fail-narrow: `tailnet_direct` turns on only for a literal `true`, and
`local_authorization` reads a missing key or a literal `false` as off and any other
non-boolean value as on. No code reads the section in this build yet, so the
section is marked `restart=True` and every help text says it is not active. `effective_limits`
resolves `0` (auto) values against a caller-supplied automatic Crew width and caps
per-person limits at the Crew's. Every switch that changes behaviour is off by
default. Defaults and meanings: [shared-crew.md](shared-crew.md) section 4, Defaults
and API.

## Unknown keys are preserved on round-trip

`save()` re-emits the whole dataclass, so anything the running build does not
model is absent from what it writes. Two capture fields stop that from erasing
the operator's settings on an upgrade:

- `_extra_sections` holds unknown TOP-LEVEL sections (an edition-contributed
  section written by a companion), classified against `_KNOWN_CONFIG_SECTIONS`.
- `_extra_keys` holds unknown keys INSIDE a modelled section, `{section: {key:
  value}}`, captured by `resolution.capture_extra_section_keys`. Without it, a
  build that renamed or removed `<section>.<key>` erased the value on the next
  `save()` of any kind (a log-level change, adding a workspace, editing an
  agent), with no backup on that path.

Both restore a key only when the emitted document LACKS it, so a captured copy
can never overwrite a live value or undo a deliberate deletion. `_extra_keys`
has two shapes: `{key: value}` for a dataclass-backed section, and
`{record_name: {key: value}}` for the maps of named records (`agents`,
`workspaces`, `memory_stores`), whose records are parsed field-by-field and
re-emitted with `asdict` and so lose unmodelled keys the same way a section
does. `hooks` needs neither: it is emitted raw and round-trips whole. A record
deleted in memory stays deleted — restore fills into existing records only.

A top-level key the core does not model is captured into `_extra_sections` and
round-tripped, and by default also reported as `Config: unrecognized top-level
keys`. Three exclusions from that warning are named in code:

- `CONFIG_RESERVED_TOP_KEYS` in `resolution.py` (`meta`, retired keys) — stamped
  by `save()` itself or written by an older build; never parsed, never
  captured, dropped on the next save, never warned about.
- `validation._APP_OWNED_TOP_KEYS` (`dev_fleet`) — a section a builtin app reads
  from the file directly (`dev_fleet.repo_path`, prescribed by the Dev Fleet
  "no checkout found" banner). Captured and round-tripped like any unknown
  section, and NOT reported as unrecognized: the product told the operator to
  write it. Private to the warning that is its only consumer; a second member
  is the point at which this becomes an app-declared registration.
- `validation._CORE_OWNED_TOP_KEYS` (`voice_reply`, `connections`) — a section this core itself
  writes and reads but does not model as a field. The settings UI persists it
  (`slack/interactions_modals.py`, `dashboard/chat_voice.py`) and the gateway reads it
  back on startup via `load_voice_reply_config` (`slack/handler_runtime/voice.py`,
  and read by `telegram/transport_dispatch.py`). Captured and round-tripped like any unknown
  section, and NOT reported as unrecognized because the product wrote it itself.
  `connections` is the second member: the Settings OAuth Apps form writes
  `connections.oauth_clients.<slug>.client_id`
  (`dashboard/handlers/connections.py`), which
  `connections/oauth_clients.py` reads back, and
  `agent_materialization/mcp_aliases.py` reads `connections.tool_aliases`.
  A separate set from `_APP_OWNED_TOP_KEYS` because the owner is core, not an
  app: it has no manifest to declare itself through, so the "second member →
  app-declared registration" trigger on the app-owned set does not apply to it.
  Both sets exempt only the shape the reader keeps — a JSON object — so a scalar
  value still warns.

A deprecated field is announced only when it holds something: `null` and an
empty map, list or string carry nothing to migrate, so `validation` stays
silent on them (`False` and `0` are chosen values and are still announced). It
is silent too on a field holding its own schema default (`_is_own_default`,
which never equates a bool with a number): that value behaves exactly as an
absent key, and the default-config writer serializes every field, so a config
Kiro Crew has just created carries `agent.subagent_cpu_cost_cores: 1.0` and the
loader would otherwise warn about its own write on the first launch. For
each populated field, `validation` warns once per process for each distinct
`repr(value)`. It stores only a SHA-256 digest of that representation in the
warning registry, so configured credentials do not stay there as raw text. A
schema entry that is marked `deprecated=True` therefore accepts that an empty
value of its type gets no notice; a field for which `""` or `[]` is itself a
meaningful choice must not rely on the deprecation notice to surface that
value. `to_dict()` also omits `telegram.accounts` when the map is empty: the
field exists only so an operator's named-account tokens survive a save, and
writing back an empty default materialized a deprecated key into every config,
which every launch then warned about.

Both capture from the BASE view of the document, not the merged one. When
`config.local.json` shadows an unknown key that `config.json` also holds, a
capture of the merged value made `save()` emit the overlay's leaf, which the
overlay subtraction then removed — permanently deleting the base file's own value,
so removing the overlay later revealed nothing. The loader therefore records the
base copy of every top-level section the overlay touches (`_shadowed_base_sections`)
at the last moment both documents exist, and captures against that. `save()`
then emits the base value, the subtraction leaves it (it differs from the overlay
leaf), and the overlay still wins on the next load. The base copy rides in the
validated-data cache's sidecar (`ConfigCache.get_with_sidecar`) so a cache hit — where
the overlay is no longer in scope — captures exactly as the disk read did.

A key is NOT captured when it is a field of the section's dataclass, starts with
an underscore, or appears in one of two explicit maps in `resolution.py`:

| map | why the key is excluded |
| --- | --- |
| `_SECTION_KEYS_EMITTED_ELSEWHERE` | `to_dict()` writes it from outside the section dataclass, either conditionally (`slack.channels`, `dm_activation`, `trusted_bot_ids` — absence is a deliberate deletion) or from the top-level object (`slack.observe_max_messages`, `observe_ttl_hours`). |
| `_SECTION_KEYS_DELIBERATELY_DROPPED` | The build chose not to round-trip it: RENAMED (`knowledge.auto_ingest_doc_links`, still read, canonical spelling written) or RETIRED (the removed local-STT install paths — re-persisting them would keep offering a setting with nothing behind it). |

A key the schema DOES model but validation rejected also stays dropped, because
re-emitting it would make the bad value permanent and re-warn on every load.

The failure direction is deliberate: forgetting an entry in
`_SECTION_KEYS_DELIBERATELY_DROPPED` preserves a key that could have been
dropped, which is cosmetic. The reverse is the data loss the mechanism exists to
prevent. `test_default_emitted_section_keys_are_all_recognized` guards
`_SECTION_KEYS_EMITTED_ELSEWHERE` against drift.

## APIs

### `KiroCrewConfig.load() -> KiroCrewConfig`
Loads config from disk. Merges `config.local.json` overlay if present.
Returns defaults if file is missing or invalid.

The installed package declares `jsonschema` as a core runtime dependency so
schema validation runs outside development environments too. The import guard
still lets an incomplete or manually damaged install load, but that fallback
must not be treated as the normal packaged behavior. Generated package metadata
is tested to ensure the validator is required without the `dev` extra.

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
present) is not cached, and neither is a load in which a present file could not
be READ whole (the `digestible` flag below is false). That second rule is what
keeps one transient read failure transient: with `config.local.json` present, a
base read that raised (a Windows sharing violation, an EIO) still yields an
overlay-only document, and caching it under the unchanged stat fingerprint made
every later load in the process serve the base settings at their defaults until
a file happened to change. A file whose bytes read fine but would not parse is a
stable fact about those bytes and is still cached. Pinned by
`test_config_overlay.py::TestConfigOverlayLoad::test_a_transient_base_read_failure_is_not_cached`.

**Content provenance on the cache entry.** Beside the `data` dict and its sidecar, each
cache entry carries a third fact: the digest of the bytes that data was parsed from.
`load_config_with_content_stamp()` returns a config together with that digest, and
`config_content_stamp()` reads the live files' digest on its own. The pair lets a caller
that holds a config across other I/O and later writes something derived from it tell
"my copy is still current" from "a save landed while I was working".

The digest must be bound **inside** the load, which is why it lives on the entry rather
than being read around the call. The fingerprint above is stat metadata, and a
replacement presenting the same `(st_mtime_ns, st_size, st_mode)` returns the earlier
cached object — so hashing the files on both sides of `load()` would pair the cached
data with foreign bytes and report a match. An entry carrying its own digest reports
the provenance of the data it holds: a hit answers for the bytes it was parsed from,
a miss for the bytes just read. A change to this cache that drops or re-derives the
digest therefore breaks exactly the case it exists for.

The digest is taken over the decoded document text — BOM stripped, newlines translated,
re-encoded as UTF-8 — not the raw file bytes, so a BOM or CRLF change alone does not
move it. `None` means only that no digest can be bound because a file could not be
read whole. A document that read whole but failed to parse still carries a digest;
whether its content is faithful is what `degraded_sections` reports. A caller must
treat `None` as unknown and
never as a match; two unknown provenances in particular are not equal. The member event
log is the current consumer: see `member-event-log.md` for how a roster read and the
startup sweep each refuse to correct the log from a config they cannot name.

**The load pipeline.** `load()` publishes what `_load_resolved` returns, and that
runs three stages in `config/loader_pipeline.py`, each callable on its own:

| Stage | Does | Never does |
|---|---|---|
| `read_config_document() -> ConfigDocument` | Draws the load-order ticket, takes one fingerprint stat pass of both files, and serves the cache entry that fingerprint names or reads, merges, repairs, validates, clamps and caches the document (a security clamp that fires records a SEL event). Reports superseded defaults, and on a disk read decides which ones are adoptable and whether the legacy `skills.lazy_load` rewrite is due. | Raise for a missing, unreadable or malformed file, or write a config file. |
| `build_config(doc, config_cls=KiroCrewConfig) -> KiroCrewConfig` | Reads `doc` and the process-wide sticky set of degraded-section observations, and extends that set. Extracts and degrades every section, runs the section builders, assembles the config and captures unknown sections and keys from the base view. Applies the fail-closed overrides for what it finds degraded itself or the sticky set records: Temporary memory mode for a degraded dashboard section or file, and publish denial for a malformed narrowing. An unloaded document (neither file read) gives the field defaults plus the seeded default crew, with the project-skills off-switch an unreadable file leaves. | Any filesystem I/O, a read-stage repair (the agent off-switch coercion, validation, the security clamp), or the write-back. |
| `persist_write_back(cfg, doc)` | Returns at once for an unloaded document. Otherwise decides the pending migrations, seeds the default crew and `default_agent` into `cfg`, writes the delta under the lock, moves `cfg` to each confirmed adoption, records the `connections_ui` marker, and drops the cache when an adoption did not land. | Write after a load that degraded a section, or raise. |

A `ConfigDocument` carries what the read tells the later stages: the ticket, the
path, `data`, whether a file `loaded`, the content digest, `base_unreadable`, the
overlay's `base_shadow`, the `overlay` as read, the `adoptable` defaults and the
`legacy_lazy_stamp`; the one other input of the build is the process-wide sticky
degraded set. `data` is the read stage's output, not raw file content: the
overlay is merged, the read-stage repairs are applied (a quoted `"false"` on an
agent off-switch reads as `false`; an unreadable source leaves
`skills.project_skills_enabled` off), and the document is validated and clamped.
When nothing loaded it is `{}` or carries only that project-skills marker. A cache
hit carries no adoption and no stamp, because a load that did not read the base
must not decide one. A document is single-use: building normalizes `data` in
place. Tests build one in memory, holding what the read stage would produce, and
call `build_config` with no config file on disk.

**Section construction.** Compound section constructors run in small private
helpers: `config/section_builders.py` holds 28 of them and `config/loader_builders.py`
keeps the agent, session, telemetry and dashboard ones (see the Overview); the
loader facade re-exports all of them. This bounds each construction frame instead of
putting every field expression in one large traced resolver frame. The helpers
preserve field evaluation order, coercion and section-local assignment
expressions. Each call creates fresh dataclasses and mutable defaults; no resolved
configuration or permission value is cached by a helper. Store admission and
workflow identity checks still run at every existing call site.

**Defaults come from the DTO fields.** A builder reads its section through
`sections.SectionReader(DTO, data)`. `get(key)` returns the stored value, `None`
included, or the default declared on the DTO's field when the section omits the
key. `read(key, coerce, *bounds, **options)` calls
`coerce(value, default, *bounds, **options)`, so a `_safe_*` coercer falls back to
that same default. A `default_factory` field gives a
fresh value on each read, and a key with no field raises `KeyError`. A DTO's own
`__post_init__`, `from_raw` or `from_dict`, and a reader a builder calls (the MCP
stub-override reader `_resolve_stub_overrides`, `_tailscale_config_from`,
`_read_skip_permissions`, the STT validators and the like), read a fallback that
is the field's default from the field as well: through a `SectionReader` of their
own; through
`fields.field_default(DTO, name)`, which `SectionReader.default` reads through too,
so the rule has one implementation, and which serves a normalizer or single-value
reader with no section mapping to wrap, in `sections` and in the section owners
(which cannot import `sections`); or through the class attribute that holds a
plain default (`DecisionProviderConfig`). Two fallbacks are the exceptions:
`monitoring.max_runtime_secs` and an unrecognised `stt.model` are coerced in
modules the config package imports (`monitoring.limits.coerce_runtime_ceiling`,
`stt.models.resolve`), which cannot import the DTO at module scope, so a
malformed value falls back to `DEFAULT_RUNTIME_CEILING_SECS` or
`stt.models.DEFAULT_MODEL`, the constants the fields' defaults name.

A few readers keep a value of their own that equals the field's default, because
their rule is about the value rather than the default. Apart from the MCP
stub-roster migration (the last item), an omitted key still reads the field; only
a present value the reader cannot use takes the reader's value:

- The empty container or string that a type guard, a shape coercer or a
  fail-closed check returns (`_safe_list`, `_safe_dict`, the id, host,
  link-pattern and session-folder coercers, the Telegram-account parser and the
  Jira-auth row filter, `_safe_color`, `_safe_avatar`, and the effort, bot-name and
  refusal-fallback sanitizers among them), so a malformed `slack.trusted_bot_ids`
  trusts no bot and a malformed `mcp.extra_path_dirs` adds no directory.
- A narrowing or safe value that holds whatever the default is: `mention` for an
  invalid `slack.channels.<id>.activation`, `true` for a non-boolean
  `dashboard.tailscale.bind_refresh_chains`, `node` for an unrecognised
  `dashboard.tailscale.pin_scope`, `auto` for an unknown `agent.jail`, `false` for
  a present non-boolean `agent.dangerously_skip_permissions` (never a grant),
  `per-channel-peer` for an unknown `messaging.dm_scope` (never one session for
  two people's DMs), and unset (`0`) for a malformed or out-of-range
  `dashboard.browser_view_port` (never a port nobody named).
- `None` for an out-of-domain `resource_limits` value, and `local` for a retired
  `stt.provider` (the recogniser that user already had).
- The sentinel a clamp lands on, among them automatic for a negative
  `instances.warm_set_cap`; disabled for an out-of-range
  `messaging.daily_reset_hour` and a negative `messaging.idle_reset_minutes` or
  `skills.max_triggered`; and no pruning or cap for a negative
  `telemetry.retention_days` or `max_total_mb`.
- The MCP stub-roster migration (`_resolve_stub_roster`), which reproduces the
  stub set a legacy install was running, whatever the field defaults are. When
  `mcp_gateway` omits `stub_servers`, it reads an omitted or non-boolean `enabled`
  as off and the roster as empty, and with `enabled: true` an omitted
  `poolable_servers` as an empty roster. `_resolve_stub_base` layers
  `MANAGED_STUB_FOLLOWERS` on top of that roster whenever `kirocrew-core` is
  stubbed; `_resolve_stub_servers` applies `stub_overrides` to that base. Neither
  the base nor the followers are ever written back to `stub_servers`.

This list describes the build stage. Values that the read stage's schema
validation rejects never reach it. The deliberate departures, all unchanged by the
reader:

- An omitted key that does not read the field default:
  - `agent.sandbox_allow_unsandboxed_exec` folds in the platform default
    (`unsandboxed_exec_platform_default`).
  - `session.pool_size` reads `DEFAULT_POOL_SIZE` at call time, a patched seam.
  - An external registry entry that omits `branch` keeps the legacy `mainline`.
  - `dashboard.import_onboarded` and `dashboard.privacy_acked` fall back to
    `dashboard.onboarded` when omitted or malformed, and
    `dashboard.model_picker_configured` is inferred from
    `model_picker_hidden_models`.
- A built value that is not the bare dataclass's although the default is:
  `agent.member_acp_backend` passes the selectable-backend gate, and an empty
  `workspaces` or `memory_stores` table gains a `default` entry. While the
  dashboard section or the whole file is degraded, or was observed degraded,
  `dashboard.default_memory_mode` builds `temporary` whatever the section holds:
  `build_config`'s fail-closed override, applied before the reader runs.
- A malformed value that does not fall back to the field default:
  - `auto_update` reads as off, and `skills.project_skills_enabled` and
    `mcp.honour_auto_approve` honour only a real `true`.
  - An unknown `stt.provider` reads as `off`, an unrecognised
    `dashboard.default_memory_mode` as `temporary`, and an invalid
    `slack.dm_activation` or `telegram.forum_activation` as `mention`: each
    narrows instead.
  - A `telemetry.beacon_endpoint` that is not a usable `https://` URL is cleared,
    which turns the beacon off.
  - `dashboard.loop_stall_exit_after_secs` reads as `LOOP_STALL_EXIT_AFTER_DEFAULT`
    rather than automatic, and `instances.connect_timeout_secs` /
    `mint_timeout_secs` as the transport constants. Validation removes a value of
    the wrong type at these keys and at `mcp.honour_auto_approve`, so through
    `load()` they read their field default (when jsonschema is installed).
  - The read stage reads a present non-boolean `auto_update` as off
    (`fields._coerce_bool`) and a present non-boolean `agent.session_control`,
    `agent.member_dispatch`, `agent.crew_panel` or `skills.project_skills_enabled`
    as `false` before validation can remove it, and resolves `stt.provider` and
    `dashboard.default_memory_mode` by the builders' rules, so those keys load as
    above.

`test_config_load_pipeline.py` builds every section from an empty one and compares
each field with the bare dataclass, allowing two declared exceptions: the
platform-resolved `agent.sandbox_allow_unsandboxed_exec` and
`agent.member_acp_backend` (the seeded `workspaces` / `memory_stores` entries are
not part of that comparison). It also checks the inline entry reads and pins the
registry branch, the pool-size seam and the platform fold-in. The top-level reads
use `KiroCrewConfig`'s own field defaults even when `load()` builds a subclass.
`test_config_load_pipeline_field_defaults.py` loads a value of the wrong type for
every section field outside the declared exceptions above and the fields another
suite owns, through the read and build stages, allowing only the read-stage repairs
above; pins what each malformed-value departure builds and loads; and moves field
defaults to show that the builders, the DTO normalizers and the shared readers
follow the field, and that a value a reader keeps of its own, or either constant
above, does not.

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

Reads the `agent_discovery.parsed_agent_specs` snapshot (stat-signature
revalidated, the same cache behind `agent_skill_globs`) rather than re-parsing
every spec per call. It runs synchronously on the event loop from the provider
factory — every session start and every background recycle — and a per-call
scan of a ~125-file agents directory was ~125 `realpath` calls plus twice as many
`is_sensitive_path` round trips through the two-worker `mc-pathres` pool; with
the skill scanner's bulk traffic on the same pool those waits queued and their
sum crossed the loop-stall watchdog (eight of eight dumps on the reporting host).
On-loop calls read only the cached snapshot dict and schedule at most one
in-flight revalidation per directory on `mc-discovery`; every `scandir`, stat
and parse stays on that worker. A snapshot that holds rows is served as it
stands, whether or not it names the agent, so a changed directory answers from
the previous rows until the refresh lands. An EMPTY snapshot on the loop (nothing
published yet, or just cleared) is `""` -- no pin -- and is NOT answered from the
named agent's own file: a spec read, however bounded, is filesystem IO on the
event loop, which the `no-blocking-call-on-event-loop` rule forbids regardless of
size. The one caller that resolves a pin against a cold snapshot is the persistent
background session (`_bg`, agent `kirocrew-lite`), created by `_ensure_background`
at gateway start before the first refresh lands; it is async, so it awaits
`agent_discovery.warm_agent_specs()` (labels `ensure_background`/`unknown`) before
the provider factory runs -- the same shape `warm_project_agent_names()` gives the
per-turn project resolver (see `resolve_agent_bindings` below) -- and the factory's
unchanged lookup then finds warm rows, so `_bg` is created on its own pin instead
of `agent.model` for the gateway's lifetime. The warm-up is one awaited
`parsed_agent_specs` parse on `mc-discovery`, no polling and no retry; it never
raises (a failed warm-up costs that session the cold answer), and a
`clear_list_agents_cache()` landing during the parse keeps its rows unpublished
(the generation guard), so the lookup then degrades to the cold answer exactly as
a concurrent agent write does today. A warm worker revalidation costs one
`scandir` and no parses, whereas off-loop callers revalidate and parse inline.
JSON-first
precedence for two live specs of different stems declaring one name is kept by
a stable sort on suffix, and any failure to import, walk or parse is `""`, never
an exception into model resolution.

### `kiro_agents_dir() -> Path` (`config/paths.py`)
Leaf helper returning `~/.kiro/agents` — the **user-level** scope. Lives in the leaf
module so the loader owners (and `_resolve_named_agent_model`'s `agents_dir` DI seam) can
locate installed agent JSONs without importing `kiro_crew.agent` — which imports
`config.loader` and would create an import cycle.

Deliberately **single-valued**: it is the WRITE target as well as a read scope
(`bridges._register_agents` and `agent.rebuild_agent_config` both write here), so it
is never widened into a search path.

A third writer is the side chat's derived read-only spec,
`dashboard/side_readonly_spec.publish_readonly_spec`: `<agent>--readonly.json` (or
`<agent>--readonly-<8 hex of the project path>.json` for a project-scope base, so two
checkouts declaring the same agent never contend for one file), the active agent's
spec with every backend-side grant emptied (`allowedTools: []`, no
`mcpServers.*.autoApprove`, no `toolsSettings.*.allowed*`/`trusted*`/`auto*`,
`includeMcpJson: false`, `autoAllowReadonly: false`, an empty KAS `permissions`) and
the lifecycle `hooks` removed, regenerated from the base spec on every side turn and
written atomically only when its content changed. It is a runtime resource, never
hand-edited (an edit is overwritten on the next turn), and it lives HERE because
kiro-cli discovers selectable agents from nowhere else: this directory and the
session's `<project>/.kiro/agents`, at process start (`acp/runtime.py`). The project
scope is the user's checkout, which Kiro Crew does not write into, so the user-level
registry is the only publishable location. The derived spec declares its own `name`
so no two files declare the base agent's name (`agent.agent_spec_path` refuses that
ambiguity), and its `description` starts with the owner marker
`Kiro Crew derived read-only spec`: the writer refuses — never overwrites — a file at
the derived path without that marker, and refuses to publish at all when another
spec (project scope, or a second user-scope file) declares the derived name, because
kiro-cli would load that one instead. See `side.md`.

### `project_agents_dir(project_dir)` / `project_kiro_dir(project_dir)` (`config/paths.py`)
The **project** scope, read-only: `<project>/.kiro/agents` (kiro-cli's own workspace
agents dir) and `<project>/.kiro` (which holds Kiro Crew's older
`*.agent-spec.json` convention). kiro-cli resolves `--agent` against
`$PWD/.kiro/agents` before the user-level dir with **no upward walk**, and Kiro Crew
spawns kiro-cli with the session's project directory as its cwd, so this is exactly
the directory the backend searches for that session.

Only `.kiro/agents/*.json` is **dispatchable**: kiro-cli does not read
`*.agent-spec.json`, so `agent_discovery.project_agent_files()` excludes it unless
the caller opts in with `include_legacy=True` (only the Slack handler does, for its
own pre-existing listing/resolution). Offering a legacy-only name on a dispatch
surface would have it accepted by the picker and by `spawn_run`, then fail at
`session/set_mode`.

### `resolve_agent_bindings(config, agent_name=None, project_dir=None) -> ResolvedBindings`
Resolves the workspace, memory store and **kiro agent** a session runs under.
Resolution order:

1. `agent_name` is a key in `config.agents` (and the stated namespace is not
   `template`) — use that alias's bindings: template, workspace, memory store
   and model pin.
2. otherwise a **template session** (#18328): `default_workspace`, Global Memory
   V1 (`DEFAULT_MEMORY_STORE`), no mate model pin, `resolved_alias` empty,
   `selection_kind="template"`. The template dispatched is `agent_name` when it
   is a **materialized kiro agent config** — a `*.json` under `~/.kiro/agents/`
   or, when `project_dir` is given, under `<project>/.kiro/agents/` — whose
   **declared `name`** matches (the filename stem only when the config declares
   no name); `kiro-cli agent list` enumerates agents by declared name, so a
   namespaced filename stem such as `mochi--mochi` is NOT a name kiro-cli can
   resolve and must not be treated as dispatchable. Otherwise it is the
   **default template**, `default_template(config)` = `agent.default_agent` or
   `kirocrew`, which always counts as resolved. A non-empty name that matched
   neither reports `requested_resolved=False`.

A plain session — no `agent_name` — is rung 2 with the default template. It is
never the `default` crewmate: `config.default_agent` names the roster's default
mate and is not read here, so that row's workspace, store and model pin stay
its own. There is no "first available alias" fallback any more.

Before plain sessions were template sessions they ran the template of the
roster default's row (the top-level `default_agent`), and an upgraded document
holds that template only there: the reserved `default` mate bound to a
template of its own (`default` → `default-2`), a template a ★ enrolled as an
alias, or an existing mate the ★ pointed at because it already ran the
chosen template. The write-back carries it into `agent.default_agent` once
(`MIGRATE_STAR_DEFAULT_TEMPLATE`, predicate `migration.star_default_template_due`)
when `agent.default_agent` is unset (any stored value, `kirocrew` included, is a
choice the ★ or `config set` already made), the top-level `default_agent` names
a row, and that row's template is not `kirocrew`: the load sets
`agent.default_agent` to that `kiro_agent` (through `dispatch_kiro_agent`) in
memory at once and in `config.json` under the write lock, re-detected there like
every other delta. Only the template is carried, whatever the row is called: its
folder, store and model pin stay the mate's own, so a plain session runs
that template on the default workspace and Global memory with no mate model
pin, and a mate with a store of its own keeps it for its DM thread and
explicit picks. The row and the roster default are left alone. One-shot by
state: once the field holds any value the predicate is false, and the ★ writes
the field directly, so resetting it to `kirocrew` is never undone by a later
load. A roster default this load seeded (to `default`, or the first-alias
fallback) is carried like a stored one: it is the row plain sessions read, and
once stored it is indistinguishable from a pick. The
overlay wins where it supplies the field, so the seed is skipped there. The warm
pool resolves a blank `session.pool_agent` through the same path as a plain
session (`pool_kiro_agent`), never through the alias table, so an
`agent.default_agent` that spells a mate alias binding another template
prewarms what the session runs.

**Two "default agent" settings, two things.** `config.default_agent` (top-level)
is a MATE alias — a key of `config.agents` — naming the roster's default
crewmate: the **Default** badge on the Mates page, its DM thread, and the
row the mate-delete guard keeps. No agent-less dispatch path reads it.
`PUT /api/config/default-agent` writes it for `agent_kind: "member"` (or a bare
mate alias) and writes `agent.default_agent` for `agent_kind: "template"`.
`config.agent.default_agent` is a TEMPLATE name — what a plain chat, a CLI
chat, a warm-pool process, or an agent-less schedule runs when nothing names
one, and the runtime's own `kirocrew` when unset. The Custom agents tab reads
that choice through `GET /api/config/default-agent` (see
[learn-cron-dashboard](learn-cron-dashboard.md)). Its body is
`{default_agent, default_template, overridden, override_path?}`:
`default_agent` is the roster's default mate alias; `default_template` is
the effective template, including the managed-template fallback when a stored
spec is gone; `overridden` says whether `config.local.json` pins
`agent.default_agent`; and the absolute `override_path` appears only for the
owner so an open read cannot disclose the owner's home-directory path. The
picker writes the template through `PUT /api/config/default-agent` with
`{agent, agent_kind: "template"}`, implemented by
[agent_admin/default_agent.py](../../../src/kiro_crew/dashboard/agent_admin/default_agent.py).
The template roster reports the setting as a `used_by` reference of kind
`default`; the writer accepts only an installed GLOBAL template, never a
mate's private copy, a background-only runtime spec, an app's materialized
agent, or a name the browser mask would hide.

`selection_kind="template"` restricts an existing conversation to the materialized
template namespace even if discovery has imported a same-named member.
`selection_kind="member"` requires the configured alias instead of falling back
to a same-named template. Both still report an unavailable explicit selection
through `requested_resolved=False`; neither flag authorizes member memory.
Dashboard callers obtain this choice from the canonical session execution record
described in [session](session.md#agent-selection-provenance).
The session resolver rejects a different agent name when a execution record
exists. Live provider switches publish their validated template choice before
history changes; ordinary resolution cannot replace provenance from metadata.

The materialized half of rung 2 exists because an app's agents are materialized
into `~/.kiro/agents/` by `bridges._register_agents` under a namespaced FILENAME
(`<app>--<agent>.json`) while the config inside keeps the app's own bare `name`,
and **nothing adds them to `config.agents`** — that mapping is authored by setup /
the user. Without it an app-bound session fell through to the default template
while the slot still advertised the requested name, with none of the app's MCP
tools. It is deliberately wider than app agents: **any** parseable config in those
directories dispatches with default bindings, because they *are* the kiro-cli
agent registry and narrowing to app-registered names would require provenance
they do not record.

`project_dir` must be the directory the session actually runs in (the same value
passed as the kiro-cli cwd).

**Neither scope touches the filesystem on the event loop.** The user-level scope is
served from the process-wide materialized snapshot (refreshed off-loop by the
writer); the project scope differs per session, so it is served by
`agent_discovery.project_agent_names()` — a per-project name set revalidated by a
stat-only signature, so a repeat scan costs two `scandir` walks rather than
re-reading every spec. `_project_declares_agent` splits on whether a loop is running:
off-loop it scans, on-loop it reads `cached_project_agent_names()`, which performs
**no syscalls at all** and reports "not declared" on a cold cache so the caller falls
back — exactly as the cold-snapshot user-level path does. Bounding the file *count*
is not the same guarantee as bounding *latency*: this runs on every turn of a
project-bound session, so a network or otherwise slow checkout would become a
recurring gateway stall the loop-stall watchdog blames on chat.

Async callers therefore **warm the cache before resolving**:
`agent_discovery.warm_project_agent_names()` runs the scan on
`executors.discovery_executor()` (the pool `/api/agents/installed` uses), after which
the on-loop read is a hit. The turn's admission phase (`_turn_admit`, in
`dashboard/chat_turn/turn_admission.py`) and the side-turn handler both do this.

`subagent._validate_agent` cannot warm — `spawn()` is synchronous and already on the
loop — so it reads the project scope from `cached_project_agent_names()` only. A
project agent is accepted once that project's cache is warm (any session that
resolved bindings for it has warmed it); a cold cache reports the name unknown, which
is fail-closed and matches that function's existing rule of refusing an unknown name
rather than silently running the default. Widening its pre-existing user-level scan to
a second directory instead would stall the gateway.

`slack/handler._resolve_agent_name` runs on the loop too, so it prefilters on the
**filename** and reads at most the one matching spec — resolving every spec's declared
name would stall Slack on a checkout with many agents.

Dashboard turns and eager allocation offload binding resolution and capture the
canonical execution record before provider work. Learned database availability
is checked by memory operations, not by persona/template admission. Their
`resolve_session_agent_bindings` wrapper converts a resolver's `StopIteration`
into an explicit unavailable-selection error before it crosses the worker
Future: asyncio cannot deliver `StopIteration` through that boundary.

`ResolvedBindings` additionally reports `requested_resolved` (whether the
requested name was honored — False means the default answered) and
`resolved_alias` (the alias key whose bindings were used). `selection_kind`
records whether the explicit selection was a template or member. Callers
persisting a member name use `resolved_alias`, never its `kiro_agent`; dashboard
template conversations retain their requested name together with their recorded
selection namespace so later alias discovery cannot change the selection.
The session resolver also captures `selection_revision` before resolving:
an empty string observes no execution record, while `None` means the caller
did not make an observation. Automatic publication checks that revision under
the writer lock before replacing a record. This transient field guards
publication. The execution record also persists a unique publication revision to
distinguish repeated selections during compare-and-set; neither revision grants
memory access.

#### App-slot cold-snapshot self-heal & fail-loud (`dashboard/chat_turn/turn_admission._turn_admit`)
The one-turn cold fallback above is acceptable for an ordinary session (the next
turn self-heals), but it is **not** acceptable for an **app-owned** slot
(`slot._app` truthy — an App-Kit slot bound to an app's own kiro agent, e.g.
`my-app-agent`). An app agent is never in `config.agents` and is resolvable
*only* through the materialized snapshot, so a cold on-loop read makes
`resolve_agent_bindings` fall back to the default agent with
`requested_resolved=False`. The result is silent: the slot still advertises the
requested name while the generic default agent answers **with none of the app's
MCP tools and no error**, leaving the app unusable until a gateway restart happens
to re-warm the snapshot. `_turn_admit` therefore guards the resolve, strictly behind
`slot._app and not bindings.requested_resolved` (zero extra work / I/O on the
common hot path — no `_app`, or already resolved). The first rescan step also
runs for a named non-app slot that did not resolve while
`materialized_snapshot_complete()` is False (a scan skipped a spec, or the boot
scan has not landed); if the re-resolve still misses, that turn fails with
`memory_unavailable`. A complete snapshot is trusted and costs no rescan:

1. **Self-heal (two escalating steps).** First, **rescan** the snapshot **off the
   loop** with the same pattern the gateway boot uses (`dashboard/server_runtime/app_platform.py`) —
   `await loop.run_in_executor(subprocess_executor(), refresh_materialized_agents)`
   (`refresh_materialized_agents` never raises, so awaiting it via the executor is
   safe) — then **re-resolve once**. This recovers an app slot whose spec is on
   disk but whose snapshot was simply not yet warmed on this loop. If the
   re-resolve *still* misses, the spec was never materialized even though the
   source is intact, so **re-register this app's resources from source** —
   `await loop.run_in_executor(subprocess_executor(), register_app, slot._app)`
   (`register_app`, `apps/bridges_register.py`, registers the app's MCP servers BEFORE its
   agents and publishes the snapshot synchronously; imported via a **local** import
   inside the function to avoid the top-level `apps`↔`dashboard` cycle, mirroring
   `dashboard/server_runtime/app_platform.py`'s local import of `reconcile_enabled_app_resources`. `register_app`
   is used rather than the narrower `refresh_app_agents` because a never-materialized
   app also has unregistered MCP servers, and re-materializing only the agent would
   inline an empty server map — recreating an agent whose own `@<app>:<server>` tool
   refs dangle, i.e. it dispatches but its tools never mount. `register_app` already
   honors the execution-admission gate, and the recovery call is additionally gated
   on `is_app_enabled(slot._app)` held under `app_lifecycle_lock(slot._app)`, so a
   concurrent disable/uninstall cannot race recovery into reactivating a
   deregistered agent — a disabled app simply falls through to the fail-loud) —
   then **re-resolve again** and use the fresh bindings. A recovery-step failure
   only logs a warning; it costs nothing beyond the fail-loud below.
2. **Fail-loud.** If the slot is app-owned and *still* unresolved after the
   from-source re-registration, `_run_chat` does **not** run the default agent. It
   raises `_AppAgentNotLoaded` (naming `slot.agent`, e.g. *"The app agent
   'my-app-agent' isn't loaded yet — try again in a moment, or restart the
   gateway"*) which a dedicated `except` arm beside the terminal turn-error
   handlers (`_on_app_agent_not_loaded`, `chat_turn/app_agent_binding.py`) surfaces
   as a normal `error` card (no `record_failure` — nothing ran). The raise happens
   *before* `get_or_create`, while no session lock is held, so the standard
   `finally` teardown runs without ever creating a session or dispatching an agent.

The eager-spawn pre-warm path mirrors the **self-heal** step (rescan →
re-register-from-source) only (so the speculative session bakes in the app's own
agent rather than the default, which a first real turn would otherwise have to
discard); the **fail-loud** lives on the real turn alone, since the eager path is
best-effort and tears itself down on any miss.

**Failed background starts back off, then stop.** The signals that schedule an
eager spawn (focus, reconnect, slot create, reset) recur, so a slot whose agent
cannot start would spawn and tear down a fresh process tree on each one. After a
failed background start, `schedule_eager_spawn` skips every signal for a backoff
window: 10s after the first failure, 20s after the second. Nothing is queued for
later, and the first signal after the window starts normally. (The window doubles
up to a 300s ceiling, which only a larger cap would reach.) After 3 failures in a row (`_EAGER_SPAWN_FAILURE_CAP`),
background starts for that slot stay **off until a start succeeds or the gateway
restarts**. `schedule_eager_spawn` refuses the slot, and the stop is logged once
at ERROR and posted once as an error row in the chat. The row's last-error text
is redacted and bounded to 500 characters. The user's next message still starts
the agent, and its success clears the count. The count lives only on the
in-memory `_ChatSlot`, so a gateway restart starts it at zero.

Only a failure of the agent start itself counts: an exception from the session
allocation in `_spawn_admitted_prefetch`, other than a shutdown
(`SessionClosingError`), a key being ended (`SessionEndingError`), a
speculative-resume refusal, or a capability refusal raised before any process ran
(`CapabilityError`, or a `CapabilityStartupError` code in
`_PRE_SPAWN_CAPABILITY_CODES`). A failed pending-reset consume, binding or
selection write, or admission step spawned no agent and is logged without
counting. Pinned by `test/test_eager_spawn_start_backoff.py`.

`register_app` (`apps/bridges_register.py`) backs the from-source recovery with a **visible
error**: when a manifest declares agents but `_register_agents` materializes none
(source missing or unreadable) it appends a `"registered 0 of N declared
agent(s)"` entry to `result.errors` — which `reconcile_enabled_app_resources`
counts and logs — instead of returning a silent 0-agent success; a partial
registration (some but not all) logs a warning.

### Materialized-agent snapshot (`config/loader_snapshots.py`)
Rung 2's membership test is a process-global `frozenset` — a pure in-memory lookup
with **no filesystem I/O, not even a stat**. It is reached on every turn of an
app-bound session from the gateway event loop (`_run_chat` →
`resolve_agent_bindings`), where a directory scan would stall chat, WebSocket and
heartbeat processing (`no-blocking-call-on-event-loop`).

The snapshot is only ever rebuilt off-loop:

- `refresh_materialized_agents()` — full rescan; **must** run off-loop. Reads each
  config through `hooks.safe_read_file`, so a symlink planted in that
  user-writable directory cannot make a boot refresh read a protected file;
  refused paths are skipped. A stem is trusted only after the file parses as a
  JSON object. A scan that skipped a spec is **incomplete**: the installed
  snapshot keeps the names and stem mappings the previous snapshot held for the
  skipped files (their stem, or the name that stem mapped to), because those
  files are still on disk, and drops everything else the scan no longer sees. A
  failed directory listing keeps the whole previous user-level snapshot. An
  incomplete scan never heals the default agent. `materialized_snapshot_complete()`
  reads the flag, in memory.
- `schedule_materialized_agents_refresh()` — safe from anywhere: offloads to the
  default executor when a loop is running, refreshes inline when not.
- `publish_materialized_agents(names)` — pure set union, no I/O, so it is safe on
  the loop. `_register_agents` publishes what it just wrote **before** scheduling
  the rescan, so a slot created before the rescan lands still resolves.
- `_register_agents` / `_deregister_agents` schedule a rescan around their writes
  (unconditionally on the register side: a call that writes nothing may follow a
  prune, and only a rescan drops a name that is gone from disk).

Two guards keep concurrent updates coherent, each with a test that fails when it
is disabled: a **generation counter** bumped by every publish (a scan that globbed
before a write unions rather than replacing, so it cannot erase a just-published
name), and a **monotonic ticket** taken when a refresh starts (a completed scan is
discarded if a refresh that started later already applied, so an older view
finishing second cannot resurrect a deleted agent). A lookup with no snapshot yet
builds one lazily **only** in a synchronous context; on a running loop it falls
back for that turn rather than block.

#### Default-agent healer

`reset_dangling_default_agent` points `default_agent` back at the shipped
`default` crew when the template the current default binds has been REMOVED — an
earlier landed snapshot in this process let it dispatch and the current one does
not. It runs only through `refresh_materialized_agents(heal_default=True)`, which
only the owner's agent-catalog fetch passes (`dashboard/handlers/agent_catalog.py`),
and only after a complete scan; the removal evidence is bounded
(`_MATERIALIZED_SEEN_MAX`, 4096 names). It declines on any uncertainty: an unreadable
or degraded config, a missing `default` alias, a default already `default`, a
template the edition still supplies, or a template the row's workspace
`.kiro/agents` still declares. The write is the default-agent PUT's own locked
read-modify-write, and every condition is re-derived inside the lock, so a PUT that
repointed the default in the window wins. The alias row itself is left alone.

### Effective-agent report (`resolve_effective_agent`)
`resolve_agent_bindings` stores the REQUESTED agent verbatim and only logs when
nothing dispatches it, because rewriting the stored name was destructive: the
resolution behind the rewrite can be momentarily stale while the overwrite is
permanent. `resolve_effective_agent(agent_name, project_dir)` is the
non-destructive other half — it names the agent that will actually answer, and
`""` for "nothing to report". The name it reports is the one the runtime
resolver binds an unhonored request to: the **default template**
(`default_template(config)`, published in the alias snapshot at load), never a
mate alias — a plain or unhonored session is a template session on the
default workspace and Global memory, so a marker naming the `default` mate
would advertise a store and a model pin the session does not use.

Three properties, all pinned by tests:

- **No filesystem I/O**, for the same reason rung 2 has none: it is called from
  `_ChatSlot.to_dict()` for every slots frame on the event loop. It reads only the
  materialized snapshot, the alias snapshot published by `KiroCrewConfig.load()`
  (`publish_agent_alias_snapshot`), and `cached_project_agent_names` — never a
  scan, stat or config re-read.
- **Fails closed to `""`.** A cold alias snapshot, a cold materialized snapshot
  and a cold project cache all report no divergence. A false "your agent was
  substituted" marker sends the user chasing a substitution that never happened,
  so silence during a boot window is the correct answer, not a guess.
- **Agrees with `resolve_agent_bindings`.** The alias snapshot's fallback slot is
  `default_template(config)`, so the marker and the runtime name the same
  template; the template's own name and every mate alias report `""`.

Consumers: the sidebar's session-row marker, and `mochi`'s `ensureSlot`, which
refuses to send into a slot whose effective agent is someone else.

Known follow-up (#1429): the snapshot makes this module a second home for agent
discovery beside `apps/registry`.

### `KiroCrewConfig.create_provider_factory() -> Callable`
Returns a factory for LLMProvider instances. Resolves `"auto"` model
before creating the provider.

### `KiroCrewConfig.to_dict() -> dict`
Serializes config to the JSON structure used by `config.json`. Uses `_configured_port`
(the file value) instead of `dashboard_port` (which may be overridden by `KIROCREW_PORT`
env var) to avoid clobbering the saved port on write-back.

### `KiroCrewConfig.save() -> None`
Writes current config to `~/.kiro/crew/config.json` via `to_dict()`, through
`write_config_atomically()` (see below). Invalidates the `load()` validated-data
cache so the next load reflects the write immediately.

It **fails closed** and writes nothing (raises `ConfigReadError`) in two cases:

- The instance was built by a load that found `config.json` present but
  unparseable (`_base_unreadable`). Such a snapshot holds defaults rather than
  the user's settings, and that holds even if the file has been repaired since.
- `config.json` exists now and does not parse (read through
  `read_config_for_update()` under the same lock).

A missing file is still created, which is what the create-default callers rely
on.

**No dashboard module calls it.** A dashboard writer publishes the keys it owns
through `run_config_write(update_config_locked, config_path(), mutate=...)`.
`test_config_save_locking.py::TestNoDashboardModuleSavesTheWholeConfig` pins
that over `src/kiro_crew/dashboard`, matching a `.save` REFERENCE handed to an
offloader as well as a call, with an empty baseline. The workspace display PUT
(`PUT /api/config/theme`) was the last such writer: it loaded the whole config
and saved it back, so on a momentarily unreadable file (a torn read, a sharing
violation, an empty file) it published pure defaults over every setting
-- from a request the SPA sends on its own at boot, because a defaults load
reports `onboarded=false` and that triggers its legacy theme migration. It now
validates the whole body first (a 400 writes nothing), writes only the named
`dashboard.*` keys (skipping the write when they already hold those values),
answers an unreadable file with `500 {"code": "config_unreadable"}` and the bytes
untouched, and builds its response from a fresh load.

### Partial config updates: `read_config_for_update()` / `write_config_atomically()`

Many callers do not hold a whole `KiroCrewConfig` — they flip one toggle
(`auto_update`), persist one channel, or seed one default. That shape is a
**read the whole file → mutate one key → write it all back** cycle, and both
halves of it are data-loss-prone. These two helpers are the required primitives
for it; do not hand-roll the cycle.

**`read_config_for_update(path=None) -> dict` fails CLOSED.** The natural
`try: json.loads(...) except Exception: data = {}` is a bug in this shape,
because the `{}` fallback is indistinguishable from "the user has no settings" —
so the write-back replaces a fully populated config with a single-key one, every
setting the user ever chose is gone, and the endpoint still reports success. So:
an **absent** file returns `{}` (a genuine empty starting point), while an
unreadable or non-JSON-object file raises **`ConfigReadError`**. Callers must let
that abort the update; leaving the existing file untouched always beats
overwriting it with defaults. `ConfigReadError` deliberately does **not** inherit
from `OSError`/`ValueError`, so a pre-existing broad `except OSError` around the
write cannot swallow it and resume the clobbering path.

The read fails for mundane reasons, most commonly a **torn read**: a
truncate-then-write config writer leaves a window in which a concurrent reader
observes a half-written file. The window is small, which is exactly what made the
resulting loss so hard to reproduce — it presented as "all my settings reset
themselves".

The last product writer of that shape was the voice settings PUT
(`PUT /api/voice/config`), which read with `json.load` and wrote back with
`open(path, "w")` + `json.dump` -- no lock, no meta stamp, no live-watcher wake,
and every failure swallowed behind `{"ok": true}`. It now persists through
`run_config_write(update_config_locked, ...)`, merging only the keys the request
named into the existing `voice_reply` block, BEFORE it applies them to the live
voice config, so a refused write (`500 config_corrupt` on an unreadable file,
`500 config_write_failed` on an `OSError`) leaves both the file and the running
setting as they were and the panel rolls its optimistic update back.
`test_config_rmw_preserves_settings.py::TestNoRawOpenWriteOfConfig` keeps the
shape out: it fails on `open(p, <writing mode>)` or `p.open(<writing mode>)`
where `p` is `config_path()` / `config_local_path()` or a name bound to one,
with an empty baseline.

**`write_config_atomically(path, data, *, fsync=False)` is atomic AND
mode-preserving.** Atomic (tmp+rename) so no reader ever sees a partial file —
this is what closes the torn-read window for everyone else. Mode-preserving
because tmp+rename creates a NEW inode, so the umask default (typically `0644`)
would silently replace an operator's tightened `0600`; `config.json` can hold
inline credentials, so a settings write must never widen who can read it. An
existing file's mode carries over, minus group/other access when the file is inside the
data home (see security.md "Data Home File Modes"); a newly created one is owner-only. On
Windows it also applies a real owner-only DACL via
`platform_compat.restrict_to_owner` (`restrict_on_error="warn"`, so a DACL that
cannot be applied warns rather than making the config unwritable).

That is a reversal of an earlier ruling recorded here, and the reason it changed
is worth keeping: the lockdown used to shell out to `icacls`, a blocking
subprocess this function could not afford because it runs inside async request
handlers and `save()` (`no-blocking-call-on-event-loop`). It is now applied
in-process through `advapi32` (measured 0.24 ms against 313 ms for the
subprocess), so the cost that forced the omission is gone and `config.json` --
which can hold inline provider tokens -- is no longer left under whatever DACL it
inherits from its parent. Follow-up work that touches the other owner-only call
sites should treat this as settled rather than re-deriving the old constraint.

**Mode preservation is POSIX-only.** `atomic_write`'s `mode` routes through
`fchmod_safe`, a documented no-op on Windows, where access is carried by the DACL
instead. The two guarantees therefore do not collide -- they apply on different
platforms -- which is why the writer branches on `IS_POSIX` rather than passing
both to `atomic_write`, which refuses `restrict_to_owner=True` alongside a wider
explicit `mode`. The three mode/symlink tests in
`test_config_rmw_preserves_settings.py` are `skipif(not IS_POSIX)` for this reason;
its Windows counterpart asserts the DACL by reading the descriptor back, and the
data-loss and AST-guard tests are platform-independent and run everywhere.

**Symlinks are followed, not replaced.** `os.replace` renames over the link
itself, so a symlinked `config.json` would become a regular file and its target
would go stale — the `write_text` this replaced followed the link. The target is
resolved before the stat and the write, so symlinking the config into a dotfiles
repo keeps working.

**Atomicity is not serialization.** `write_config_atomically()` guarantees a
reader never sees a partial file; it does NOT serialize a read-modify-write
against another process. Two writers that interleave (the CLI and the gateway,
say) are still last-writer-wins per key, since each read its own snapshot before
mutating. In-process dashboard handlers additionally take `_get_config_lock()`,
which serializes them against each other but not against a separate process.

One deliberate exception: the interactive `kirocrew config set --local` path
overwrites a corrupt `config.local.json` rather than failing closed — the user
typed an explicit command and sees the result on stdout. Pinned by
`test_config_overlay.py::TestCliConfigSetLocal`.

### `config_dir() -> Path`
Returns `~/.kiro/crew/` (nested under kiro-cli's `~/.kiro/` base). Overridden by
`KIROCREW_HOME` env var (refuses system directories like `/`, `/usr`, `/System`,
`/etc`). It creates the selected home and, on the default path, refreshes the
recovery breadcrumb; it never reads or migrates a leftover `~/.kirocrew` tree.

### `config_path() -> Path`
Returns `~/.kiro/crew/config.json` (or `$KIROCREW_HOME/config.json` if overridden).

### Agent Bookkeeping Sidecar (`agent_model_state.json`)

A `publish` receipt on a destination entry records the owning member, original
private template and internal source/target digests plus the pinned Parent
identity. It is saved before binding publication and retained after completion
so a same-name retry can distinguish its own publish from an occupied name.
Finalization removes only `private_to` and `forked_from`; model bookkeeping and
the receipt survive. Receipts contain no transport bodies and never appear in
agent specs or API responses. See [crew-mode](crew-mode.md#owner-reviewed-capability-inheritance).

Explicit capability enrollment adds a `capabilities` object to the private
agent's existing sidecar entry, never to its harness JSON. It records schema
version 1, one pinned Parent descriptor, accepted Parent rows, explicit local
operations, the catalog URI snapshot and the saved materialization digest.
`pending` means publication has not been verified; `saved` verifies disk state
only. Neither means a provider loaded that version. Capability responses expose
runtime `status`, `saved_revision`, `sessions` and an optional `error_code`;
Parent errors live in `template.error_code`. There is no duplicate `warnings`
array or constant `runtime.apply_mode`; adoption still requires a new runtime.
Rows expose `managed` for transport ownership, not constant `editable` or
`locked_reason` metadata; managed-server Enabled remains supported.
Schema-v1 reads require
all capability section maps and validate accepted row values and persisted
`set`/`remove` overrides. Present null intent, missing sections and malformed
rows fail closed; they are never dropped or interpreted as legacy mode. The
owner API returns its bounded unavailable response without exposing source
bytes. The pinned Parent requires string name, scope, source, path and project
fields; an empty project remains valid. Optional catalog, revision, materialization
and ordinary-field bookkeeping remain optional, but present values are checked
before a consumer can use them. Publish receipts use the same Parent check;
explicit null receipts are corrupt, not absent. Known MCP transport fields are
checked before accepted or local rows
can be materialized, including string-list contents, string-valued environment
and header maps, boolean `disabled`, and positive finite `timeout`. The same
field validator runs on source transports and editor sets. Source metadata and
policy-only entries remain intact; native `oauth.oauthScopes` arrays are valid
in persisted source rows. The editor's narrower request allowlist and managed
or app transport ownership checks still apply separately. A corrupt persisted
transport refuses cold allocation before reconciliation can publish a new
spec or change a member binding. The owner API and resolver contract is
documented in [crew-mode](crew-mode.md#owner-reviewed-capability-inheritance).

Selected `accept_parent` rows must be genuine pending Parent changes in each
member's pre-operation snapshot. An explicit `inherit` in the same request may
advance that row to the current Parent (including removal) without invalidating
its selected acceptance. `inherit` also works without selected acceptance;
acceptance alone preserves local values and removal overrides. Missing or
unchanged selections still refuse with `parent_change_missing`, including when
paired with `inherit`. Batch acceptance validates every selected member before
publication; local operations apply only to the primary member, and unselected
rows and peers gain no new approvals. Stale revisions and all Parent identity,
managed transport, wildcard and ambient MCP exclusion checks remain enforced.

Capability GET, preview and PUT projections mask every non-empty environment
and header value and native `oauth.clientSecret`, regardless of length or
recognizable token prefix. Credential-bearing argument options are also masked,
including split values (`--api-key VALUE`) and inline values (`--token=VALUE`).
Complete `NAME=VALUE` argument elements also identify credentials when NAME is
an environment-variable identifier with a credential suffix, including assignments
passed after `-e` or `--env`. Values may be short and contain further `=` characters.
A bare name without `=` never consumes the following argument. The whole original
assignment is masked and retained byte for byte, without parsing a shell command.
Native OAuth edits and selected connections retain nested `oauthScopes` lists;
the shared transport validator still requires every scope to be a string.
The basic-auth forms `-u user:password`, `-uuser:password`, `--user user:password`
and `--user=user:password` are masked when the value contains a colon, including
an empty username or password.
No other short aliases or arbitrary positional credentials are inferred, and
option detection stops at `--`. A bare `-u` or a colon-free value stays visible;
another program's colon-bearing `-u` value may be conservatively masked.
Known credential copies in the same transport's strings, argument arrays and
nested metadata are masked too, without changing map/list shapes or rewriting
unrelated rows. Empty credential values remain empty. The original secret stays
on disk; a whole-transport edit retains it only through the existing signed
preview and revision-bound `retain_paths` pointer (for example `/oauth/clientSecret`
or `/args/1`) paired with `[REDACTED]`. An inline option is retained as its entire
original argument, byte for byte. A stale revision or a path to a
non-masked/non-scalar leaf still refuses without writing. Legacy reset and
publish routes also bound
strict capability and publish-receipt reads: unreadable or malformed state
returns `503 capabilities_unavailable` before any mutation. Absent intent and
absent receipts still fall through to legacy handling; owner checks remain
before these reads. Legacy PATCH and binding changes use the same bounded
`503 capabilities_unavailable` response for strict-read failures; an enrolled
legacy write remains a 409 conflict. PATCH checks both the actual file stem and
the declared name, regardless of which spelling resolved the request. It repeats
both checks using the fresh read under the existing spec lock before model
bookkeeping or spec writes, retaining the spec-then-sidecar lock order. A late
binding-check failure preserves the old binding. Legacy publish
may already have staged a private destination at that point and runs its existing
reference-aware rollback; this is rollback, not a claim that no writes occurred.

Each pending generation records only its own `materialized` digest. Failed spec
publication leaves the old generation's bytes intact; a failed final receipt can
be completed without minting another generation. Startup still refuses pending
state until reconciliation succeeds.

With ambient MCP loading enabled (`includeMcpJson` true or absent), resolved
removals of servers, tools or approval entries refuse with
`global_mcp_exclusion_unrepresentable` before publication. The check compares
the saved and final projected rows, so Parent reconciliation, selected acceptance,
per-item inheritance and whole reset cannot bypass the explicit-remove guard.
A final projection with `includeMcpJson: false` can represent these removals.
Under ambient loading an `@server` entry in `allowedTools` whose server the spec
does not declare is kept, because the global `mcp.json` can serve that server;
with `includeMcpJson: false` such an entry is dropped. An entry naming a server
the spec declares `disabled` is dropped in both cases.

Sidecar reads are capped at 8 MiB and require a single-link regular file.
Mutators refuse unreadable state instead of replacing it with an empty map.
The stable sidecar lock refuses non-regular/multiply-linked handles and uses
no-follow opening where supported. Writes use owner-restricted atomic replace.
Capability publication takes the config lock, spec lock and sidecar lock in
that order. Config loading and catalog preparation happen before that hold;
locked publication rechecks the relevant bindings, sources and intent.
Base and config.local locks are acquired in that order before the spec and
sidecar locks. A local member keeps its binding delta in config.local; a
multi-member batch spanning layers commits one overlay delta atomically.
Public capability versions are random identifiers tied to the saved internal
materialization digest, never the digest of secret-bearing source bytes.

Kiro Crew tracks two pieces of per-agent state that are **not** part of the
kiro-cli agent schema: `model_managed` (whether an agent's `model` tracks the
shipped default or is a frozen user pick) and `cc_model` (a per-agent Claude
Code model). kiro-cli validates `~/.kiro/agents/*.json` with serde
`deny_unknown_fields` and rejects the *entire* spec on any unknown key, then
silently falls back to the default agent (`--agent <name>` resolves to default
with only a stderr "no agent with name X found" line). To keep every spec
schema-valid, this state lives in a Kiro Crew-owned sidecar
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
  A **managed** spec's `model` is set on every refresh to the shipped default,
  or to the `"auto"` sentinel when the shipped template pins none — never left
  as-is. That is what makes the global `agent.model` reversible: the global is
  propagated into the spec when it is a concrete pick, and because a spec pin
  outranks the global in `resolve_effective_model`, returning the global to
  `"auto"` must take the pin back off or `"auto"` is unreachable from the
  configuration surface. Ownership decides who may clear: `model_managed=false`
  (an explicit user pick) and an **absent** sidecar entry (legacy status, owner
  unknown) both keep their pin untouched. Once a spec write publishes the
  concrete global over a legacy-status spec, `rebuild_agent_config` claims it
  (`model_managed=true`, set only when the entry holds no flag, under the
  sidecar lock), since the published value is the propagation's; a failed
  write claims nothing and an explicit pick is never claimed.
- `migrate_agent_specs()` runs at startup (top of `rebuild_agent_config`): lifts
  the keys out of every `~/.kiro/agents/*.json` into the sidecar and removes
  them (idempotent), fixing installs polluted by older builds.
- The dashboard model PATCH writes the sidecar, never the spec; agent DELETE
  prunes the sidecar entry.
- `agent_state.lift_and_strip_bookkeeping()` is the single shared
  implementation of the lift/strip/no-clobber rule above (with a type guard —
  a non-`bool` `model_managed` or non-`str` `cc_model` is stripped but never
  lifted, since coercing it could silently flip its meaning). Every spec writer
  calls it — among them the dashboard's whole-config `PUT /api/agent/config`
  handler, the per-agent `PATCH /api/agents/detail/{name}` handler,
  `migrate_agent_specs()` and `_refresh_dynamic_fields()`; the function's callers
  are the authoritative list — so none of them can drift from the others.

Note: Kiro Crew uses KiroACP (kiro-cli) only — the deleted `claude_code` provider
was the sole reader of spec `cc_model`, so `cc_model` is now dead config. The
lite/heartbeat installers still write it to the sidecar (harmless bookkeeping)
purely to keep the kiro spec schema-clean; nothing in the fork resolves it.

**Invariant:** `~/.kiro/agents/*.json` must contain only kiro-cli schema keys at
all times — after install, refresh, and any dashboard edit — or kiro-cli drops
the agent and silently falls back to default.

## Monitoring runtime policy

`monitoring.max_runtime_secs` is the finite wall-clock ceiling shared by monitor
MCP tools and API mutations; it is checked when a budget is written, never
against a persisted record on load. It defaults to 604800
seconds; an operator may set up to 2592000 (30 days). Invalid config
values fall back to the shipped ceiling, and `coerce_runtime_ceiling` logs a
warning naming the rejected value and the fallback whenever a configured value
is replaced (an unset key is the ordinary default and is silent). Validation
errors quote the ceiling with a duration gloss, `(7 days)` for the default.
`monitoring.limits` reads the live
snapshot (or the loader in standalone MCP processes). Raising or lowering the
ceiling does not change existing budgets, creation times, deadlines, active
state or the generic four-hour arming default. A PR-specific daily/30-day preference belongs in that
installation's maintenance instructions and explicit new requests.

## Live config: one watcher, one applier registry

`config/live.py` is the single mechanism by which a write to `config.json`
reaches the objects that already copied a value out of it. Before it, a
long-lived object built at boot (a session manager's idle timeout, a channel
transport's allow-list, a workflow ceiling) kept its boot copy forever unless the
particular writer happened to know that copy existed — so `kirocrew config set`,
a dashboard save and an `$EDITOR` edit each hot-applied a *different* subset of
fields, and every other field was silently inert until the next restart.

Three parts, each deliberately singular:

**One poll.** `ConfigWatch` runs one background task that compares
`loader._config_fingerprint()` (mtime_ns + size + mode of `config.json` and
`config.local.json`) every `DEFAULT_POLL_INTERVAL_SECS` (2s, floored at 0.05).
The same cycle also compares `loader.env_fingerprint()`, the credential twin of
the document fingerprint over `.env`, tracked separately on purpose: the document
fingerprint is the parsed-config cache key, so folding `.env` into it would bust
that cache on every unrelated credential edit. The `stat` calls run in
`asyncio.to_thread`, never on the loop. Nothing else in the gateway polls either
file.

**One kick.** An in-process writer does not wait for the tick: it calls
`live.notify_config_written()`, which sets a force flag and wakes the poll
through `call_soon_threadsafe`. The force flag matters independently of the
wake — the fingerprint is mtime-based, and a coarse filesystem clock can make a
write invisible to it, so a kicked cycle reloads whether or not the fingerprint
moved. Safe from any thread and a no-op in a process with no watcher (the CLI).

**One reload, one diff, one dispatch.** A cycle performs exactly one
`KiroCrewConfig.load()` off the loop, so the `publish_*` snapshots the loader
maintains ride along rather than needing a second reader. Old and new documents
are flattened to dotted leaf paths (`flatten_config`; lists and empty dicts are
leaves, because every list-typed field is a whole value whose consumers rebuild
from the full list) and diffed (`diff_config_docs`). The new config is adopted
*before* dispatch so an applier reading `live.snapshot()` sees it, and the
PRE-load fingerprint is recorded so a write landing mid-read leaves it unequal to
the file and the next tick reloads. Cycles are serialized by a lock, so the diff
is always old-vs-newer and an applier never sees an out-of-order pair.

### `.env` credential hot-reload

Before, `.env` was read once at gateway boot and nothing re-read it, so a
credential added or rotated in `$KIROCREW_HOME/.env` took effect only after a
full gateway restart — inconsistent with `config.json`, whose settings the
watcher already hot-applies ([#22450](https://github.com/kirodotdev/KiroCrew/issues/22450)).
The dashboard's own credential writer applied its `.env` changes live (direct
`os.environ` set/pop plus a channel restart off the config half of its
transaction), but a hand edit or any non-dashboard writer did not.

Now the same cycle that reloads `config.json` also reconciles `.env`. When the
`.env` fingerprint moves, the watcher re-reads the raw credential map
(`loader.read_env_credentials_raw()`, off the loop — literal line values, so a
`secret://NAME` reference stays the reference) and diffs it against the last one
(`loader.changed_env_keys`). The changed keys are pushed into `os.environ`
(`loader.apply_env_credential_changes`, which the watcher hands the previous
baseline so it can tell a value `.env` itself propagated from one the process
inherited): a key present in the new file is set, and a key gone from it is
popped ONLY when the current process value still equals what `.env` last set.
This uses `os.environ[k] = v` / `pop`, not boot's `setdefault` — boot lets an
inherited shell value win (the separate precedence concern of
[#22449](https://github.com/kirodotdev/KiroCrew/issues/22449)), but an operator
editing `.env` on a running gateway *means* the edit, so a live change
overrides. Only keys the operator actually changed are touched; the two boot
carve-outs are kept for every caller (the Docker `_KIROCREW_CREDS_SCRUBBED` skip
for credential keys, and `PATH` extension rather than replacement). Values are
never logged, only key names. A read that could not be decoded — UTF-16/UTF-32,
or a plain invalid byte under the active encoding (both raise
`UnicodeDecodeError`; `read_env_credentials_raw` catches both and marks the
read non-definitive rather than letting either propagate out of the poll
cycle) — is never diffed: an empty map from it would otherwise read as every
credential cleared. Its `.env` fingerprint is also **not advanced**, so a tick
that hit a one-off transient failure (an `OSError`, e.g. a Windows sharing
violation mid-rotation) is retried on the very next cycle instead of being
silently dropped the moment the file's mtime stops moving; a persistently bad
file just pays a harmless repeated re-read (the undecodable warning still
fires only once).

The apply itself is the other way the cycle can fail, and it is treated the
same way. `apply_env_credential_changes` is best-effort — a bad apply must not
kill the poll — so it is wrapped and a raise is logged, not propagated. But the
baseline and the `.env` fingerprint advance **only after the apply succeeds**:
on a raise, both are left exactly as they were and nothing is dispatched. If
either advanced on failure, a rotation that never reached `os.environ` would be
recorded as in force, so a later repair that reverts the offending edit would
diff only that edit and never re-apply the rotation — stranding the channel
after the fix. The failing case is a malformed line saved in the same write as
a rotation (an empty-name `=value` parses to the key `""`, which `os.environ`
rejects); left unadvanced, the next tick re-reads and retries the whole diff,
and because nothing dispatched, no channel restart runs against a credential the
apply did not install. That retry means the same bad file re-fails every tick,
so the failure WARNING (with its traceback) fires ONCE per distinct failing
`.env` fingerprint and the retries log at DEBUG — the same warn-once shape the
undecodable path uses — rather than spewing a traceback every poll interval
while a malformed line sits in the file; a successful apply clears the
remembered failure signature so a later failure warns afresh.

The apply is also **atomic across the batch**, not just at the cycle level.
`apply_env_credential_changes` validates every key it would WRITE (a changed key
with a non-empty new value, not one the Docker scrub skips) BEFORE mutating
anything — the key must be a legal environment name (`is_legal_env_name`: not
empty, no `=` or NUL) and the value must carry no embedded NUL, the two things
`os.environ[k] = v` rejects — and raises `ValueError` touching nothing if any is
illegal. Without that, a diff is applied key by key, so a
malformed key midway (the empty name again) would raise only after the keys
iterated before it — in hash order — were already written to `os.environ`. The
caller keeping its baseline does not undo those writes: a later restore of the
old file would then diff to nothing, leave the stray value in the process, and a
subsequent `restart_channel` → `load_credentials()` would prefer that process
value over the restored `.env` line and reconnect with the wrong credential. A
pop is never validated — removing a key cannot raise — but an illegal key in the
diff at all means the file is malformed, so the whole apply is refused and
retried once the operator fixes it.

The watcher's diff baseline must be what `.env` **actually propagated into
`os.environ` at boot**, not a disk read taken when the watcher starts. The
gateway loads credentials (`load_credentials`) well before the watcher's
post-bind `start()`, so a rotation made in that window would, if the baseline
were a fresh disk read at `start()`, be adopted as the baseline the first cycle
re-reads unchanged — no diff, so the new value is never applied and the channel
stays on the revoked credential until the next full restart. To close that, the
FIRST `load_credentials()` of the process records the raw `.env` map it read
(`loader.record_boot_raw_env`, before its own environment-wins override, so the
record equals what `read_env_credentials_raw` returns) and the watcher's
`start(initial=...)` seeds `_creds` from it (`loader.take_boot_raw_env`,
consume-once) rather than reading disk. A window rotation is then a real diff
the first cycle applies; a boot with no window edit sees the baseline equal the
current file and dispatches nothing, so channels do not bounce on every boot.
Only the first load wins — a later dashboard/doctor `load_credentials()` whose
disk read already reflects a window edit cannot overwrite the record and hide
the change. With no record (a test driving the watcher directly), `start()`
falls back to the off-loop `_prime_creds_baseline` read.

The first read on a watcher whose baseline was *properly primed*
(seeded from the boot record, or read `.env` definitively) establishes the same
map again and dispatches nothing, because the process already booted on those
values. But `_creds` is `None` on a watcher whose boot read was **not**
definitive (the file was undecodable or unreadable at boot, so `load_credentials`
also never propagated anything from it) — there, the first later definitive
read diffs against an *empty* baseline and applies/dispatches every key, since
none of them was actually in force yet. Adopting an unprimed first read as a
silent baseline would otherwise drop an `.env` repair on the floor: the
operator fixes a UTF-16 save to UTF-8, and the credential it now holds never
reaches `os.environ` because nothing diffs it in.

A live **removal does not unset an inherited value**, and ownership is decided
by a RECORD of what `.env` propagation actually wrote, not by value equality. A
set `_env_owned_keys` holds the keys `.env` genuinely put in `os.environ`: boot's
propagate loop adds a key only when its `setdefault` finds the name ABSENT and
writes it, and a live apply adds a key when it sets one. A key the process
already holds from its service/shell environment is never recorded, because
`setdefault` was a no-op for it — even when `.env` carried the identical value.
On removal, the key is popped only when `.env` owns it in that record (and the
record drops it); an unowned inherited value is left in place rather than
stranding the channel uncredentialed until the next restart. Value equality is
deliberately NOT the test: an inherited credential that merely equals the `.env`
line would otherwise be deleted when the `.env` line goes away, disabling a
channel whose service-supplied credential should remain. The ownership record is
home-derived boot state, so the integration second-boot harness resets it
between boots, the same as the boot `.env` baseline. Ownership and removal are
keyed by the canonical `os.environ` ENTRY — the name case-folded on Windows,
where `os.environ` is case-insensitive, and the name itself on POSIX. So a
case-only `.env` rename (`https_proxy` -> `HTTPS_PROXY`), which the diff sees as
one key added and one removed, does not let the removal pop the entry the
addition just wrote: a removed key is skipped when another key in the same batch
writes its entry, and ownership follows the entry rather than the exact case. Because an in-app restart
(`os.execv`) keeps `os.environ` but resets this process's Python state, the
record would be empty on the successor and every `.env` key would read as
inherited — so every change to the record hands the owned key names to
`platform_compat.keep_for_reexec` under the `_KIROCREW_ENV_OWNED_KEYS` marker,
the same channel the managed-service launch marker uses. The `reexec_*` seams
write it into the environment only right before the exec, and the successor's
first `load_credentials` reads it back and deletes it
(`adopt_env_owned_keys_from_reexec`), keeping removal correct across a restart. Post-boot credential reads are also
NON-propagating: EVERY gateway-process reader after the one boot seed — the
channel-start section adoption, the channel reconnect, the readiness badge, and
every dashboard settings panel (Slack, Telegram, Discord, WeCom, Teams, Webex,
Weixin, Feishu, Jira, the upload destination) — calls
`load_credentials(propagate=False)`, because a concurrent worker-thread read that
re-ran the `setdefault` propagate loop could restore a credential the live
watcher just popped and silently undo an external `.env` removal. After boot,
`os.environ` credential mutation belongs to the live applier alone. An
AST-based contract test
(`test_only_the_boot_seed_propagates_credentials_into_os_environ`) scans the
WHOLE source tree and fails if any `load_credentials` call omits
`propagate=False`, with a four-entry allowlist for the reads that legitimately
propagate: the gateway boot seed (`process_entry`), the orchestrator-init
fallback, and the standalone `kirocrew` CLI and `doctor` processes (separate
short-lived processes that race no watcher and seed their own children). The key list
is COMMA-joined, not
NUL-joined: `os.environ` rejects a NUL in a value (the same reject the batch
validation enforces), so a NUL separator would raise and silently lose the whole
record the moment two keys are owned; a comma is legal in an env value and never
appears in an env var name. A degraded channel section is handled too: a
`credentials.<KEY>` path for a channel whose section the loader discarded is
DEFERRED alongside that section's own paths (the credential path is in its own
namespace, so it must be added to the deferral explicitly), and the watcher
retries it once the section validates — rather than being dropped and leaving
the transport on the old token. The overwrite **warning fires only on a genuine
collision** — when the
current process value matches neither the new nor the last-propagated `.env`
value, i.e. a foreign/inherited value is being overwritten. A routine rotation
does not warn (boot propagated the old `.env` value, so the environment held
exactly what `.env` last set). In that collision case the warning notes that the
live value will not survive a restart, because boot's `setdefault` rebuilds the
inherited value — the one remaining live-vs-restart divergence, the
[#22449](https://github.com/kirodotdev/KiroCrew/issues/22449) precedence
interaction this change does not otherwise alter.
`test_env_credential_hot_reload.py` pins each of these.

The dashboard's channel-credential writer applies its own `.env` changes to
`os.environ` the same way, through `apply_env_credential_changes` (with no
baseline, so a cleared key is popped unconditionally — the dashboard owns no
diff baseline, the watcher does), which is how it inherits the
`_KIROCREW_CREDS_SCRUBBED` skip rather than re-exposing a scrubbed credential in
`/proc/<pid>/environ` through a hand-rolled set/pop loop.

The credential change rides the SAME dispatch as a synthetic `credentials.<KEY>`
path — a namespace no config section owns, so it cannot collide with a real
leaf. The channel-restart applier (`_on_channel_config_change`) maps it onto the
channels whose descriptor names that key in its `credentials` tuple OR its
`restart_credentials` tuple (`registry.changed_credential_keys`, the credential
twin of `changed_boot_keys`) and restarts each one through `restart_channel`,
which re-reads `cfg.load_credentials()` on the way back up — so a rotated
Telegram/Discord/WeCom/… token reconnects that channel with the new value,
exactly as a changed `config.json` connection parameter does. The two tuples are
disjoint by intent: `credentials` names what the channel needs ALL of to connect
(so `channel_readiness` reports a missing one), while `restart_credentials` names
a key that is restart-relevant but not required — Teams' `MICROSOFT_APP_TENANT_ID`
is the case, since a live `TeamsClient` authenticates against a fixed tenant and
cannot adopt a corrected one, yet an unset tenant is the supported multi-tenant
mode and must not read as a missing credential. Folding the tenant into
`credentials` would restart on a tenant edit but wrongly mark Teams un-ready when
no tenant is set; leaving it out of both would leave a tenant-only edit undetected
and the client stuck on the wrong tenant. Slack is the one exception to the
restart: its socket client is host-managed (`_connect_slack`) with no in-process
restart path, so a Slack token changed in `.env` logs a restart-needed notice to
the operator (the same honest fallback as `slack.command`) rather than being
silently ignored.

`changed_credential_keys` matches by the canonical environment-entry name, the
same case-fold the apply side uses: on Windows a `.env` key whose case differs
from the declared credential constant addresses the same token, so matching the
literal case would miss a rotation and restart nothing; the match canonicalizes
both sides and returns the declared constant the restart path expects. On POSIX
the canonical form is the name itself, so matching is unchanged.

Closing a channel clears what outlives its client. Teams is the case: its
inbound webhook ROUTE is pre-registered once and survives every client close,
dispatching to the late-bound `state.teams_on_activity`. So `_close_channel_locked`
clears that handler when it closes Teams — otherwise a credential removal that
closed the client would leave the route running turns for authenticated,
allow-listed senders; the route handler already answers 503 when the handler is
unset. As defence in depth the `TeamsClient` itself answers 503 from
`on_activity` once closed — both at entry AND rechecked after the
request-processing awaits (JWT verify, body read), immediately before the turn
task is created and registered, since a request already past the entry guard is
not yet in the cancel sweep's `_handler_tasks` and would otherwise start a turn
on a just-closed channel.

### The applier registry

`live.subscribe(*prefixes, callback=..., name=...)` is the only sanctioned place
for work a gateway does in response to a config write. Rules the registry keeps:

- **Prefix-scoped.** An applier fires only when a changed path is one of its
  prefixes or lies under one (whole dotted segments: `agents` is not under
  `agent`). No prefixes means every reload — a catch-all, and a review smell.
- **Registration order is dispatch order**, so an applier may depend on one
  registered before it (a rebuild before the consumer that reads it).
- **Sync or async**, awaited if awaitable.
- **A raising applier cannot starve the rest, and is not left stale.** Each call
  is guarded; the failure is logged at ERROR with the applier's NAME and dispatch
  continues. The watcher remembers the changed paths that applier missed and
  re-dispatches exactly those to it on the next tick (a synthesized change whose
  `old` and `new` are the current snapshot), quietly at DEBUG until it succeeds;
  a real change arriving first carries the missed paths in the same dispatch. So
  a transient failure delays adoption by one poll interval instead of until the
  same fields happen to change again.
- **An awaitable applier that hangs cannot stall the rest indefinitely either.**
  Appliers dispatch sequentially under one cycle lock, so a call that never
  returns would otherwise block every applier after it and any concurrent
  `refresh_now()` caller. `_apply_one` wraps an awaited result in
  `asyncio.wait_for(..., timeout=APPLIER_TIMEOUT_SECS)` (10s); a timeout is
  logged and staled exactly like a raised exception. This bounds an async
  applier that hangs on an internal `await` — it cannot bound a synchronous
  applier that blocks the loop outright, since nothing suspends until the
  result is awaitable in the first place. Write appliers non-blocking.
- **A bound method is held weakly** (`weakref.WeakMethod`), so a manager
  discarded by a test or a provider reload falls out of the registry on its own.
  A free function or lambda is held strongly — it has no owner to outlive.
- **Values are never logged, only changed paths**: `to_dict()` carries channel
  tokens and the diff sees them.
- **`cancel()` is the removal verb** (there is no `close()`), and is idempotent.

### The three one-line registrations

`subscribe` is the primitive; almost no applier should be written against it
directly. Three shapes on top of it cover every value-adoption site and are what
a new setting registers with, in the owning object's constructor:

| Shape | When | What it does |
|---|---|---|
| `live.watch_section(owner, "wecom", "messaging", target="transport")` | a subsystem owns one top-level section and exposes `reconfigure(section_cfg)` | fires under `wecom` (or `messaging`); resolves `owner.transport` at dispatch time and calls its `reconfigure(change.new.wecom)`; a `None` target (transport not connected yet) is a no-op; **fails closed** on a section the loader marked degraded (`fail_closed=True` by default and mandatory for anything carrying authorization), so an allow-list is never rebuilt from an unparseable document |
| `live.watch_object(owner, "memory", "skills.max_skills")` | an object's settings span sections or are normalized together, and it exposes `reconfigure(cfg)` | fires under any prefix and hands the whole `KiroCrewConfig` over |
| `live.bind("agent.max_channels", mgr.set_max_channels)` | one scalar with an existing setter | calls `setter(value at that path)` when that leaf changes |

All three hold the owner weakly (through the setter's `__self__` for `bind`),
so a discarded object drops out of the registry like a weakly held bound
method. What they remove is the per-site ceremony that used to be copied by
hand — the prefix gate, the `None`-transport guard and, above all, the
degraded-section refusal, which is an authorization safety check and must not
exist as nine hand-written copies. What stays on `subscribe` is orchestration
rather than value adoption: the in-process channel restart, the provider
switch, the SEL-audited approval widening, the Slack section's fan-out.

`ConfigWatch.replay(sub)`, called on a subscription `watch_object` just returned,
closes the registration gap for an owner that applied a config it loaded itself.
A reload adopts its config and only then
snapshots the registry, so one that snapshotted before the owner registered never
reaches it. Run right after registration: when the
watcher's fingerprint still matches the file, it hands the adopted snapshot to the
applier, re-reading it after each apply so a reload landing mid-replay is not undone;
when the file has moved past the snapshot (or nothing is fingerprinted yet), the
snapshot may be older than the owner's own load, so it marks the subscription stale
for its prefixes and the next tick delivers the new document even where it leaves
them unchanged. A deferred, failed or async applier is marked stale the same way.
Unstarted watchers (nothing adopted) make it a no-op. `VectorMemoryStore` is the one
caller, and replays only when it was built with `config=` (a raised
`memory.episodic_max_count` adopted during construction is not lost); a `config=None`
store keeps its constructor defaults at construction. The other `watch_object`
owners that copy caller-loaded config before registering (`cron_history.py`,
`subagent_parts/manager.py`, `history_consolidation.py`, `adaptive/controller.py`,
`slack/gateway_runtime/admission.py`) keep the registration gap; they are out of scope for #10889.

### The point-of-use read

`live.current(fallback, log_prefix=...)` is for a call site that reads a value
mid-turn rather than reacting to a change — every channel dispatcher's per-turn
threshold and DM-scope reads. It returns the watcher's `snapshot()` when armed,
else a disk `KiroCrewConfig.load()`, else *fallback* (with a warning naming
*log_prefix*) when even that raises. `snapshot()` alone is not enough for these
callers: a dispatcher can run before the watcher is primed (see boot ordering
below) or in a process with none at all, and `current` is the one place that
disk-load-then-fallback chain is written, rather than nine copies of it.

A load that raises keeps the previous snapshot, records `last_error`, dispatches
nothing, and retries on the next tick. A file that does not PARSE as a JSON
object right now (`_document_is_torn`, checked on the file itself because the
loader's degradation flag is sticky for the process) also keeps the previous
snapshot's VALUES — only the flag is taken from the new load, so the gates
that fail closed on `degraded_sections` still see it — and diffs empty, so no
applier and no `current()` reader ever sees the all-defaults document the
loader answers for an unparseable file (a forum activation of `always`, an
empty allow-list, for the length of the tear). Once the file parses again the
load is adopted as it is, still flagged, and a repair to new values diffs and
dispatches them. A load that *succeeds degraded* with the file parseable (a
section the loader itself discarded, named in `degraded_sections`) does
dispatch: the watcher does not refuse on the applier's behalf, because whether
a default is safe is a property of the consumer. A fail-closed applier — every
channel allow-list — reads `change.new.degraded_sections` and keeps its previous
authorization state; a plain one (a timeout, a log level) adopts the default,
which is the correct answer for it.

A fail-closed refusal is **deferred, never dropped**: the applier raises
`ConfigDeferred(paths)` and the watcher records those paths in the same stale
table an applier exception lands in (logged once at WARNING, then at DEBUG while
the applier keeps deferring). The same exception carries a second meaning for one
applier: the channel-restart applier raises it before the transports have
started, so a boot-window edit waits in the stale table and is applied by the
first tick after they are up — through `_apply_one`, with the degraded check,
rather than by a replay of the boot loop's own. The distinction matters because the watcher adopts the
degraded document as its snapshot — the refused section at DEFAULTS — so a
repair that writes back exactly those defaults diffs EMPTY. Before this a skip
was a silent success and nothing would ever re-run the applier: a revocation
written alongside a malformed field stayed unapplied for good. Now every clean
load re-runs the stale appliers against the current document even when it has
nothing to diff (`_retry_stale` on the empty-diff and unchanged-fingerprint
paths), so the roster catches up with the repair. `_section_applier`,
`_object_applier`, the `bind()` leaf applier (a discarded section holds a leaf's
DEFAULT, and a bound setter handed that default would reset a cap or a ceiling),
`_on_slack_config_change`, `_on_default_agent_config_change`,
`_on_channel_config_change` and the session manager's
applier all raise it; the channel applier raises only the DEGRADED channels'
paths after scheduling the healthy channels' restarts, so the retry never
restarts a channel twice.

The refusal keys on the applier's OWN section being in `degraded_sections`, never
on the whole-config marker `*` alone. The loader keeps every degradation it has
observed for the life of the process (`resolution._OBSERVED_DEGRADED_SECTIONS`:
its gates must stay closed after a save has normalized the evidence away), so a
document loaded after a since-repaired tear still carries `*`; an applier that
refused on it would defer every tick until a restart while each save reported
applied — a revocation written after one transient typo would never land. It
does not need to: a document that is torn NOW never reaches an applier, because
the watcher keeps the previous snapshot while the file does not parse (above),
and the one way the two could disagree — a write landing between the loader's
read and the torn-file probe — is closed in `_cycle`: when the probe finds the
file whole but the load carries `*`, the fingerprint is re-read, and a read the
file moved under is treated as torn (previous snapshot kept, next tick reloads).
So on a dispatched change `*` is only the loader's memory of a repaired tear,
and the section flags — which the loader assigns only to the fail-closed
sections `dashboard` (including `dashboard.tailscale`), `memory`, `publish` and
`workspaces` (`validation._FAIL_CLOSED_PATHS`; validation removes a malformed
non-object anywhere else before the loader could flag it) — are the ones a tear
never produces and a save destroys the evidence of, hence the ones that stay
sticky and keep their appliers deferred until the restart main documents for a
malformed fail-closed section.

Where appliers live: an applier owned by a long-lived object registers in that
object's constructor (session manager, subagent manager, each channel
dispatcher; `WorkflowService` binds `agent.workflow_run_timeout_secs` to its
`set_timeout_secs` and `ChannelManager` binds `agent.max_channels` /
`agent.max_channel_agents` to its cap setters, both with `live.bind`). Only the
ones whose holder is `DashboardState`, that must rebuild agent artifacts, or that set
process-wide registry state
live in `dashboard/server_runtime/config_watch.py::_register_config_watch` — `agent.provider`,
`agent.model`, `agent.role_models.background`, `agent.log_level`
(→ `handlers/updates_streams.py::apply_log_level_from_config`),
`dashboard.dynamic_dashboard_cards` (→ `DashboardState.set_dynamic_cards_enabled`), and
`agent.model_context_tokens` (→ `model_registry.apply_operator_windows`, process state of
the model registry rather than of any holder, so it is also installed once from the boot
document: the first tick reports only what changed since boot). The log-level applier
shares `apply_log_level` with the Logs page's `POST /api/logs/level`, and that
one function moves the `kiro_crew` logger only — which is the ONLY level gate
on the way to `gateway.log`: the file handler and the queue handler
`cli._setup_cli_logging` installs carry no level of their own (kiro_crew
records are gated at the kiro_crew logger, third-party records on the detached
gateway's root-attached handler at the root logger's WARNING), so the runtime
change reaches the file with nothing else to update. The handlers used to hold
a boot-time copy of the level that nothing updated, so a gateway booted at
WARNING dropped its raised INFO records before the file until a restart while
the live Logs stream showed them (#14231); a level re-added to either handler
is that bug again, and `test_cli_logging.py` pins the contract.
Both model appliers rebuild the installed agent specifications before the
watcher finishes dispatching the change. After a successful `agent.model`
rebuild, its applier emits a refresh frame, so dashboard PATCH responses and
that frame expose the new effective model only after the corresponding
specification is ready, including when the value is cleared back to `auto`.
If the rebuild fails, the applier logs the failure and emits no refresh frame;
the previous specification and effective-model readout remain in force. It also
raises an actionable dashboard notification and remains stale in the watcher,
which retries the rebuild on later ticks until the generated spec catches up.
The first successful retry emits both the refresh frame and a recovery
notification, so the operator is not left with a stale failure message after
the saved model becomes active. A rebuild refused because this instance must not
own the shared agent home (`wrote=False`) takes the same failure path, notify and
defer, rather than reading as applied; a successful rebuild also re-runs
`SessionManager.refresh_defaults` so warm-pool providers minted from the old spec
are reconciled (`dashboard/server_runtime/config_watch.py` `_apply_default_model`).
The `wrote=False` refusal is one case of a general ownership guard: every write through
the shared-spec primitive (`agent_parts/spec_files.py` `_atomic_json_write`) is refused with
`SharedAgentHomeRefused` when the data home does not own the shared agents directory. The
derived worker, conductor and service-agent specs and the read-only side spec all take
that refusal, so the worker's stale mirror raises `DerivedSpecStale` rather than
re-deriving, and the dashboard spec editor and the MCP toggle, remove, sync and apply
handlers answer HTTP 409 `agent_home_not_owned` (listed in learn-cron-dashboard.md)
instead of recording a change the file never took. Every admitted or refused shared-home
write is audited as the SEL `agent_home_write` operation, allowed or denied.
The provider applier only schedules the switch: `reload_provider_factory` clears
the session registry and then shuts the retired providers down one at a time,
which can outlast the applier bound, and a timed-out applier is retried on the
next tick — a retried switch would clear the sessions created with the new
provider in between. So the switch runs off the cycle as a task tracked in
`state._background_tasks` (the same shape as a channel reconnect), the applier
returns at once, and the switch runs exactly once per change. Because it runs
later, it installs the watcher's snapshot current at install time rather than
the document it was scheduled with -- a change that landed in between (a
`refresh_defaults` install) must not be reverted -- and falls back to the
scheduled document only when that snapshot is torn, since a torn `agent`
section holds defaults.
That function must be called before `runner.setup()` freezes the signal lists;
the watcher itself starts in `on_startup`, because it needs the running loop, and
stops in `on_cleanup`.

### `restart=True` is the single source of restart truth

`ConfigEntry.requires_restart` in `config/schema.py` is the ONE statement of
which fields a running gateway cannot adopt. It is emitted into the schema API as
`requiresRestart` only when true (absent means hot), and `requires_restart(path)`
resolves it through ancestors, so a marked container covers keys the schema never
enumerates (`mcp_gateway.stub_servers.<name>`, `agent.jail.<key>`).

Consequences, and they are the point:

- A request handler keeps **no list of boot-only keys**. The dashboard's PUT
  computes `restart_required` as
  `_changed_paths_need_restart(changed)` over the schema, and the old
  `_STARTUP_READ_AGENT_KEYS` ladder is gone.
- The hint is computed over paths whose value actually **moved**. The dashboard
  sends every setting on each save, so "was applied" is not "was changed" — an
  untouched `restart=True` field must not make a live edit claim a restart.
- Adding a hot-appliable field is a schema change plus an applier, never a
  handler edit. Marking a field `restart=True` is the admission that no applier
  exists for it.
- That admission is enforced, not conventional: `ConfigWatch.subscribe`,
  `watch_section`, `watch_object` and `bind` refuse (`ValueError`) a prefix at
  or under a `restart=True` path at registration time, so an applier for a
  boot-only field cannot be added without first dropping the mark — and the
  owner's own constructor tests trip it. A section-wide registration
  (`"whatsapp"`) stays allowed: its applier adopts the section's live fields
  and ignores the marked leaf. `agent.approval_mode` is marked for this reason:
  every channel dispatcher resolves it once at start, so no single consumer may
  take it live.
- **Review checklist for a new field.** An unmarked field is a UI-visible
  promise ("this applied live"), and nothing mechanical ties that promise to
  code. So a PR that adds a config field must name, in its description, one of:
  the applier that adopts it — normally one line, `live.watch_section` /
  `live.watch_object` / `live.bind` in the owner's constructor, or a new field
  read inside an existing `reconfigure` — the point-of-use read
  (`live.snapshot()` at the call site) that makes construction-time capture
  irrelevant, or the `restart=True` mark. A field with none of the three is the
  silently-inert bug this design exists to kill, now with the settings UI
  affirming that the value took effect.
- **A reload that can widen approvals is audited.** `HookManager` follows
  `hooks.*` live, and `config.json` — sealed read-only against an in-sandbox agent
  shell — is still written by every settings surface outside the seal (the config
  PATCH, the operator CLI, an unsandboxed spawn), so its applier SEL-logs an
  `auto_approve_tools` / `auto_approve_sources` /
  `auto_approve_subagent_*` change (`hook_manager.reconfigure`,
  `auto_approve_changed`, counts and flag names only) the way the channel
  transports audit an allow-list reload. Governance still caps the resulting
  set; the audit closes the gap between "widened at a restart" and "widened in
  two seconds, mid-session, with no trace".

The schema is the source of truth: a key needs a restart exactly when
`requires_restart()` over `SCHEMA_REGISTRY` says so. Regenerated from that call:
`agent.approval_mode`, `agent.jail`, `agent.dangerously_skip_permissions`,
`agent.task_dispatch_window`, `agent.task_store_journal_mode`,
`agent.admit_wait_secs`, `agent.start_collect_timeout_secs`,
`agent.recovery_backoff_base_secs`, `agent.recovery_backoff_max_secs`,
`agent.session_start_concurrency`, `messaging.dm_scope` (it names the session-key
namespace whose per-conversation generation counters are seeded at boot, so a live
flip could resume a stale session persisted under the other namespace),
`memory.embedding_dim`, `memory.embed_model_path`, `memory.embed_model_id`,
`knowledge.extraction_effort`, most of `mcp_gateway.*` (every field except
`enabled`, `apps_enabled` and `resolve_once_refresh_hours`),
`mcp.extra_path_dirs`, `mcp.honour_auto_approve`, `hooks.confirm_tools` (the gate
follows it live, but the static grants it withholds are written when the agent
spec is rebuilt), `instances.enabled`,
`instances.tunnel_base_port`, `slack.command`, `whatsapp.db_path`, `dashboard.url`,
`dashboard.tailscale.*` except `keep_awake`, `dashboard.restore_sessions`,
`dashboard.restore_window_minutes`, `dashboard.idle_slot_sweep_days`,
`dashboard.surface_channel_sessions`,
`dashboard.loop_stall_exit_after_secs` (read once at boot to build the loop-stall
watchdog), `dashboard.cautious_boot`, `dashboard.auto_open_browser` and `tunnel.*`.
When the two disagree, the schema wins.

The broker's admission keys are in that `mcp_gateway.*` set and ride the
daemon's argv from `GatewayManager._spawn_once`: `spawn_concurrency_initial`
(4), `spawn_concurrency_min` (1) and `spawn_concurrency_max` (8, raised on the
daemon's argv to the subagent ceiling when that is higher --
`mcp_gateway.admission.derive_spawn_gate_ceiling`) size the
daemon-global spawn gate (a fixed count of backend spawn+initialize windows in
flight, FIFO past it; the band is what the adaptive controller later moves the
live value within); `spawn_queue_wait_secs` (600) is the CEILING on how long a
queue-aware stub is held before a `capacity` refusal and matches the DEFAULT of
the stub's own reconnect budget (`stub.py` `_RECONNECT_TOTAL_BUDGET_SECS`, a
constant the stub reads no config for, pinned equal by
`test_stub_reconnect_budget.py`); the wait the daemon actually arms is
`min(asked, key)` less `daemon/admission_protocol.py::_QUEUE_REFUSAL_MARGIN_SECS`
([`mcp.md`](../../architecture/mcp.md#admission-before-allocation)), so the
daemon gives up strictly first and raising the key above 600 s buys a queued stub
no extra wait — what the stub asked for caps it before the margin comes off, and
that constant is what has to rise instead. Because the key's help text is the
only statement of this an operator reads, `test_config_baseline.py` pins the
sentence against that arithmetic rather than leaving it to review;
`initialize_timeout_secs` (10) bounds a fresh backend's first
`initialize` and is threaded onto each `Backend` as a constructor field (on Windows the
effective deadline is floored to 30 s by `_WINDOWS_INITIALIZE_TIMEOUT_FLOOR_SECS` through
`_effective_initialize_timeout_secs`, since a Windows cold start routinely overruns the
POSIX-measured 10 s default and would otherwise be reaped and respawned; a configured
value already above the floor and every POSIX host are unchanged);
`host_budget_max_procs` / `host_budget_max_rss_mb` / `host_budget_max_fds` (all
0) cap what the host budget admits across pooled, private and fallback
backends, where `0` means derive from the available-memory sample the gateway
takes at spawn and the daemon's descriptor soft limit (processes never below
`max_backends`; memory left unbounded). The loader clamps them (floor ≥ 1,
ceiling ≥ floor, budgets ≥ 0). Semantics and the wire protocol they govern:
[`docs/architecture/mcp.md`](../../architecture/mcp.md#admission-before-allocation).

### Which write paths kick the watcher

Every door onto `config.json` ends at `notify_config_written()`, so the dashboard,
the CLI and an `$EDITOR` save all reach the hot-apply path identically. A writer
that forgets the kick is one setting that stays silently inert until the poll
happens to notice — which is the bug class this closes.

| Write path | Where |
|---|---|
| `update_config_locked` | `config/loader_document.py` — the required path for new mutations; skips the kick when the mutate returns `None` (no write); an optional `after_write` callback runs inside the lock only after the rename committed, and its exceptions propagate |
| `KiroCrewConfig.save()` | `config/loader_config.py` |
| `_persist_config_migration` | `config/loader_writeback.py` — a boot migration is a config write like any other |
| `refresh_config_meta_stamp` | `config/loader_document.py` — kicks only when the stamp actually moved (no rewrite, no mtime churn) |
| `_atomic_json_write` | `agent_parts/spec_files.py` — via `_notify_if_config_write`, and ONLY when the target resolves to `config_path()`. A safety net rather than a route: no handler writes `config.json` through it — the per-channel savers (`messaging._LockedSectionWrite`), the STT PUT and the MCP gateway-enable toggle all go through `update_config_locked`, and `TestTheAtomicJsonWriteConfigFamilyIsRatcheted` pins that population to an empty baseline so a new saver cannot reopen it |

A handler that must answer only after the new value is in force calls
`ConfigWatch.refresh_now()` (`handlers/core_config_view.py::_hot_apply_after_write`), which
forces one cycle and is a no-op before the watcher is started. The config PUT
and every per-channel saver (the `*_settings.py` owners in `dashboard/messaging_api/`,
the WhatsApp saver in `handlers/whatsapp_setup.py`) do, because their writes carry authorization: a
narrowed allow-list is applied to the running transport before the caller sees
"saved", never one poll interval after it. For a connection field the same
dispatch also drops the old client's mirror registration synchronously and
schedules its reconnect, so a request that follows the response (a WhatsApp QR
start, a mirror send) is never handed the client about to be closed.

**A two-file save runs under `live.hold()`.** Every channel saver that writes
`config.json` and then `.env` (Slack, Teams, Webex, WeCom, Feishu, Discord,
Telegram) rolls the config back when the credential write fails, through the one
shared `_write_env_or_roll_back` in `messaging_api/channel_config_writes.py`, and wraps the whole
transaction — snapshot through rollback and the `os.environ` sync — in
`with live.hold():`. Without it the
config write's own kick (`_atomic_json_write` → `notify_config_written`) wakes
the watcher while the handler is still awaiting the `.env` write, and a widened
allow-list the committed state never granted is applied to the running transport
for the length of the failing write. Under a hold the cycle records that a
reload is owed and returns without loading; the release wakes it on the
committed (or restored) file. Pinned for WeCom by `test_wecom_config_handlers.py`
(`test_the_config_and_env_writes_run_under_the_live_config_hold`), for Discord,
Telegram and the shared rollback step by `test_channel_env_write_off_loop.py`, and
for the watcher itself by `test_config_live.py`.

Tests: `test/test_config_live.py` (diff, registry, lifecycle, fingerprint,
dispatch order and scope, every write path, the schema/handler agreement), its
parts `test/test_config_live_server_appliers.py` (the
`dashboard/server_runtime/config_watch.py` appliers),
`test/test_config_live_subscriptions.py` (cancellation, the owned-applier shapes
`watch_section` / `watch_object` / `bind`, and replay on registration),
`test/test_config_live_restart_hint.py` (the `restart_required` hint, also over
HTTP) and `test/test_config_live_untrusted_documents.py` (the point-of-use
read's fallbacks and the bounded applier dispatch), and
`test/test_channels_a_hot_reload.py` (every
channel's applier, its fail-closed degrade refusal and its point-of-use reads,
parametrized over the case table in `test/_hot_reload_helpers.py`;
`test_channels_b_hot_reload.py` and `test_channels_c_hot_reload.py` carry the
per-channel claims that are not shared).

## Schema

Selected fields only, not the full schema. The authoritative per-field catalogue is
the owner modules (the `config/sections_*.py` owners, `service_sections.py`,
`memory_sections.py`, `integration_sections.py`) and `GET /api/config/schema`.

```python
@dataclass
class AgentConfig:
    approval_mode: str = "auto"    # "auto" or "interactive"
    streaming: bool = True
    model: str = "auto"            # resolved from agent config
    model_context_tokens: dict[str, int] = {}  # operator-declared served context window per EXACT agent.model id, e.g. {"server-a/org/model-7b": 32768}; value an int in [1024, 10000000], first 64 entries read, a warning per dropped entry (coerce_model_context_tokens); live via model_registry.apply_operator_windows
    provider: str = "acp"          # fixed to "acp" (kiro-cli) — the only provider
    bot_name: str = ""             # the name the agent identifies as in conversations (the {bot_name} placeholder in prompts); "" = the provider's default (Kiro). Loaded through _sanitize_bot_name (sections_agent_fields.py): NFC-normalized, capped at 50 code points (_BOT_NAME_MAX), then filtered to Unicode letters (Lu/Ll/Lt/Lm/Lo), the combining marks that attach to them (Mn/Mc), decimal digits (Nd) and letter numbers (Nl, the ideographic zero) in any script, plus the ASCII space, "_", "-" and "." -- the character policy of the artifact tag rule (artifacts.md § Validation & Limits), applied as a filter that drops instead of a validator that refuses. Everything else is dropped: braces (the placeholder must not recurse), other punctuation, symbols and emoji, enclosing marks (the keycap), other numbers (², ½), controls, format characters (zero-width, bidi), the letters and marks that render as nothing (variation selectors, Hangul fillers: terminal_safe._is_invisible, the one shared table), surrogates and private-use. A non-ASCII space (NO-BREAK SPACE, IDEOGRAPHIC SPACE) becomes the plain space; a space run a removal leaves behind folds to one and a leading or trailing one is stripped; a double space the user typed is kept; the result is NFC-normalized again, since a removal can leave a composable pair adjacent. Three tag rules are not applied: compatibility forms (full-width ｋｉｒｏ) are kept as typed because a drop cannot name the plain spelling; a name may open with a separator; and a run of combining marks is not bounded, since the bound guards a chip's width and nothing draws this name. A name that filters to "" takes the default. Nothing is logged when a name changes, and the CLI `config set` echo shows the raw value (#18728 follow-ups). dashboard.bot_name is a separate field and is not filtered.
    sandbox: str = "auto"          # "auto" (default: standard tier -- namespace on Linux, seatbelt on macOS; leaves ~/.aws, ~/.ssh, ~/.kube visible for credential tooling; delegates to kiro-cli's internal sandbox on macOS when enabled) | "strict" (opt-in: also hides ~/.aws incl. sso/cache, ~/.ssh bar known_hosts, ~/.kube, ~/.config/gh and the _CC_FILES credential files; applies to sessions started after the change) | "off" (skips Kiro Crew's sandbox). Any other value falls back to "auto"
    sandbox_allow_no_isolation: bool = False  # SEC-009: acknowledge running un-isolated when no sandbox backend exists; false = loud SECURITY warning, true = info-level
    soft_stop_budget_secs: float = 10.0  # seconds to wait for cooperative cancel before hard kill [0.5, 60.0]
    dangerously_skip_permissions: bool = False  # persistent all-tool approval; restart required
    yolo_duration: str = "6h"      # duration for ad-hoc auto-approval; 30m|1h|6h|12h|24h|until_shutdown
    max_subagents: int = 0         # 0 = auto: the subagent_auto_max ceiling (3 when host memory cannot be read); memory bounds starts beneath it (spawn_min_memory_gb). With spawn_min_memory_gb <= 0 the floor is off and auto is sized from memory (compute_memory_sized_parallel_cap). Fixed pins load in [3, 64]
    subagent_auto_max: int = 32    # the count ceiling when max_subagents=0 (provider concurrency / fd / PID stand-in; not sized from memory while the spawn floor is on, and capping the memory-sized figure when spawn_min_memory_gb <= 0 disables it); also caps the TaskRunner's memory-sized auto value. Load-time clamped to [3, 64]
    subagent_max_turns: int = 1000  # default per-subagent tool-call budget. Load-time clamped to [1, 1000]
    subagent_timeout_secs: int = 10800  # per-run wall-clock deadline, captured when the run starts (a reload applies to the next run); 0 uses the default; load-time clamped to 60..86400
    subagent_result_ttl_secs: int = 3600  # seconds a delivered subagent's result.txt is retained before the reaper prunes it
    chat_turn_timeout_secs: int = 14400  # wall-clock ceiling for one chat turn. Load-time clamped to [300, 86400]; the ACP prompt wait follows it (resolve_prompt_timeout)
    tool_approval_timeout_secs: int = 600  # how long a chat turn waits for a human to answer a tool-approval prompt. Load-time clamped to [30, 7200] AND to 60s below chat_turn_timeout_secs
    apps_ui_stream_timeout_secs: int = 30  # total transfer deadline for one response body on the unauthenticated /apps/<app>/ui/ route. Load-time clamped to [5, 600]; read per request (live), and no off switch — the route has eight descriptor permits and this deadline is what stops a client that quits reading from holding one indefinitely
    task_queue_enabled: bool = True   # persist every accepted subagent spawn to $KIROCREW_HOME/tasks/tasks.db before its id is returned; memory pressure defers instead of refusing, and with false (or no store) a floor miss also queues, in memory. false = the in-memory spawn queue, for one release (tasks.db left in place, unread). See modules/taskq.md
    task_dispatch_window: int = 64    # max queued spawns held in memory; the rest are rows read FIFO as the window drains. Load-time clamped to [1, 4096]; restart=True
    task_store_journal_mode: str = "auto"  # tasks.db SQLite journal: "auto" = WAL locally, DELETE when $KIROCREW_HOME is on a network filesystem; "wal" | "delete" force one (RFC overload-resilience §13 Q6 reversal). Unknown -> "auto"; restart=True
    admit_wait_secs: int = 30         # admitted -> queued after this, and the longest a memory-deferred spawn waits before its re-check (a memory event re-checks it sooner). Load-time clamped to [1, 3600]; restart=True
    subagent_queue_max_wait_secs: int = 1800  # DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS; longest a spawn deferred by spawn_min_memory_gb stays parked (spawns are not gated on the memory posture tier) (time spent eligible, queued for a slot, is not counted) before it ends as 'never started: waiting for memory' (delivered, depth 0). Also the per-start and per-episode bound of the macOS kernel memory-pressure hold. 0 = no bound. Load-time clamped to [0, 86400]. Live (SubagentManager.LIVE_CONFIG_PATHS). See modules/subagent.md § Durable task queue and § macOS: the kernel memory-pressure hold
    start_collect_timeout_secs: int = 300  # how long the session-start gate's StartCollector keeps a timed-out session/new (row `recovering`) to adopt a late answer before the attempt is abandoned. Load-time clamped to [10, 3600]; restart=True
    session_start_concurrency: int | str = "auto"  # ACP session/new requests outstanding per gateway event loop (SessionStartGate; fixed, not adaptive). Queue time behind it is not start time. "auto" is sized once per process by session_start_sizing: clamp(min(cpus // 4, available_GB // 3), 2, 16), cpus = affinity capped by cgroup v2 cpu.max; an integer is load-time clamped to [1, 64]; junk falls back to "auto"; restart=True
    child_env_defaults: dict[str, str] = {}  # env vars set on every spawned kiro-cli child (AcpRuntime via KiroHarness/KasHarness.apply_spawn_env, auxiliary AcpClient via _resolve_spawn_env(kiro_api_key=True)) ONLY when the key is absent from the inherited env (ambient or per-session overlay; an explicit empty value wins). Applied before the agent-env scrub. A name left out of this map gets the host-sized default min(cpus, 4) (host_task_sizing.thread_pool_env_defaults) only where the agents slice's task ceiling is in force (host_task_sizing.task_ceiling_in_force, i.e. sandbox._probe_cgroup_scope); elsewhere (macOS, Windows, Linux without the slice) no default is set; a value set here wins, e.g. {"TOKIO_WORKER_THREADS": "8"}. Allowlist only (CHILD_ENV_DEFAULT_NAMES): TOKIO_WORKER_THREADS and RAYON_NUM_THREADS, each a decimal integer string (ASCII digits, nothing stripped) in [1, 1024], stored as str(int) ("04" -> "4"); any other name or value is dropped with a warning (key named, never value). Live (next spawn). See acp/child_env_defaults.py
    env: dict[str, str] = {}  # agent.env: operator env vars laid over the inherited gateway env of EVERY agent child (every backend, both drivers) by the shared launch tail (acp.launch.launch via acp/agent_env.py::agent_env_overlay), so shell tool calls and agent-started MCP servers inherit them. Precedence: gateway env < agent.env < request.extra_env < the driver's env_before_scrub (session identity, credential repair) < scrub_agent_subprocess_env (still removes any denied name) < everything set after the scrub; the set-if-unset defaults (child_env_defaults in env_before_scrub, inject_xdist_auto_cap's PYTEST_XDIST_AUTO_NUM_WORKERS after the scrub) only fill a name agent.env left unset, so agent.env wins over them. For non-sensitive values only (proxy, CA bundle, AWS profile, registry). Refused at SAVE by the config publish floor (config.loader._refuse_agent_env_entries, reached by every writer -- kirocrew config set in each form, every dashboard write, KiroCrewConfig.save): sections.agent_env_write_refusals applies the same per-entry rule as the load path plus the scrub check, and ConfigWriteRefused names each refused key and its reason (never a value) with nothing written; a write that leaves an on-disk mapping holding a refused entry exactly as it found it publishes with a warning, and an unreadable prior file refuses. Dropped with a warning (key named, never value) at load by sections.coerce_agent_env: a non-POSIX-identifier name, a non-string or NUL-bearing value, a name in AGENT_ENV_DENIED_NAMES / AGENT_ENV_DENIED_PREFIXES / AGENT_ENV_DENIED_SUFFIXES unless it is in AGENT_ENV_DATA_ONLY_NAMES (GIT_TERMINAL_PROMPT, GIT_SSL_CAINFO) (upper-cased compare; refused are the names Crew or the sandbox sets and the names known to make the loader, a shell, a runtime or a tool run code the command did not name -- a denylist that narrows that class and cannot close it; toolchain selection such as JAVA_HOME or CC is accepted: process identity and paths Crew/the sandbox sets, LD_ / DYLD_ / GCONV_PATH / GLIBC_TUNABLES / OPENSSL_ / KRB5 loader and C-library hooks, DBUS_*_BUS_ADDRESS and SYSTEMD_ read by the pre-sandbox cgroup scope, shell start-up and option names BASH* / ENV / SHELLOPTS / PS0-PS4 / ZDOTDIR / IFS, runtime hooks PYTHON / PERL / RUBY / GEM_ / LUA_ / NODE_OPTIONS / JAVA_TOOL_OPTIONS / COR_ / CORECLR_, GIT_ / SSH_ / XDG_ namespaces, config files that name a program such as AWS_CONFIG_FILE / KUBECONFIG / DOCKER_CONFIG, and every name ending PATH / LIB / LIBS / MODULE / MODULES / OPT / OPTS / OPTIONS / FLAGS / _PRELOAD / COMMAND / SHELL / EXECUTABLE / EDITOR / PAGER / ASKPASS / BROWSER / _RSH / WRAPPER / RUNNER / CONFIG / CONFIG_DIR / CONFIG_FILE / CONFIG_HOME; KIROCREW_ / KIRO_ / DSH_ / OPENCODE_ namespaces), a harness sign-in home override (every name in agent_sdk.host_auth.home_override_env_vars(), because the credential read gate anchors those from the gateway's own environment), and a credential-shaped name (AGENT_ENV_CREDENTIAL_CLASS: a _-separated name segment that is or ends in KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL, optionally plural or numbered, case-insensitive -- API_KEY, GITHUB_TOKEN, APIKEY are refused, GOPRIVATE and TOKENIZERS_PARALLELISM accepted) because values are plaintext in config.json; dropped at spawn by agent_env_overlay: a name sections.agent_env_scrub_removes() matches (sandbox.agent_env_scrub_prefixes(), upper-cased compare). Unreadable config applies nothing. Live (next agent spawn; a change also parks the shared background runtime, SessionManager._on_config_change -> retire_for_agent_env_change).
    cold_start_concurrency: int | str = "auto"   # background cold starts one SessionManager runs at once (_start_sem; a person's start has one more reserved permit). "auto" = the effective session_start_concurrency width clamped to [4, 16] (cold_start_sizing). Integer load-time clamped to [1, 32]; junk -> "auto"; restart=True
    runtime_spawn_concurrency: int | str = "auto"  # ACP runtime spawn + initialize handshakes per gateway event loop (cold-start admission). "auto" = half the effective session_start_concurrency width clamped to [2, 8]. Integer load-time clamped to [1, 32]; junk -> "auto"; restart=True
    lane_weights: dict[str, int] = {}    # per-lane weight overrides keyed by root session key or 'system'; unlisted lanes weigh 1, and a weight shapes the share of picks and, while a lane waits for memory, that lane's share of running dedicated subagents, never a hard cap. Each value load-time clamped to [1, 64]; non-string and empty keys dropped. Live
    child_reserve: int = 1               # execution slots a depth-0 task may never take while a nested task is queued or a parent waits on children; also lifts an adaptive squeeze to adaptive_floor + child_reserve while a parent waits (never above max_subagents). 0 disables. Load-time clamped to [0, 8]. Live. See modules/subagent.md § Fairness lanes and the child reserve
    recovery_backoff_base_secs: float = 2.0    # first retry delay of the shared recovery ladder (tool call / backend / ACP runtime) and of a dependency wait; doubles with equal jitter. Snapshotted onto the process ladder by `recovery.ladder.configure_default_ladder(cfg)` in `GatewayOrchestrator._init_subagents`; the gatewayd supervisor's rung is pinned and does not follow it, and the two import-time readers (`acp/client_timeouts._ACP_RESPAWN_BACKOFF_S`, `taskq/model.recovery_backoff_secs`) keep the static defaults. Load-time clamped to [0.1, 60]; restart=True. See modules/session.md § Recovery ladder
    recovery_backoff_max_secs: float = 120.0   # cap on that delay; a server retry hint is honoured up to it. Same snapshot seam and same exclusions as the base. Load-time clamped to [1, 3600], never below the base; restart=True
    adaptive_concurrency: bool = True        # run the adaptive concurrency controller: a runtime execution cap beneath max_subagents (the ceiling, never written; starts AT it; cut only by failing work, never by loop lag or memory) plus the MCP daemon's spawn-gate capacity. false = user cap only. Live. See modules/adaptive-concurrency.md
    adaptive_concurrency_mode: str = "aimd"  # "aimd" | "fixed" ("fixed" pins both caps at their initial values, the execution cap at its ceiling -- the one-flip reversal). Live
    adaptive_floor: int = 1                  # lowest execution cap under sustained work pressure. Load-time clamped to [1, 64]. Live
    adaptive_initial: int = 4                # Inert (not flagged deprecated: every save writes it): the execution cap starts at its ceiling. Load-time clamped to [1, 64] and preserved on save
    adaptive_slow_start: bool = True          # before the first corroborated pressure, double a cap below its ceiling per clear 5 s window instead of +1 per 30 s, bounded by max_subagents. Live
    # AIMD tuning uses fixed constants in adaptive/policy.py.
    controller_sample_secs: int = 5          # adaptive controller sampling interval. Load-time clamped to [1, 300]. Live
    dependency_max_attempts: int = 20          # coordinated probes a dependency scope gets before every waiter is failed. Load-time clamped to [1, 1000]
    dependency_wait_deadline_secs: int = 3600  # wall-clock ceiling on one dependency wait; 0 = attempts cap only. Load-time clamped to [0, 86400]
    dependency_wake_per_tick: int = 0          # waiters released per wake tick after the recovery probe; 0 = the current effective admission capacity. Load-time clamped to [0, 4096]
    dependency_wake_spacing_secs: float = 1.0  # pause between staged wake ticks. Load-time clamped to [0, 60]
    interactive_command_policy: str = "cancel"  # "cancel" | "wait": what the tool-stall watchdog does when a stalled shell command is classified waiting_input -- cancel that call non-lethally and re-drive with a non-interactive hint, or announce waiting_input once and keep the turn open (bounded by the turn ceiling). Never answers the prompt. An unknown value loads as "cancel". Read by _load_watchdog_settings (new handles + hot-apply). See modules/acp-client.md § Interactive-command policy

@dataclass
class SessionConfig:
    timeout_secs: int = 3600       # 60 min idle timeout (DEFAULT_SESSION_TIMEOUT)
    idle_exempt_keys: list[str] = []  # session keys the idle sweep never expires on the clock (a dashboard chat is dashboard:<sid>; a tab linked to a cron job or channel thread uses that link's key, e.g. cron:<job>, per chat_utils.session_key_for). Loaded as stripped non-empty strings, at most IDLE_EXEMPT_KEYS_MAX (= POOL_SIZE_MAX, 10: each listed key can keep a whole kiro-cli process tree running, the warm pool's per-entry cost) keys of at most IDLE_EXEMPT_KEY_MAX_CHARS (512) characters each; a longer key is dropped rather than truncated, and one warning is logged per load when any key is dropped; a non-list loads as []. Read at every sweep. A listed dashboard or cron-linked session is still reaped when its tab closes (a channel thread's session is channel-owned, so closing its viewer never reaps it), and the RSS recycle, when enabled, still applies. A listed key that never had a tab, such as a cron job run without one, is reaped on neither axis, even after its job is deleted; that recycle or a restart still ends it, and so does the identity sweep (retire_kiro_identity_sessions, which reads no list). Before each dashboard chat turn and each agent cron run (chat_turn.account_identity._retire_sessions_on_identity_change) it retires a kiro-backed one once the live Kiro account is no longer the one it was spawned under, except that an agent cron run touches no session when the live fingerprint is empty without proving a sign-out (a social login the reader cannot identify, an unreadable or relocated store); a Kiro sign-out from the dashboard (kas_login._retire_runtimes_after_sign_out) runs it with no live account, which retires every idle kiro-backed session at once, whatever account each signed in with; a busy session is flagged and retired later. See session.md "Idle cleanup".
    empty_response_auto_continue: bool = True  # after TWO consecutive empty model responses, auto-send synthetic "continue" nudges on the same live session (transcript-visible notice; count bounded by empty_response_max_continues; the config gate fails OPEN to the default so a config-load hiccup cannot disable self-healing). See session.md "Empty-response recovery ladder".
    empty_response_max_continues: int = 1  # how many continue nudges may run back to back before the give-up card (EMPTY_RESPONSE_MAX_CONTINUES_MIN/MAX; load-time clamped to [1, 10] so a hand-edited 0 cannot disable recovery and a large value cannot arm an unbounded ladder). Default 1 keeps the pre-knob behavior byte-identical; above 1 the notice numbers each recovery ("recovery 2 of 3").
    autocompact_pct: float = 70.0  # context usage % at which auto-compaction triggers (DEFAULT_AUTOCOMPACT_PCT). Load-time clamped to [5.0, 90.0] (one constant pair shared with the dashboard write gate)
    pool_size: int = 0             # pre-warmed kiro-cli processes kept ready for instant session start; 0 (the default) disables. Single source of truth: DEFAULT_POOL_SIZE, read by both the field default and load()'s file-parse fallback. Load-time clamped to [0, 10]
    watchdog_rss_max_mb: int = 0   # DEFAULT_WATCHDOG_RSS_MAX_MB: recycle a session when its process tree RSS exceeds this many MiB; 0 disables and is the default, because a fixed ceiling cannot tell a leak from a session with many MCP servers; at 0 the internal background runtime still recycles at BACKGROUND_RSS_FALLBACK_MB (1536). Busy sessions (turn in flight) are never recycled, and neither is a parent whose sub-agents are still running, queued, or delivering their results on its runtime.

@dataclass
class TaskRunnerConfig:
    max_parallel_steps: int = 0    # 0 = auto; a positive value only lowers the host-safe cap
    workspace_dir: str = ""       # empty = per-run workspace; otherwise the validated absolute target directory
    test_command: list[str] = []  # argv run after each step, no shell; malformed = []; restart=True (taskrunner.md "Test verification")
    require_verification: bool = False  # only an exit-0 test run lets a step pass; strict `is True`; restart=True

@dataclass
class MemoryConfig:
    embed_rebuild_generation: str = ""  # managed explicit-apply request; each store acknowledges after invalidation
    embed_model_stamp: list[int] = field(default_factory=list)  # managed device/inode/size/mtime_ns/ctime_ns; empty means unverified
    embed_model_legacy_ids: list[str] = field(default_factory=list)  # managed compatibility labels retained across restarts; explicit model apply clears them and rebuilds inherited vectors
    history_idle_hours: float = 3.0  # consolidate history after N hours idle
    history_max_days: int = 365      # prune daily history files older than this
    persistence_enabled: bool = True # global switch: off = no automatic memory writes (lessons, consolidation, task-runner) AND no stored memory/lessons injected
    writes_enabled: bool = True      # off = the same automatic writers persistence_enabled stops, while injection and memory_recall continue (read-only memory)
    lesson_extraction_enabled: bool = True  # off = consolidation (default and member stores) and the task runner save no extracted lessons; other writes and learn_add continue
    kind_routing: str = "off"        # off | shadow | enforce: whether consolidation tags each project.* write with a kind (fact/event/state) and what it does with it; off keeps today's prompt and writes, shadow writes as before and keeps a bounded log; unknown values read as off (memory_kind_routing.py)
    inject_memory: bool = True       # inject the stored memory block (preferences, activity index, recent-session snippets) into new-session context
    inject_lessons: bool = True      # inject the [Learned corrections] + [USER PROFILE] blocks into new-session context
    inject_lessons_per_turn: bool = False  # on follow-up messages, add up to 3 matching lessons the session was not shown; requires inject_lessons
    inject_activity: bool = True     # inject the budgeted [Memory activity] block (projects, daily history (14 full days, then decayed summaries and counts to day 180), task facts, relevant episodes); requires inject_memory
    startup_recall_timeout_secs: float = 1.0  # how long a session-start build waits for a first-message embedding already running (the dashboard starts it early); past it, task facts, episodes and lessons rank by keyword. Any other build keeps context._PROMPT_BUILD_EMBED_TIMEOUT_SECS (5.0); 0 turns the early embedding off, and the early session-start memory reads with it; clamped to [0, 10]

@dataclass
class KnowledgeConfig:
    # Knowledge Library ingestion toggles. Embedding/retrieval settings live
    # under MemoryConfig (shared via create_embedder_from_config).
    auto_add_documents: bool = False                    # opt-in; agent adds documents it reads (aggregate "Auto-added" source); legacy spelling auto_ingest_doc_links accepted
    folder_ingest_chunk_budget: int = 300               # chunks per sweep for a folder source; per-source chunk_budget overrides; 0 = unbounded
    dedup_every_n_sweeps: int = 12                      # full dedup pass cadence; 0 disables
    auto_ingest_artifacts: bool = False                 # opt-in; ingest local artifacts into the KB (aggregate "Artifacts" source)
    auto_ingest_artifact_kinds: list[str] = ["markdown", "text", "html", "json"]  # reader-extractable kinds (widget/svg excluded)
    embed_timeout_secs: float = 10.0                    # per-request embed timeout; 0/unset -> built-in TIMEOUT (10s)
    embed_content_budget: int = 0                       # chunk-content fold budget (chars); 0/unset -> built-in _EMBED_CONTENT_BUDGET

@dataclass
class ChannelConfig:
    activation: str = "mention"    # "always", "mention", "observe", "review", or "off"
    agent: str = ""                # per-channel agent override (empty = use default)

@dataclass
class SttConfig:
    enabled: bool = True           # on by default: the default provider needs no account
    provider: str = "local"        # "local" | "apple" | "transcribe" | "off"; a retired value degrades to "local", any other unknown value to "off" (see stt-streaming.md)
    model: str = "base"            # a kiro_crew.stt.models CATALOG name; a superseded name resolves via its alias table
    language_code: str = "auto"    # stored preference; effective_language_code resolves auto to en-US for Apple/Transcribe
    streaming: bool = True         # live partials; every provider produces them
    silence_ms: int = 700          # end-of-phrase pause; clamped to _STT_INTERVAL_MS_MIN.._MAX
    partial_interval_ms: int = 400 # live-transcript refresh cadence; same clamp
    idle_evict_secs: int = 600     # release the resident local model after this idle; 0 = at end of recording
    threads: int = 0               # local decode threads; 0/"auto" = half the cores capped at 16; KIROCREW_STT_THREADS wins
    endpointing: bool = False      # semantic auto-submit on a complete utterance; needs streaming
    submit_on_release: bool = False  # a deliberate stop sends once the transcript settles; any provider
    polish: bool = False           # hand the FINISHED transcript (never the audio) to a fast model; off = nothing leaves the machine
    dictation_panel: bool = True   # animated recording panel; falls back to the status bar
    timeout_secs: int = 300
    transcribe_region: str = "us-east-1"   # transcribe provider only
    transcribe_profile: str = ""           # transcribe provider only; empty = default credential chain
    transcribe_vocabulary: str = ""        # transcribe provider only; custom vocabulary name, empty = none; an unusable stored name degrades to none
    transcribe_languages: list[str] = []   # transcribe provider, live stream only; 2-5 candidates (one dialect each) turn on mixed-language detection; an unusable stored list degrades to empty
    custom_model_path: str = ""            # ggml file in models/whisper to run when model == "custom"
    custom_model_sha256: str = ""          # its 64-hex sha256; without both, "custom" degrades to the default

@dataclass
class ComputerUseConfig:
    # DISPLAY + LIMITS ONLY. There is deliberately NO `enabled` field — see the
    # note under "Computer use: no enabled field here" below.
    max_tree_nodes: int = 1200          # accessibility-tree node budget per snapshot
    max_tree_depth: int = 64            # depth budget (the walk is iterative, so this is a cost bound)
    text_limit: int = 500               # per-element text truncation (chars)
    attach_screenshot: bool = True      # default for the `screenshot` tool param
    screenshot_max_px: int = 1280       # longest-edge downscale (NOT browse's 1920 — the tree is the primary channel)
    screenshot_jpeg_quality: int = 55   # JPEG quality (NOT browse's 70); 1280/q55 measured at ~8.3K tokens vs 41K for a raw PNG
    cursor_motion: bool = False         # macOS-only cosmetic overlay; draws a fake cursor and grants no capability

@dataclass
class AwsConfig:
    project_profiles: dict[str, str] = {}   # absolute project dir -> AWS named profile; see "Project AWS profiles" below

@dataclass
class MessagingConfig:
    use_transport: bool = True     # route inbound Slack through SlackTransport → TurnDriver → SlackRenderer (the canonical path); false falls back to the native handle_message monolith

@dataclass
class SkillsConfig:
    max_triggered: int = 0         # max skills loaded per message (>=0)
    lazy_load: bool = True         # true (default) = bounded usage-ranked index with paths and a families line; false = short eight-name skill_search entry; neither expands background admission
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
    default_memory_mode: str = "persistent"  # persistent | incognito | temporary; default for user-created dashboard chats only
    verbosity: str = "answer_only" # "default" | "concise" | "answer_only" (retired "ultra" is read as "answer_only"); anything but "default" injects a [RESPONSE PREFERENCES] block into SESSION CONTEXT for every agent (see "Response verbosity reaches every agent" below). Read/written via GET/PUT /api/dashboard/config (rejects values outside the enum). A fresh install starts at "answer_only"; an existing config keeps the level it already stores, since every full save writes the key. An unrecognized value injects nothing.
    theme_mode: str = ""           # "dark" | "light" | "system"; empty = unset (frontend falls back to localStorage or "system")
    theme_color: str = ""          # color-theme slug (e.g. "kiro", "emerald", "monokai"); empty = unset
    translucent_panels: str = ""   # the Translucent panels (Liquid Glass) switch: "on" | "off"; empty = never chosen on this workspace (the browser's own stored choice holds). See "Dashboard theme persistence" below.
    language: str = ""             # dashboard UI language, BCP-47 (e.g. "en", "zh-CN"); empty = auto-detect from the browser. See "Dashboard UI language" below.
    show_unread_dot: bool = True   # the per-row unread dot on sidebar session cards (Settings → Display → Sidebar); only a literal false hides it. Folder and aggregate unread counts are unaffected. PATCH-able via /api/config/kirocrew
    folder_sort: str = "custom"    # sidebar folder order: "custom" (stored positions) | "name" (natural, 01. < 02. < 10.) | "created" (newest first). A view preference; never rewrites a folder's stored order. See "Sidebar folder order" below.
    session_sort: str = "date-desc" # sidebar session order: "date-desc" / "date-asc" (last activity), "created-desc" / "created-asc" (creation time), "name-asc" / "name-desc" (title), "manual" (the person's own drag order). A view preference; pinned rows keep their own order. See "Sidebar session order" below.
    onboarded: bool = False         # whether the "Choose your look" onboarding modal was completed
    import_onboarded: bool = False  # whether foreign-agent import was completed or skipped
    terminal: dict                  # the built-in Terminal tab: shell ("" = $SHELL), max_sessions (12, server-wide ceiling), orphan_timeout_s (43200 = 12 h a shell runs with no browser attached before it and its processes are ended; floor 60), cwd ("" = $HOME), reuse_current (false; true = Run-in-terminal focuses the selected terminal and copies the command, never runs it), completion.enabled (true). PATCH-able via /api/config/kirocrew
    crewmates_onboarded: bool = False  # legacy record of the retired Meet Mates flow; nothing reads it
    tips_enabled: bool = True      # feature-discovery tips (GET /api/tips/next); live-read
    tips_cadence_hours: float = 6.0    # min hours between surfaced tips (server-side gate; clamped >= 0)
    tips_snooze_hours: float = 48.0    # hours before a snoozed tip is eligible again (clamped >= 0)
    tips_recency_decay: float = 0.6    # weighted-random newer-bias decay (clamped to [0, 1])
    tips_model: str = "auto"  # model for tips generation ("auto" inherits the account's governed model)
    tips_explore_ratio: float = 0.2    # probability of random catalog pick vs personalized (clamped to [0, 1])

@dataclass
class TelegramConfig:
    enabled: bool = False              # start the Telegram Bot API channel (long-polling) at gateway startup
    bot_token: str = ""                # @BotFather token; prefer the TELEGRAM_BOT_TOKEN credential
    allowed_user_ids: list[int] = []   # numeric user IDs allowed to drive the bot; empty = deny all (fail closed)
    soft_threshold_pct: int = 80       # prompt to /compact or /new when context passes this %
    allow_forum: bool = False          # serve supergroup forum Topics as per-Topic sessions (Slack-thread style). Fail-closed: also requires the supergroup's chat_id in allowed_forum_chat_ids, and only real Topics (message_thread_id present) are served — ordinary groups and the supergroup General chat are denied
    allowed_forum_chat_ids: list[int] = []  # numeric supergroup chat_ids permitted to run forum-topic sessions; empty = deny all groups (fail closed)

# Additional top-level DTOs (not fully expanded here — see the owner modules in the Overview):
# CronHistoryConfig, TunnelConfig, InstancesConfig, HeartbeatConfig,
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
    computer_use: ComputerUseConfig
    aws: AwsConfig
    hooks: dict                    # raw hooks from config.json
    dashboard: DashboardConfig     # dashboard.url, e.g. "http://my-host.example.com:8080"
    auto_update: bool = True
    snapshot_dir: str = ""         # snapshot output dir (default ~/.kiro/crew/snapshots)
    slack_channels: dict[str, ChannelConfig]  # per-channel config keyed by channel ID
    slack_dm_activation: str = "always"       # activation mode for DMs (D-prefix channels)
```

### Per-crew avatar override (`agents.*.avatar`)

`KiroCrewAgentConfig.avatar` is a sparse override, exactly like the per-crew
`model` and `session_color` fields: absent or `{}` means the face is derived from
the crew's name (zero migration). `_safe_avatar` (`config/sections_agent_fields.py`) is the
total coercer applied on load, in the create/update endpoints and by the roster's
`_roster_avatar`,
and it is deliberately NOT re-exported from `loader.py` — the loader's
`from kiro_crew.config.sections import (...)` list is a frozen pre-split snapshot
(`test_config_module_boundaries`), so post-split internals are reached through the
`sections` module. Three accepted shapes:

- `{"kind": "ghost", "traits": {eyes, brows, mouth, accessory, prop: str; blush,
  flip: bool; tile: "#rrggbb"}}` — string traits are truncated to 32 chars and
  are NOT checked against the frontend's trait vocabulary (the renderer resolves
  an unknown option to "absent", so a new hat needs no backend release);
  booleans must be real JSON booleans (`bool("false")` is `True`, so a
  string-typed value is read as `False`); `tile` is the one pinned value — it is
  interpolated into SVG markup, so it goes through the same `#rrggbb` validator
  as `session_color`. An all-empty trait set drops the `traits` key rather than
  storing a featureless third state, and a ghost override left with nothing but
  `kind` collapses to `{}` (the one canonical "reset" spelling). `traits` is
  therefore optional: `{"kind": "ghost", "sounds": {...}}` is valid and means
  "name-derived face, plus these per-state reactions". The ghost is the ONE tier
  that carries `motions` and `sounds` (below).
- `{"kind": "image", "v": <int>, "file": "<16-hex>.<png|jpg|webp>"}` — the crew
  wears an uploaded picture served from `GET /api/agents/{name}/avatar`; the
  file itself lives under `<data home>/run/avatars/` and the record only marks
  the choice. A picture has no face to move, so it carries no `motions`; it does
  still store and round-trip `sounds`, though the frontend plays a crew-record cue
  only on a ghost (`crewAvatarState.ts` `soundsFrom` is ghost-gated, and
  `retiredCueFrom` reports a picture or pack cue that has gone silent). `v` (a positive real int; `True` is rejected) is the cache-busting
  mtime stamp the frontend appends as `?v=`; `file` pins the exact committed,
  content-addressed variant and must match `^[0-9a-f]{16}\.(png|jpg|webp)$`.
  Wire-only keys (`promote`, `token`) never reach the record.
- `{"kind": "pack", "id": "<pack id>"}` — the crew wears an appearance pack from
  the crew library (`GET /api/appearances`, specified in
  `learn-cron-dashboard.md`, *Crew appearance library*). `id` is validated by
  `appearance_packs.safe_pack_id`, the SAME function the pack store applies to a
  directory name, so a value that persists here can always be looked up; a
  second copy of the character class is what would drift. A junk id collapses the
  whole override to `{}` rather than storing `{"kind": "pack"}`: a pack avatar IS
  its id, so an override naming no art has nothing to render. Whether the pack
  still EXISTS is deliberately not checked — config load must not touch the disk,
  and a pack deleted out of band would otherwise make the whole config unloadable
  instead of making one face fall back — so a dangling id renders as the
  name-derived ghost on the client. A pack carries its own per-state art
  (`GET /api/appearances/{id}/slot/{slot}`) and its own per-state audio
  (`GET /api/appearances/{id}/sound/{state}`), so it needs no `motions` on the
  record. It keeps accepting `sounds` as the picture tier does, so a stored value
  survives a save, though only a ghost plays it.
- `{"kind": "icon", "pose": "pose-N", "bg": "#rrggbb"}` — the crew wears a
  shipped ghost POSE over a solid background. This is the PRIMARY tier new crews
  are built with (the ghost "捏脸" builder and the pack library stay for crews
  already wearing them). The whole override is `pose` + `bg`: `pose` names one of
  the hand-drawn silhouettes in `website/src/lib/avatarPoses.ts` (`POSE_IDS`),
  `bg` is the colour painted behind it. The
  silhouette stays white and the eyes stay black — the background is the whole
  colour model, seeded from the active theme's `--accent` when the builder opens.
  `pose` is a bounded (`_AVATAR_TRAIT_MAX_LEN`), charset-safe (`_AVATAR_POSE_RE`)
  string, NOT a membership test against the shipped pose set: like a ghost trait, an
  unknown-but-well-formed value is the renderer's to resolve (`resolvePose` falls
  back to the default pose), so pinning it here would drop a pose a newer client
  wrote and wipe the face on an unrelated save. A record naming no `pose` (or a
  junk one) collapses to `{}` the way a pack with no id does — a pose IS the
  icon's whole identity. `bg` is pinned to `#rrggbb` by the same `_safe_color`
  validator `tile` uses, because it is interpolated into SVG markup on the
  client; a dropped `bg` renders the art's shipped purple. An icon is a static
  drawing like a picture, so it carries NO reaction keys (`motions`, `sounds`,
  `expressions`).

  **A pack round-trips through the editor.** The crew editor loads a
  pack-wearing crew's draft as `{"kind": "pack", "id": ...}` and sends it back on
  every save, so an unrelated save (a model change, a colour) writes the pack
  the crew already wears; a save that carries no `avatar` key at all leaves the
  record alone. `PUT /api/agents/{name}` therefore applies one rule on every
  tier, pack included: `avatar: null` and `avatar: {}` are resets that take the
  face off, a ghost with no `traits` is the name-derived face plus its
  reactions (a tier change when the crew wears a pack or a picture), and any
  other face replaces the current one. The editor sends `null` for its
  Reset to default, so the wire says the reset was meant.
  **A ghost's `motions` survive a save that does not name them**
  (`dashboard/agent_admin/avatars.py::_carry_motions_through_motionless_save`): the shipped
  editor rebuilds a ghost draft from the axes it can draw and submits exactly
  those, so a `motions` pick set through the API would be erased by the next
  unrelated save with no click that meant it. The rule is the tri-state
  `save_pack` gives a pack's cues — a payload with NO `motions` key leaves the
  stored ones alone, a payload naming the key (`{}` included) replaces them —
  and it applies only when the stored record and the validated save are both
  ghosts: a tier change is a real face replacing the old one and `motions` is the
  ghost's alone, and a reset (`null`, `{}`, the all-empty collapse) means reset.
  It retires with the frontend change that submits `motions` itself.

**Per-state reactions (`motions`, `sounds`).** Where a reaction may be stored
follows from which tier can play it, and the two keys differ. `motions` is the
GHOST's alone: it names a built-in animation of a trait-composed face, so a
picture has nothing to move and a pack animates from its own files. `sounds` is
stored and round-tripped on EVERY tier, but only a ghost plays it: the frontend's
`soundsFrom` is ghost-gated, so a picture or pack cue is silent and `retiredCueFrom`
says so. Both
are keyed on the agent lifecycle state (`working`, `done`, `error` exactly; any other
key is dropped, so a version-skewed caller cannot grow the key set):

- `motions: {"done"?: "none"|"bounce"|"nod"|"sparkle", "error"?: "none"|"shake"|"cross-eyes"|"droop"}`
  — a built-in reaction animation the frontend implements. Each state has its OWN
  vocabulary (`_AVATAR_MOTIONS`) and a value from the other state's list is
  dropped: `{"done": "shake"}` would play a failure animation on success, which is
  not what its author wrote. There is no `working` entry — the ghost's working
  animation is its idle breathing, and a reaction fires on a transition. `"none"`
  is kept as explicit stillness, distinct from an absent state, so one state can
  opt out of a motion the others use.
- `sounds: {"<state>": "none"|"chime"|"ding"|"blip"|"pop"|"pulse"}` — a
  synthesized cue preset. Unlike a trait value this IS pinned to a vocabulary,
  because the name selects a shipped preset rather than an option the renderer can
  resolve to absent. `"none"` is explicit silence, distinct from an absent state
  (also silent). No per-crew audio upload exists: a crew that needs its own audio
  wears a pack, which carries its own.

Either key is omitted from the record when validation leaves it empty, so a
stored avatar never carries `{}` for one, and a key illegal on this tier is
DROPPED rather than refused — `{"kind": "image", "motions": {...}}` loads as a
bare picture. No stored cue is lost by that rule: `sounds` stays legal wherever it
already worked, so an existing picture- or pack-wearing crew keeps the sound its
owner chose. Junk (`motions: "x"`, `sounds: {"working": 5}`, a list) is stripped
silently and never refused: the same forgiveness traits get, so a malformed
reaction costs that reaction and never the crew's whole avatar. `expressions`
(a per-state `eyes`/`mouth` pick, which `motions` supersedes) round-trips on
EVERY tier — `{"<state>": {"eyes"?: str, "mouth"?: str}}`, only those two
axes, 32-char truncation, empty strings dropped — but that round-trip is now the
last of it: the picker is gone and no renderer draws it, so the key is carried
only so a record written by the previous release survives the gap until the
validator change that drops it. `sounds` on a picture or a pack is vestigial in
the same way, for the same window. `sounds` stays for the same reason on every tier, and only
`motions` is tier-gated. The roster (`_roster_avatar`) masks the string values of `traits` and
`expressions` through `_roster_mask`, and leaves `motions`, `sounds` and `file`
intact — a value pinned to a closed vocabulary is not user-authored text, and
masking it would break the reaction while destroying nothing an attacker could have
put there.

Anything else — a non-dict, an unknown `kind`, a ghost override carrying no
trait, motion or sound that survives validation — collapses to `{}` on load (config.json is hand-editable,
so junk must never crash the load), while the endpoints answer a
non-empty raw value the coercer collapses with 400 `invalid_avatar` — except a
well-formed ghost override whose traits all coerce to absent, which is the
validator's own all-empty → reset rule rather than caller junk and so stores as
the canonical reset. The staging
and commit protocol behind the image tier is specified in
`learn-cron-dashboard.md` (*Crew avatars*).

### Computer use: no `enabled` field here

`ComputerUseConfig` carries display and limits only. The switch for native desktop
GUI automation lives **outside `config.json`**, on the keystone at
`~/.kiro/crew/computer_use.json` (path via `config.loader.computer_use_state_path()`,
leaf on `security._CREW_SECRET_LEAVES`):

```json
{
  "enabled": false,
  "allowed_apps": [],
  "extra_denied_apps": []
}
```

The absence is deliberate. `config.json` is a `READONLY` sandbox leaf, so an
in-sandbox shell cannot write it, but it is an ordinary settings file that every
config writer (the config PATCH, `kirocrew config set`) reaches without an owner
gate. A security ceiling there would be one settings edit away from being
lifted.

- **`enabled`** — the primary enable for full desktop observation plus input
  synthesis. A security ceiling, so it goes on the keystone: the agent's file tools
  cannot read or write it, and its shells can read it but not write it. Read with a strict `is True` identity test, so a truthy string such as
  `"enabled": "false"` does **not** enable desktop control, and the read fails soft
  to `{}` → **off**.
- **`allowed_apps` / `extra_denied_apps`** — the operator's own narrowing. These
  are the ONLY other keys `PolicyConfig.from_state` reads.

**There is no `allow_pointer_move` key, and writing one has no effect.** There is no
second consent switch for `click_method: "global"` (the one path that warps the real
mouse pointer) and no `capabilities.computer_use_pointer` governance row: there are no `computer_use.*` governance scopes at all, and
`from_state` reads only `allowed_apps` and `extra_denied_apps` besides `enabled`, so a hand-written
`{"enabled": true, "allow_pointer_move": false}` silently grants the pointer path —
the operator would believe they had withheld consent. What actually contains that
path is that the model must NAME the method (`auto` never resolves onto it) and every
use is SEL-audited under its own `tool_kind`. Do not re-document the flag without
re-implementing it. See [security.md](security.md), [governance.md](governance.md)
and [computer-use.md](computer-use.md).

### Project AWS profiles (`aws.project_profiles`)

`aws.project_profiles` is an operator-owned map from an absolute project
directory to an AWS named profile (`aws_project_profiles.py`). It guards against
running a command on the wrong account. It is not isolation: the agent can still
pass `--profile`.

- **Validation.** Keys must be absolute paths, and values must match
  `constants.AWS_PROFILE_NAME_RE`.
  - At load, `coerce_project_profiles` drops a bad entry with a warning that names
    the directory only. It normalizes keys with `os.path.normpath`, and the first
    of two keys naming one directory wins.
  - At save, `write_config_atomically` refuses a write that introduces a bad entry
    (`_refuse_aws_project_profiles` raises `ConfigWriteRefused`), scoped like the
    `deepseek_env` floor: a bad entry already on disk, in a mapping the write leaves
    unchanged, publishes with a warning.
  - The refusal never carries the profile value.
- **Match.** `resolve_project_binding` runs `os.path.realpath` (plus `normcase`)
  on the session cwd and on every key, then compares them component by component.
  The deepest containing key wins. `/a/proj` never matches `/a/project2`, and a
  symlink is judged by its target.
- **Crewmate worktrees.** A cwd in a crewmate's worktree is matched as the same
  path in the repository's own checkout (`crewmate_worktree.source_path`):
  `<workspace>/worktrees/<slug>/app-<hash>/src` matches as `/repos/app/src`, so
  a worker keeps the binding on `/repos/app` and never takes one on its
  workspace. It is asked of git each time, through the sandboxed runner, with
  nothing remembered, so it holds after a restart. It applies only when git's
  work-tree top sits at `worktrees/<slug>/`, carries the name `path_for` gives
  that repository, and is registered by that repository as a worktree. Anything
  else, a repository cloned inside the worktree included, matches as itself. A
  path with no `worktrees` folder in it runs no git, and a git failure leaves the
  path as it is.
- **Injection.** `SessionAllocation._get_or_create_impl` resolves the binding
  against the final `effective_cwd`. This happens after capability preparation and
  before the warm-pool decision. `bound_profile_env` sets both `AWS_PROFILE` and
  `AWS_DEFAULT_PROFILE` in `extra_env` to the bound name, unless the caller's
  `extra_env` already names either one. Setting both matters because botocore and
  the AWS CLI v1 read `AWS_DEFAULT_PROFILE` first, so an inherited one would
  otherwise outrank the binding.
  - When nothing binds (an empty map, no cwd, or an unmapped directory), the
    caller's `extra_env` object is handed through as-is, so the spawn environment
    and pool decision are unchanged.
  - A bound session takes `bypass_env` rather than a pooled process spawned
    without the profile.
  - `open_task_session` sends a step in a bound directory to `get_or_create`,
    because the run's shared runtime has one environment.
  - The profile the binding set is stamped on the registered `_Session` as
    `aws_project_profile` (`None` when it set none). A subagent run resolves its
    own `cwd`'s binding off the event loop (`SessionManager.project_aws_profile`
    through `asyncio.to_thread`, since `realpath` over the keys can block on a
    stalled mount) and hands it to `_sharing_plan`, which reads
    `SessionManager.aws_profile_differs_from_parent(parent_key, child_profile)`
    (no path) and takes the dedicated arm when the child binds another profile,
    or none, than the binding set on the parent, so `get_or_create` sets the
    child's own profile. A caller-chosen profile on the parent is not stamped, so
    its unbound children keep sharing it. An empty map with an unstamped parent
    (`aws_profile_split_possible` False) resolves no path and takes no thread
    hop. The synchronous admission probe resolves no path either: while a split
    is possible it prices the start dedicated.
  - The applied profile is logged by name only.
  - The name reaches the child only as the `AWS_PROFILE` / `AWS_DEFAULT_PROFILE` value. It is never put in
    an argv or a shell string. `scrub_agent_subprocess_env` still strips
    `AWS_SECRET*` / `AWS_SESSION*`.
- **Sources.** Only `config.json` is read. No file inside the project checkout
  and no credential material is consulted. A repository-supplied binding was
  rejected because it would let a cloned repo choose credentials.
- **Live.** `SessionManager` subscribes to `aws`, so an edit is adopted into
  `_cfg` and reaches the next spawn. A running session keeps its environment.
- **Dashboard.** `GET/PUT /api/aws/project-profile`
  (`dashboard/handlers/aws_project_profile.py`) backs the read-only profile shown
  in the composer's project pill and the AWS profile row of the project picker.
  - Both verbs are owner-only, through the audited
    `require_owner_dashboard_request` gate.
  - The GET returns the binding (`profile`, `boundDir`), the picker's names
    (AWS Control registry names plus mapped values, charset-checked) and a health
    word. The health word is `ok`, `expired` (an SSO profile whose
    `aws_consent.probe_identity` fails) or `unavailable`. No account id, ARN or CLI
    text is returned.
  - The PUT binds exactly the given directory, or unbinds it with `profile: ""`,
    through `update_config_locked` under `run_config_write`.

### Decisions: no `enabled` field here either

`DecisionsConfig` has no `enabled` field; its fields are listed in
[decisions.md](decisions.md).
Consent to send message text and skill descriptions to Jev lives **outside
`config.json`**, on the keystone `~/.kiro/crew/decisions_consent.json` (path via
`config.loader.decisions_consent_path()`, module `decisions.consent`, leaf on
`security._CREW_SECRET_LEAVES` and `sandbox._CREW_READONLY_LEAVES`):

```json
{
  "enabled": false,
  "endpoint": ""
}
```

`endpoint` is the `provider.endpoint` the owner consented to; the gate sends only
while the configured endpoint still equals it, because that field is in this
same settings file. Same reasoning as `computer_use.json` above: `config.json` is
sealed read-only against an in-sandbox agent shell (`sandbox._CREW_READONLY_LEAVES`;
see security.md) but remains an ordinary settings file every config writer reaches
without an owner gate, and every `decisions.*` field is hot-applied by the live
watcher, so an `enabled` toggle here would be one settings edit away from starting
the egress of the owner's conversation without a restart. Reads fail soft to `{}` → **not
consented**, and only a literal `true` consents. The only writer is the owner-only,
browser-called `PUT /api/decisions/consent` (`dashboard/handlers/decisions.py`);
`PATCH /api/config/kirocrew` refuses `decisions.enabled`, and an `enabled` key written
into the section by hand is inert — the parsed dataclass has no such attribute. See
[decisions.md](decisions.md).

#### `computer_use.cursor_motion` — the one new `config.json` flag

Cursor Motion (the cosmetic fake-cursor desktop overlay) is the exception that
proves the rule above: it belongs in `config.json` precisely *because* it grants no
capability. `computer_use.cursor_motion` is a **display preference, default OFF** —
the overlay draws an image, never moves the pointer, cannot deliver input, and is
invisible to `screencapture`, so an agent flipping it could at most decorate its own
clicks. A keystone flag would imply a security decision that does not exist.

`overlay.cursor_motion_enabled()` reads it through `getattr(section,
"cursor_motion", False)` **even though the field is now declared** on
`ComputerUseConfig`, and that stays deliberate: it makes the read
**forward-compatible and fail-OFF**: a build whose `ComputerUseConfig` predates the
field resolves to OFF rather than raising inside a tool call, and a missing field can
only ever mean "no decoration", never "start drawing on the user's screen".

Three consequences for this module: `"computer_use"` MUST be present in
`_KNOWN_CONFIG_SECTIONS` (the guarded invariant that `to_dict()`'s emitted sections
equal that set); the dashboard's `_EDITABLE_CONFIG` exposes only the limits
(`computer_use.max_tree_nodes`, `computer_use.screenshot_max_px`) — never an
`enabled` key; and every numeric knob is clamped to
the same `*_LIMIT` ceiling the MCP tool schemas enforce, so a hand-edited
`config.json` cannot ask for an unbounded accessibility walk or a full-resolution
screenshot.

### Session Environments: configuration only

`session_environments` (`SessionEnvironmentsConfig` in `config/sections.py`)
persists what the Settings UI needs to offer remote session environments, and
nothing else: `coder_url`, `profiles`, and the optional `sprites_profiles`,
`boxd_profiles`, `default_profile` and `stop_on_shutdown`.
`config/session_environments.py` owns
every rule: its strict `validate_*` functions back the dashboard write gate (a
400, nothing written) and its lenient `load_session_environments` backs
`SessionEnvironmentsConfig.from_raw`, which keeps every valid part of a
hand-edited section (an invalid `coder_url` loads as `""`; each invalid,
duplicate or over-cap profile is dropped with a warning).

- `coder_url`: `""` (not configured) or an `http`/`https` URL with a hostname and
  no username, password, query or fragment.
- `profiles`: at most 20 `{id, name, template, workdir}` records plus the
  optional boolean `compute_survives_stop` (default `true`; `false` marks a
  template whose stop destroys its compute and files, so Kiro Crew never stops
  that workspace, see [providers](providers.md)). `id` is an ASCII letter or
  digit then letters, digits, `_` or `-` (64 max, unique); `name` is 1-128
  characters, `template` 1-255, `workdir` an absolute POSIX path of at most
  1024. Text is whitespace-stripped and may not hold control characters.
- `sprites_profiles` (Fly.io Sprites): at most 20 `{id, name, token_secret,
  workdir}` records. `token_secret` names the Settings > Secrets entry holding
  the Sprites API token (a letter or `_`, then letters, digits or `_`); the token
  itself is never stored here.
- `boxd_profiles`: at most 20 `{id, name, snapshot, vcpu, workdir}` records.
  `snapshot` is `""` or a boxd snapshot name or id, `vcpu` one of 0 (the org
  default), 1, 2, 4 or 8, and a profile naming both is refused (a snapshot keeps
  its own size). `id`, `name` and `workdir` follow the Coder profile rules.
- One id may not name profiles of two providers, since a session's choice
  resolves by id. Leaf and whole-section saves check the final merged section
  under the config write lock; a load keeps such a collision visible (the picker
  omits the id and launch refuses it as ambiguous) and drops each malformed
  entry individually.
- `default_profile`: a saved profile id of any provider or `""` (this machine); a whole-section
  save refuses a default the same save drops, and a `sprites_profiles` or
  `boxd_profiles` leaf write refuses to drop the profile the saved default names
  (`unknown_profile`).
- `stop_on_shutdown`: a boolean, default `true`.

There is no `enabled` field (the opt-in is the per-device preview flag) and no
credential field, so the masked GET returns the whole section; unknown keys are
refused. An absent or non-object section loads with every key at its default,
and an unmodelled key inside it is preserved by `save()` and omitted from the
GET.

`PATCH /api/config/kirocrew` accepts `session_environments` and each of its seven
keys as paths, all through the `"normalized"` spec type. Whole-section saves
require the Coder keys; an omitted `SECTION_OPTIONAL_KEYS` key keeps its saved
value under the config lock. A write goes through `update_config_locked`,
preserves unrelated config and is hot-applied. Saving registers no provider,
checks no connection and provisions nothing.

### Security-Bounded Config Clamp

Resource-limit and timeout knobs are clamped to hard ceilings **at load time**, not
just at the dashboard write gate. The ceilings are owned in `sections_limits.py`
and re-exported by `sections.py` and `loader.py`; the load-time clamp lives in
`config/loader_builders.py`:

| Constant | Value | Field |
|----------|-------|-------|
| `SUBAGENT_AUTO_MAX_CEILING` | 64 | `agent.subagent_auto_max`, `agent.max_subagents` |
| `SUBAGENT_MAX_TURNS_CEILING` | 1000 | `agent.subagent_max_turns` |
| `SUBAGENT_TIMEOUT_MAX` (`constants.py`, beside `SUBAGENT_TIMEOUT_MIN`) | 86400 | `agent.subagent_timeout_secs` |
| `POOL_SIZE_MAX` | 10 | `session.pool_size` |
| `CHAT_TURN_TIMEOUT_MIN` / `_MAX` | 300 / 86400 | `agent.chat_turn_timeout_secs` |
| `TOOL_APPROVAL_TIMEOUT_MIN` / `_MAX` | 30 / 7200 | `agent.tool_approval_timeout_secs` |

The table names the ceilings; `_SECURITY_BOUNDED_FIELDS` is the authoritative list
of every clamped `(section, key, min, max)` and also covers
`agent.session_start_timeout_secs`, `dashboard.loop_stall_exit_after_secs`,
`dashboard.chat_entry_cache_max_entries`, `dashboard.chat_entry_cache_max_bytes` and
`session.reconcile_max_kills`. Each min matches that field's runtime floor, so they
are not uniform (`agent.subagent_auto_max` floors at 3), and a legitimate in-range
value is never altered. `_clamp_security_bounds(data)` runs **once on the disk-read (cache-miss)
path, before the validated dict is cached** — so subsequent cache hits already
serve clamped values. It clamps out-of-range real integers in place (a JSON
`true`/`false` bool or any non-int is skipped and left to dataclass
coercion/defaults), logs a WARNING, and emits a best-effort `config_bounds_clamped`
SEL security event (never fatal — config loading must not raise).

Two **cross-field** clamps run after that generic pass, so both operands are
already in range:

- `agent.max_subagents`: 0 is the auto-size sentinel, so an explicit pin below
  `MAX_SUBAGENTS_FIXED_FLOOR` (3) is raised UP to the floor.
- `agent.tool_approval_timeout_secs` is pulled to `APPROVAL_TURN_MARGIN_SECS`
  (60) below `agent.chat_turn_timeout_secs`. An approval window that reaches the
  turn ceiling can never fire: the turn is cut first and reports itself as a turn
  timeout, so the unanswered approval is never named and an unattended run burns
  the whole ceiling on every prompt. `dashboard/turn_dispatch.py`
  `tool_approval_timeout_secs()` repeats the cap against the **resolved** ceiling,
  which the ACP prompt timeout can lower below the configured one, and then
  against the budget REMAINING in the running turn (`_TURN_DEADLINE`, published by
  `_bounded_turn`). The arm-time bound is the one that makes the invariant hold
  for a prompt arming late in a long turn; with under a margin left it returns
  `0.0` and the runner declines without waiting.

Why load-time (not just the API): the REST API rejects out-of-range writes, but a
direct edit of `config.json` (any process running as the same OS user — including
a prompt-injected agent with file-write access) bypassed that gate entirely. Each
knob controls a resource-consumption dimension (concurrent subagent processes,
per-subagent turn budget, pre-warmed pool processes), so an inflated on-disk value
could exhaust host memory/CPU/the process table (DoS). The dashboard write gate
(`dashboard/handlers/core_config_view.py`) and the runtime pool cap **import these same
constants**, so write-gate / load-clamp / runtime-cap cannot drift apart —
closing the direct-config-edit DoS gap.

### `resource_limits`: one block, three mechanisms, two meanings of `0`

`ResourceLimitsConfig` (`config/sections_limits.py`, re-exported by
`config/loader.py`) carries the kernel confinement ceilings for spawned agent
processes. It is the one config block whose keys are
read by more than one enforcement mechanism, and two of those keys mean
**different things** to two of them:

| Key | POSIX rlimit (`security.apply_resource_limits`) | cgroup v2 scope (`sandbox.cgroup_scope_argv`) | xdist (`resource_status`) |
|---|---|---|---|
| `max_open_files` | `RLIMIT_NOFILE`; `0` = leave inherited | — | — |
| `max_processes` | `RLIMIT_NPROC`; `0` = leave inherited | `TasksMax` (counts THREADS); `0` = use default | — |
| `max_memory_mb` | `RLIMIT_AS`; `0` = leave inherited | `MemoryMax`; `0` = use default | — |
| `max_cpu_seconds` | `RLIMIT_CPU`; `0` = leave inherited | — | — |
| `cpu_weight` | — | `CPUWeight`, 1..10000 | — |
| `max_cpu_percent` | — | `CPUQuota`, opt-in: unset emits no property | — |
| `max_total_memory_mb` | — | slice `MemoryMax` (all trees together) | — |
| `max_total_processes` | — | slice `TasksMax`; unset = host-sized default (`host_task_sizing.size_slice_tasks`) | — |
| `scope_memory_high_pct` | — | per-scope `MemoryHigh` as % of `MemoryMax`, 1..100; unset = 90; `0` = emit none | — |
| `slice_reclaim_trigger_pct` | — | slice page-cache reclaim trigger, % of slice `memory.high`; unset = `95`, `0` = off for this gateway only, `50..99` used, else `95`; never inside a pod | — |
| `xdist_auto_cap` | — | — | `-1` auto, `0` off, `N` fixed |

On Windows a fourth mechanism applies: a Job object attached to each agent spawn
(`sandbox.apply_windows_resource_ceiling`) enforces only `max_processes`
(`ActiveProcessLimit`) and `max_memory_mb` (`JobMemoryLimit`). The other keys
(`cpu_weight`, `max_cpu_percent`, `max_cpu_seconds`, `max_open_files`,
`max_total_memory_mb`, `max_total_processes`, `scope_memory_high_pct`,
`slice_reclaim_trigger_pct`) are accepted but not enforced there,
and one warning names the ones that are set
(`sandbox._WINDOWS_INERT_RESOURCE_LIMIT_KEYS`).

The slice task default is sized once per process from the host
(`host_task_sizing.host_task_facts`: affinity CPUs, RAM, `kernel.pid_max`,
`kernel.threads-max` and the user slice's `pids.max`). The slice gets 512 tasks
for every 512 MB of the slice `MemoryMax` it is applied with, plus one 8192-task
scope ceiling, so legitimate load reaches `MemoryMax` first. It is capped at half
the tightest host limit, and never set below 32768 unless that limit is itself
lower. A failed host read falls back to the fixed 32768. The per-scope default
stays a fixed 8192. `kirocrew doctor`'s
Task ceilings section prints the values, their source and the live slice usage;
it is advisory and never adds an issue.

On macOS no mechanism bounds the agent tree's memory: the cgroup path is a no-op,
and the session host's rlimit profile sets only `RLIMIT_NOFILE`, so `max_memory_mb`
has no effect there, set or unset (see
[`docs/decisions/2026-10-09-macos-has-no-agent-tree-memory-ceiling.md`](../../decisions/2026-10-09-macos-has-no-agent-tree-memory-ceiling.md)).

`0` cannot be normalised away in either direction. On the rlimit path it is a
documented request ("leave the inherited limit unchanged") with existing configs
behind it; on the cgroup path systemd **rejects** a zero property and the scope
never starts, so `0` there has to mean "use the module default" and the ceiling
is never left unset. Every field is therefore `int | None`, and `None` ("not
configured") stays distinct from `0`.

Defaults deliberately do NOT live in the dataclass. Each mechanism keeps its own
(`security._RLIMIT_DEFAULTS`, `sandbox._CGROUP_DEFAULT_*` /
`_default_max_memory_mb()`), because a copy here would be a third default set
that could drift from both.

**Single parse site.** `ResourceLimitsConfig.from_raw()` is the only code that
coerces these keys; `_limit_int` is its rule. One parse site is what keeps the
two meanings of `0` from drifting apart. The rule: bools are not numbers (`True` would become a 1-task
ceiling); a non-integral float truncates toward zero (`512.5` -> `512`, so a
stricter parse can never loosen a ceiling); a value in `(0, 1)` is REFUSED
because `int()` would turn it into the `0` that already means something else;
NaN and `±Infinity` (both producible by `json.loads`) are refused before `int()`
can raise on them; and an out-of-range value is refused rather than clamped, so
a confinement ceiling is never silently moved away from the number in the
operator's file. Every refusal is logged once per key per process.

`test_resource_limits_schema.py::TestSingleParseSite` fails if any other code
reads one of these keys.

### Dashboard theme persistence

`DashboardConfig.theme_mode` / `theme_color` / `translucent_panels` / `onboarded`
are workspace-persistent (shared across ports and devices) rather than
browser-local. The frontend reads them at boot via `GET /api/theme/boot`; empty
`theme_mode`/`theme_color` mean unset (the frontend falls back to `localStorage`
or the built-in default).

`translucent_panels` is the "Translucent panels" switch (Settings -> Display ->
View; the Liquid Glass primitive). It is a string like `theme_mode`, not a bool,
because `""` carries a meaning a bool cannot: "never chosen on this workspace".
The setting was browser-local (`mc-liquid-glass`) before it was server-backed,
and `localStorage` is per ORIGIN, so a new gateway port, `localhost` against
`127.0.0.1`, a second browser or the desktop app each started from an absent
key and read as "the glass keeps turning itself off". The browser copy stays as
the render cache (`index.html` reads it before hydration so the first paint
matches). At boot `useTheme` adopts `"on"` / `"off"` into the browser
(`adoptServerLiquidGlass` in `website/src/utils/liquidGlass.ts`); on `""` it
leaves the browser copy alone and, when that copy says on, publishes `"on"` once
through `PUT /api/config/theme`, so an existing user's choice survives the
upgrade and reaches their other browsers. An absent (off) browser copy is never
published on `""`: it is also what a fresh browser holds. `useLiquidGlass`
publishes every flip of the switch (and the first-run seed) the same way. The
loader (`_translucent_panels_from`) reads anything outside
`TRANSLUCENT_PANELS_VALUES` (`"", "on", "off"`) as `""`, and the PUT refuses it
(400), so a bool written by hand never persists as a choice. Adopting an
explicit value also writes the browser's `mc-liquid-glass-seeded` marker, so the
first-run "Pick your look" seed (which turns the glass on for a fresh install)
never runs over a workspace that already holds a choice. The change event the
hooks follow carries the new value itself, not a pointer to the store: with
site storage blocked the cache write fails silently, and a listener that re-read
the store would answer the solid default and undo the flip. A failed workspace
write is not surfaced, the same as a failed theme-mode write: the browser copy
holds for the session and nothing the user can act on has happened.

### Sidebar folder order

`DashboardConfig.folder_sort` is the chat sidebar's folder sort mode — `custom`
(the stored per-container `order` positions set by dragging or by the
`chat_folder_move` tool; the default, so an upgrade changes nothing), `name` (an
ASCII-case-insensitive natural order, so `01.` < `02.` < `10.` and `alpha` < `Beta`;
only `A`-`Z` fold, every other letter compares as written, because that is the one
fold both readers perform identically without a Unicode table) or `created` (newest first
on the `created_at` epoch stamp every folder creator writes). It is workspace-
persistent rather than browser-local because two readers must agree on it: the
sidebar (and the folder pickers) through the shared `GET /api/config/kirocrew`
query, and the `kirocrew-dashboard` MCP server's `chat_folder_tree`, which reads the
same `config.json` through the loader (the HTTP route is cookie-only) and lists
folders in the order the sidebar draws them so an agent can pick a `before`/`after`
anchor from it (see `docs/architecture/mcp.md`). Written only through the
`PATCH /api/config/kirocrew` allowlist (`dashboard.folder_sort`, enum
`FOLDER_SORT_MODES`); the loader reads anything outside that set as `custom`.
Choosing a mode is a VIEW change — no folder's stored `order` is rewritten — so
switching back to `custom` restores the manual arrangement exactly; the sidebar
re-sorts when the save lands (the success write into the shared `kirocrewConfig`
cache), not on the pick, so every reader of that cache switches together. A sidebar
drag among siblings is a write to the stored positions computed against the drawn
order, so it is offered only when the mode is known to be `custom`: outside that
(and while the settings query is still loading or has failed, when the tree draws
the stored order as a fallback) the folder rows stop being reorder targets — their
sortable's droppable side is off, so no slot opens — while dragging a folder into
another still works; before the FIRST read lands no folder drag is offered at all
(both sortable sides off, no grab cursor), since nothing on screen could yet say
why a lift died at the drop. The status line that answers a withdrawn drop carries
a **Switch to Custom** action on the same write path as the menu row. What the UI
keys on is what it KNOWS, never the transient query status (`useFolderSortRead`):
the mode is known when a config body is on hand — fresh, cached, or kept across a
failed background refetch, which react-query retries on its own and which is
therefore silent; a read that failed with no body to fall back on is said on an
`ErrorNotice`, held through the retry's pending phase (`errorUpdatedAt`, so an
observer mounted mid-retry reports it too) so the banner does not unmount and
remount around each automatic retry, and cleared when a body arrives.
One screen says it once: the sidebar's banner over its tree (the plain title leads,
the server's own words sit under it as a smaller line -- they stay the notice's
`message` because that string is the error-journal key the hand-off reads -- then
a plain subline and the hand-off stacked under the text), and, on the screens with
no sidebar, the job form beside its folder picker and the Command Bar above its
folder list. The subline is ONE phrase wherever this failure is said -- *All
folders are shown, in your Custom order; retries automatically* -- because a person
may see it on up to four surfaces at once and two phrasings read as two failures,
and it says that membership is intact, since a picker under a failure notice was
not trusted to still list every folder; where the
notice carries no hand-off of its own (the job form and the Command Bar: unsaved
input beside them) it adds *Open the chat sidebar to ask the agent about it.* The
session menu
and the folder-suggestion card say nothing of their own while the sidebar is on the
screen; when it is not (a phone with the drawer closed, a desktop with the panel
collapsed, embed chat) they say it themselves — the menu in the rule's in-menu form
(passive notice, the picker's subline, a sibling **Ask the agent** item described by
the notice, a separator closing the block), the card as the same notice above it
without a hand-off, with the picker-plus-pointer subline.

The **Folder order** rows (*Custom* / *By name* / *By date created (newest first)* -- the
direction spelled, as the session rows spell theirs, and worded so
that no label mirrors the chat session sort rows in the same menu, whose heading
names its object, **Sort sessions by**, because two orderings in one menu read as
sorting twice; and *Custom*, not *Custom order*, under a heading that already says
"order") are offered in every sidebar lane. The flat lane draws no folder tree and the conductor lane nests
by lineage, but the mode is not idle there: every row menu's **Move to folder**
picker, the history search's folder groups, the Command Bar, the job form and the
MCP tree all list in it, and the menu rows are the only control that writes it -- a
mode a person cannot change from the lane they are in would be a trap. Only the drag
note under the rows (*Folders can be dragged into place in Custom order only*, a
fact about the modes -- not the sidebar hint's "Switch to Custom" sentence, which
sits beside a button that does the switching and would read as an inert action
here) is confined to the lanes that draw a folder row to drag (the tree, and the
board unless flat view empties its columns of folders). In `created` mode a second
fact line joins it whenever a folder in the list has no `created_at` -- a folder
from before the stamp existed -- because the comparator puts such rows after every
stamped one, in the stored order: on a pre-upgrade tree that is the order the person
already had, and the pick looks broken unless the menu says why (*Folders made
before dates were recorded have no date; they come last, in your Custom order*);
`chat_folder_tree` states the same fact in its header for the agent reading the
tree, so neither reader takes that stored-order tail for a date order. The hint itself stays until
the person's next interaction away from it -- a pointer or key landing anywhere but
on the line -- a switch back to Custom, or the next drag; never a clock, which took
the action away from under a hand reaching for it. The mode's saves go out ONE at a
time, in pick order (react-query mutation `scope`): two picks inside one round-trip
would be two concurrent `PATCH`es to the same path, and the server persists
whichever arrives last -- a delayed first request would land after the second and
store the earlier pick, and the settle-time refetch would then draw that order as
if chosen. Queued behind an in-flight save, the newer pick's request starts when
the previous one settles, so the last pick is both the last request the server sees
and the persisted one; a refusal behind a newer pick is not reported (the newer
save's own outcome is).

### Sidebar session order

`DashboardConfig.session_sort` is the chat sidebar's session sort key: `date-desc`
(last settled activity, newest first; the default) or `date-asc`, `created-desc` /
`created-asc` (the slot's creation time) and `name-asc` / `name-desc` (the title).
It is stored here rather than in the browser so a pick follows the person to every
browser and the desktop app. Written only through the `PATCH /api/config/kirocrew`
allowlist (`dashboard.session_sort`, enum `SESSION_SORT_MODES`). A carry-over PATCH
sets `if_unset: true`: the handler checks raw key presence under the config write
lock and returns 409 with code `already_set`, without writing, if any value is
stored. Menu picks remain unconditional. The loader and the sidebar's reader both
read anything outside the enum as `date-desc`. Unlike the folder order, the sidebar
re-sorts on the pick, through an optimistic overlay on the shared `kirocrewConfig`
cache, because nothing outside the dashboard reads this key; a refused save puts
the stored key back and says why. Pinned rows sort first under every key, in their
own manual order. Earlier builds kept the choice in the browser's local storage
(`mc-session-sort`): once the config body is read, the sidebar carries a valid
non-default browser choice only while the gateway stores no value for the key.
A choice made in another browser, the default included, is never overwritten,
even if it lands between this browser's read and write. The local key is removed
when skipped, saved, or refused as already stored; any other carry-over failure
keeps it for the next load. Settle-time invalidation draws the stored value.

`manual` is the person's own order. The order itself is not config: it lives in
the pinned order's store (`dashboard/pinned_session_order.py`), as a second file,
`chat_manual_order.json`, in the same fenced `pinned-order` directory, and reaches
the browser on every slot row as `manual_rank` / `manual_rev`. Under `manual`,
pinned rows stay on top in their own order, unpinned rows show a drag handle in
the row's left pad, outside the text column, so the agent name, title and folder
name keep one left edge, there are no date headers, and a session with no place
(a new one) shows above the placed ones. A drop between two unpinned rows of one container, in any sort, switches
this key to `manual`, writes the order, and offers "Sorted by hand" with Undo;
under `manual`, Alt+Arrow on a focused unpinned row moves it the same way. A drop
on a folder files the session and changes neither. Every write is one operation of `POST /api/chat/manual-order`:
the first drag (or picking Manual with nothing saved) seeds the order on screen
and lands only while nothing is saved; a later drag moves one row next to
another; and the undo of a first drag clears the order only at the revision that
drag wrote. Undo, or a drop whose write does not take, also puts the previous
sort back. Picking another sort keeps the order, so picking Manual again
restores it. Only the person's own browser
writes it: the one writer refuses a caller whose origin is not `dashboard` (an
agent or crew member, which reaches the gateway with `X-Internal-Secret`) and an
app or member principal; no MCP tool writes it. A build without `manual` reads
the key as `date-desc` and never touches the second file.

### Interactive model picker visibility

`DashboardConfig.model_picker_hidden_models` is a workspace-persistent list of
model IDs hidden from interactive chat model pickers. The default is `[]`, which
shows the full advertised list. The loader accepts only string arrays, trims and
deduplicates entries, and ignores empty strings and `auto`. The dashboard PUT
endpoint applies the shared model-ID grammar and a bounded list length. Changes
apply to ChatPage and ChatPane without a restart; they do not alter `/api/models`,
entitlement, defaults, role or fallback models, bulk switching, crew editors, or
app-specific selectors. `model_picker_configured` records the first successful
visibility save and is read-only through the dashboard API; the same atomic write
that replaces the hidden list sets it. Existing configurations with a non-empty,
valid hidden list migrate to configured, while an empty or invalid legacy value
does not dismiss the first-use shortcut.

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

A cross-tab `storage` event is also an explicit user choice. Once one arrives,
`LanguageProvider` refuses to adopt the older `/api/theme/boot` response that may
still be in flight, so the UI, local mirror, and workspace write cannot diverge
because of response ordering.

The picker's Auto row is labelled plain **"Auto"**, not "Auto (follow browser)".
The desktop app has no browser preference to follow — its locale comes from the
OS — so naming the browser was wrong on that surface. The row annotates itself
with the language Auto actually resolves to ("Auto — Deutsch"), which answers the
question accurately on every surface.

The backend's **write path** validates **shape only** (`_LANGUAGE_TAG_RE`, a
conservative BCP-47 subset), not membership in the set of shipped catalogs — a
well-formed tag with no catalog stays writable and falls back to detection
client-side. The **agent-injection read path** additionally requires catalog
membership: `context.ui_language_tag()` checks the tag against
`context._UI_LANGUAGE_CATALOGS` (a mirror of the non-dev-only
`SUPPORTED_LANGUAGES` entries) and treats a non-catalog tag exactly like
`""`/Auto — no `[UI LANGUAGE]` steer is emitted, so the agent is never steered
to a language the chrome cannot render (#1130). Adding a language is therefore
the frontend edits [website/docs/i18n-catalog.md](../../../website/docs/i18n-catalog.md)
owns **plus one mechanical backend entry** in `_UI_LANGUAGE_CATALOGS`, which the drift gate in
`test/test_context_ui_language.py` names explicitly on failure.

Shipped catalogs (ordered by global speaker count, which is also the picker
order): `en`, `zh-CN`, `hi`, `es`, `fr`, `bn`, `pt`, `ru`, `de`, `ja`, `ko`, `it`. Right-to-left
languages are deliberately **not** shipped yet: the catalogs would translate
fine, but the dashboard's layout uses physical-direction utilities (`pl-*`,
`left-*`, `text-left`) and unmirrored directional icons, so an RTL locale would
render correct text in a visibly wrong shell. RTL requires `dir="rtl"` plus a
logical-property conversion first.

English ships with the boot bundle and every other catalog loads lazily, one chunk
per language: `website/src/i18n/lazy.ts` holds one dynamic import per authored
catalog (`AUTHORED_LOADERS`), and `main.tsx` awaits `ensureCatalog` for the active
language before the first render (bounded, so a stalled chunk renders English), so
`t()` stays synchronous. `scripts/check-bundle-size.mjs` budgets each language's
chunk separately (`CATALOG_CHUNK_BUDGETS`).

#### The tag reaches the agent, too

`context_assembly/output_contract.py::_build_ui_language_section` injects the configured tag — after the
catalog-membership gate described above — into session
context as a `[UI LANGUAGE] <tag>` block (next to `[CURRENT AGENT]`/`[RUNTIME]`,
and in `minimal_context` mode as well). It exists for one string: the tool-call
purpose, which the dashboard paints as the tool-call pill label and the
messaging renderers reuse as the task title. Which field carries that string
depends on the harness — the Kiro backend's reserved `__tool_use_purpose`
argument, or the `description` field other backends' shell tool takes beside
`command` (`acp/_dispatch_parts/surfaced_text.py::select_tool_title` reads it first) — so the block
names both; a model on the second kind never sees a field called "purpose" and
would otherwise miss the steer. That is the only piece
of model-generated prose rendered as *chrome*, and without the block the model
has nothing to go on and mirrors the language the user typed in — an inferred
signal that flips mid-session the moment the user pastes an English stack trace,
and one that persists, since purposes are stored in session history.

Reading it back off the wire matches by **shape**, not by a list of literals.
kiro-cli injects the `__tool_use_purpose` property into every tool schema it
exposes, and echoes it back in `rawInput` as either that name or a camelCased
`__toolUsePurpose` — but nothing validates the key, and the model paraphrases
it: `__purpose`, `__thinking_purpose` and `__woohoo_purpose` all appear in real
transcripts. `acp/_dispatch_parts/surfaced_text.py::extract_tool_purpose` prefers the canonical
spellings in `acp/types.py::TOOL_PURPOSE_KEYS`, then accepts any *reserved*
(dunder-prefixed) key whose name ends in `purpose`
(`acp/_dispatch_parts/surfaced_text.py::is_tool_purpose_key`), scanned in sorted order so the reading is
deterministic. It is the single reader for both transports; matching literals
drops the purpose for every paraphrased spelling, and the concise pill silently
falls back to the raw command line while the unrecognized key leaks into the
arguments view as if it were a real parameter. The dunder prefix is what keeps a
tool's own functional `purpose` argument out of the match.
`website/src/utils/toolPurpose.ts` is the frontend mirror, used by the
pending-approval preview and the Mochi approval bubble.

Three properties are load-bearing:

- **`""` injects nothing.** Auto is resolved client-side by `detect.ts`; the
  backend does not know the outcome, so there is no truthful value to inject and
  un-configured installs keep byte-identical context.
- **The raw tag is injected, not a display name.** A backend code→name table
  would be a second list to keep in sync with `SUPPORTED_LANGUAGES` and would
  degrade to the tag for anything missing from it regardless. Raw is not
  unchecked: the builder re-validates the shape (`_UI_LANGUAGE_TAG_RE`, a
  superset-safe local mirror of `_LANGUAGE_TAG_RE`) and drops anything that is
  not tag-shaped. `PUT /api/config/theme` is not the only way a value reaches
  the field — the loader coerces whatever the JSON holds into `str`, so a
  hand-edited `"language": null` arrives as the literal `"None"` — and a value
  that lands in the system prompt should not depend on its writer having
  validated it.
- **Scope is the purpose text only.** The block says so explicitly, because
  widening it would collide with the base prompt's rule to reply in the user's
  language.

It is best-effort steering with no enforcement path: nothing validates the
language a model actually emits.

#### The tag also names the session

Auto-titling (`dashboard/chat_title.py`) asks a background model for the session
name that renders in the chat sidebar, and that name is chrome by the same
argument as the tool-call purpose above: the date group headers, filter labels and
rename menu around it are all in the UI language, and the name is *persisted*, so
one written in the conversation's language leaves two languages on the row for
good. With no directive the model simply mirrors the language of the prompt it was
given, so a fully Chinese conversation gets an English-language title.

The tag reaches the titler through the **prompt**, not the `[UI LANGUAGE]` block:
titling runs on the shared `_bg` session, and that block scopes itself explicitly
to tool-call purpose text. `chat_title._ui_language()` resolves the same tag
through the shared `context.ui_language_tag()`, and `_build_title_prompt()`
interpolates a directive into the prompt's `{language}` slot — outside the
delimited transcript, so a message that quotes the directive cannot restate it.
`""` omits the slot entirely and the prompt stays byte-identical to the one
auto-language workspaces have always sent.

Two consequences fall out of naming in a non-latin script:

- **The prose guard needs a second ceiling.** `label_guard.looks_like_prose`
  (shared by every label path -- the session title, the Slack/Telegram
  conversation name, the nav link chips and the session summary -- and reached
  from `chat_title` through its `_looks_like_prose` alias) rejects a reply that
  is a sentence rather than a name, and its word ceiling counts `str.split()`
  tokens — which is 1 for any length of Chinese, Japanese or Thai.
  `TITLE_MAX_UNSPACED_CHARS` bounds those scripts by character instead, counting
  only unspaced-script characters so latin identifiers in a mixed title stay
  free, and the full-width terminators `。！？` are matched without the ASCII
  rule's trailing-whitespace requirement (those scripts do not space after
  punctuation). Both ceilings are parameters, because the prompts differ: the
  title defaults (12 words / 24 characters) sit above its 3-6 word contract,
  and the 18-word session summary passes its own.
  Korean is spaced, so the word ceiling bounds its long sentences, but a SHORT
  Korean refusal clears every other check -- and Korean puts the refusal verb
  last, so English-style prefix openers cannot catch it. `looks_like_prose`
  therefore also matches Korean sentence shape: the sentence-final polite
  conjugations (`KO_SENTENCE_ENDINGS`, the formal "-nida" family and the
  informal-polite "-yo" family) plus the apology opener (`KO_PROSE_OPENERS`),
  which a title as a noun phrase never carries. A plain-form (banmal) Korean
  refusal remains a documented false negative, and a sentence-form Korean
  title loses to the fallback name -- the deliberate direction of the trade,
  since a fallback name is still the user's own words while a stored refusal
  is the bug. The sentence-shape signals (terminators, Korean conjugation) and
  the narration openers (`PROSE_OPENERS`) are parameters too, for the one path
  whose label IS a descriptive sentence: the session summary runs the guard
  with `sentence_shape=False` and without the conversation-referring openers,
  since "the conversation covers ..." is its legitimate shape and a false
  positive there is re-spent on a model turn at every later list.
  Every other shipped language is covered by one language-neutral rule,
  `label_guard.ends_like_sentence`: a reply that ENDS in a sentence terminator
  of any script (`.` `!` `?`, the danda `।`, the Arabic `؟`, `。` and kin) and
  holds a comma of any script or `SENTENCE_MIN_WORDS` (4) words is a sentence,
  not a name -- the prompts ask for no punctuation, and a name never closes a
  clause. A name of three words or fewer with a stray final period stays a
  name. The danda and the other spaced terminators also count mid-reply, like
  the ASCII ones, so a two-sentence Hindi or Bengali refusal is caught. Paths
  that strip a final period before checking (`chat_title._validate_title_reply`,
  `messaging/auto_title.clean_title`) read this shape from the reply line
  before the strip; the session summary runs with `sentence_shape=False` and
  skips it. A short refusal with NO terminator in a language other than
  English or Korean is the remaining documented false negative: without the
  terminator it has a name's shape.
- **The reveal animation needs characters.** The sidebar types a new title in one
  word at a time; a single-token title skipped the animation entirely, so
  `_title_reveal_prefixes` steps unspaced scripts two characters at a time
  instead, landing in the same step count as an equivalent latin title. Every cut is extended to the end of the grapheme cluster it lands in (`_grapheme_end`), so a frame never shows half a Thai tone mark, flag, skin-tone or ZWJ emoji sequence.

`_clean_title` strips the full-width and CJK quote/period forms (`「」`, `“”`,
`。`) alongside the ASCII ones, since that is what a zh/ja reply wraps a name in.
It also keeps the reply's first line only -- the rule
`messaging/auto_title.clean_title` states as "Keeps the first line only" -- so a
`SKIP` verdict followed by a reason collapses back to the bare control word.
`_validate_title_reply` treats BOTH taught control words (`SKIP`, `KEEP`) as
no-title sentinels on every path, matched case-insensitively, alone or with a
punctuation-separated reason on one line (`label_guard.is_verdict_reply`, the
same check the messaging namer and the session summary run against their own
taught word) -- while a real title that merely opens with the word ("SKIP and
KEEP handling", "KEEP-ALIVE header bug") survives.

#### The tag also writes a crewmate's hidden turns

A crewmate's first welcome and its goal check-ins are hidden gateway turns
(`mate_welcome.dispatch_hidden_turn`) with no user text to follow, and the
`[UI LANGUAGE]` block scopes itself to tool-call purpose text, so without a
tag named in the prompt they come back in English. Both preambles
(`welcome.preamble`, `wake.preamble`) carry one language sentence,
`hidden_turn.language_default` ("`[UI LANGUAGE]` when it is shown, else English"),
and `mate_welcome.with_language` swaps it in place for one naming the tag, so a
prompt always holds exactly one language directive. The tag comes from
`mate_welcome.hidden_turn_language()`: the configured tag through
`context.ui_language_tag()`, else -- for the welcome only -- the opening tab's
`X-UI-Lang` header (`activeLocale()`, sent by `api.memberGreet`), validated by
`normalize_ui_language_tag` and used for that request only, never persisted.
`en` and an unusable tag swap nothing, so an English install's prompts stay
byte-identical. The welcome is told to write in the tag
(`hidden_turn.language_first`); a check-in runs in a thread the user has written
in, so it is told to follow the user's language there, falling back to the tag
(`hidden_turn.language_thread_or_tag`). A check-in has no tab behind it, so on a
"follow the browser" install it keeps the preamble's sentence.

### Response verbosity reaches every agent

`dashboard.verbosity` describes how the PERSON wants replies to read, so it is
delivered as session-context chrome — the same class as `[CURRENT DATE]` and
`[UI LANGUAGE]` — not as a token an agent prompt has to opt into.
`context_assembly/sections.py::_build_response_preferences_section(cfg)` renders the level's
rules (`_reply_style_rules`) inside a `[RESPONSE PREFERENCES — MANDATORY]` …
`[END RESPONSE PREFERENCES]` frame whose one sentence of preamble states that the
rules bind every reply, on every surface, for every agent, and outrank any
response-style guidance in the agent prompt. `default` and any unknown value
render `""`, so an install that never touched the setting sees byte-identical
context.

`build_message` mints the frame as its own trusted part on every session-start
turn (full, `minimal_context` cron, and slim resume), placed AFTER the scrubbed
session-context block rather than inside it, so a built-in agent, a custom
agent spec and a cron digest all receive it at the same once-per-session cost.
The placement is load-bearing: both frame markers are in
`_STRUCTURAL_MARKER_RES`, so a forged frame inside memory, channel history or a
peer's message is rewritten to `[marker-removed]`, and a frame that rode inside
the scrubbed block would be rewritten too. The genuine frame is therefore
minted only after the scrub, exactly as the post-compaction skills index is.

A `subagent:` session is the one kind that does NOT receive the frame
(`_response_preferences_apply`, resolved through the same runtime-source seam
as `[RUNTIME]`): its final message is read by its parent agent, which needs the
caveats and edge cases the `answer_only` level tells the writer to
drop.

Session-start context is what compaction drops, so `build_message` re-injects
the block on a continuing turn with `needs_reinjection` set, beside the skills
index, under a `[REINJECTED AFTER COMPACTION — response preferences]` line. It
re-reads the setting at that moment: a level changed mid-session is what comes
back, not the pre-compaction copy. The messaging pipeline
(`messaging/dispatch.py`) consumes and forwards that one-shot flag the way the
dashboard chat runner does, so a channel session's compaction re-injects the
skills index and this block.

There is no `{{VERBOSITY_BLOCK}}` prompt token. No shipped prompt (`config/prompt.md`, the
conductor/worker prompt constants in `agent_parts/spec_prompts.py`) carries the token, and `test/test_verbosity_config.py` pins that;
`_resolve_prompt_templates` still strips a stale token from a spec copied before
the move so the literal never reaches the model. `context_blocks._MARKERS` knows
the frame as `response_preferences`, so the context-breakdown panel attributes
its bytes to their own block rather than to `[UI LANGUAGE]`.

`{{WIDGET_BLOCK}}` deliberately stays a prompt token. `dashboard.widget_density`
is not a preference about the person; it describes what the rendering surface
can show, and `_resolve_prompt_templates` already gates the block on
`has_dashboard_surface(session_key)`, so a session with no chat window gets
none of it whatever the prompt says. There is no author-diligence gap to close.

`{{COMPUTER_USE_BLOCK}}` is a prompt token for the same reason: it describes
what the session's agent spec mounts, not a preference. `_resolve_prompt_templates`
fills it from `agent._computer_use_spec_gate()`, read when the session's backend
spawns (session start and resume); see
[computer-use.md](computer-use.md).

`{{BROWSER_BEGIN}}` and `{{BROWSER_END}}` mark the Browser section in place for
the same reason: whether a session can browse is a property of the host. The
section stays while `browser_cli.install.available()` holds or cannot be read,
and becomes a short pointer while it does not, read at session start; see
[browser.md](browser.md#capability-model).

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
Kiro Crew value on collision, and reports unsupported or secret-bearing source
settings without copying them. Foreign credentials, security policy,
approval/sandbox settings, agent/runtime state, hooks, and arbitrary unknown
config sections cannot enter configuration through this path.

### Meet Mates first-run state

`DashboardConfig.crewmates_onboarded` is a legacy completion record of the
retired guided create flow. Nothing reads it to decide what to show: entering
Mates opens Mate's chat or the remembered one, and creating a mate
happens on the New mate card (see [crew-mode](crew-mode.md)), which neither
reads nor writes the flag. `GET /api/theme/boot` and `PUT /api/config/theme`
keep carrying it for compatibility; the dashboard neither reads nor writes it.

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
| `KIROCREW_PORT` | Operator-selected dashboard port; also persisted by service install | `5476` |
| `KIROCREW_BOUND_PORT` | Gateway-observed bound port exported to child processes after listen | Unset before bind |
| `KIROCREW_BOUND_HOST` | Gateway-exported bound host family, inherited by children: absent for a `127.0.0.1`/`0.0.0.0` bind, `::1` for a `::`/`::1` bind, the address itself for a specific-interface bind (`dashboard/server_runtime/listener.py`); app backends read it to pick their origin host, and pods drop it | Unset before bind |
| `KIROCREW_BIND` | Explicit IP bind-address override for containers/orchestrators | `127.0.0.1` in the public build |
| `KIROCREW_CORS_ORIGINS` | Comma-separated additional browser origins accepted by CSRF/WebSocket checks | Empty |
| `KIROCREW_WORKSPACE` | Override workspace root directory | Platform-dependent |
| `KIROCREW_PROJECT_DIR` | Override agent config/skills directory | Auto-detected |

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
    "history_max_days": 365,
    "persistence_enabled": true,
    "inject_memory": true,
    "inject_lessons": true,
    "inject_activity": true
  },
  "knowledge": {
    "auto_add_documents": false,
    "auto_ingest_artifacts": false,
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

The `dashboard.url` field supplies the browser-facing/reverse-proxy origin and,
when present, a candidate port; its origin is added to the CSRF/WebSocket allowlist.
It does **not** widen the TCP bind in the public build, which remains
`127.0.0.1` unless the operator explicitly sets `KIROCREW_BIND` to an IP address
(for example inside a container). When omitted, the dashboard defaults to
`localhost:5476`.

A **malformed** `dashboard.url` (e.g. an unterminated IPv6 literal `http://[::1` or a non-numeric port `http://host:notaport`) does **not** abort startup: `parse_dashboard_url` degrades to the defaults (`""` host, port `5476`) and logs a warning, so a single typo in the config can never take the gateway down on boot. `KIROCREW_PORT` still overrides the port regardless.

The moment the dashboard's port is **reserved** (bound and listening, not yet
accepting — before any app backend spawns), the gateway **exports the
actually-bound port as `KIROCREW_BOUND_PORT`** into its own environment, so
every child it spawns (kiro-cli sessions and their MCP stdio servers) inherits
the truth instead of re-deriving a guess from `dashboard.url` — a portless URL
would otherwise collapse to the default port in the child even when the
gateway is bound elsewhere (including `--port auto`, where the OS assigns the
port and no config field ever names it). It is a **distinct variable from
`KIROCREW_PORT`** on purpose: `KIROCREW_PORT` means operator intent and is
persisted by `service_environment()` into unit files, while
`KIROCREW_BOUND_PORT` is ephemeral observed truth that must never be frozen
into persistent config. Clients read it via `port_resolution.resolve_client_port`,
one precedence step below the operator override.

## Model Resolution Chain

Default-model precedence is owned by [model-selection](../common/model-selection.md),
and `loader.resolve_effective_model` is its single implementation: crew agent
model, then the bound kiro agent's pin, then `agent.model`, then
`KiroCrewConfig._resolve_agent_model()` (the installed `kirocrew.json`, then the
bundled `config/defaults.json`, then `DEFAULT_MODEL`, the `auto` sentinel that the
ACP client leaves off the wire).

## Load-time Error Handling

Every in-tree reader of `config.json` and `config.local.json` reads the live file
through `loader.read_config_text`, which drops a leading UTF-8 BOM an editor may
add; readers of copies (the pod seed and export bundles) and `agent._load_json` use
`user_json.strip_utf8_bom` directly. Writers emit plain UTF-8, so a locked write
removes the mark. `test/test_config_json_bom.py` walks the source by AST and
allows a direct read of the live files only inside `read_config_text`.

These rules apply to `KiroCrewConfig.load()`; mutation helpers fail closed on a
corrupt existing document as described above.

- Missing file → defaults
- Invalid JSON, or a document that is not a JSON object → defaults (warning
  logged), with the base file marked unreadable and its sections degraded (see the
  degraded-sections rules above)
- Missing fields → individual defaults: each is the default declared on its DTO
  field, apart from the departures listed under "Defaults come from the DTO fields"
  above
- An enum value outside its set → that field's default, with a logged line
- An out-of-range number → clamped (see "Security-Bounded Config Clamp")

### Editable string lists (`str_list`)

`PATCH /api/config/kirocrew` accepts a `str_list` entry of `_EDITABLE_CONFIG` as a JSON list
whose items are all strings, and nothing else (a scalar, `null`, an object or a mixed list is a
400 `must be a list of strings`, with nothing written). Every `str_list` entry declares
`max_items` and `max_item_len`, and a longer list or a longer item is a 400 (`must be at most
<n> items`, `each item must be at most <n> characters`), as every `str` entry has a `max_len`;
`skills.extra_paths` allows 64 items of up to 4096 characters. A refusal records only the key
as its SEL resource, never the value. Within the bounds the check is shape only: `[]`, empty
items from a non-UI caller, duplicates, relative and non-existent paths are stored as sent, in
order. `skills.extra_paths` is the one such entry. It is written through the normal locked
writer, and the `SkillsLoader` watcher applies it live (no restart): the loader's own
screening (expansion, resolution, sensitive-path exclusion, existence) stays the only judge of
a path. Settings → Skills trims each line and drops blank ones before it sends the list; the
same list is what `kirocrew config set skills.extra_paths --value-base64=…` stores.
`dashboard.change_cards.check_setting_value` has the same `str_list` branch and bounds, so a card fails up
front rather than falling through (no card can carry the list; see crew-mode).

### Default context discovery

`skills.max_triggered=0` disables per-message trigger injection, not discovery.
The default entry (`lazy_load=true`) is a bounded usage-ranked index carrying each
skill's path, and one line naming the families it leaves out. An install last
written by 0.6.x or earlier has its materialized `false` removed once on upgrade
(see "The legacy `skills.lazy_load` rewrite"). `lazy_load=false`
selects the shorter entry: up to eight names (up to six of them the user's own
skills, the rest the highest-ranked remaining skills) with short purposes plus
`skill_search` guidance for short keywords. An agent with its own `skill://`
mapping gets neither -- those skills arrive as complete instructions. Both preserve pinned instructions, confined project-body
limits and explicit loading. Thread history scales with the model window
independently of the fixed old-activity allowance; no additional config switches
are introduced. The model window also derives the non-configurable protected-content
safety ceiling: `max(3 * 33,000, floor(window_tokens * 4.0 * 0.125))` characters.
Crossing it omits complete lesson entries first and reports their count; preferences,
safety rules, and date/runtime identity stay whole. This does not change the fixed
33,000-character optional-content allowance.
