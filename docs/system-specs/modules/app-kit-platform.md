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
`lifecycle !== 'locked'`). Provenance LABELS and the verified badge are a
separate question: `/api/apps/registry` rows carry server-computed
`provenance` (`"official" | "external" | "builtin"`, with `"core"` accepted by
clients as the pre-migration spelling of `"official"`) and `verified` fields,
stamped by `_apply_trust_fields` in `registry.py` where the server-attached
`_registry` tag is authoritative. The helper OVERWRITES anything an index
publishes, and derives `verified` from the INDEX-declared author snapshotted
before the app.json merge — never from the repo-fetched manifest — because
`origin` and `author` are otherwise copied verbatim from index or manifest
content for a not-yet-installed app: deriving trust from either let an added
registry publish `origin: "builtin"`, or a third-party core repo publish
`author: "KiroCrew"`, and self-award the first-party mark next to a button
that runs its setup code with gateway privileges. The author comparison runs
through `_fold_author` (NFKC, drop category-`Cf`, collapse whitespace, lower)
against `FIRST_PARTY_AUTHORS`, so both the joined historical spelling and the
two-word org name we actually publish mint the mark, and a fullwidth or
zero-width rendering of our name does not silently lose it. Folding WIDENS the
match, which is safe only because the `_registry` short-circuit runs first:
the author is consulted exclusively for rows whose index we ship or sign.

`origin` on a registry row is stamped under the same rule. The install-status
enrichment matches installed apps by NAME alone, so it withholds the `origin`
copy from external rows (`_is_external_row`): an external index publishing an
app named after an installed built-in must not inherit `origin: "builtin"`
beside the `provenance: "external"` stamped on the same row.
`_apply_trust_fields` additionally scrubs any `origin` other than the
server-stamped `"external"` (a `detectInstalled` hit) from `_registry` rows,
because an index-published `origin` key survives a failed manifest fetch —
`_resolve_manifest` returns the row unprojected on that path — and would
otherwise reach the wire.

`"official"` means "an app WE list". The bundled `app-registry.json` is one
delivery of that list — the offline seed shipped inside the wheel — so it
carries the same value a signed remote catalog will, not a second one. Two
values for one claim would put a weaker integrity guarantee (it rides on the
install artifact and cannot be revoked before the next release) behind a label
the client cannot tell apart from the stronger one. Provenance names WHOSE list
an app is on; how that list reached the client is a separate axis and belongs in
a separate field once there is more than one answer to record.

The merge that builds those rows projects the index row explicitly rather than
copying it: `_merge_manifest` starts from `_REGISTRY_ROW_KEYS` — identity, the
clone coordinates, the install-path flags, the spotlight flag, and the two
server-attached tags — and takes every display field from the fetched
`app.json`. An index is untrusted content, so a key it invents reaches no
client, and it cannot publish display copy for an app whose manifest says
otherwise. Install-status and trust fields are absent from that projection by
design: `_enrich_with_install_status` and `_apply_trust_fields` run afterwards
and stamp them server-side, so an index-supplied value for one of them can
never be read before it is replaced.

The client
(`isVerified`/`sourceLabel` in `website/src/components/appstore/types.ts`)
reads the server fields, still rejects a `_registry`-tagged row first (so
nothing smuggled through an older gateway can relabel an external row), and
falls back to the legacy `origin`/`author` derivation only for rows from
older gateways that emit neither field. `_registry` itself must keep being
emitted: besides the external-source label and older clients,
`appManifest.ts::keysFor` (first-party copy gate) and `pickFeatured`'s
legacy arm still read it.

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
flag itself into that slot: `_apply_trust_fields` strips `featured` from
external rows server-side, and `pickFeatured` additionally excludes any row
whose `provenance` (or legacy `_registry` tag) marks it external. With nothing
flagged, selection falls back to a
deterministic order (hero art, then verified publishers, then name), so the
surface is never empty and never arbitrary.

Writers: `apps/manager.py` (`_BUILTIN_APPS`, `_DEFAULT_ON_BUILTINS`,
`register_builtin_apps`), `apps/discovery.py::discover_builtin_apps`,
`apps/registry.py::_apply_trust_fields`;
consumers: `website/src/pages/AppsPage.tsx` (`pickFeatured`),
`website/src/components/appstore/types.ts` (`isVerified`, `sourceLabel`).

