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

**The dashboard's whole-config PUT merges rather than replaces, for exactly this
reason.** `PUT /api/agent/config` persists a whole-file snapshot the client read
earlier, so an app registration landing between that read and the PUT used to be
silently clobbered — the app's tools stopped resolving with nothing logged. The
handler now takes bridges' own flock, re-reads the on-disk spec under it, and
applies one rule: **preservation requires positive evidence of app or host
ownership.** An absent `mcpServers` entry is kept only when its owner can be
named — a host-managed server, or one of the exact `<app>:<server>` names an
installed app's manifest DECLARES (`_register_mcp_servers` builds every key it
writes from `manifest.mcpServers`, so the declared set names precisely what an
app can own). Ownership is matched by exact name, never by namespace prefix: with
`demo` installed, a client entry named `demo:custom` that the app never declared
is the client's and is deletable. If that source cannot be read the PUT is
REFUSED (500, `code: app_ownership_unreadable`) rather than guessed — preserving
every namespaced entry would make entries undeletable, and treating the declared
set as empty would clobber live bridges over a possibly-transient fault.
Everything else is the client's and is deleted.

**"Host-managed" means the entries the rebuild RE-ADDS, not every name in the
managed map** — the preservation is justified by that re-add, so it reaches
exactly as far. The handler therefore asks the emitter
(`agent.emission_eligible_mcp_servers`, the one predicate both spec writers also
consult) rather than testing membership itself. Two managed entries are NOT
re-added and stay deletable: `kirocrew-dashboard` carries `opt_in`, an assignable
set that `build_agent_config` never emits and that a refresh keeps current without
ever re-granting, so preserving it left the grant unrevocable through the only
surface that can revoke it; and `kirocrew-computer` carries a `spec_gate` that both
writers `pop` while it is shut, so preserving it resurrected the backend the gate
exists to keep unspawned — and the next rebuild removed it again, the two surfaces
disagreeing about one config. A gate that raises counts as shut, matching the
emitter's own fail-closed reading.

The direction is load-bearing. The inverse test — keep anything no mcp.json scope
declares — reads as equivalent but made a server the user added *through this same
editor* permanently undeletable, because it lives only in the installed spec and
the spec is not a scope, so every retry re-read it and put it back. Requiring
evidence costs a bridge nothing, since a bridge is always identifiable. The scope
census plays no part in the decision: an earlier cut subtracted every
scope-declared name ahead of the ownership test as **precedence**, but against
exact manifest names that could only ever remove a name that IS provably owned, so
a user who also declared `demo:notes` in their own mcp.json had every stale PUT
delete app `demo`'s live bridge. Proven ownership outranks a declaration, and a
declared name with no proven owner is deleted by the general rule anyway.

| Case | Outcome |
|---|---|
| scope-declared name that an installed, ENABLED app also declares by exact name, absent | **preserved** — proven ownership outranks the declaration |
| scope-declared name with no proven app or host owner, absent | **deleted** — the general rule; the declaration adds nothing |
| direct entry the user added here, absent | **deleted** — the ordinary lifecycle works |
| `<app>:<server>` of an installed, ENABLED app that declares it, absent | **preserved** — remove it through the app lifecycle, not this editor |
| `<app>:<name>` the app never declared, absent | **deleted** — squatting a namespace confers nothing |
| `<app>:<server>` of a DISABLED app, absent | **deleted** — the disable lifecycle owns bridge removal, so a failed removal must be cleanable here |
| `<app>:<server>` whose app dir or `installed.json` is absent | **deleted** — not installed |
| host-managed server the rebuild always emits (`kirocrew-cron`, `kirocrew-core`, edition extras), absent | **preserved** — the rebuild re-adds it anyway, so removing it here never stuck |
| host-managed server flagged `opt_in` (`kirocrew-dashboard`), absent | **deleted** — an assignable grant no rebuild re-adds (a refresh keeps an existing one current but never re-grants), so revocation must stick here |
| host-managed server whose `spec_gate` is CLOSED (`kirocrew-computer` off or unsupported), absent | **deleted** — both spec writers `pop` it, so the rebuild would not re-add it and preserving it resurrects the backend the gate withholds |
| `installed.json` present but corrupt | **refused** (500, `app_ownership_unreadable`) |
| `installed.json` present but non-regular (broken symlink, directory, unstattable) | **refused** (500, `app_ownership_unreadable`) |
| apps root present but not a directory | **refused** (500, `app_ownership_unreadable`) |
| apps-root child listable but unstattable (e.g. a symlink loop) | **refused** (500, `app_ownership_unreadable`) |
| app manifest unreadable | **refused** (500, `app_ownership_unreadable`) |
| apps directory unenumerable | **refused** (500, `app_ownership_unreadable`) |

Ownership therefore requires **installed AND enabled AND declared**. Enablement is
read through `manager.app_enabled_state`, whose tri-state exists for exactly this
kind of caller — its docstring separates "not installed" from "unreadable" because
collapsing them is the wrong answer for a caller deciding whether to *delete*.
`is_app_enabled` and `list_apps` are both unusable here: each turns an unreadable
record into a plain "no", which silently narrows ownership and deletes that app's
bridges.

A record with no `enabled` field counts as **enabled**, matching the manager's own
parse (`InstalledApp.from_dict` reads `bool(data.get("enabled", True))`) so a legacy
record is treated here exactly as the rest of the tree treats it.

**Absence is proven by `lstat` raising `FileNotFoundError`, nothing weaker.**
`Path.is_file()` / `Path.is_dir()` answer False for a malformed path as readily as
for a missing one, so screening on them alone read a broken symlink or a
directory-where-a-file-belongs as "not installed" and made that app's live bridges
deletable. The shape screen lives at the handler's call site
(`_require_present_shape`), so `manager.app_enabled_state` keeps the contract its
other callers rely on. The apps-root ENUMERATION obeys the same rule: each child is
stat'ed explicitly instead of filtered through `is_dir()`, which routes its fault
through pathlib's `_ignore_error` and returns a plain False for ENOENT, ENOTDIR,
EBADF and ELOOP — so a child that is a symlink loop looked like a regular file and
was skipped, deleting the bridges of the app under that name.

The complete row-by-row table, including which test pins each row, is the docstring
of `dashboard/handlers/agents.py::_app_declared_server_names`.

Scoped to `mcpServers`: all three bridges writers of this file touch that key and
nothing else, so every other key still replaces wholesale. The merge runs *ahead
of* the governance filter, so a preserved entry's `autoApprove` is governed like
any other. An unreadable spec preserves nothing and lets the snapshot land, which
keeps this endpoint the repair path for a corrupt spec.

The flock spans the merge read **and** the spec write, so neither an app
registration nor a deregistration can interleave. That matters asymmetrically:
the deregistration direction never self-healed, because
`reconcile_enabled_app_resources` only re-registers ENABLED apps, so a bridge
resurrected from a disabled or uninstalled app would have persisted indefinitely.
Lock order is unchanged (transaction → config → bridge-file); the widened hold is
the innermost one.

### 1a. The submission may not CREATE a name in the `<app>:<server>` region

Every row above is about a name the submission **omits**. The mirror question is a
name the submission **contains**, and until #7089 the answer was "persist it
verbatim": an editor tab that loaded while an app was installed and running
re-created that app's bridge on save after the app had been uninstalled or
disabled. Same non-self-healing direction as above — reconciliation only revisits
ENABLED apps — so the resurrected bridge stayed live and callable.

