# App Kit platform contracts (agents, MCP scoping, window entries)

Last Updated: 2026-08-01 (initial spec: the contracts a gateway-managed app relies on — where its MCP servers land, how its agent JSON is materialized and refreshed, how a generated prompt is pinned, how standalone window entries are served, and how `permissions.spawn` is gated)

Everything here is **generic App Kit surface**, not one app's arrangement: each
item is what the FIRST app to need it exposed, and every later app builds on the
same contract. The manifest field reference lives in
[../../app-kit/manifest-reference.md](../../app-kit/manifest-reference.md); this
document is the behaviour and the one-way doors.

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
