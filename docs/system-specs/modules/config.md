# Config Module

## Overview

Foreign-agent onboarding is gated independently by `dashboard.import_onboarded`,
migrated from the older `dashboard.onboarded`, and the settings it projects are
merged strictly (merge-only, never a wholesale replace). The loader preserves legacy
numeric strings and integral floats already present in a config file, while rejecting
booleans and non-integral, malformed, or non-finite values. Imported settings are
type-validated before they are written, and the CLI converts typed values before
writing.

The config package loads runtime configuration from `~/.kiro/crew/config.json`
using stdlib dataclasses with sensible defaults. Responsibilities are split in
one direction: `config/sections.py` owns section DTOs, field defaults, and their
coercion/normalization rules; `config/resolution.py` owns raw overlay merging,
top-level section classification, and degraded-input tracking; and
`config/loader.py` owns the compatibility facade plus persistence, validation
orchestration, cache fingerprinting, migration, and runtime binding resolution.
`loader.py` re-exports the historical DTO, helper, and constant names so existing
callers keep the same import surface.

A feature whose section spends tokens on the user's behalf defaults to off and
documents its knobs in its own spec — `session_summary` is the current example
(see [session-summary.md](session-summary.md)), following the shape
`SkillsConfig` established: every field carries `_meta` label/help for the config
surfaces, out-of-range values are clamped with a warning rather than raising, and
a malformed section degrades to defaults so a hand-edited file cannot prevent the
gateway from starting.

## Data Home Location

KiroCrew's data root nests **under kiro-cli's own `~/.kiro/` base** so all
Kiro-family apps share a single directory a user can secure. `config_dir()`
(in `kiro_crew/config/paths.py`, re-exported from `kiro_crew/config/loader.py`)
is the single accessor and resolves to:

1. `$KIROCREW_HOME` when set (used as-is; refuses system directories like `/`,
   `/usr`, `/System`, `/etc`), else
2. `~/.kiro/crew` (the default).

**No migration — net-new users only.** All supported installs start directly in
`~/.kiro/crew`; there is no `~/.kirocrew` to relocate, so `config_dir()` simply
resolves and `mkdir`s the home above. The one-time `~/.kirocrew` → `~/.kiro/crew`
data-home migration that earlier releases carried has been **removed** (see
`docs/system-specs/post-launch-removals.md`). A leftover top-level `~/.kirocrew`
from an old install is never read, migrated, or deleted; it is left in place —
still credential-gated by the `.kirocrew` security-path spelling — and `kirocrew
doctor` reports it, warning rather than advising deletion when it still holds a
virtual environment (`venv`/`.venv`/`venvs`), since that may be the running
interpreter.

**Repository-controlled uninstall contract.** Every uninstall path owned by this
repository preserves the KiroCrew data home by default. `kirocrew service
uninstall` removes only its service definition; the Python/npm packages define
no uninstall lifecycle hook; and the desktop shell's generated NSIS uninstaller
removes only installed program state: its install directory, shortcuts,
channel-scoped updater cache, and any legacy “start with Windows” registry entry
(`deleteAppDataOnUninstall` stays false), without
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
first imported, which breaks two things at once: pod isolation (a pod exports
its own `KIROCREW_HOME`) and test isolation — `conftest.py`'s autouse `_isolate_kirocrew_home`
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

## Superseded Defaults (reported, never rewritten)

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