## 13. An app token's WebSocket stream is scoped by its manifest, deny-by-default

`/api/ws` is the third surface an app token reaches, alongside the HTTP API and
MCP. Connecting grants no events by itself: the socket records the caller's app
identity and its `permissions.events` declarations, and every fan-out is filtered
per socket at ONE chokepoint — `DashboardState._send_ws_all` →
`_ws_client_allowed` → `dashboard/ws_event_scope.py`. Both dispatch paths
(`broadcast_ws` and the `_broadcast` `_type` translation) and the
subagent-subscriber fan-out funnel through it. An event absent from the module's
tables is DENIED, so a new event name is a silent loss of function for apps until
it is classified — `test_ws_event_scoping.py` fails the build on an unclassified
broadcast name rather than letting it reach production.

Three tiers. Tier 0 (`dashboard`, `refresh`, `update_progress`) carries no
sensitive payload and always delivers — and that classification is a claim about
CONTENT, so it has to be maintained: `_push_status` writes the `dashboard` frame
straight to each socket every few seconds, and its payload is deliberately
counts-and-environment only. The checkout's `branch`/`commit` are stripped for app
tokens (they say what the operator is working on and have no consumer outside the
owner surfaces); `/api/status` and the SSE stream run on dashboard-user tokens and
keep the full snapshot. Moving the whole frame behind a declaration was rejected:
every client needs its `version` to force a reload across a gateway upgrade, so
that would silently cut existing apps off from the upgrade signal. Tier 1 is slot-scoped: visibility follows
the slot's `SlotOrigin` and the app's `slots:*` declarations (`slots:own` is the
default, then `slots:user`, `slots:app:<name>`, `slots:all`), with `subagent:*` an
independent dimension so an app can watch subagent status without receiving chat
content. Tier 2 is global and needs an explicit declaration; notifications split
by source, so `notification` covers the app's own pushes while gateway-internal
ones (cron output, `send_message`, watchlist results) require
`notification:system` — bundling them would make one declaration a broad grant.

**`SlotOrigin` is declared by the layer that knows it, never derived.**
`get_or_create_slot` cannot distinguish a person typing from a background
injection, so it leaves an undeclared non-app slot UNTAGGED (`""`) instead of
calling it USER. The request layer decides USER/APP because only it sees whether
an app token was presented; background callers pass CRON/SYSTEM explicitly. `""`
is invisible to every cross-slot scope, so a caller that forgets to declare loses
visibility rather than leaking. The origin round-trips through session metadata:
both the write (`_save_slot_to_history`) and the restore (the rehydrate paths) are
required, or every slot comes back unattributed after a restart.

**A socket's scope can only SHRINK while it stays open.** `permissions.events` is
resolved at connect, and `disable_app` rewrites the registry without closing
sockets, so every decision INTERSECTS the connect-time snapshot with the
currently declared set (`ws_event_scope.effective_allowed_events`).

**Revoking a disabled app takes TWO checks, because the own-slot default never
consults declarations.** `disable_app` flips `enabled` in `installed.json` and
leaves `app.json` intact, so a manifest-only read would report a disabled app's
declarations unchanged — hence enablement is read as part of the declared set, and
a disabled or uninstalled app declares nothing. That alone does not revoke it:
`disable_app` does not invalidate the app token (`token_auth` has no enablement
check; each app backend route gates on `is_app_enabled` itself), so the app can
keep an authenticated `/api/ws` socket, and `_slot_visible` grants an app its OWN
slots on the ownership check BEFORE `allowed_events` is read. Emptying the
declaration set cannot reach that branch. So the enablement flag is also exposed
as `ws_event_scope.app_events_revoked`, which the own-slot branches and the gate
consult; only then does a disabled app actually collapse to Tier 0.

The two facts come from ONE off-loop read and are cached as one entry, because a
separately-keyed enablement cache could report `enabled` for a declaration set
that was read while the app was disabled. They cannot be collapsed into the scope
set either: a disabled app and an enabled app that declares no events both present
an EMPTY set, and those must differ — the latter still sees its own slots.
Revocation therefore requires POSITIVE evidence of disablement, so an unreadable
`app.json` on a still-enabled app declares nothing but is NOT revoked; blanking a
working app's own chat over a transient filesystem error is the more costly error.

