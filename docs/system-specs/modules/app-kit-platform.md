# App Kit platform contracts (agents, MCP scoping, window entries)

Everything here is **generic App Kit surface**, not one app's arrangement: each
item is what the FIRST app to need it exposed, and every later app builds on the
same contract. The manifest field reference lives in
[../../app-kit/manifest-reference.md](../../app-kit/manifest-reference.md), and
the publish-facing policy in
[../../app-kit/publishing-guide.md](../../app-kit/publishing-guide.md); this
document is the behaviour and the one-way doors.

## 0. Three-axis classification: origin, resources, lifecycle

An installed app's `installed.json` carries three **independent** fields, each
answering one question. They exist as three because a single "managed" value
conflated them, and the valid combinations it could not express (a registry app
whose resources the app itself registers; a self-registered app that wants
gateway-managed symlinks) are ordinary cases.

| Field | Values | Question it answers |
|---|---|---|
| `origin` | `builtin`, `registry`, `local`, `external` | How the app was acquired. Effectively read-only after install. |
| `resources` | `gateway`, `app` | Who registers agents/skills/SOPs/MCP/crons. |
| `lifecycle` | `gateway`, `app`, `locked` | Who owns updates and uninstall. |

`origin` is a categorical enum; `source` beside it is free-form concrete
provenance (a filesystem path, `registry:<name>`, or the literal `builtin`).
`origin` drives behavioural branching, `source` drives display and re-install
lookups. Both are needed and neither substitutes for the other.

Behaviour hangs off `resources` and `lifecycle`, never off `origin`:

| Operation | `resources: gateway` | `resources: app` |
|---|---|---|
| Enable | register resources, start backend, resolve dependencies, run `onEnable` | run `onEnable` only |
| Disable | run `onDisable`, run hooks, stop backend, deregister | run `onDisable` and hooks only |

| Operation | `lifecycle: gateway` | `lifecycle: app` | `lifecycle: locked` |
|---|---|---|---|
| Update | re-clone or re-copy, re-register | 400 | 400 |
| Uninstall | teardown then remove files | teardown then remove files | 400 (disable instead) |

An unknown value in any of the three is repaired to that field's default with a
warning rather than raising: `installed.json` is read on every boot, and a
metadata typo must not make an app unloadable. A record written before the fields
existed is migrated once from the old `managed` value and stamped
`schemaVersion: 2`.

**Provenance is immutable at runtime, in both directions.** A self-registration
(`POST /api/apps/register`) is REFUSED for a name a builtin owns: accepting it
would downgrade `origin`/`lifecycle` to `external`/`app`, handing a third party a
shipped builtin's execution exemption and leaving the boot-warmed first-party
name and MCP-server sets stale until the next restart. Symmetrically,
`register_builtin_apps` stands down when a user-installed app already occupies the
directory, leaving their install untouched, because taking it over is
unrecoverable.

Frontend badges and affordances read the same three fields
(`origin === 'builtin'`, `resources === 'app'`, `lifecycle === 'gateway'`,
`lifecycle !== 'locked'`). Provenance LABELS are a separate question: they must
test the server-attached `_registry` tag FIRST, because `origin` and `author` are
copied verbatim from an index entry for a not-yet-installed app, so an added
registry could otherwise publish `origin: "builtin"` and self-award the
first-party mark next to a button that runs its setup code with gateway
privileges.

## 1. App MCP servers land in KiroCrew's agent config, never the shared kiro file

An app's `mcpServers` are written into KiroCrew's own agent config
(`<kiro agents dir>/kirocrew.json`, resolved through `config.paths.kiro_agents_dir`
so test/dev home redirects are honoured), **not** the shared
`~/.kiro/settings/mcp.json`.