`_drop_unbacked_app_entries` closes it with one rule: **a submitted name containing
`:` that the installed spec does not hold is dropped**, because that region is
reserved *from this endpoint* — the raw editor is not one of its writers. Two paths
legitimately put a colon-containing key here: `_register_mcp_servers` (every app
bridge, as `<app>:<server>`) and the MCP page's
`handlers/mcp.py::_sync_mcp_to_agent_unlocked` (a global mcp.json server copied in
under `mcp_server_alias`, which returns a slash-free name unchanged and so keeps a
colon). A name either of them actually wrote is present on disk, and therefore
untouched.

| Submitted `mcpServers` entry | Verdict |
| --- | --- |
| plain name (no `:`), not on disk | **persisted** — this is how the user adds a server here |
| `<app>:<server>` on disk | **persisted as submitted** — the snapshot still wins where the platform agrees the name exists |
| `<app>:<server>` NOT on disk, app uninstalled | **dropped** — `_deregister_mcp_servers` removed it |
| `<app>:<server>` NOT on disk, app installed but DISABLED | **dropped** — same, and reconciliation never revisits it |
| `<app>:<server>` NOT on disk, app installed, ENABLED and DECLARING it | **dropped** — `_register_mcp_servers` skips an HTTP server with no live port and scrubs stale rows for it; a manifest's illustrative port is a dead URL that breaks every kiro session |
| host-owned name containing `:` (an edition extra), not on disk | **persisted** — the host's key, not an app's; the host axis is unchanged |
| any, spec readable but carrying no `mcpServers` key | **dropped** — a keyless spec holds no bridge, which is a definite answer; reading it as "unknown" lets the resurrection through |
| any, spec unreadable, or `mcpServers` present but not an object | **persisted** — best-effort, so this endpoint stays the repair path for a corrupt spec, and nothing is deleted on evidence that cannot be read |

The declared-name census is deliberately **not** consulted on this axis, and the
last row above is why: it would rescue exactly the entry the registration path
scrubbed on purpose. The two directions ask different questions — the absent axis
must name an owner before KEEPING something the client asked to remove, while here
every candidate is one the client is ADDING to a region it does not author, which
the on-disk map answers alone. A consequence: this rule performs no manifest I/O
and cannot raise `app_ownership_unreadable`, so it adds no new failure mode. Both
rules read the spec **once**, through `_on_disk_mcp_servers`, so they cannot
disagree about their baseline.

The cost is that a name containing `:` can no longer be introduced through this raw
editor; the MCP page is the path that adds a server, after which the name is on
disk and this rule leaves it alone. Nothing becomes unremovable — an entry on disk
stays deletable through the absent-axis rule.

**Not closed here:** where the name is on disk and the submission carries a
SUPERSEDED definition of it (an older port or command), the submitted row still
wins and reverts a correction the registration path had made. Fixing that reverses
the editor-snapshot-wins contract kept in #5899, and unlike resurrection it
self-heals on the next gateway start, so it is left to a separate ruling.

What the ruling is weighing, since "less severe than resurrection" understates it:
the reverted value is the exact artefact `_register_mcp_servers` refuses to write
and scrubs on sight — a `backend.port:"auto"` app's illustrative manifest port,
i.e. a reachable-LOOKING dead URL whose cost that path states as breaking *every*
kiro session, not just this app's. Two facts set the window. The PUT's own tail
calls `_reset_all_sessions`, which drains every active session **and** the warm
pool, so the next cold start reads the reverted row rather than the revert lying
dormant. And the only writer that puts the live port back is
`reconcile_enabled_app_resources`, whose single call site is the gateway boot path
(`dashboard/server.py`) — the mid-turn rung `_recover_app_agent_binding` is gated
on an UNRESOLVED agent binding, which a reverted port does not produce. So the
self-heal is a restart, and nothing shorter. Both axes above plus this open cell
are enumerated in one table by
`test_the_app_namespace_region_decides_every_axis_it_claims_to`, so a change to
any of them has to come through it.

Writer: `apps/bridges.py::_apply_agent_mcp_policy`, `_mcp_json_path`,
`_scrub_legacy_shared_mcp`;
`dashboard/handlers/agents.py::_merge_unowned_servers` and
`_drop_unbacked_app_entries` for the PUT side.

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

### 7.1 A hung startup lifecycle hook is bounded at the dispatch boundary

`LifecycleDispatcher` invokes startup hooks **serially** in lexicographic app-name
order, so one hook that never returns would stall the whole loop and keep the
gateway from ever reaching readiness. An awaited startup coroutine is therefore
bounded by a per-hook deadline (`lifecycle._HOOK_TIMEOUT_SEC`, 30s). The task
is registered in the dispatcher-owned map keyed by app immediately when it is
created, before the dispatcher first awaits it, so parent cancellation cannot
orphan live work during the pre-deadline window. A completion observer retrieves
its result and removes proven-terminal ownership. On deadline expiry the same
owned task remains tracked while startup continues; the dispatcher does not wait
indefinitely for it to settle and does not cancel it. Cancelling an asyncio task
awaiting `asyncio.to_thread` makes the wrapper terminal while its worker thread
still executes app code, so any observed child cancellation records a
process-lifetime residual marker before releasing the task reference. The timeout
is treated as a hook failure (the app is marked degraded and the event is
SEL-recorded with `outcome="timeout"`), and startup **continues with the remaining
apps**. The deadline is a safety net against a hang, not a performance budget,
which is why it is generous.

The per-app ownership is also a teardown contract. Dashboard disable, local or
registry install, update, uninstall, and registry replacement perform a bounded
ownership preflight and return a retryable refusal without mutating app state when
retained startup execution cannot be proven stopped. Trust withdrawal also remains
bounded, but if the hook continues executing, teardown fails and revocation leaves
the trust grant in place for a retry rather than claiming the app stopped while its
code remains live. In that failure case an app-declared `on_shutdown` is skipped:
it is unbounded third-party code and must not overlap the still-running startup
hook against partially initialized state.

Normal recovery is to retry after retained startup execution exits. If a startup
hook is permanently wedged, the operator must **stop the gateway completely**,
run `kirocrew app disable <name>` while no gateway process can execute app code,
and then restart the gateway. The CLI command only writes `enabled=false` to
installed-app metadata; it is not runtime teardown and must not be run against a
live gateway as evidence that old app code stopped. The disabled app is skipped
on the next startup, allowing the operator to repair or remove it without
re-entering the wedged hook.

Graceful gateway shutdown sweeps retained startup ownership for **every enabled
app**, including apps with no `on_shutdown` declaration. All ownership checks
start concurrently and are non-blocking: ownership that is already terminal
permits that app's shutdown hook, while active or residual startup work skips only
the affected hook fail-closed. The sweep consumes none of the gateway's remaining
10-second cooperative shutdown window, preserving time for unaffected app hooks
after the existing bounded slot-history save; the service manager's separate
10-second signal margin is not hook-execution time. An app's `on_shutdown` is
invoked in reverse lexicographic order only after that app's startup ownership
settled; otherwise the hook is skipped rather than running concurrently against
partially initialized state. Once an `on_shutdown` hook is invoked, it is
intentionally not detached at the deadline: the task still owns its `AppContext`
capabilities, so teardown awaits it to completion. The startup deadline governs
**async hooks only**: a synchronous hook returns before the
`asyncio.iscoroutine` check and runs to completion outside the timeout; a
successful async startup hook that returns within the deadline is unaffected.

Writer: `apps/lifecycle.py::LifecycleDispatcher._invoke`.