**The CONNECT path resolves enablement too, and refuses.** Since the token
survives `disable_app`, a disabled app can reconnect at will, and a connect-time
read of `app.json` alone would hand it a full snapshot from the intact manifest —
which the initial `slots` push and the log replay are then judged against before
any background refresh runs, with `app_events_revoked` reporting NOT revoked on
the cold cache. So `ws_event_scope.load_declared_events_for_connect` returns the
enablement flag with the scopes AND primes the cache, and `api_ws` closes the
socket when the app is disabled. Refusing (rather than admitting at Tier 0, which
is what an already-open socket narrows to) costs nothing at connect: there is no
in-flight streaming turn to cut, which was the reason narrowing does not close
live sockets. The read and the refusal both happen BEFORE `register_ws`, because
refusing after registration would need the cleanup scope that only exists once
registration succeeds.

**Every decision is audited, grants included.** `AUTOSDE.yaml`
(`backend-security-controls`) requires a SEL event for every permission
decision, so the gate records the grants as well as the refusals. Because a
decision is made per client per frame, both go through one deduplicated path
(`_audit_decision`, 5-minute window per app/event/reason, carrying the suppressed
count) — an un-deduplicated write per grant would be unbounded on the broadcast
path. Grants and refusals use different dedup keys so neither starves the other
out of the window, and the grant record is emitted from the `ws_event_allowed`
wrapper rather than from each `return True`, so a branch added later is covered
without having to remember to report itself.

Four paths read the set and all four narrow: the gate, the `slots` payload
filter, the subagent-batch payload filter, and the LOG fan-out. The log path is
the odd one — `subscribe_logs` grants once and the ring handler then writes
straight to `_ws_log_subscribers` without passing the chokepoint, so the re-check
lives at the send (`handlers/updates._safe_ws_send`), which also drops the
subscription. That check must stay on the event loop: the handler's `emit()` runs
on arbitrary threads, where a cold cache miss would fall back to a synchronous
manifest read.

A NARROWED or deleted manifest therefore takes effect within one refresh
interval, while a WIDENED one does not reach an open socket at all — that requires
a reconnect, so a live session can only ever hold scopes it was authenticated for.
The reload uses the same off-loop stale-while-revalidate shape as `exposeToApps`,
with one deliberate difference: a cold miss falls back to the connect snapshot
rather than to empty, because an empty fallback would withhold every event from
every app on the first broadcast after a restart. Closing the socket instead was
rejected — it would cut a streaming turn mid-flight and turn a manifest save into a
reconnect storm without giving a tighter guarantee than withholding already does.

**Cross-app visibility is mutual.** `slots:app:X` also requires X's manifest to
name the observer in `permissions.exposeToApps`, so an app cannot name a sibling
unilaterally. That list is read through a stale-while-revalidate cache because the
gate is synchronous and sits on the broadcast hot path: it never reads the disk
itself, a cold miss denies (fail-closed) and schedules an off-loop refresh, and a
stale entry serves the previous value while refreshing.

**A grant that is not a list denies.** `permissions.api`, `events`, `mcpTools` and
`exposeToApps` are list-valued; a JSON scalar is refused rather than coerced,
because iterating a string yields its characters (`"*"` → the wildcard, and
`"/api/chat"` → the prefix `"/"`, which matches every path).

**Filtering the frame is not always enough.** Two event shapes carry other
tenants' data inside a payload the gate admits wholesale, so they are narrowed on
the send path in `_serialize_for_client`: the `slots` re-push (a full slot list)
and the coalesced `subagent_batch_*` frames (one frame, many subagents' rows, no
single slot to judge). The `slots` envelope additionally carries global
safety-posture booleans that no slot scope narrows — `yolo` rides the same
declaration that gates `yolo_expired`, and `channelTrusted` is withheld from app
tokens outright. A withheld field is OMITTED, never sent as `false`, because a
falsy default still answers a question the app must not ask.