Why it is a contract and not a detail: the shared file is read by everything else
living under `~/.kiro` — the Kiro IDE and every other kiro-cli agent — so
registering an app's servers there leaked that app's private tools into surfaces
that never installed it, and a dead HTTP entry there broke EVERY kiro session, not
just the app's. KiroCrew sessions read only the agent config (`includeMcpJson` is
pinned False in `agent.py`), so the narrower target is also sufficient.

**Migration is finished at boot, not at disable.** `reconcile_enabled_app_resources`
scrubs the app's entries out of the legacy shared file for every ENABLED app on
every gateway start. Scrubbing only on deregister meant an already-enabled app
kept leaking until the user happened to disable it.

Writer: `apps/bridges.py::_apply_agent_mcp_policy`, `_mcp_json_path`,
`_scrub_legacy_shared_mcp`.

## 2. Auto-approve is intersected with the governance ceiling

A granted server normally lands in the agent's `allowedTools` (auto-approve):
the user asked for that server explicitly, and for an unattended app agent a
prompt resolves to "rejected", so granting it means granting its use.

**Except where the enterprise ceiling forbids it.** Auto-approve is the one path
that never reaches `hooks.on_tool_call`: kiro-cli only sends
`session/request_permission` for tools it must ask about, and the governance deny
hangs off that request. Writing a ceiling-denied server into `allowedTools` would
therefore route around the one control the docs promise cannot be routed around.

So the grant is intersected with Level 1 POLICY at policy-write time
(`_ceiling_forbids_mcp`, `gate_decision(ceiling, None, …)`):

| Ceiling | Result |
|---|---|
| permits | auto-approved, as before |
| **denies** | stays in `tools` (the grant is not discarded) but NOT in `allowedTools`, which forces every call through `request_permission`, where the gate denies it |
| absent (standalone) | unchanged behaviour |

A user may grant anything; whether it RUNS remains the policy's call.

**Documented residual:** Level 2 PROFILE is per-surface and resolved at call
time, so a profile that narrows FURTHER than the ceiling still cannot retro-deny
an auto-approved tool. Closing that would mean never auto-approving on any host
that has a profile at all — a real UX cost for a narrower guarantee, so it is
deliberately not done. Granularity is per-server for the same reason (a grant is
per-server); tools a per-tool ceiling rule denies are still denied at the gate on
every non-auto-approved path.

## 3. App agent JSONs are materialized copies, refreshed field-wise

App agents are written to `<kiro agents dir>/<app>--<agent>.json` as a **copy**,
not a symlink: the source may live inside the installed Python package (a builtin,
which must stay read-only) while the config needs per-user MCP policy merged in.

The copy is re-materialized on every registration, and the gateway reconciles
registration at startup, so an edit to the packaged template takes effect on the
next boot without a reinstall.

**A wholesale rewrite would silently revert user edits**, so the refresh is
field-wise, the same split `agent._refresh_dynamic_fields` uses for managed MCP
servers:

- **Framework-owned, always refreshed** (`_FRAMEWORK_OWNED_AGENT_KEYS`): `name`,
  `mcpServers`, `tools`, `allowedTools`, `prompt`. Each is derived from the
  manifest, the per-app policy, or the running install — a stale value is a bug,
  not a preference.
- **Everything else on disk wins**: `model`, `description`, extra
  `toolsSettings`… it can only be there because the user put it there. Preserved
  keys are logged so the reason a template change did not appear is visible.

The prior file is snapshotted BEFORE the replace (the write path unlinks a legacy
symlink first, so reading afterwards would find nothing). An unreadable prior file
means "nothing to preserve", never "abort the refresh".

Writer: `apps/bridges.py::_register_agents`, `_preserve_user_agent_edits`,
`_read_agent_config`.

## 4. A generated prompt is pinned through the app's policy

An agent template packaged inside an app can only name paths that exist at
packaging time, so an agent whose system prompt is RENDERED at runtime (from user
settings — a pet name, a chosen persona) had no way to reference it.