After the hook sweep, graceful shutdown stops the backend **processes this
gateway spawned** (`apps/hooks_integration.py::on_gateway_shutdown` →
`stop_app_backend`). Spawned backends are gateway children: without this stop
they reparent to PID 1 when the gateway exits and keep listening on their
ports, and the startup stale-reap only recovers them at the **next** boot.
Ordering is deliberate — hooks first, so an app's `on_shutdown` still has its
own backend alive. Stop targets come from the runtime tracking table
(`apps/backend.py::spawned_backend_names`), never from persisted `enabled`
metadata: the metadata filter is wrong in both directions (it would signal an
**adopted** externally-managed backend, whose contract is to survive gateway
exit and be re-adopted on the next start, and it would miss a still-running
child whose app was disabled cross-process, metadata-only). Driving the sweep
from the tracking table also keeps `stop_app_backend`'s pidfile-record erasure
away from apps with nothing running, so a retained prior-generation orphan
record stays recoverable by the stale-reap. The stops are offloaded to the
subprocess executor and run **concurrently under one shared deadline**
(`_BACKEND_STOP_BUDGET_SECS`, kept under the gateway's 10-second cooperative
shutdown budget): a serial sweep would multiply the per-app SIGTERM grace by
the number of apps, and the supervisor's force-exit would orphan every backend
the sweep had not reached. The sweep runs in a `finally` around hook dispatch,
so a wedged or failing `on_shutdown` hook (dispatch awaits an invoked hook to
completion) cannot skip it — the shutdown deadline's cancellation still reaches
the sweep on its way out. The stop futures are shielded from the deadline: the
executor is shared with the rest of shutdown, so a stop can still be queued
when the budget fires, and cancelling it then would mean that backend is never
signalled at all — instead the sweep returns and the stops finish in the
background. The sweep is not gated on the lifecycle dispatcher being
initialized, and one app's failing stop does not skip the rest.

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
source of truth for the exemption (`projects`, the Task Runner, and `command-bar`,
which replaces the quick-search gesture rather than adding a sidebar entry),
read by the policy tests over both the hardcoded list and the file-based
manifests, so a builtin cannot become default-on through one registration path
while the other path's test still forbids it. A default-enabled builtin is
persisted at first registration and never routes through `enable_app`, so the
governance `apps` activation allowlist is re-applied at that write: a
governance-denied app registers disabled.

`defaultEnabled` is read on FIRST registration only — every later start preserves
the user's own state — so ADDING a name to the exemption reaches new installs
alone. An install that registered the app while it was still default-off keeps
`enabled: false` through every restart, update and version bump, because the
record lives in the user's data home and a code update does not touch it. For a
builtin that replaces a host surface that is terminal rather than inconvenient:
it has no page, so it is absent from the launcher's own app list, and it is
absent from Discover unless the published catalog carries a row, which leaves a
disabled row in Library as the only trace of an app the user never heard of.

`manager.backfill_default_on_builtins()` closes that gap, invoked from
`agent.run_first_run_setup()`. It reads a SECOND set, `_DEFAULT_ON_BACKFILL`, and
the separation is load-bearing rather than bookkeeping: `_DEFAULT_ON_BUILTINS`
answers "what does a fresh install enable", while this one answers "which
promotion has not yet reached installs that predate it". Reading the first for the
second question reverses deliberate opt-outs — `projects` has shipped
`defaultEnabled: true` since it was aligned with the other builtins, long before
the allowlist existed, so it has been enabled and visible in the sidebar on every
existing install and a disabled record for it is a user who found it and turned it
off. A name qualifies only when a fresh install enables it AND existing installs
were never in a position to choose; a ratchet test pins both halves.

The backfill is ONE-SHOT, and the record of that is
`InstalledApp.defaultOnBackfilled`, written in the SAME atomic record write that
flips `enabled`. One document deliberately: a separate marker file has no correct
ordering, because whichever of the two writes goes first leaves a window the other
one owns — marker-last loses the record of an enable that happened, so every later
start re-applies the promotion and reverses the user's own disable forever;
marker-first can outlive a flip that failed, so the app is skipped forever and the
promotion is never delivered. Both are real failures; neither is reachable when the
flag and the state it guards land or fail together. A record created under the
promoted default is born `True`, because a first registration with `defaultEnabled`
IS the promotion being received — without that, "install, disable the app in the
same session, restart" would re-enable it, and no write ordering can fix that since
on a fresh install the record may not exist yet when first-run setup runs.
Disabling the app must survive, since it is the only thing that gives a replaced
host surface back. Both the `_builtin_owns_install` boundary (a user's own install
under the same name is never touched) and the governance activation gate are
re-applied, and a governance-denied app is deliberately left unflagged so the
promotion is still delivered if policy later permits. The activation is recorded in
the SEL, because it moves an app from inactive to active with no user request
behind it.

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

`stargazersCount` follows the same precedent, because it is a trust cue: only
the official catalog's publish step may mint it (baked at publish for
git-source entries from the GitHub API, bounded to the JS safe-integer range),
and `_apply_trust_fields` strips it entirely from external rows — an external
index self-reporting a count would render identically to a publisher-verified
one, and a false trust cue is worse than none. Client-side the count enters
through `official_catalog.inventory()` **alone**: the one projection where a
row's identity (repository) and its count come from the same catalog entry.
`annotate()` matches rows by name — a same-name seed row can pin a different
repository, so it never overlays the count — and the cache-served
`list_catalog_rows()` never carries it (the cache is agent-writable). The
count is **frozen at publish time**: it refreshes only when the catalog
republishes (each publish re-fetches; the content-digest revision changes
with it), so it is a trust-scale indicator, not a live metric. Absence means
"unknown" and renders nothing — never zero.

Writers: `apps/manager.py` (`_BUILTIN_APPS`, `_DEFAULT_ON_BUILTINS`,
`_DEFAULT_ON_BACKFILL`, `register_builtin_apps`, `backfill_default_on_builtins`),
`agent.py::run_first_run_setup`, `apps/discovery.py::discover_builtin_apps`,
`apps/registry.py::_apply_trust_fields`;
consumers: `website/src/pages/apps/useAppsData.ts` (`pickFeatured`),
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
counts-and-environment only. The `cron_jobs` and `lessons` counts are nullable:
`null` means the gateway's count refresh has not succeeded yet (unknown), never
zero — app consumers must treat `null` as "no data", not `0`. The checkout's `branch`/`commit` are stripped for app
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

**Implicit self-ownership stops at the shared literal routes.** Beyond the
declared `permissions.api` allowlist, `_app_owns_path` grants an app token
implicit ownership of its own namespace on both the reverse-proxy/UI surface
(`/apps/<name>/...`) and the per-app management surface (`/api/apps/<name>/...`),
via a path-boundary match so `foo` cannot reach `foo-bar`. That implicit grant is
carved back on the `/api/apps` surface for the literal first path segments that
resolve to a SHARED route registered before the `/api/apps/{name}` catch-all:
`RESERVED_APP_PATH_SEGMENTS` in `token_auth` holds `registry`, `registries`,
`blob`, `install`, and `register`, and the `/api/apps/<name>` branch of
`_app_owns_path` refuses to match when the app name is one of them. Without the
carve-out an app that named itself after such a segment, for example `registries`,
would implicitly own that segment's endpoints, including the state-changing
`POST /api/apps/registries/refresh` that triggers outbound git fetches of every
configured registry, with no `permissions.api` grant at all (CWE-269
authorization bypass). The carve-out is the primary boundary and binds even an
app already published under one of these names; the `/apps/<name>` reverse-proxy
branch is a distinct namespace whose literal-page reservations live in
`RESERVED_ROUTE_APP_NAMES` and is intentionally left unchanged. As
defense-in-depth for NEW apps, `apps/manifest.py` mirrors the same set as
`RESERVED_APP_PATH_SEGMENTS` and rejects those names at validation
(`app_name_error`, `is_reserved_app_name`); reserving a name is a one-way door, so
that backstop only refuses names not yet admitted while the carve-out covers any
already-published one. The two sets are duplicated rather than shared to avoid a
`manifest` <-> `token_auth` import cycle and must stay in sync with the
`/api/apps/` literal routes in `apps/routes.py`.