`_APP_TOKEN_IMPLICIT_ALLOW` holds `/api/ws` alone. An endpoint belongs there only
with a compensating per-response control — event scoping is that control for
`/api/ws` — so `/api/status` is not in it despite being a liveness probe: it
returns owner hash, host specs, cron and usage stats, and the live safety-override
state, and an app that wants it declares it in `permissions.api`.

Writers: `dashboard/ws_event_scope.py`, `dashboard/ws.py` (connect-time scope
resolution), `dashboard/state.py` (`_send_ws_all`, `_ws_client_allowed`,
`_serialize_for_client`, `SlotOrigin`), `dashboard/token_auth.py`
(`_APP_TOKEN_IMPLICIT_ALLOW`, `app_token_path_allowed`), `apps/manifest.py`
(`_granted_list`); consumers: `website/src/app-sdk/index.ts` (mirrors the tables
for developer-facing diagnostics, drift-guarded by
`website/src/test/appSdkEventScope.test.ts`). Runtime-facing summary for app
authors: [../../../src/kiro_crew/docs/app-platform-trust-model.md](../../../src/kiro_crew/docs/app-platform-trust-model.md).

## 14. The published catalog is the store's inventory

`GET /api/apps/registry` answers from the published catalog when it is reachable:
`handle_registry` prefers `list_catalog_apps` (`registry.py`), which maps the
published `official-registry.json` entries through
`official_catalog.list_catalog_rows` and then applies the same install-status and
trust stamping as the seed path. The bundled `app-registry.json` seed is the
catalog's OFFLINE SNAPSHOT, not a peer source: a reachable catalog means the
store renders the published document's list, display copy, AND installable
inventory; an unreachable one degrades the listing to the seed.

User-configured external registries (`config.registries`) are a separate,
always-present source: both `list_registry` and `list_catalog_apps` merge them
through one shared site, `_append_external_registry_apps`, so the online and
offline paths enrich, probe (`detectInstalled`), and trust-stamp external rows
identically and cannot drift. A catalog/seed/builtin row wins a `name`
collision; the catalog path reserves every catalog name (snapshotted before the
`git`-installability filter) plus every seed name, so an external row can only
ADD a name no catalog or seed row claims and can never shadow a name install
resolves by. External rows keep their `provenance: "external"`/`verified: false`
stamp.

The catalog is trusted only as far as TLS, so its power is bounded by
pin-or-refuse rather than by withholding coordinates.
`official_catalog.inventory()` materialises each `git`-source entry as an
installable row carrying `gitUrl`/`repo`/`commit` (`builtin` entries produce
nothing); a row that fails coordinate validation (https-only URL, 40/64-hex
`ref`, contained relative `subdir`, kebab-case name, no duplicates) is dropped,
never repaired. What keeps a compromised document from pointing an install at
attacker-selected code with owner credentials is the posture stack, each layer
independently load-bearing:

- **Pin or refuse.** A catalog row installs by `_git_fetch_commit` — fetch the
  pinned SHA, assert the landed commit equals the pin, hard-fail otherwise. The
  row carries no `branch`, so no code path can quietly clone a tip and succeed.
- **Credential-free clone posture.** Catalog rows clone anonymously
  (`anonymous_git_env`); they never inherit the owner-designated credential
  carve-out.
- **No provenance minting.** `inventory()` rows never carry `origin`,
  `author`, or `_registry`; `verified` stays `false` for a catalog `git` app
  until the catalog signature is checked — wiring signature verification into
  `official_catalog` is what flips that, not a field the catalog can assert
  about itself.
- **Install coordinates never come from a cache.** `inventory_for_install` and
  `list_registry`'s inventory both resolve through `fetch_inventory_entries`, a
  fresh HTTPS fetch; the on-disk cache may enrich display fields of a row that
  exists from another source but may never introduce or rewrite one
  (`annotate` skips `_catalog` rows).
- **Refuse, don't fall back.** A catalog fetch failure refuses installs,
  updates, and execution grants for catalog-listed names rather than falling
  back to the unpinned seed or an agent-writable external cache —
  `_resolve_registry_row` distinguishes "the document does not name this app"
  (seed may answer) from "the document could not be asked" (refuse).
- **Supersession is URL-scoped.** A catalog row replaces a same-repo seed row
  (scheme/host case-folded, path case preserved); a different-repo name
  collision keeps the seed, so a republished document cannot silently re-home
  an app to a new repository under a familiar name.