`_apply_agent_prompt` reads a `prompt` key from the per-agent policy, validates
that the path exists, and writes it into the materialized agent JSON. The app
renders the file into its own data dir and points the policy at it; re-rendering
plus `refresh_app_agents` is what makes a settings change take effect.

Writer: `apps/bridges.py::_apply_agent_prompt`. Consumer side is the app's own
policy builder.

## 5. Builtin resource paths resolve against the PACKAGE dir

For an installed app, manifest-relative resource paths (`agents/*.json`,
`skills/<dir>`) resolve against the app directory in the data home. **A builtin is
different**: its code ships inside the Python package and its data-home directory
holds only `installed.json`, the snapshot `app.json` and `data/`. Resolving
against the data home therefore always missed — silently, because registration
only logs a warning. That is how the first builtin to declare agents/skills
registered zero of them while its `mcpServers` (which need no path) registered
fine.

Builtin package dirs use **underscores** where the app name uses hyphens
(`auto-research` ships as `builtins/auto_research`) — the same normalisation
`lifecycle._resolve_hook` applies. Without it the lookup missed for every
hyphenated builtin and fell back to the data home, reproducing the exact silent
miss this function exists to prevent.

Writer: `apps/bridges.py::_app_resource_root`.

### 5.1 Resource-path containment is host-independent and flavour-explicit

Every manifest resource path (`agents`/`skills`/`sops`, `ui.entry`,
`ui.pages[].entryPoint`, `backend.entryPoint`) is joined onto the app root, so
`manifest._path_escapes_app_root` refuses any path that could relocate that join.
It applies three checks, and the two lexical ones run **first and unconditionally**
— before, and independent of, whether `app_root` is known:

1. `_is_rooted_path` — `PureWindowsPath(p).drive or .root`.
2. `_has_dotdot_segment` — a `..` segment under **either** path flavour.
3. Canonical containment (only when `app_root` is given) — `resolve()` +
   `is_relative_to`, which catches what no lexical check can see: a symlink or
   reparse point inside the root whose target leaves it.

A manifest is portable data validated on whichever host installs the app, so all
three must reach the same verdict everywhere. Both lexical checks are therefore
written **flavour-explicitly** rather than via the running host's `os.path`
(matching the `PurePosixPath|PureWindowsPath` idiom in
`dashboard/handlers/knowledge.py`), and neither is deferred to `resolve()`:

