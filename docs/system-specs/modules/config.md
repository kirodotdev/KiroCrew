# Config Module

## Overview

Foreign-agent onboarding is gated independently by `dashboard.import_onboarded`,
migrated from the older `dashboard.onboarded`, and the settings it projects are
merged strictly (merge-only, never a wholesale replace). The loader preserves legacy
numeric strings and integral floats already present in a config file, while rejecting
booleans and non-integral, malformed, or non-finite values. Imported settings are
type-validated before they are written, and the CLI converts typed values before
writing.

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

**Repository-controlled uninstall contract.** Every uninstall path owned by this
repository preserves the KiroCrew data home by default. `kirocrew service
uninstall` removes only its service definition; the Python/npm packages define
no uninstall lifecycle hook; and the desktop shell's generated NSIS uninstaller
removes only its install directory and shortcuts (`deleteAppDataOnUninstall`
stays false), without
resolving or removing the KiroCrew home. App Kit uninstall also preserves the
app's `data/` subtree unless the dedicated `purge_data=true` API action (CLI
`--purge-data`, or an explicit dashboard choice) is supplied. The API checks
for the literal boolean `true`; absent, legacy, or malformed values fail closed
to preservation. A whole-home purge is never coupled to uninstall.

**Uninstaller consideration (external dependency).** Because the data home now
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

**Paths are resolved per call, never captured at import.** Because
`config_dir()` re-reads `$KIROCREW_HOME` on every call and the migration above
is deliberately lazy, the resolved value is only correct at the moment it is
needed. Modules therefore MUST NOT bind a path factory result to a module-level
constant:

```python
_SOME_DIR = config_dir() / "some"        # WRONG -- frozen at import
```

An import-time binding captures whatever home was active when the module was
first imported, which breaks three things at once: pod isolation (a pod exports
its own `KIROCREW_HOME`), the one-time legacy-home migration (resolved after
import), and test isolation — `conftest.py`'s autouse `_isolate_kirocrew_home`
fixture runs *after* collection has already imported the module under test, so
it cannot reach a frozen constant. That last hole let a local test run write
2128 fixture rows into an operator's real usage store.

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
newly added factory is covered without editing the test. Issue #874.

**`config_dir()` maintains; `data_home()` only resolves.** `config_dir()` is
*resolve + maintain*: besides resolving the home it `mkdir`s it, refreshes the
`~/.kiro-crew-location` recovery breadcrumb (a stat + a read) and re-runs
`_sweep_ungated_archive_leftovers()`, which can `shutil.rmtree` a leftover
archive from an earlier release. That work belongs to process start —
`ensure_data_home()` is the startup hook — and the distinction did not matter
while callers froze the result in a module constant, because the maintenance
then ran exactly once, at import.

Resolving per call makes it load-bearing: a request handler would otherwise
perform a destructive sweep **on the event loop** as a side effect of asking
where a directory is. So the accessors above call **`data_home()`**:

| branch | behaviour |
| --- | --- |
| a **valid** `KIROCREW_HOME` override | delegates to `config_dir()` every call, so an override set *after* import is honoured. That branch performs neither the breadcrumb refresh nor the sweep — only a cheap `mkdir`. |
| default home already resolved | returns the cached `_resolved_home` directly — no `mkdir`, no breadcrumb, no sweep. |
| not yet resolved | delegates to `config_dir()`, so the **first** resolution in a process still migrates, creates the home and sweeps once. |

The first row tests `_valid_override_home()` — the **same predicate `config_dir()`
gates on**, not merely "is the env var set". An override naming a system
directory (`/`, `/usr`, …) is rejected there and resolution falls through to the
default home, so gating on the raw env var would send every call down the
maintenance path and put the destructive sweep back on the request path for
anyone with a bad override. The two predicates must not drift apart; a regression
test pins both directions.

That last row is what keeps the sweep's documented contract intact: it specifies
"a leftover created between two starts … is still caught on the **next start**".
The sweep is specified per *start*; running it per *call* was the mechanism, not
the requirement. `data_home()` keeps no cache of its own — the override branch
must stay live, and the cached branch reads the same `_resolved_home` that
`config_dir()` populates, so there is one source of truth for the location.

Existing direct `config_dir()` callers are unchanged and keep the maintenance
behaviour, including 25 pre-existing calls that already sit inside async
handlers.

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

### `resolve_agent_bindings(config, agent_name=None) -> ResolvedBindings`
Resolves the workspace, memory store and **kiro agent** a session runs under.
Resolution order:

1. `agent_name` is a key in `config.agents` — use that alias's bindings.
2. `agent_name` is a **materialized kiro agent config** — a `~/.kiro/agents/*.json`
   whose **declared `name`** matches (the filename stem only when the config
   declares no name) — take the *default* alias's workspace/memory bindings but
   dispatch **that agent itself**. `kiro-cli agent list` enumerates agents by
   declared name, so a namespaced filename stem such as `mochi--mochi` is NOT a
   name kiro-cli can resolve and must not be treated as dispatchable.
3. otherwise `config.default_agent`, then the first available alias, then bare
   defaults.

Rung 2 exists because an app's agents are materialized into `~/.kiro/agents/` by
`bridges._register_agents` under a namespaced FILENAME (`<app>--<agent>.json`)
while the config inside keeps the app's own bare `name`, and **nothing adds them
to `config.agents`** — that mapping is authored by setup / the user. Without it an
app-bound session fell through to `default_agent` and the DEFAULT agent answered
while the slot still advertised the requested name, with none of the app's MCP
tools. The rung is deliberately wider than app agents: **any** parseable config in
that directory dispatches with default bindings, because the directory *is* the
kiro-cli agent registry and narrowing to app-registered names would require
provenance it does not record.

`ResolvedBindings` additionally reports `requested_resolved` (whether the
requested name was honored — False means the default answered) and
`resolved_alias` (the alias key whose bindings were used). Callers that store a
name must store `resolved_alias`, never `kiro_agent`: the stored value is
re-resolved later with aliases matched FIRST, so a physical agent name that also
happens to be an alias key would dispatch that alias's target instead.

### Materialized-agent snapshot (`config/loader.py`)
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
  JSON object.
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