Registered so far: `mcp_gateway.forward_declared_env` (False -> True, #4566),
`session.autocompact_pct` (90.0 -> 70.0, #4388), `stt.streaming` (False -> True,
0.5.0), `stt.model` ("turbo" -> "base", 0.5.0),
`dashboard.loop_stall_exit_after_secs` (25 -> unset, #6651) and
`instances.warm_set_cap` (5 -> 0, #7248).

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

Two surfaces render it, and neither writes:

- The load path emits **one** warning naming every drifted key plus the command to
  resolve it, evaluated on the **stored base document before the
  `config.local.json` merge** -- an overlay value is the operator's live choice and
  says nothing about what the base materialized, so a base drift is still reported
  when an overlay masks it, and an overlay-only value is not reported at all. One
  line rather than one per key: the registry is append-only, so a per-key line
  grows without bound on exactly the long-lived installs with the most real drift,
  and it lands on every short-lived `kirocrew` invocation, where the
  once-per-process guard buys nothing because there the process IS the invocation.
  The per-key text is still emitted at debug, so `-vv` keeps it in the log.
- `kirocrew doctor` prints a `Stored Defaults` section reading `config.json`
  directly. Drift is informational and does NOT become an issue; an unreadable or
  malformed config does.

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
  since it was listed is left alone;
- `--keep [KEY...]` records the stored values as intentional, which suppresses the
  load-path line for exactly those values.

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
  config-then-ack at the only site that nests them.

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

One entry today: `stt.provider`, whose retired and unknown values degrade to
`local`. The `is_coerced` predicate rides on the ENTRY, not in the detector's loop,
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
Leaf helper returning `~/.kiro/agents` — the **user-level** scope. Lives in the leaf
module so `loader.py` (and `_resolve_named_agent_model`'s `agents_dir` DI seam) can
locate installed agent JSONs without importing `kiro_crew.agent` — which imports
`config.loader` and would create an import cycle.

Deliberately **single-valued**: it is the WRITE target as well as a read scope
(`bridges._register_agents` and `agent.rebuild_agent_config` both write here), so it
is never widened into a search path.

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

1. `agent_name` is a key in `config.agents` — use that alias's bindings.
2. `agent_name` is a **materialized kiro agent config** — a `*.json` under
   `~/.kiro/agents/` or, when `project_dir` is given, under
   `<project>/.kiro/agents/` — whose **declared `name`** matches (the filename stem
   only when the config declares no name) — take the *default* alias's
   workspace/memory bindings but dispatch **that agent itself**. `kiro-cli agent
   list` enumerates agents by declared name, so a namespaced filename stem such as
   `mochi--mochi` is NOT a name kiro-cli can resolve and must not be treated as
   dispatchable.
3. otherwise `config.default_agent`, then the first available alias, then bare
   defaults.

Rung 2 exists because an app's agents are materialized into `~/.kiro/agents/` by
`bridges._register_agents` under a namespaced FILENAME (`<app>--<agent>.json`)
while the config inside keeps the app's own bare `name`, and **nothing adds them
to `config.agents`** — that mapping is authored by setup / the user. Without it an
app-bound session fell through to `default_agent` and the DEFAULT agent answered
while the slot still advertised the requested name, with none of the app's MCP
tools. The rung is deliberately wider than app agents: **any** parseable config in
those directories dispatches with default bindings, because they *are* the
kiro-cli agent registry and narrowing to app-registered names would require
provenance they do not record.

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
the on-loop read is a hit. `chat_runner._run_chat` and the side-turn handler both do
this.

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

**Only the warm is offloaded — never `resolve_agent_bindings` itself.** The resolver
can raise `StopIteration` (its defensive `next(iter(config.agents))` branch on a
malformed config), and `StopIteration` cannot be delivered through a `Future`:
asyncio rejects it, so an awaiting caller hangs instead of seeing the error, and the
`except Exception` that callers rely on never runs. Keeping resolution synchronous
preserves its exception contract for every call site.

`ResolvedBindings` additionally reports `requested_resolved` (whether the
requested name was honored — False means the default answered) and
`resolved_alias` (the alias key whose bindings were used). Callers that store a
name must store `resolved_alias`, never `kiro_agent`: the stored value is
re-resolved later with aliases matched FIRST, so a physical agent name that also
happens to be an alias key would dispatch that alias's target instead.

#### App-slot cold-snapshot self-heal & fail-loud (`dashboard/chat_runner._run_chat`)
The one-turn cold fallback above is acceptable for an ordinary session (the next
turn self-heals), but it is **not** acceptable for an **app-owned** slot
(`slot._app` truthy — an App-Kit slot bound to an app's own kiro agent, e.g.
`my-app-agent`). An app agent is never in `config.agents` and is resolvable
*only* through the materialized snapshot, so a cold on-loop read makes
`resolve_agent_bindings` fall back to the default agent with
`requested_resolved=False`. The result is silent: the slot still advertises the
requested name while the generic default agent answers **with none of the app's
MCP tools and no error**, leaving the app unusable until a gateway restart happens
to re-warm the snapshot. `_run_chat` therefore guards the resolve, strictly behind
`slot._app and not bindings.requested_resolved` (zero extra work / I/O on the
common hot path — no `_app`, or already resolved):

1. **Self-heal (two escalating steps).** First, **rescan** the snapshot **off the
   loop** with the same pattern `server.py` uses at boot —
   `await loop.run_in_executor(subprocess_executor(), refresh_materialized_agents)`
   (`refresh_materialized_agents` never raises, so awaiting it via the executor is
   safe) — then **re-resolve once**. This recovers an app slot whose spec is on
   disk but whose snapshot was simply not yet warmed on this loop. If the
   re-resolve *still* misses, the spec was never materialized even though the
   source is intact, so **re-register this app's resources from source** —
   `await loop.run_in_executor(subprocess_executor(), register_app, slot._app)`
   (`register_app`, `apps/bridges.py`, registers the app's MCP servers BEFORE its
   agents and publishes the snapshot synchronously; imported via a **local** import
   inside the function to avoid the top-level `apps`↔`dashboard` cycle, mirroring
   `server.py`'s local import of `reconcile_enabled_app_resources`. `register_app`
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
   handlers surfaces as a normal `error` card (no `record_failure` — nothing ran).
   The raise happens *before* `get_or_create`, while no session lock is held, so
   the standard `finally` teardown runs without ever creating a session or
   dispatching an agent.

The eager-spawn pre-warm path mirrors the **self-heal** step (rescan →
re-register-from-source) only (so the speculative session bakes in the app's own
agent rather than the default, which a first real turn would otherwise have to
discard); the **fail-loud** lives on the real turn alone, since the eager path is
best-effort and tears itself down on any miss.

`register_app` (`apps/bridges.py`) backs the from-source recovery with a **visible
error**: when a manifest declares agents but `_register_agents` materializes none
(source missing or unreadable) it appends a `"registered 0 of N declared
agent(s)"` entry to `result.errors` — which `reconcile_enabled_app_resources`
counts and logs — instead of returning a silent 0-agent success; a partial
registration (some but not all) logs a warning.

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

### Effective-agent report (`resolve_effective_agent`)
`resolve_agent_bindings` stores the REQUESTED agent verbatim and only logs when
nothing dispatches it, because rewriting the stored name was destructive: the
resolution behind the rewrite can be momentarily stale while the overwrite is
permanent. `resolve_effective_agent(agent_name, project_dir)` is the
non-destructive other half — it names the agent that will actually answer, and
`""` for "nothing to report".

Two properties, both pinned by tests:

- **No filesystem I/O**, for the same reason rung 2 has none: it is called from
  `_ChatSlot.to_dict()` for every slots frame on the event loop. It reads only the
  materialized snapshot, the alias snapshot published by `KiroCrewConfig.load()`
  (`publish_agent_alias_snapshot`), and `cached_project_agent_names` — never a
  scan, stat or config re-read.
- **Fails closed to `""`.** A cold alias snapshot, a cold materialized snapshot
  and a cold project cache all report no divergence. A false "your agent was
  substituted" marker sends the user chasing a substitution that never happened,
  so silence during a boot window is the correct answer, not a guess.

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
existing file's mode carries over and a newly created one is owner-only. On
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
  A **managed** spec's `model` is set on every refresh to the shipped default,
  or to the `"auto"` sentinel when the shipped template pins none — never left
  as-is. That is what makes the global `agent.model` reversible: the global is
  propagated into the spec when it is a concrete pick, and because a spec pin
  outranks the global in `resolve_effective_model`, returning the global to
  `"auto"` must take the pin back off or `"auto"` is unreachable from the
  configuration surface. Ownership decides who may clear: `model_managed=false`
  (an explicit user pick) and an **absent** sidecar entry (legacy status, owner
  unknown) both keep their pin untouched.
- `migrate_agent_specs()` runs at startup (top of `rebuild_agent_config`): lifts
  the keys out of every `~/.kiro/agents/*.json` into the sidecar and removes
  them (idempotent), fixing installs polluted by older builds.
- The dashboard model PATCH writes the sidecar, never the spec; agent DELETE
  prunes the sidecar entry.
- `agent_state.lift_and_strip_bookkeeping()` is the single shared
  implementation of the lift/strip/no-clobber rule above (with a type guard —
  a non-`bool` `model_managed` or non-`str` `cc_model` is stripped but never
  lifted, since coercing it could silently flip its meaning). All four spec
  writers call it — the dashboard's whole-config `PUT /api/agent/config`
  handler, the per-agent `PATCH /api/agent/<name>` handler,
  `migrate_agent_specs()`, and `_refresh_dynamic_fields()` — so none of them
  can drift from the other three.

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
    chat_turn_timeout_secs: int = 7200  # wall-clock ceiling for one chat turn. Load-time clamped to [300, 86400]; the ACP prompt wait follows it (resolve_prompt_timeout)
    tool_approval_timeout_secs: int = 600  # how long a chat turn waits for a human to answer a tool-approval prompt. Load-time clamped to [30, 7200] AND to 60s below chat_turn_timeout_secs

@dataclass
class SessionConfig:
    timeout_secs: int = 3600       # 60 min idle timeout (DEFAULT_SESSION_TIMEOUT)
    empty_response_auto_continue: bool = True  # after TWO consecutive empty model responses, auto-send ONE synthetic "continue" nudge on the same live session (transcript-visible notice; bounded to once per user message; the config gate fails OPEN to the default so a config-load hiccup cannot disable self-healing). See session.md "Empty-response recovery ladder".
    autocompact_pct: float = 70.0  # context usage % at which auto-compaction triggers (DEFAULT_AUTOCOMPACT_PCT). Load-time clamped to [5.0, 90.0] (one constant pair shared with the dashboard write gate)
    pool_size: int = 0             # pre-warmed kiro-cli processes kept ready for instant session start; 0 (the default) disables. Single source of truth: DEFAULT_POOL_SIZE, read by both the field default and load()'s file-parse fallback. Load-time clamped to [0, 10]
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
    auto_add_documents: bool = False                    # opt-in; agent adds documents it reads (aggregate "Auto-added" source); legacy spelling auto_ingest_doc_links accepted
    folder_ingest_chunk_budget: int = 300               # chunks per sweep for a folder source; per-source chunk_budget overrides; 0 = unbounded
    dedup_every_n_sweeps: int = 12                      # full dedup pass cadence; 0 disables
    auto_ingest_artifacts: bool = False                 # opt-in; ingest local artifacts into the KB (aggregate "Artifacts" source)
    auto_ingest_artifact_kinds: list[str] = ["markdown", "text", "html", "json"]  # reader-extractable kinds (widget/svg excluded)
    embed_timeout_secs: float = 10.0                    # per-request embed timeout; 0/unset -> built-in TIMEOUT (10s)
    embed_content_budget: int = 0                       # chunk-content fold budget (chars); 0/unset -> built-in _EMBED_CONTENT_BUDGET

@dataclass
class ChannelConfig:
    activation: str = "mention"    # "always", "mention", "observe", or "off"
    agent: str = ""                # per-channel agent override (empty = use default)

@dataclass
class SttConfig:
    enabled: bool = True           # on by default: the default provider needs no account
    provider: str = "local"        # "local" | "apple" | "transcribe"; a retired value degrades to "local"
    model: str = "base"            # a kiro_crew.stt.models CATALOG name; a superseded name resolves via its alias table
    language_code: str = "en-US"
    streaming: bool = True         # live partials; every provider produces them
    silence_ms: int = 700          # end-of-phrase pause; clamped to _STT_INTERVAL_MS_MIN.._MAX
    partial_interval_ms: int = 400 # live-transcript refresh cadence; same clamp
    idle_evict_secs: int = 600     # release the resident local model after this idle; 0 = at end of recording
    endpointing: bool = False      # semantic auto-submit on a complete utterance; needs streaming
    dictation_panel: bool = True   # animated recording panel; falls back to the status bar
    timeout_secs: int = 300
    transcribe_region: str = "us-east-1"   # transcribe provider only
    transcribe_profile: str = ""           # transcribe provider only; empty = default credential chain

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

# Additional top-level DTOs (not fully expanded here — see sections.py):
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

### Per-crew avatar override (`agents.*.avatar`)

`KiroCrewAgentConfig.avatar` is a sparse override, exactly like the per-crew
`model` and `session_color` fields: absent or `{}` means the face is derived from
the crew's name (zero migration). `_safe_avatar` (`config/sections.py`) is the
total coercer applied on load, in the create/update endpoints, and nowhere else,
and it is deliberately NOT re-exported from `loader.py` — the loader's
`from kiro_crew.config.sections import (...)` list is a frozen pre-split snapshot
(`test_config_module_boundaries`), so post-split internals are reached through the
`sections` module. Two accepted shapes:

- `{"kind": "ghost", "traits": {eyes, brows, mouth, accessory, prop: str; blush,
  flip: bool; tile: "#rrggbb"}}` — string traits are truncated to 32 chars and
  are NOT checked against the frontend's trait vocabulary (the renderer resolves
  an unknown option to "absent", so a new hat needs no backend release);
  booleans must be real JSON booleans (`bool("false")` is `True`, so a
  string-typed value is read as `False`); `tile` is the one pinned value — it is
  interpolated into SVG markup, so it goes through the same `#rrggbb` validator
  as `session_color`. An all-empty trait set collapses to `{}` (the one canonical
  "reset" spelling) rather than storing a featureless third state.
- `{"kind": "image", "v": <int>, "file": "<16-hex>.<png|jpg|webp>"}` — the crew
  wears an uploaded picture served from `GET /api/agents/{name}/avatar`; the
  file itself lives under `<data home>/run/avatars/` and the record only marks
  the choice. `v` (a positive real int; `True` is rejected) is the cache-busting
  mtime stamp the frontend appends as `?v=`; `file` pins the exact committed,
  content-addressed variant and must match `^[0-9a-f]{16}\.(png|jpg|webp)$`.
  Wire-only keys (`promote`, `token`) never reach the record.

Anything else — a non-dict, an unknown `kind`, a ghost override without a
`traits` dict — collapses to `{}` on load (config.json is hand-editable and
agent-writable, so junk must never crash the load), while the endpoints answer a
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

The absence is deliberate and the precedent is `denied_commands.json`:
`is_sensitive_write_path("~/.kiro/crew/config.json")` is `True` (the *tool* path is
protected), but `is_sensitive_bash_command("echo x > ~/.kiro/crew/config.json")` is
`None` — `config.json` is not among `_WRITE_PROTECTED_BASH_LEAVES` (which fences
only a few specific control files elsewhere under the home). A config
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

Resource-limit and timeout knobs are clamped to hard ceilings **at load time**, not
just at the dashboard write gate. The ceilings are owned beside the field models
in `sections.py` and re-exported by `loader.py`; the load-time clamp remains in
`loader.py`:

| Constant | Value | Field |
|----------|-------|-------|
| `SUBAGENT_AUTO_MAX_CEILING` | 64 | `agent.subagent_auto_max`, `agent.max_subagents` |
| `SUBAGENT_MAX_TURNS_CEILING` | 200 | `agent.subagent_max_turns` |
| `POOL_SIZE_MAX` | 10 | `session.pool_size` |
| `CHAT_TURN_TIMEOUT_MIN` / `_MAX` | 300 / 86400 | `agent.chat_turn_timeout_secs` |
| `TOOL_APPROVAL_TIMEOUT_MIN` / `_MAX` | 30 / 7200 | `agent.tool_approval_timeout_secs` |

`_SECURITY_BOUNDED_FIELDS` lists each `(section, key, min, max)`; the mins match
the existing runtime floors (0/1) so a legitimate in-range value is never
altered. `_clamp_security_bounds(data)` runs **once on the disk-read (cache-miss)
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
(`dashboard/handlers/core.py`) and the runtime pool cap **import these same
constants**, so write-gate / load-clamp / runtime-cap cannot drift apart —
closing the direct-config-edit DoS gap.

### `resource_limits`: one block, three mechanisms, two meanings of `0`

`ResourceLimitsConfig` (`config/sections.py`, re-exported by
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
| `max_total_processes` | — | slice `TasksMax` | — |
| `xdist_auto_cap` | — | — | `-1` auto, `0` off, `N` fixed |

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
coerces these keys; `_limit_int` is its rule. Before #3474 six readers each had
their own, which is how the two meanings of `0` drifted apart with nothing
recording it. The rule: bools are not numbers (`True` would become a 1-task
ceiling); a non-integral float truncates toward zero (`512.5` -> `512`, so a
stricter parse can never loosen a ceiling); a value in `(0, 1)` is REFUSED
because `int()` would turn it into the `0` that already means something else;
NaN and `±Infinity` (both producible by `json.loads`) are refused before `int()`
can raise on them; and an out-of-range value is refused rather than clamped, so
a confinement ceiling is never silently moved away from the number in the
operator's file. Every refusal is logged once per key per process.

`test_resource_limits_schema.py::TestSingleParseSite` fails if a seventh reader
appears.

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
the three frontend edits — add `locales/<tag>.json`, register the picker entry
in `SUPPORTED_LANGUAGES`, and add the static import plus `AUTHORED_CATALOGS`
entry in `i18n/catalogs.ts` — **plus one mechanical backend entry** in
`_UI_LANGUAGE_CATALOGS`, which the drift gate in
`test/test_context_ui_language.py` names explicitly on failure.

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
`website/src/i18n/catalogs.ts` — the module that owns every catalog import — plus
a `<Suspense>` boundary in `main.tsx`; no call site changes, and
`registerCatalogs()` is where a fetching backend hands its catalog over.
**Catalog #13 belongs behind that seam**: Korean is #12 and the last one this
chunk absorbs in front of it. Re-measure when the seam lands — the figure above
is what says whether it worked.

#### The tag reaches the agent, too

`context.py::_build_ui_language_section` injects the configured tag — after the
catalog-membership gate described above — into session
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

The `dashboard.url` field controls where the dashboard is reachable. From it, the system derives the port to bind on, the bind address (`0.0.0.0` for non-loopback hosts, `127.0.0.1` otherwise), and the allowed origins for CSRF/WebSocket checks. When omitted, defaults to `localhost:5476`.

A **malformed** `dashboard.url` (e.g. an unterminated IPv6 literal `http://[::1` or a non-numeric port `http://host:notaport`) does **not** abort startup: `parse_dashboard_url` degrades to the defaults (`""` host, port `5476`) and logs a warning, so a single typo in the config can never take the gateway down on boot. `KIROCREW_PORT` still overrides the port regardless.

Once the dashboard's TCP site is listening, the gateway **exports the
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

When `agent.model` is `"auto"` (default):

1. `~/.kiro/agents/kirocrew.json` → `model` field (installed agent config)
2. `config_package_dir()/defaults.json` → `model` field (bundled `src/kiro_crew/config/defaults.json`)
3. Falls back to `DEFAULT_MODEL` (passed through to provider)

## Error Handling

- Missing file → defaults
- Invalid JSON → defaults (warning logged)
- Missing fields → individual defaults