- **`is_absolute()` is flavour-bound.** It and `os.path.isabs` answer for the
  RUNNING host, and `os.path` **is** `ntpath` on Windows, so pairing the two in an
  `or` yields a single Windows-only test there. Windows' flavour reads
  `/etc/passwd` as unanchored (no drive), which would let a POSIX-absolute resource
  path through a Windows gateway. The Windows flavour treats both `/` and `\` as a
  root, making it a strict superset — testing it alone covers both syntaxes on
  either host.
- **Drive-relative paths need drive-OR-root.** `D:evil.py` carries a drive but no
  root, so `is_absolute()` is False, yet `app_root / "D:evil.py"` yields
  `D:evil.py` and escapes the root entirely.
- **`..` needs both flavours, and needs checking even when `app_root` is known.** A
  POSIX host reads `..\evil.py` as one opaque filename, so a POSIX-only split
  misses a backslash traversal — and `app_root / "..\evil.py"` *resolves inside*
  the root on POSIX, so relying on containment alone makes the verdict differ by
  host: accepted on POSIX, rejected on Windows. `a..b` and `notes..md` are single
  segments and remain accepted.

### 5.2 App skills are linked with a junction on Windows, not a symlink

`_register_skills` links each declared skill directory into the skills tree twice
(namespaced `skills/<app>/<skill>` plus a flat `skills/<skill>`). The link is a
symlink on POSIX and a **directory junction** on Windows, via
`platform_compat.symlink_or_junction`.

The mechanism is load-bearing, not an implementation detail: a Windows symlink
needs `SeCreateSymbolicLinkPrivilege`, which a standard (non-elevated,
non-Developer-Mode) account does **not** hold. Raw `os.symlink` there raises
`WinError 1314`, and because registration only logs a warning per skill, every app
on an ordinary Windows install registered **zero** skills — silently. A junction
needs no privilege and is transparent to every operation performed on the result
(`is_dir`, `resolve`, reading files through it, and the `_iter_skill_files` walk
that indexes app skills through their trusted-provider root).

**Consequence for every link test in this subsystem:** a junction reports
`is_symlink() is False`, so link-ness must be asked with
`platform_compat.is_link_or_junction` and removal done with
`platform_compat.unlink_link_or_junction`. Two failure modes follow from getting this
wrong, and both are Windows-only and silent:

- `is_symlink()` on re-registration classifies our own junction as a real
  directory and hands it to `shutil.rmtree`, which **refuses any directory link**
  — breaking every re-registration.
- `is_symlink()` in the `_deregister_skills` sweep and the `reconcile_app_skills`
  stale-link sweep finds zero links, so the **flat** link (which lives in the
  skills root, outside the namespaced directory the `rmtree` removes) leaks: the
  skills root keeps advertising a skill whose app is deregistered, and the link
  dangles once the app is uninstalled.

`_copy_app_tree` is the deliberate exception — it **omits** a junction found in an
app source rather than reproducing it, since `copytree` cannot preserve one as a
link and copying through it would duplicate the target's bytes (the multi-GB-walk
failure mode) or expose a sensitive location.

Writer: `apps/bridges.py::_register_skills`, `_deregister_skills`,
`reconcile_app_skills`. Shim: `platform_compat.symlink_or_junction` / `is_link_or_junction` /
`unlink_link_or_junction`.

## 6. App window entries: discovery, nested routes

An app may ship standalone HTML windows (a separate Vite bundle loaded by a shell
window rather than the SPA router) as
`dist/src/apps/<app>/<name>.html`. At startup the gateway enumerates them and,
from that ONE enumeration, both registers `GET /app-windows/<app>/<name>.html` and
excludes that exact path from the unauthenticated SPA-shell fallback. Registering
both from one loop makes route/exclusion drift impossible — and the exclusion is
load-bearing: the fallback answers unauthenticated GETs so the token bootstrap can
load, and a window entry left inside it would be shadowed by an unauthenticated
dashboard shell (the window would open showing a full dashboard instead of its own
UI).

Routes are built from the enumerated FILES; the request path never participates in
building a filesystem path, so there is no traversal surface.

**The `/app-windows/<app>/<name>.html` route keeps the app and window in separate
path segments, so a collision is structurally impossible.** An earlier revision
served windows FLAT at `/<app>-<name>.html`, which is ambiguous the moment either
name contains a hyphen — app `foo` + window `bar-baz` and app `foo-bar` + window
`baz` both spell `/foo-bar-baz.html`. That cost two pieces of machinery: a
collision refusal in the gateway, and a `vite.config.ts` middleware that guessed
the split by trying each hyphen position (and could resolve to the WRONG file
rather than refuse). Putting the boundary the filesystem already has back into the
URL deletes the whole class — neither piece exists any more. A duplicate check is
kept only as a cheap invariant: with distinct segments the filesystem cannot
produce two identical routes, so a hit means the convention changed under us.

Writer: `dashboard/server.py::discover_app_window_entries`
(`APP_WINDOW_URL_PREFIX = "app-windows"`);
exclusion: `dashboard/token_auth.py::register_app_window_paths`.

## 7. Enabled-app resources are reconciled at startup

Registration used to happen ONLY in the enable path, so an app that gained
agents/skills in a later version never registered them for a user who had already
enabled it — silently, because a missing resource only logs a warning.
`reconcile_enabled_app_resources` re-registers every enabled gateway-managed app
at boot, making on-disk state a function of the current manifests instead of of
install history. Idempotent: agent configs are refreshed field-wise (§3), and
skills/crons/MCP registration overwrite in place.

Writer: `apps/bridges.py::reconcile_enabled_app_resources`.

## 8. An app's EventBus only exists with a real broadcast function

`build_app_context` returns `events=None` when `broadcast_fn` is None, and
`EventBus.publish` is then never reached — so **every app event becomes a silent
no-op**. The gateway once passed `state.broadcast if hasattr(state, "broadcast")`
while the method is actually named `broadcast_ws`, which disabled app events
entirely with no error anywhere. Both halves are pinned by tests; a new host
surface that constructs an app context MUST pass a real broadcaster.

Writer: `apps/lifecycle.py`; consumer: an app's `publish`/`_broadcast`.

## 9. Desktop-shell (Electron main-process) code is a first-party-only exception

App Kit apps are **renderer + backend** only. Mochi's `website/electron/mochi/`
(pet overlay windows, panel/settings windows, global-shortcut registration,
multi-instance) runs in the Electron **main process** — a deliberate first-party
exception because Mochi is a first-party desktop pet whose windows the shell must
own. It is **not** a precedent that a third-party (or non-desktop) builtin may
ship main-process code; those stay renderer+backend. See
`docs/system-specs/modules/mochi.md` § Deliberate divergences.

Relatedly, Mochi's vendored `ChatPanel`/`panelBridge` are a **deliberately owned
fork**, not a convergence-pending copy of the dashboard's `ChatEmbed` — an
approval-flow or widget-protocol change in the dashboard chat must be ported to
Mochi's panel too. Do not replace `ChatPanel` with `ChatEmbed` in an upstream
sync.

## 10. Teardown order is a precondition chain, not a cleanup list

Uninstall is irreversible, so the whole sequence runs inside the per-app
lifecycle lock and the one step that can safely refuse runs FIRST:

1. **Cron cleanup** (gateway-managed apps). Owned jobs are removed in one atomic
   transaction. A contended store aborts the uninstall with a retryable 409
   having changed nothing. This must precede everything else: past this point
   deregistration drops the per-app cron manifest and the final step deletes the
   app directory, so still-enabled owned jobs become permanent orphans that the
   scheduler keeps firing with nothing left that knows they belong to a removed
   app. "Durably disable the jobs instead" is not a fallback, because disabling
   is itself a store mutation needing the very lock that is contended.
2. `onUninstall` script, reached only once cron cleanup succeeded, so a
   non-idempotent teardown never runs on an uninstall that will be retried.
3. Backend stop and resource deregistration (gateway-managed only).
4. Dependency cleanup (see §11).
5. File removal, preserving `data/` unless the caller asked to purge.

The lock spans the script deliberately: the script may itself be destructive, so
holding the lock across it stops a racing enable or update from starting a
backend mid-teardown. The cost is that a concurrent same-app lifecycle operation
waits up to the script timeout, which is acceptable because those operations
genuinely conflict and the lock is per-app.

Data deletion requires the dedicated literal `{"purge_data": true}`. Absence and
malformed values preserve data, and a legacy `keep_data: false` is deliberately
ignored, so no request shape can become an implicit purge. The script sees the
decision as both `KEEP_DATA` and `PURGE_DATA` in its environment.

`setup.onUpdate` parses, validates, and round-trips through `SetupConfig`, but
**no code path executes it**. Treat the field as declared-not-wired: an app whose
update correctness depends on it is broken, and the fix is an idempotent
`onInstall` (a registry update re-runs it), not a new call site added quietly.

Writers: `apps/routes.py::handle_uninstall_app`, `_deregister_crons_with_retry`,
`_run_lifecycle_script`; `apps/manager.py::uninstall_app`.

## 11. Dependencies are reference-counted, and only sole ownership is removable

`~/.kiro/crew/dependency-ledger.json` records which apps caused which capability
dependency to be resolved. Uninstall classifies each dependency the manifest
declares into one of three buckets, and the bucket alone decides what happens:

| Bucket | Condition | On uninstall |
|---|---|---|
| `removable` | in the ledger, this app is its only recorded owner | cleaned, unless the request names it in `keep_specific` |
| `shared` | in the ledger with other owners | kept; this app drops out of `installedBy` |
| `userInstalled` | absent from the ledger | never touched; the user installed it |

Classify-and-update is ONE operation under a single exclusive ledger lock. Doing
it as a read, then a decision, then a write would let two apps sharing a
dependency be uninstalled concurrently and both conclude they were the sole
owner.

A dependency type with no cleanup operation (`capability.agents`) keeps its
ledger row and only loses this app's ownership even when classified removable:
dropping the row for something nothing can uninstall would orphan the installed
package untraceably.

Client-supplied `keep_specific` ids are normalized to canonical keys before the
membership test, because a dashboard session that loaded its uninstall preview
from an older build echoes pre-rename ids back, and an unnormalized comparison
would silently delete a dependency the user explicitly chose to keep.

`GET /api/apps/{name}/uninstall/preview` is the read-only classification that
feeds the confirm dialog, and it is **additive**: a client that skips it and
POSTs straight to uninstall gets the same safe default (clean removable, keep
everything else). The handler exists and is exercised by the dashboard client;
if a route table refactor drops its registration the dialog silently degrades to
no preview, since the frontend treats the fetch as best-effort.

Dependency resolution itself is **non-blocking by design**: no capability manager
may exist (the public edition ships none), network failures are transient, and
some dependencies are optional for degraded operation. `resolve_dependencies`
returns a result the caller decides on, and the counts surface in the API
response as warnings. Missing REQUIRED `commands` and missing `optionalCommands`
are reported in separate lists precisely so "absent" stays distinguishable from
"broken".

Writers: `apps/dependency_ledger.py`, `apps/dependencies.py`;
`apps/routes.py::handle_uninstall_preview`.

## 12. Store visibility is a manifest flag, not a code removal

Built-in apps ship default-DISABLED. `manager._DEFAULT_ON_BUILTINS` is the single
source of truth for the exemption (currently only `projects`, the Task Runner),
read by the policy tests over both the hardcoded list and the file-based
manifests, so a builtin cannot become default-on through one registration path
while the other path's test still forbids it. A default-enabled builtin is
persisted at first registration and never routes through `enable_app`, so the
governance `apps` activation allowlist is re-applied at that write: a
governance-denied app registers disabled.

`hidden: true` on a builtin manifest removes it from the Discover catalog while
leaving its code and routes fully intact. It stays installable and enablable by
name from the CLI, and remains visible in the Library once enabled. **Channels**
carries this flag. **Board** is not hidden but removed: it is listed alongside
`knowledge` (promoted to a built-in surface) and `orchestrated` (merged into the
unified Chat surface) in the escalation-cleanup sweep, which deletes stale
installed-app directories so an orphaned entry cannot resurface in the store.
That sweep never follows a symlinked app directory and requires the resolved path
to stay under the apps root, so it cannot delete anything outside the tree.

Curator control over the Discover editorial layer is the registry entry's
`featured` flag, and it is honored **only** for core-registry entries. The
spotlight is the store's most persuasive install surface and its action runs
third-party setup code with gateway privileges, so an external registry cannot
flag itself into that slot. With nothing flagged, selection falls back to a
deterministic order (hero art, then verified publishers, then name), so the
surface is never empty and never arbitrary.

Writers: `apps/manager.py` (`_BUILTIN_APPS`, `_DEFAULT_ON_BUILTINS`,
`register_builtin_apps`), `apps/discovery.py::discover_builtin_apps`;
consumers: `website/src/pages/AppsPage.tsx` (`pickFeatured`),
`website/src/components/appstore/types.ts` (`isVerified`, `sourceLabel`).