Known follow-up (#1429): the snapshot makes this module a second home for agent
discovery beside `apps/registry`, and `_resolve_named_agent_model` below still
reads that directory without the sensitive-path gate.

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

**`write_config_atomically(path, data, *, fsync=False)` is atomic AND
mode-preserving.** Atomic (tmp+rename) so no reader ever sees a partial file —
this is what closes the torn-read window for everyone else. Mode-preserving
because tmp+rename creates a NEW inode, so the umask default (typically `0644`)
would silently replace an operator's tightened `0600`; `config.json` can hold
inline credentials, so a settings write must never widen who can read it. An
existing file's mode carries over and a newly created one is owner-only. It
deliberately does NOT call `platform_compat.restrict_to_owner`: that helper shells
out to `icacls` on Windows, and this function runs inside async request handlers
and `save()`, so calling it would put a blocking subprocess on the gateway event
loop (`no-blocking-call-on-event-loop`). Omitting it is no worse than the
truncate-then-write it replaced, which applied no DACL either.

**Mode preservation is POSIX-only.** `atomic_write`'s `mode` routes through
`fchmod_safe`, a documented no-op on Windows, where access is carried by the DACL
instead. Applying one would mean an `icacls` subprocess, which this function must
not run (above) — so on Windows the replacement file inherits the directory's ACL,
exactly as the `write_text` it replaced did. The three mode/symlink tests in
`test_config_rmw_preserves_settings.py` are `skipif(not IS_POSIX)` for this reason;
the data-loss and AST-guard tests are platform-independent and run everywhere.

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
    sandbox: str = "auto"          # default "auto" (namespace on Linux, seatbelt on macOS; delegates to kiro-cli's internal sandbox on macOS when enabled); "off" skips Kiro Crew's sandbox
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
    auto_add_documents: bool = True                     # agent adds documents it reads (aggregate "Auto-added" source); legacy spelling auto_ingest_doc_links accepted
    auto_register_project_docs: bool = True             # register each worked-in project's documents as a folder source (document filter only)
    auto_ingest_chunk_budget: int = 150                 # chunks per sweep for auto-registered sources; 0 = unbounded
    folder_ingest_chunk_budget: int = 300               # chunks per sweep for hand-added folder sources; per-source chunk_budget overrides; 0 = unbounded
    dedup_every_n_sweeps: int = 12                      # full dedup pass cadence; 0 disables
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
class ComputerUseConfig:
    # DISPLAY + LIMITS ONLY. There is deliberately NO `enabled` field — see the
    # note under "Computer use: no enabled field here" below.
    max_tree_nodes: int = 1200          # accessibility-tree node budget per snapshot
    max_tree_depth: int = 64            # depth budget (the walk is iterative, so this is a cost bound)
    text_limit: int = 500               # per-element text truncation (chars)
    attach_screenshot: bool = True      # default for the `screenshot` tool param
    screenshot_max_px: int = 1280       # longest-edge downscale (NOT browse's 1920 — the tree is the primary channel)
    screenshot_jpeg_quality: int = 55   # JPEG quality (NOT browse's 70); 1280/q55 measured at ~8.3K tokens vs 41K for a raw PNG

@dataclass
class MessagingConfig:
    use_transport: bool = True     # route inbound Slack through SlackTransport → TurnDriver → SlackRenderer (the canonical path); false falls back to the native handle_message monolith

@dataclass
class SkillsConfig:
    max_triggered: int = 0         # max skills loaded per message (>=0)
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
    verbosity: str = "default"     # "default" | "concise" | "ultra"; "concise" injects a brevity guideline block into the agent prompt ({{VERBOSITY_BLOCK}}), "ultra" injects a stricter punchline-first block (answer within a ~3-sentence opening, then scannable detail). Read/written via GET/PUT /api/dashboard/config (rejects values other than default|concise|ultra). Resolved for all transports in ContextBuilder._resolve_prompt_templates; an unrecognized value injects an empty block.
    theme_mode: str = ""           # "dark" | "light" | "system"; empty = unset (frontend falls back to localStorage or "system")
    theme_color: str = ""          # color-theme slug (e.g. "kiro", "emerald", "monokai"); empty = unset
    language: str = ""             # dashboard UI language, BCP-47 (e.g. "en", "zh-CN"); empty = auto-detect from the browser. See "Dashboard UI language" below.
    onboarded: bool = False         # whether the "Choose your look" onboarding modal was completed
    import_onboarded: bool = False  # whether foreign-agent import was completed or skipped
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
    computer_use: ComputerUseConfig
    hooks_data: dict               # raw hooks from config.json
    dashboard_url: str = ""        # e.g. "http://my-host.example.com:8080"
    auto_update: bool = True
    snapshot_dir: str = ""         # snapshot output dir (default ~/.kiro/crew/snapshots)
    slack_channels: dict[str, ChannelConfig]  # per-channel config keyed by channel ID
    slack_dm_activation: str = "always"       # activation mode for DMs (D-prefix channels)
```

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

The absence is deliberate and the precedent is `denied_commands.json`:
`is_sensitive_write_path("~/.kiro/crew/config.json")` is `True` (the *tool* path is
protected), but `is_sensitive_bash_command("echo x > ~/.kiro/crew/config.json")` is
`None` — `_WRITE_PROTECTED_BASH_LEAVES` is `('.data-home-ready',)` only. A config
toggle would therefore be flippable by a prompt-injected agent through any shell
redirect.

- **`enabled`** — the primary enable for full desktop observation plus input
  synthesis. A security ceiling, so it goes where the agent can neither read nor
  write it. Read with a strict `is True` identity test, so a truthy string such as
  `"enabled": "false"` does **not** enable desktop control, and the read fails soft
  to `{}` → **off**.
- **`allowed_apps` / `extra_denied_apps`** — the operator's own narrowing. These
  are the ONLY other keys `PolicyConfig.from_state` reads.

**There is no `allow_pointer_move` key, and writing one has no effect.** An earlier
revision documented it here as a second consent switch for `click_method: "global"`
(the one path that warps the real mouse pointer), gated together with a
`capabilities.computer_use_pointer` governance row. Both were removed by product
decision: there are no `computer_use.*` governance scopes at all, and
`from_state` reads only the three keys above, so a hand-written
`{"enabled": true, "allow_pointer_move": false}` silently grants the pointer path —
the operator would believe they had withheld consent. What actually contains that
path is that the model must NAME the method (`auto` never resolves onto it) and every
use is SEL-audited under its own `tool_kind`. Do not re-document the flag without
re-implementing it. See [security.md](security.md), [governance.md](governance.md)
and [computer-use.md](computer-use.md).

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

The picker's Auto row is labelled plain **"Auto"**, not "Auto (follow browser)".
The desktop app has no browser preference to follow — its locale comes from the
OS — so naming the browser was wrong on that surface. The row annotates itself
with the language Auto actually resolves to ("Auto — Deutsch"), which answers the
question accurately on every surface.

The backend validates **shape only** (`_LANGUAGE_TAG_RE`, a conservative BCP-47
subset), not membership in the set of shipped catalogs. That keeps "which
languages exist" a pure frontend data change: add `locales/<tag>.json`, register
the picker entry in `SUPPORTED_LANGUAGES`, and add the static import plus
`AUTHORED_CATALOGS` entry in `i18n/index.ts`. No backend edit is required; a
well-formed tag with no catalog falls back to detection client-side.

Shipped catalogs (ordered by global speaker count, which is also the picker
order): `en`, `zh-CN`, `hi`, `es`, `fr`, `bn`, `pt`, `ru`, `de`, `ja`, `ko`, `it`. Right-to-left
languages are deliberately **not** shipped yet: the catalogs would translate
fine, but the dashboard's layout uses physical-direction utilities (`pl-*`,
`left-*`, `text-left`) and unmirrored directional icons, so an RTL locale would
render correct text in a visibly wrong shell. RTL requires `dir="rtl"` plus a
logical-property conversion first.

All catalogs are **statically bundled**, so `t()` stays synchronous (see the
rationale in `website/src/i18n/index.ts`). The cost is that every user downloads
every language: at 8592 keys the catalogs share one chunk that is **~173 KB gzip
per catalog, ~2.0 MB gzip for the twelve combined** (`npm run analyze`, then gzip
the `assets/t-*.js` chunk). This is tolerable only because the dashboard is served
from a loopback gateway — over a network it is already past the point of
justification, and each further catalog adds another ~173 KB to every user's first
load regardless of the language they read.

The documented next step is therefore to keep `en` static and lazily fetch the
active non-English catalog. That seam is already isolated to
`website/src/i18n/index.ts` plus a `<Suspense>` boundary in `main.tsx`; no call
site changes. **Catalog #13 belongs behind that seam**: Korean is #12 and the last
one this chunk absorbs in front of it. Re-measure when the seam lands — the figure
above is what says whether it worked.

#### The tag reaches the agent, too

`context.py::_build_ui_language_section` injects the configured tag into session
context as a `[UI LANGUAGE] <tag>` block (next to `[CURRENT AGENT]`/`[RUNTIME]`,
and in `minimal_context` mode as well). It exists for one string: the tool-call
purpose (`__tool_use_purpose`), which the dashboard paints as the tool-call pill
label and the messaging renderers reuse as the task title. That is the only piece
of model-generated prose rendered as *chrome*, and without the block the model
has nothing to go on and mirrors the language the user typed in — an inferred
signal that flips mid-session the moment the user pastes an English stack trace,
and one that persists, since purposes are stored in session history.

Reading it back off the wire matches by **shape**, not by a list of literals.
kiro-cli injects the `__tool_use_purpose` property into every tool schema it
exposes, and echoes it back in `rawInput` as either that name or a camelCased
`__toolUsePurpose` — but nothing validates the key, and the model paraphrases
it: `__purpose`, `__thinking_purpose` and `__woohoo_purpose` all appear in real
transcripts. `acp/_dispatch.py::extract_tool_purpose` prefers the canonical
spellings in `acp/types.py::TOOL_PURPOSE_KEYS`, then accepts any *reserved*
(dunder-prefixed) key whose name ends in `purpose`
(`_dispatch.py::is_tool_purpose_key`), scanned in sorted order so the reading is
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
given — measured on `claude-haiku-4.5`, a fully Chinese conversation is named
"Chat Title Language Mismatch".

The tag reaches the titler through the **prompt**, not the `[UI LANGUAGE]` block:
titling runs on the shared `_bg` session, and that block scopes itself explicitly
to tool-call purpose text. `chat_title._ui_language()` resolves the same tag
through the shared `context.ui_language_tag()`, and `_build_title_prompt()`
interpolates a directive into the prompt's `{language}` slot — outside the
delimited transcript, so a message that quotes the directive cannot restate it.
`""` omits the slot entirely and the prompt stays byte-identical to the one
auto-language workspaces have always sent.

Two consequences fall out of naming in a non-latin script:

- **The prose guard needs a second ceiling.** `_looks_like_prose` rejects a reply
  that is a sentence rather than a name, and its word ceiling counts
  `str.split()` tokens — which is 1 for any length of Chinese, Japanese or Thai.
  `_TITLE_MAX_UNSPACED_CHARS` bounds those scripts by character instead, counting
  only unspaced-script characters so latin identifiers in a mixed title stay
  free, and the full-width terminators `。！？` are matched without the ASCII
  rule's trailing-whitespace requirement (those scripts do not space after
  punctuation). A short refusal with no terminator remains a documented false
  negative.
- **The reveal animation needs characters.** The sidebar types a new title in one
  word at a time; a single-token title skipped the animation entirely, so
  `_title_reveal_prefixes` steps unspaced scripts two characters at a time
  instead, landing in the same step count as an equivalent latin title.

`_clean_title` strips the full-width and CJK quote/period forms (`「」`, `“”`,
`。`) alongside the ASCII ones, since that is what a zh/ja reply wraps a name in.

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
    "auto_add_documents": true,
    "auto_register_project_docs": true,
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