Writers: `dashboard/ws_event_scope.py`, `dashboard/ws.py` (connect-time scope
resolution), `dashboard/state.py` (`_send_ws_all`, `_ws_client_allowed`,
`_serialize_for_client`, `SlotOrigin`), `dashboard/token_auth.py`
(`_APP_TOKEN_IMPLICIT_ALLOW`, `app_token_path_allowed`, `_app_owns_path`,
`RESERVED_APP_PATH_SEGMENTS`), `apps/manifest.py`
(`_granted_list`, `RESERVED_APP_PATH_SEGMENTS`); consumers: `website/src/app-sdk/index.ts` (mirrors the tables
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

That degradation is silent -- a failed fetch overwrites the on-disk cache with a
failure sentinel and the seed listing renders with no error -- so the store
header carries a manual refresh. `POST /api/app-store/refresh`
(`handle_registry_refresh`) drops the on-disk caches of all three published
documents (catalog, category order, editorial) via each module's
`forget_cache()`, which clears a stale document and a `_fetchFailedAt` back-off
sentinel in one unlink. The handler never fetches: the follow-up
`GET /api/apps/registry` pays the fetch on the cold-start path, so a refresh
cannot behave differently from the load it repairs. It is a POST rather than a
query parameter on the GET because cache deletion plus outbound fetches is a
state change, and a state-changing GET is reachable by cross-site top-level
navigation behind the CSRF middleware's back. The path sits outside
`/api/apps/` because that namespace grants an app token implicit ownership of
`/api/apps/<its-own-name>/*` (`token_auth._app_owns_path`): an app named
`registry` must not inherit the power to purge the shared catalog caches. The
same exposure reached the fixed-segment siblings that do stay under `/api/apps/`
(`registries`, `install`, `register` — e.g. `POST /api/apps/registries/refresh`,
claimable by an app named `registries`), and that whole class is now closed by
`RESERVED_APP_PATH_SEGMENTS` (§13): the `_app_owns_path` carve-out refuses those
segments outright, mirrored by a manifest-time name reservation. Route placement
remains the stronger guarantee for a NEW shared endpoint, which is why this one
keeps it — a path outside `/api/apps/` cannot collide with any app name at all,
so it does not depend on a hand-maintained segment list staying in sync with the
route table. The
dashboard's refresh button
also posts `/api/apps/registries/refresh` (the external-registry index sweep),
then refetches, so both of the store's sources are rebuilt by one click.

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

**Execution consent is repository-bound for new grants.** Registry and installed
app responses carry a server-overwritten `trustRepository`: the normalized clone
target the grant handler itself would resolve, never an index/manifest assertion.
This field is deliberately separate from the legacy/display `repo` alias because
an entry may legitimately carry a different `gitUrl`, and `_entry_git_url`
prefers `gitUrl` for the actual clone. The trust dialog displays
`trustRepository` and posts it back as consent proof.
`POST /api/security/trusted-apps/{name}` freshly resolves the target and requires
the normalized proof to match before it records that target in
`agent.apps_trusted_repositories` beside the name in `agent.apps_trusted`.
Missing or stale proof for a repository-backed app is refused. A local installed
app with no repository remains grantable with no proof and records its grant kind
in `agent.apps_trusted_local`; this prevents a pre-binding name-only grant from
silently becoming local consent after a same-name takeover. The stored repository
uses the same normalization as catalog supersession (scheme/host case-folded,
trailing slash and `.git` removed, path case preserved). HTTP(S) userinfo is
removed in full. Username-only SSH/git+ssh userinfo and scp-style
`user@host:path` are retained because they select transport routing. Git passes
colon-bearing SSH userinfo to OpenSSH as the complete username rather than
treating the suffix as a password, so executable and governance paths reject it
instead of rewriting it to a different identity. SCP likewise has no password
field: in a no-scheme target whose first colon precedes `@`, Git treats the text
before that colon as the host and everything after it as the path. Such an
ambiguous target is rejected rather than rewritten to a different host. URI
query and fragment components are omitted from the durable/API identity because
free-form sources can carry provider tokens there. A registry clone target with
either suffix or an ambiguous SSH/SCP identity is rejected before trust
comparison or fetch: displaying one identity while transporting another would
allow repository rebinding and trusted-host bypass. Legacy installed provenance,
catalog fallback,
or stored bindings containing any of these unsupported forms are inert: they
cannot produce consent proof, compare equal, or pass the runtime gate and require
revoke plus fresh consent. Clone-host trust parses bracketed IPv6 literals in URI
and scp forms as validated canonical full addresses; malformed brackets/non-IPv6
contents fail closed, and sharing a first hextet never grants host equivalence.
`install_from_registry` compares its freshly resolved row against that value
before manifest fetch, credential selection, clone, build, or setup code. A bound
mismatch returns `app_trust_repository_mismatch` and requires revoke plus fresh
consent without returning either repository coordinate. A legacy name grant with
no binding is inactive for every repository-backed source — including the same
repository it historically used — and returns `app_execution_denied` so the
normal consent dialog can create the missing binding. A still-installed app with
positively local provenance retains migration compatibility; an unknown/fresh
same-name local source does not inherit that old grant. The commit is deliberately
not bound: a new pin in the same repository is the ordinary catalog update path
and does not warrant a new consent prompt.

A name is a filesystem path on install, so `inventory()` and
`list_catalog_rows` drop any entry whose name fails the manifest name contract
(`app_name_error` / `KEBAB_RE`), and the catalog fetch runs off the event loop
(`asyncio.to_thread`) so a cache-expired request never blocks the gateway loop.

Writers: `apps/official_catalog.py` (`list_catalog_rows`, `inventory`,
`fetch_inventory_entries`, `inventory_for_install`), `apps/registry.py`
(`list_catalog_apps`, `_resolve_registry_row`, `_git_fetch_commit`,
`_append_external_registry_apps`, `_detect_installed_probe`),
`dashboard/handlers/security.py` (`api_trusted_app_grant`),
`apps/routes.py` (`handle_registry`, `handle_list_apps`, `handle_get_app`), and
`website/src/components/appstore/TrustAppModal.tsx`.

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

## 16. Store guidance and product screenshots are manifest-owned

An App detail page carries three different kinds of information and does not
substitute one for another:

- `highlights` describes capabilities;
- `useCases` says when an operator should reach for the App;
- `configuration` says how to make it usable, including prerequisites that live
  outside the page (provider CLIs, credentials, desktop shell, or another App).

All three remain English in `app.json` so catalog-less consumers such as the CLI
print meaningful copy. Builtins resolve them through the frontend catalogs; the
manifest/catalog sync gate derives `use_case_N` and `configuration_N` keys and
requires the English values to stay byte-identical. External apps fall back to
their manifest copy because a third-party app id is not first-party provenance.

Store artwork and proof are likewise distinct. `heroImage*` is illustrative
banner art, while `screenshots*` must be a capture of the real App UI. The detail
page prefers the wide `heroImageDetail*` banner when present and renders the
screenshot gallery independently. Registry manifests project `useCases` and
`configuration` as display metadata and rewrite repo-relative screenshot and
hero paths through the same-origin blob proxy.

**An INSTALLED app's art is served from its own install directory, not the blob
proxy.** `GET /apps/{name}/art/{path}` (`handle_app_art_file`) reads the bytes
the install itself wrote, and the four surfaces that render an installed app's
art — the left rail and command palette (`appNav`), the Library card, the Updates
worklist, and the detail page — resolve through `installedArt` /
`installedArtList`.

The proxy reaches those same bytes by a **git clone gated by an SSRF allowlist**,
which is the wrong mechanism for a file already on local disk and had a visible
failure mode: the allowlist is warmed by a network fetch a page render can
outrun — the Library card list gates only on the installed-apps query while the
catalog fetch rides the separate store listing — and an `<img>` does not retry a
403, so a catalog-listed app's art vanished for that paint. Reading the file has
no ordering, needs no network, survives a CDN outage, and names no host. It also
takes the repo identifier off the art path: an app with no registry row, no
manifest `repo` and no recorded `sourceUrl` now renders its real icon where
before it fell back to the generic box.

Two narrowings against `handle_app_ui_file`, whose shape this follows:

- **Images only** — `_ART_IMAGE_EXTENSIONS`, which is deliberately NOT
  `_ALLOWED_EXTENSIONS`: that set (the UI-bundle route's) admits `.json`, so
  reusing it on a route rooted at the install directory would also serve
  `installed.json` and `app.json`, a widening paid to show an icon. The blob
  proxy screens on the same set, and that parity is load-bearing rather than
  incidental — this route REPLACES the proxy per surface, so a file one serves
  and the other refuses would make the same app's art render or 403 depending
  only on whether it happens to be installed. One set, pinned by
  `test_both_art_paths_screen_on_the_SAME_extension_set`, which asserts on
  MEMBERS so a duplicate under any new name fails.
- **Declared paths only** — the path must equal one the manifest names
  (`iconPath`, `heroImage`, `heroImageDetail`, `screenshots`, and their `Dark`
  variants, with a leading `./` normalized off). The manifest, not the request,
  chooses the file, which is what lets the route carry no traversal reasoning of
  its own. Containment is still checked, because a manifest is the app's own
  untrusted content and a declared value is not evidence the file lands inside
  the directory. Undeclared, escaping and missing all answer one 404, so a probe
  cannot map a manifest's declarations by status.

The containment check catches `OSError`, `RuntimeError` and `ValueError`, not
`ValueError` alone. `Path.resolve()` raises `RuntimeError` — not an `OSError` —
on a symlink loop, so a declared self-referential link (or a mutual pair) would
otherwise leave the handler as a 500 on a route whose every other refusal is a
clean status; the app that plants the link is the one whose art this serves, so
that input is exactly what the endpoint reads. None of the three is a different
ANSWER: all mean the path is not servable, which is the same 404 as undeclared
and missing. The publisher's `EditorialAssets.add` in KiroCrewApps documents the
same `RuntimeError` trap from the other side of the same operation.

**The route serves BYTES read under a pinned descriptor, not a path.** Validating
a path and handing it to `web.FileResponse` opens it a SECOND time, so the app that
owns the install directory can swap a declared name for a symlink between the check
and that open and have the gateway read the target instead — and the gateway is not
sandboxed, so that launders a read the app's own code can be refused. Checking a
path and then acting on a re-resolution of it is worse than not checking, because
it reports success.

So `_read_declared_art` does one open through `pinned_fs.open_in_pinned_parent`
(one `openat` per component, each carrying `O_NOFOLLOW`), validates the
**descriptor** with `fstat`, reads from it, and the handler serves those bytes.
Three consequences worth stating:

- A symlink at the declared final name is refused **even when its target is a
  legitimate file inside the root**. It is the indirection that is refused, not the
  destination, because only the indirection is swappable.
- Containment on the resolved parent is still required and is not redundant with the
  pinned walk. `pin_parent`'s contract is explicit that a component swapped BEFORE
  the parent was resolved is followed by that resolution, so an ANCESTOR that is
  already a link is refused by the containment check alone.
- The bytes are held rather than streamed, so `_ART_MAX_BYTES` caps what one
  declared file can make the gateway buffer, and the `ETag` is derived from the
  descriptor that was read rather than from a second stat of the path. `no-cache`
  means the browser revalidates on every load and the rail renders on every load, so
  without that validator each load would be a full 200 instead of a 304.

On Windows the pinned walk is unavailable (no `O_NOFOLLOW`, no descriptor-relative
open), so the route refuses any reparse point between the root and the target and
then opens by path. That narrows the window rather than closing it, and it narrows
it against what the platform actually permits: creating a FILE symlink there needs
elevation (the reason `platform_compat.symlink_or_junction` exists), so the
reachable swap is a junction on an ancestor, which the reparse-point probe covers.

**Every guard tuple around a path operation carries `ValueError`, and that is not
redundant with `OSError`.** `os.open` raises `ValueError` — never an `OSError` — for
a name the OS layer cannot encode, and there are two reachable classes: an embedded
NUL (`ValueError`) and a lone surrogate (`UnicodeEncodeError`, a `ValueError`
subclass). Such a name survives every earlier check, which is what makes it
reachable: the extension allowlist reads the suffix AFTER the bad byte
(`bad\x00.png` → `.png`), and containment resolves the PARENT, which is clean when
the bad byte sits in the final component. So the open is the first thing that
touches it, and uncaught that is a 500 on a route whose every other refusal is a
clean status.

The guard is on the EXCEPTION, not on the character, and the surrogates are why:
screening NUL at the door would admit every one of them. Three sites need it — both
opens and the `realpath` on the parent — and each is pinned separately, because a
mutation on one reddens only its own cases. The Windows branch needs a test that
forces `supports_pinned_walk()` False, since on a POSIX runner that branch never
executes and its guard was untested until one existed.

**The descriptor check is `S_ISREG` AND `st_nlink == 1` AND the size cap**, and the
nlink half is the only one that can see a HARDLINK. An alias shares its target's
inode, so every path-based guard is blind to it: `is_symlink()` is False, `realpath`
yields the alias's own name so containment passes, and `O_NOFOLLOW` has no link to
refuse. Measured before it was added: a declared `assets/icon.webp` hardlinked to a
file outside the install directory opened cleanly, reported `S_ISREG`, sat under the
cap, and its bytes were served with a 200 — laundering, through an unsandboxed
gateway, a read the app's own sandboxed code can be refused. Every other
descriptor-validated read in the tree applies the same gate (`hooks.py`, `memory.py`,
`spec_builder`, `onboarding_import.py`, `pinned_fs.copy_file_pinned`), so this route
was the outlier rather than a new rule.

Spelled inline rather than through `pinned_fs.refuse_hardlink_alias`, which is the
same check behind an exception: that helper CLOSES the descriptor before raising and
this function closes in a `finally`, so routing through it would double-close — and a
reused descriptor number makes that worse than the bug it fixes. The sibling sites
spell it inline for the same reason: their refusal is a return value, not a raise.

**The response carries its own `Content-Security-Policy` and `nosniff`, and the CSP is
load-bearing rather than decoration.** `.svg` is in the allowlist because an SVG in an
`<img>` is script-inert — but a TOP-LEVEL NAVIGATION to an art URL makes the response a
DOCUMENT on the dashboard's own origin, and the dashboard's `_BASE_CSP` is deliberately
permissive there (`script-src 'self' 'unsafe-inline'`, so widget and MCP-app iframes can
run inline script). It would therefore NOT stop a scripted SVG an app declared as its
art, and same-origin script reaches the authenticated dashboard API with the viewer's
session.

So the handler answers with `default-src 'none'; sandbox`: `sandbox` with no tokens
gives the document an opaque origin and no script, and `default-src 'none'` stops it
fetching anything. A response CSP does not apply when the bytes are consumed as an
`<img>` subresource, so the store's own rendering is unaffected. `nosniff` is part of
it because the `Content-Type` is derived from the EXTENSION, not the bytes — without it,
art named `.png` whose content is markup could still be sniffed into a document.

Set on the response rather than in the middleware because
`dashboard/server.py`'s security-header middleware uses `setdefault` precisely so a
handler can tighten its own answer. Applied to EVERY art response, not only `.svg`: a
per-extension shortcut is one `if` away from a gap, and a mutation that narrows it to
`.svg` is one of the cases pinned.

The same exposure exists on `/api/apps/blob` and `handle_app_ui_file`, which also serve
`.svg` from this origin with no per-response policy. Both are pre-existing and tracked
rather than widened from here.

**The deferral against `handle_app_ui_file` is wider than the CSP, and stating only the
CSP half would understate it.** That route serves the same app-owned tree by
`resolve()` + `relative_to` and then RE-OPENS by path through `web.FileResponse`, so it
carries the identical check-then-reopen window this function exists to close — with no
pinned open, no `st_nlink` gate, and a wider extension set (`.js`/`.json`/`.mjs`/`.css`).
It is therefore weaker on EVERY platform, not only where the pinned walk is unavailable.
Both halves — the missing response policy and the TOCTOU/hardlink exposure — are
deliberately left to that route rather than fixed from here, because it is not a route
this function's change otherwise touches. Anyone hardening it should read the invariant
list above as the checklist, and should not copy `default-src 'none'` onto it without
first confirming nothing loads a UI asset as a document rather than a subresource.

**`O_NONBLOCK` is in the open flags because it is what makes those checks
reachable.** Opening a FIFO blocks until a writer appears, and the handler runs inside
`asyncio.to_thread` — so an app declaring a FIFO as its icon path parks a thread-pool
worker forever, and enough such requests starve every other blocking call in the
gateway. The descriptor checks cannot help, because the block happens BEFORE `fstat`.
Measured: the open hangs indefinitely without the flag and returns immediately with
it, after which `S_ISREG` refuses the FIFO; on an ordinary file the flag changes
nothing. Its guard test bounds itself with `asyncio.wait_for`, so a regression fails
the suite instead of hanging it.

**`iconUrl` is not an art path, and the frontend must not treat it as one.**
`_ART_MANIFEST_FIELDS` carries `iconPath`/`iconPathDark`, never
`iconUrl`/`iconUrlDark` — because for a FETCHED app `iconUrl` is ignored by design,
so a publisher cannot name a host the client would load. A surface that built
`/apps/<name>/art/<relative iconUrl>` would therefore produce a URL this route
refuses by construction: a guaranteed 404 dressed as a fallback, and an internal
contract disagreement where the frontend asks for what the backend cannot answer.

So `useHeroArt` exposes two resolvers and the caller picks by what the field MEANS,
not by which happens to be imported:

| helper | reads | relative value | absolute same-origin value |
|---|---|---|---|
| `installedArt(path, name)` | `iconPath`, `heroImage*`, `screenshots*` | `/apps/<name>/art/<path>` | passed through |
| `clientLocalArt(path)` | `iconUrl`, `iconUrlDark` | **refused** | passed through |
| `installedIcon(path, url, name)` | both icon fields, in that ORDER | — | — |

Both route through the one `classifyManifestArt`, so the cross-origin refusal rules
cannot drift between them. `clientLocalArt` refusing a relative value is what keeps a
builtin's absolute `/app-assets/…` working (its primary path) without inventing an
art URL for a field the backend never declared.

`installedIcon` exists because the two-term icon rule is needed at eight call sites
(rail, command palette, Library card, Updates list, detail page — light and dark each)
and spelling it out at each one is not stable: four of them resolved `iconPath` first
and four resolved `iconUrl` first, so a manifest declaring BOTH wore one icon in the
rail and a different one on its own Library card. Nothing went red, because the order
is observable ONLY for a manifest that declares both. `iconPath` wins, being the field
that addresses a file inside the install directory; `iconUrl` is the fallback. The
order now exists in exactly one place, and a mutation flipping it reddens one test
rather than needing a test per surface.

The handler's own `..` guard is load-bearing rather than belt-and-braces:
measured against the real router, `../x` and `%2e%2e/x` are normalized away
before matching, but `assets/..%2fx.webp` reaches the handler with
`match_info['path'] == 'assets/../x.webp'` because the encoded slash stops that
normalization. The whole decision runs in one `asyncio.to_thread` hop — manifest
read, declaration check and containment are each a blocking syscall, and the
gateway runs everything on one loop (`no-blocking-call-on-event-loop`).

The route's verb must also appear in `token_auth._APPS_SPA_EXCLUDED_RE`, which
enumerates the `/apps/` sub-namespaces that have real handlers. A verb missing
from it is classified as a React Router navigation, so the middleware answers the
SPA shell and an `<img>` receives HTML with a 200 and renders nothing — silent,
because the handler is never the thing that fails. The pre-existing drift guard
cannot catch this (it scans `server.py` only, and its `"{" in p` escape hatch
treats any pattern route as a real handler without consulting the regex), so
`test_apps_routes_get_paths_are_matched_by_the_apps_spa_regex` instantiates each
`/apps/` route literal in `apps/routes.py` and matches the concrete path.

The blob proxy keeps serving a **not-installed** external-registry row, which
genuinely has no local copy; that half of `known_registry_repos` is untouched.

Writers: builtin `app.json` manifests, `apps/registry.py` (`_merge_manifest`),
`apps/routes.py` (`handle_app_art_file`),
`website/src/components/appstore/appManifest.ts`,
`website/src/components/appstore/useHeroArt.ts`,
`website/src/pages/AppDetailPage.tsx`, and
`website/scripts/check-app-manifest-sync.mjs`.

## 17. A backend's health is a standing observation, not a startup verdict

`healthy` on the `AppProcess` record is what the reverse proxy gates on
(`get_app_backend_port` returns the port ONLY while the flag is set) and what
`/api/apps` reports. Establishing it once at startup would make it a write-once
cache: a backend that died an hour later would keep collecting proxied requests
into a dead port, and the dashboard would keep calling it healthy, with no
transition anywhere in the tree able to say otherwise.

So the per-backend daemon thread that waits for the backend to come up does not
exit when it does — it keeps watching, at the much coarser
`_HEALTH_WATCH_INTERVAL`, for as long as the record stays tracked. The two phases
are `_health_check_loop` (bounded startup poll) and `_watch_backend_health`
(unbounded watch), joined by `_supervise_backend_health`.

- **Liveness is cheapest-first, and only a dead process is decisive.** A backend
  we spawned is judged first by `Popen.poll()`, which answers from an
  already-reaped exit status without touching the app. An exited process cannot
  recover on its own, so one observation demotes it and the watch stops. An
  **adopted** backend has no `Popen` handle — it belongs to another supervisor —
  and is judged by its health endpoint alone.
- **An HTTP failure from a live process is not decisive.** A backend can be
  briefly busy, so demotion needs `_HEALTH_WATCH_FAILURES` consecutive misses;
  demoting on a single miss would let one slow response take a working app
  offline.
- **Demotion is reversible.** The watch keeps running after it demotes and
  re-promotes on the next successful probe, which is what lets a backend that
  wedged briefly heal with no operator action.
- **Both directions move the MCP entry**, through the same
  `_gate_mcp_registration` branches the startup phase uses: a demotion scrubs the
  app's HTTP MCP url so kiro-cli does not dial a dead port on every session, and a
  recovery re-registers it. The scrub goes through the **no-live-port registration**
  path, not a blanket deregister: an app's stdio/command servers are launched by kiro-cli
  itself and have no port to be dead, so removing them because an HTTP backend died would
  take working tools away for a reason unrelated to them. That path pops each HTTP entry
  and keeps the rest, and its port lookup is health-gated, so it cannot resurrect the
  port it is removing. It calls `_register_mcp_servers` DIRECTLY rather than
  `reregister_app_mcp_servers`, because the latter also re-materializes the app's agents
  — an ungated write that would land before the caller's enablement check and make a
  disabled app's agents dispatchable in the gap. The scrub owns the mcp.json half only;
  the agent refresh belongs to the caller, which gates it. The admission gate is applied
  explicitly here so a denied app still gets a FULL removal rather than the selective
  keep-stdio treatment. It falls back to removing EVERY entry for the app when the
  manifest cannot be resolved or declares no servers: that case cannot tell a
  backend-dependent server from an independent one, and the dead url must not survive on
  the strength of not knowing. The fallback **never deletes the app's materialized
  agents**. Deleting them is unrecoverable — it takes the user-owned fields
  `_preserve_user_agent_edits` carries across every refresh — whereas what it would
  prevent, an agent naming a server that is gone, costs failed tool calls until the next
  refresh with a readable manifest rewrites it. An unreadable manifest is frequently
  TRANSIENT, so destroying data over it trades a temporary fault for a permanent one.
  `deregister_app` is the path that legitimately owns removing those files.

**An adopted backend registers through the serialized transition BEFORE its watch is
armed.** Registering afterwards — from a caller that returns and leaves the work queued —
lets the watch demote and scrub in between, after which the queued write restores the
dead url and, having bypassed `_set_backend_health`, leaves `mcp_healthy` agreeing with a
state that is no longer on disk, so nothing retries. **The scrub also re-materializes the app's agents**, because
  an agent JSON COPIES the server's launch spec and the agent config is what kiro-cli
  loads — clearing the global map alone leaves the dead url one file over. Registration
  already refreshes agents for this reason; the scrub mirrors it by calling the SAME
  `refresh_app_agents`, which carries the two guards this path must honour: an app with
  `resources="app"` publishes its own agents and the gateway must not duplicate them, and
  a denied app's agents are scrubbed rather than rewritten back into dispatchable
  existence. Both return an empty list, which is "nothing to do" rather than a failure.
  **Registration owes the same guarantee**: it writes the same agent JSONs, so an agent
  write that failed there leaves the app's agent without the MCP tools the registration
  was supposed to make reachable. Both directions collect I/O failures and report the
  reconcile unlanded, so the watch retries either way. A FAILED refresh makes
  the whole reconcile unlanded, so the watch retries it. Registration treats its own
  refresh as non-fatal, but that path has no retry behind it — there, non-fatal means
  "do not fail the registration". Here a re-scrub is idempotent and cheap, and the
  alternative is a dead url left permanently in the file kiro-cli actually reads. An app
  with no resolvable manifest has nothing materialized to correct and counts as
  reconciled, since retrying that would never converge. The same distinction applies
  WITHIN the refresh: `_register_agents` skips a failing agent and continues, so it
  reports the agents it could not read or write for **I/O** reasons and only those make
  the reconcile unlanded. Its other skips — a path escaping the app root, an unsafe agent
  name, malformed JSON, an unresolved placeholder — are permanent refusals, and treating
  "declared minus registered" as the retry signal would spin forever on an app that can
  never converge.
- **The probe treats a malformed HTTP response as a failed probe, not an error.**
  `http.client.HTTPException` is NOT an `OSError` or `URLError` subclass (only
  `RemoteDisconnected` is, via `ConnectionResetError`), and `urllib` re-raises
  `getresponse()` failures unwrapped — so a `BadStatusLine` from a port answering with a
  non-HTTP first line escapes a socket-errors-only catch. An app backend is arbitrary
  third-party code, including `exec` backends and adopted processes, so that is a real
  condition rather than a hypothetical one. Both probes catch it. The watch additionally
  survives an unexpected fault in any single sweep, because the failure MODE is the bug
  itself: a dead watch thread freezes `healthy` at its last value and silently restores
  the write-once behaviour, with the proxy still routing to a port nothing serves.
- **Every health verdict uses the same loopback-only probe boundary.** Adoption,
  startup polling, and the standing watch pass the manifest's app-authored
  `healthCheck` through `_health_probe_url`: the port must be valid and the value must
  be an absolute path whose restricted character set cannot move the URL authority or
  be silently normalized by the parser. Rejected paths fail closed as unhealthy and
  warn once per distinct value. The shared `_health_probe` opens accepted URLs through
  `loopback_urlopen`, which ignores HTTP proxy environment variables and rejects
  redirects, so a probe cannot be redirected or proxied away from `127.0.0.1`.
- **Every writer of an app's MCP and agent state shares one serialization.** Two
  independent families write it: the lifecycle paths in `apps/bridges.py` (enable,
  update, boot reconcile) and the backend's health watch. Unserialized they interleave
  their DECISIONS — each performing a correct read-modify-write, with the stale one
  landing last — so `_register_mcp_servers`, `_deregister_mcp_servers` and
  `_register_agents` all acquire `health_reconcile_lock()`. It is an **RLock**, because
  the health path already holds it before calling into those writers; a plain Lock
  deadlocks the watch thread there. Agent materialization holds it across the READ as
  well as the write, since an agent copies the ambient server spec and a read taken
  before a scrub could otherwise be written after it. The order is always this lock
  first, then `_lock` or `_mcp_lock` — never the reverse — which is what keeps the two
  families from deadlocking against each other.
- **A promotion requires a positively confirmed ENABLED app; a demotion never does.**
  `kirocrew app disable` runs in its own process: it deregisters the app's resources and
  never touches the gateway's tracking table, so the record survives and a later health
  recovery would re-register the MCP servers and agents the operator just removed. The
  check fails CLOSED — an unreadable enabled-state refuses the promotion rather than
  guessing, because guessing wrong makes a disabled app dispatchable again. Demotion is
  deliberately ungated: refusing a scrub because enablement could not be read would
  strand the dead url this whole mechanism exists to remove.

  The flag has to be flipped BEFORE the resources come down, or the check reads a stale
  `enabled` while they are already gone. The gateway's own disable path has no such
  window — `teardown_app_runtime` stops the backend first, which pops the record and ends
  the watch — but `kirocrew app disable` runs in ANOTHER process and cannot pop it, so it
  closes the window by ordering: `disable_app` then `deregister_app`.

  Ordering closes one interleave; it cannot close the other, where the check passes and
  the disable completes before the write lands. There is no lock to share across
  processes, so the promotion is **verified after the write** and undone through
  `deregister_app` when the app turns out to have been disabled meanwhile. Check-act-
  verify is the convergence available here, and the undo is idempotent with the CLI's own
  deregistration. `deregister_app` reports most problems SOFTLY, in
  `RegistrationResult.errors` rather than by raising, so the undo reads that list and
  moves `mcp_healthy` to False only on a complete removal — otherwise a failed cleanup
  would read as done and a disabled app would stay dispatchable with nothing left to
  retry. The refused-promotion path retries an unlanded undo on every sweep, because
  nothing else revisits a disabled app.

  A demotion's **scrub** is never gated or verified, for the same reason: removing a
  disabled app's entry is the desired outcome, not something to roll back. Its **agent
  refresh is**, and the distinction is the point — the scrub REMOVES, while the refresh
  RE-MATERIALIZES, so for a disabled app it writes back the very files a concurrent
  `deregister_app` just removed. That refresh is checked before and after and undone if
  the disable wins, and the cleanup's OWN result is the reconcile's result, since
  `deregister_app` reports softly and discarding it would record a removal that never
  happened.

  The identity guard runs BEFORE the enablement check, because the undo deregisters by
  app NAME: an unreadable enabled state is exactly what would otherwise send a retired
  watcher into deleting the SUCCESSOR's resources.

  Enablement is **tri-state** — enabled, disabled, or unreadable — read through
  `manager.app_enabled_state`, NOT `is_app_enabled`. That distinction is the whole
  mechanism: `_read_installed` returns None for both a missing file and a caught read
  error, so `is_app_enabled` collapses "unreadable" into False WITHOUT raising. Building
  the tri-state on it left the unknown branch unreachable for precisely the transient
  fault it exists to catch. The two callers want opposite defaults on that third state. Refusing to ADD when it is unknown is
  safe: the app stays as it is. DELETING when it is unknown is not, since the cleanup
  unlinks materialized agents and `installed.json` can fail to read transiently, so
  collapsing "unknown" into "disabled" would destroy user edits over a temporary fault.
  Unknown therefore refuses a promotion AND refuses a deletion, reporting unlanded so the
  watch retries. That applies at **every** deletion site — the demotion path and both
  undo calls in `_set_backend_health` — since all three reach the same
  `deregister_app` → `_deregister_agents` → unlink. A demotion does not even read the
  state: it is a file access with no bearing on a scrub.

  A **transition always reconciles**, even when `healthy` and `mcp_healthy` agree. That
  flag can be stale in the other direction after a partial reconcile — an MCP write that
  landed followed by an agent write that did not leaves it unmoved while the entry is on
  disk — so matching it across a flip would skip the scrub and strand the dead url. The
  short-circuit is valid only when the verdict did not change, which is what keeps a
  settled backend from rewriting `mcp.json` every sweep.

  An **unreadable manifest leaves the scrub unlanded.** Keeping the agents there is
  right, but it leaves them naming the server just removed, and `refresh_app_agents`
  gives up on the same condition — so nothing else revisits those files. Reporting it
  unlanded makes the watch retry until the manifest is readable and the refresh can
  correct them, instead of recording a job that was only half done.
- **Teardown participates in the same serialization.** `stop_app_backend` takes
  `_health_reconcile_lock` across its pop, so it and a reconcile are mutually exclusive:
  either the reconcile finishes and the pop follows it (the caller's subsequent
  `deregister_app` then wins), or the pop lands first and the reconcile's identity check
  fails. Without that, a watcher already past its identity check could still be inside
  the MCP write when the caller scrubbed, and its write would land after — restoring the
  dead url this gate exists to keep out of `mcp.json`.
- **An ADOPTED recovery re-binds ownership before it promotes.** `adopted_pids` is what
  `stop_app_backend` signals and what uninstall acts behind, and it was captured at
  adoption. A recovery means the EXTERNAL supervisor put something back, possibly a
  different process — so the owner set is re-captured through the same consistency
  sandwich adoption uses, and the promotion is REFUSED when ownership cannot be
  confirmed. Unhealthy-but-serving is recoverable on the next sweep; a record that claims
  freshly-valid ownership of a process that is gone is not, because stop would then
  signal the wrong PIDs while the live replacement keeps running.
- **The watch is bound to the RECORD, not the app name.** A stop/start installs a
  new `AppProcess` under the same name with its own watch, so every transition is
  guarded by `_processes.get(name) is ap`. Resolving by name instead would let a
  retiring watch demote a successor it never observed. The same guard is how the
  watch terminates — `stop_app_backend` pops the record — so it needs no separate
  teardown, and a stale watcher exits before it can probe a port some other
  backend has since taken.
- **A reconcile that did not land is retried, not stranded.** The health flag moves
  whether or not the mcp.json write succeeded, so gating the reconcile on the health
  *transition* alone would leave a transient failure uncorrected until the next
  transition — which for a backend that then stays put never arrives, leaving either a
  dead URL kiro-cli dials every session or a live backend with no MCP entry.
  `AppProcess.mcp_healthy` records the value last SUCCESSFULLY written — **tri-state**,
  where `None` means *unknown* (no write has been confirmed) and only `False` means
  confirmed-scrubbed, so it must never be read as a plain boolean. Each sweep
  reconciles when the verdict changed **or** when that record is behind the verdict.
  The terminal exited-process path consults it too, and does not return until the entry
  is reconciled or the record is dropped. That path is the one place where giving up is
  permanent — nothing revisits an exited backend — so returning on an unlanded scrub
  would strand the dead URL for kiro-cli to dial on every session.
- **The startup poll belongs to ONE generation, bound at the spawn.** The supervisor is
  handed the `AppProcess` itself and derives both name and port from it; every attempt
  re-checks that the record is still the tracked one. A name plus a port are two
  independent inputs that can DISAGREE — a restart landing between the record's insertion
  and the supervisor thread's first statement would give a name lookup the successor
  while the port argument still named the predecessor, and the poll would then act on a
  backend whose port it never probed. Re-resolving by name — even per attempt — lets a stop/start inside the ~30s
  startup window hand a later attempt the successor, which the poll would then promote,
  or on exhaustion scrub, on evidence gathered entirely about its predecessor, having
  never probed the successor's port. The exhaustion scrub is identity-guarded for the
  same reason: it deregisters by app NAME, and because a bypassing scrub never touches
  the successor's record, its `mcp_healthy` would still read True and the retry above
  would never fire to put the entry back.
- **That identity guard has to stay effective THROUGH the MCP reconcile**, which is
  why every health transition goes through `_set_backend_health`. The MCP writers key
  on the app NAME, not on the record — `_deregister_mcp_servers` removes every
  `<app>:` entry — so checking identity only alongside the flag write would leave a
  window in which a retiring watcher scrubs the SUCCESSOR's live servers, or
  republishes its own dead port. `_lock` cannot just be held across the reconcile: it
  does manifest and config file I/O, so the reverse proxy's `get_app_backend_port`
  would block behind it on every request, and the `bridges ↔ backend` import cycle
  makes it a deadlock risk. The ordering is established with a separate
  `_health_reconcile_lock` that every health-driven reconcile takes, including the
  startup registration. That is what makes it sufficient: passing the identity check
  proves the successor is not yet in `_processes` and so has not registered, and its
  own registration must then queue behind the retiring one — so the last write always
  belongs to the live record.

**Teardown stops the backend before scrubbing its resources.** `stop_app_backend` pops
the tracking record, and that pop is what ends the watch's authority to reconcile MCP for
the app — so it has to happen BEFORE `deregister_app`. Scrubbing first leaves a window in
which a backend that recovers re-registers the OLD manifest's servers, and entries the
update removed survive it. Uninstall and the disable rollback already ordered it this
way; the two update paths in `apps/routes.py` now match them, and
`test_update_stops_the_backend_before_deregistering_resources` pins it.

`backend_status.running` on `/api/apps` is likewise read off the record
(`AppProcess.is_running`) rather than asserted from the record merely existing.
For an adopted backend, where we hold no handle to poll, "we still track it"
is the only honest answer and `healthy` is the load-bearing signal.

Writers: `apps/backend.py` (`_health_check_loop`, `_watch_backend_health`,
`_demote`, `_promote`, `_supervise_backend_health`, `_start_health_supervisor`,
`_start_adopted_health_watch`, `AppProcess.is_running`), `apps/routes.py`
(`handle_list_apps`).