A name is a filesystem path on install, so `inventory()` and
`list_catalog_rows` drop any entry whose name fails the manifest name contract
(`app_name_error` / `KEBAB_RE`), and the catalog fetch runs off the event loop
(`asyncio.to_thread`) so a cache-expired request never blocks the gateway loop.

Writers: `apps/official_catalog.py` (`list_catalog_rows`, `inventory`,
`fetch_inventory_entries`, `inventory_for_install`), `apps/registry.py`
(`list_catalog_apps`, `_resolve_registry_row`, `_git_fetch_commit`,
`_append_external_registry_apps`, `_detect_installed_probe`),
`apps/routes.py` (`handle_registry`).

## 15. A registry's credential posture follows its index's change control

The published catalog serves one deployment's inventory over TLS from a fixed
URL. An organisation that publishes its OWN catalog uses the external-registry
path instead: `config.registries` (plus whatever the edition pins via
`AppsLoader.default_registries()`) names repos whose index this client fetches at
runtime, so adding an app is a change to that repo rather than a client release.

`_effective_registries()` is the ONE list every consumer reads — index
fetch/refresh, the trusted-host allowlist, row lookup, install, and the
blob-proxy allowlist. That is deliberate rather than incidental: a registry
visible to the listing but not to install would surface apps the install path
then refuses, which is worse than not listing them.

Whether a registry's apps clone with this machine's git identity is decided by
`ExternalRegistryConfig.trust`, and the reasoning is about **who controls the
index**, not which host it lives on:

- **`index` (the default)** — the index is untrusted content. The
  confused-deputy case is concrete: host trust is host-granular, so an index on
  a trusted forge can list an app whose `repo` is a *private sibling repo* on
  that same forge, and the manifest and blob-proxy paths clone automatically on
  browse. Such clones therefore run credential-free (`anonymous_git_env` +
  `strict` sandbox), so a private sibling simply fails to clone.
- **`owner`** — the operator asserts the index itself is under change control
  they own (a review-gated repo on a protected branch). That retracts the
  premise the defense rests on, deliberately and per registry, which is what
  makes an organisation-wide registry usable at all: its apps live in many
  repos, none equal to the index URL, so the byte-identical same-repo carve-out
  alone leaves every one of them unclonable on a forge that needs auth.

**The tier is not readable from a cached row, and that is the whole difficulty.**
By the time a credential decision is made, the row was read from
`_read_external_registry_cache` — the same agent-writable file
`_resolve_registry_row` refuses to resolve an install from. Honouring the tier
there would relocate the confused-deputy read from the index to its cache:
anything able to write `_registry_<name>.json` could name a private repo on the
operator's own forge and have it cloned with the gateway's identity. So the
escalation is split across two predicates with different reach:

- `_is_owner_designated_repo` — the pre-existing byte-identical same-repo
  ground, and the ONLY escalation the **automatic** browse/refresh paths get. It
  compares against a URL the operator typed, so a poisoned cache row cannot
  widen it. `anonymous_git_env`'s contract — automatic clones stay
  credential-free because no per-repo owner action gates them — therefore still
  holds unchanged.
- `_owner_tier_confirmed` — **install only**, and honours the tier only after a
  FRESH fetch of that registry's index confirms an entry whose clone URL is
  byte-identical to the row's. Same rule as the official catalog, whose install
  coordinates likewise never come from a cache.

Four properties keep the tier from becoming a hole, and none is optional:

- **It cannot widen the reachable host set.** Every clone still passes
  `is_clone_host_trusted` first. The tier only decides whether credentials are
  offered to a host that gate already allows.
- **It is never index-supplied.** `_registry_trust_tier` reads the
  build-pinned registry row. A `trust` key on an index ENTRY
  is ignored — otherwise a hostile index would grant itself credentials. The
  freshly fetched index is authority for the URL only, never for the tier.
- **It fails closed.** An unrecognised value reads as `index`; so does an
  unknown registry name and any lookup failure. An unreachable or unparseable
  fresh index refuses the escalation rather than falling back to the cache. Only
  the exact token, freshly confirmed, grants.
- **It is audited in both directions, without carrying the credential.** Grants
  emit `_sel_credential_decision(..., granted=True)` under distinct operation
  names, so the same-repo ground and the tier are separable in the log. REFUSALS
  are recorded too, and are the more interesting record: `_owner_tier_confirmed`
  returns False when a fresh read of the registry's index does not list the
  coordinates the local row claims, which is what a poisoned cache looks like from
  here — left to a rotating log alone, the one event an incident responder wants
  is the one that ages out. Only a decision on an ATTEMPTED escalation is
  recorded: a default-tier registry or a bundled entry is not a credential
  decision, and recording it would put a row in SEL per browse and bury the
  refusals that matter. A clone URL is index-supplied and may
  embed `user:token@`, and the SEL trail is dashboard-readable and persistent, so
  `_redact_url_userinfo` strips userinfo from every logged URL. Userinfo is
  removed rather than the whole URL: a record saying "credentials were offered to
  clone THIS" is worth little if it cannot name the repository, and a bare host
  cannot tell two repos on one forge apart.

**A registry name claimed by two different repositories is refused outright.**
The on-disk index cache is keyed by registry NAME, so if a pinned row and an
operator row share a name but not a repo, serving either would read the other's
cached index under the winner's identity — and every reader stamps `_registry`
from the registry it asked for, so those rows would be attributed to it: apps the
winning repository does not list, presented as its own and installable under it.
`_effective_registries` therefore serves NEITHER row for a contested name and
logs both claimants. Same name AND same repo is not contested: the pinned row
simply supersedes an operator row that already agreed, and the shared cache is
correct. `PUT /api/apps/registries` refuses to create such a collision, so the
case that reaches this rule is a `config.json` that already used the name before
the build pinned it. (Re-keying the cache on `(name, repo)` would fix the wider
pre-existing case — an operator repointing a registry's `repo` has the same
hazard — and is left as separate work.)

**Only the BUILD can grant `owner`.** `_registry_trust_tier` resolves the tier
solely from `AppsLoader.default_registries()`; a row in `config.json` reads as
`index` no matter what it declares. The reason is that `config.json` is
agent-writable — `security.py` says so directly, with the check inline
(`is_sensitive_bash_command("echo x > …/config.json")` is `None`) — so a tier read
from there would not be an operator's assertion at all. A prompt-injected shell
could mint `owner`, and the *same* write also adds its chosen host to
`_configured_registry_hosts()` and lets it control the index that
`_owner_tier_confirmed` re-fetches: every layer downstream of that decision would
already be satisfied by the one write that started it. `default_registries()`
ships in the wheel, so an `owner` tier is a claim the build makes and the agent
cannot forge.

Consequences worth stating, because they close off designs that look reasonable:

- The tier is only honoured for a registry that is BOTH build-pinned and in force.
  A name contested between a pinned row and a config row is served by neither, so
  reading the tier off the pinned list alone would keep granting `owner` for a
  registry whose apps are not being listed.
- `PUT /api/apps/registries` **refuses** `trust: "owner"` rather than storing it,
  and `GET` reports `index` for every operator row. Persisting or echoing a tier
  the runtime ignores would report a grant that does not exist, which is worse
  than declining it. There is correspondingly nothing to preserve across a
  replace-all PUT: an operator row's tier is always `index`.
- No dashboard control writes the tier, and adding one would not help — the
  question is not how the value is typed but whether the file it lands in is
  agent-writable.

The API reports pinned registries under a separate read-only `pinned` key rather
than inside `registries`, because `PUT /api/apps/registries` replaces that list
verbatim: folding them in would let a dashboard round-trip persist an edition
default into the operator's `config.json`, where a later edition change could no
longer move it. `PUT` carries `trust` through for the same class of reason —
dropping it would silently downgrade a registry the operator had marked trusted.

Writers: `apps/registry.py` (`_effective_registries`, `_pinned_registries`,
`_registry_trust_tier`, `_is_owner_designated_repo`, `_owner_tier_confirmed`,
`_sel_credential_decision`,
`anonymous_git_env`), `platform/interfaces.py`
(`AppsLoader.default_registries`), `config/loader.py`
(`ExternalRegistryConfig.trust`), `apps/routes.py` (`handle_registries`).
