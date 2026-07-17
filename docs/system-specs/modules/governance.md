# Governance Model (two-level Policy ∩ Profile)

The `kiro_crew.platform.governance` + `kiro_crew.platform.governance_profiles`
modules implement KiroCrew's **two-level security governance model**. Governance
is resolved by a single rule — *the tightest boundary wins*:

- **Level 1 — POLICY** (`GovernanceCeiling`): the enterprise security ceiling,
  loaded once at boot from a trust-root path the agent process does not own.
  Once present, the running app **and its agent cannot weaken it**.
- **Level 2 — PROFILE** (`Profile`): a per-surface / per-app / per-task scope
  that may only *narrow* what policy permits.

The effective permission for any item is `policy ∩ profile`. This spec is the
implementation companion to the design doc (Pippin `kirocrew/MVTDhLpm2SSW`).

> Scope: this governs **KiroCrew's own** security boundaries — what the host
> performs on behalf of the agent across every surface (CLI, dashboard, Slack,
> cron, heartbeat, sub-agents, apps). The underlying kiro-cli agent config
> (`~/.kiro/agents/*.json`) is **out of scope**: KiroCrew enforces its own
> ceiling at its own gate even when the kiro side grants more.

## The four archetypes (one composition algebra each)

Every governed control is exactly one of four shapes. The evaluator dispatches
on archetype, never on a scope *name* — this is what keeps the model decoupled
and extensible (adding a scope is data, not engine code).

| Archetype | Shape | Composition (policy ∘ profile) |
|---|---|---|
| `ScopedRuleset` | `{mode, allow[], deny[]}` | Rule 1 within a level (allow beats deny); Rule 2 across (allow = ∩, deny = ∪) |
| `OrdinalControl` | a single enum value | strictest-of, on an **enforcer-owned** scale |
| `CapabilityGate` | `{enabled, scopes{…ruleset}}` | `enabled` = AND; each scope is a ScopedRuleset |
| `ScopedMap` | `{members: ruleset, posture{…}}` | members = ScopedRuleset; `posture` is policy-only |

**Enforcer-owned registries** (never sourced from a governed file, so no profile
can reorder strictness or redefine matching):

- `_ORDINAL_SCALES`: `approval = yolo < auto < interactive`;
  `sandbox = off < standard < cc < strict` (verified against `sandbox.py`).
- `_MATCHERS`: `identifier` (case-insensitive), `command` (case-sensitive
  `fnmatchcase`), `path`, `host`, `mcp` (a `@server` grant covers `@server/tool`).
  The `path` matcher normalizes **only the queried item** (`_norm_item`: expand
  `~`/`$VAR` → `os.path.abspath`, which anchors a relative path to the host CWD
  and collapses `.`/`..`) and matches it against the operator's pattern **expanded
  but otherwise verbatim**. This does two jobs and avoids one trap (CR-284272012):
  (1) a `..` traversal cannot satisfy an allow-prefix (`/home/u/ws/../.bashrc`
  collapses to `/home/u/.bashrc` and no longer matches `/home/u/ws/**`, which an
  un-normalized `*` would wrongly span); (2) an agent-supplied **relative** item
  is absolutized so it can still match an absolute *deny* glob (`../../etc/passwd`
  cannot dodge `/etc/**` by failing to match). The pattern is **never** run
  through `normpath` — `normpath` treats `*`/`**` as ordinary segments and would
  collapse an adjacent `..` against them (`/a/**/../b` → `/a/b`, silently dropping
  the `**`), widening an allow or shrinking a deny. Normalization is purely
  lexical (no filesystem `resolve()`), so it is mode-safe and adds no I/O; the
  `abspath` anchor cannot reconstruct an ACP backend's actual CWD, so the
  resolved `is_sensitive_path` keystone remains the separate, always-on,
  authoritative block for the trust-root / credential dirs. `_norm_item` also
  collapses a leading `//` to `/` (POSIX leaves a two-slash prefix
  implementation-defined and `normpath` preserves it, so `//etc/passwd` would
  otherwise dodge a `/etc/**` deny while the OS opens `/etc/passwd`).

  **Path matcher — lexical-only contract.** The `path` matcher does **not**
  resolve symlinks (no `realpath`): a symlink lexically inside an allow-prefix
  (`<allow>/link -> <secret>/key`) passes the matcher even though the OS write
  lands outside the allow-list. This is intentional — resolving would add I/O to
  every gate call and refuse writes through operator-placed symlinks. Treat
  allow-mode prefixes as a **lexical scoping aid, not a hardened sandbox against
  symlinks**; the resolved `is_sensitive_path` keystone is the authoritative
  guard for trust-root / credential dirs, and operators must not rely on an
  allow-mode prefix to confine writes in a directory containing untrusted
  symlinks.

`SCOPE_CATALOG` is the single place a scope name binds to its archetype +
matcher. `register_scope` / `register_matcher` are append-only extension seams;
the test suite proves a synthetic scope resolves end-to-end with **zero**
evaluator edits.

## Loading + precedence

`load_security_policy()` precedence (first present wins):

1. `KIROCREW_SECURITY_POLICY` env path — fleet hot-override, highest.
2. companion-bundled resource (the `amazon` edition packages it; the public core
   passes `None`).
3. `~/.kirocrew/security_policy.json` — standalone operator-authored.
4. none → `None` → editable secure-defaults (ungoverned ceiling).

A **present-but-unreadable / invalid** policy raises `PlatformCompositionError`
(fail-closed to strictest), mirroring `admission.load_admission_policy`. Parsing
is **pure-Python and structural** (it does not depend on `jsonschema`, which is
an optional, possibly-absent dependency) so a malformed policy never silently
degrades to ungoverned.

## Boot composition

`build_default_context` (the single chokepoint backing both a real boot and the
lazy `current_context` default) calls `load_security_policy()` and stores the
result in the frozen `PlatformContext.governance` field. `CONTRACT_VERSION`
stays **1** (pinned pre-launch — the companion rebuilds in lockstep, so the
mismatch guard always compares `1 == 1`; see `platform-context.md`). Every
enforcement chokepoint reads `current_context().governance`.

## Self-protection (the keystone)

Under *"secure by default, not by mandate"* there is **no compiled-in floor** —
the entire posture is operator-editable. The only invariant is the
**agent-vs-operator split**: the agent cannot edit the policy/profile files.
This is enforced solely by adding them to `security._SENSITIVE_HOME_DIRS`
(`~/.kirocrew/security_policy.json`, `~/.kirocrew/profiles`,
`~/.kirocrew/admission_policy.json`) — `is_sensitive_path` is the shared
read+write gate across every surface. `assert_governance_paths_protected()` is a
boot integrity check that fails closed if a refactor ever drops them.

## Profile resolution + binding

A profile binds to a `surface` (cron/slack/dashboard/subagent/…), an `app` slug,
or a `task` id. `resolve_active_scope(session_key, agent, app)` resolves the
active profile, classifying the session key via `sel._infer_source` (the single
canonical taxonomy parser — never re-implemented). Resolution is:

- **app bind → task/agent bind → surface bind** (most specific first).
- No bound profile on an **attended/proven** surface → `None` (policy alone).
- No bound profile on an **unattended + unproven** surface → `deny_all_profile`
  (fail-closed, never a permissive fall-through), mirroring the dashboard
  `api_session_tool_policy` precedent.

**`host` surface (in-process host actions).** A governance check that is not
driven by a user-facing surface — app activation
(`apps.manager._app_activation_denied`) and Slack workspace admission
(`slack.enterprise`) — runs under the `_host` sentinel session key, which
classifies to surface `host`. Operators can bind a `surface:host` profile to
narrow these on top of the policy ceiling (e.g. an `apps` allowlist that further
restricts which apps may activate). NOTE: these callers used to pass an empty
session key, which mis-classified to `slack` and accidentally picked up
`surface:slack` profiles; they now use the honest `host` surface, so a
`surface:slack` profile no longer governs host-side app activation. The Slack
posture check itself stays policy-only (a profile cannot carry `posture`,
Rule 6).

Profiles hot-reload via an mtime fingerprint (`ProfileStore`); a schema-invalid
profile falls back to deny-all (Validation rule 5), **not** the ceiling.
`extends` is monotonic narrowing (`compose_profiles`).

## Enforcement planes

- **Plane A — the host gate** (`HookManager.on_tool_call`, the primary
  chokepoint). After the always-on sensitive-path + deny-floor checks, it
  evaluates `gate_decision(ceiling, profile, title)` — **including MCP titles**
  (`mcp__server__tool`, converted to `@server/tool`) — *before* the auto-approve
  loop, so a governance deny wins over a user auto-approve and denies a tool the
  kiro agent config granted. The call sites thread `session_key`/`agent` (they
  default to `""`, so non-governed callers are unaffected).
- **Plane B — kiro agent JSON**: out of scope (v1). The gate is authoritative;
  KiroCrew does not regenerate `~/.kiro/agents/*.json`.
- **Plane C — out-of-band executors**: the cron `command` (runs via `sh -c`
  outside the ACP flow) is gated in `mcp_cron._vet_command_governance`; the
  cron *capability* on/off gate in `mcp_cron._vet_cron_capability_governance`
  (at `cron_add`); the sandbox ordinal floor is clamped in `sandbox.wrap_argv`;
  spawn in `subagent._vet_spawn_governance`; outbound messaging in
  `mcp_core._vet_messaging_governance` plus the per-transport `channels` check
  in `mcp_core._vet_channel_governance`; durable memory writes in
  `mcp_core._vet_memory_writes_governance` (at `learn_add`); script-hook
  execution in `hooks._script_hooks_capability_denied` (at `run_script_hook`);
  app activation in `apps.manager._app_activation_denied` (at `enable_app`). All
  route through the same `governance_permits` / `governance_floor_ordinal`
  decision source.

### Filesystem + egress at the host gate (tool kind + real args)

`filesystem.read` / `filesystem.write` / `network.egress` are enforced at the
**host gate** (`HookManager.on_tool_call` → `gate_decision`), not at a separate
per-call chokepoint, because every tool call already passes through that gate on
every surface. The display *title* is backend-variable and cannot reliably carry
a path or URL, so these scopes are resolved from the tool's **semantic kind +
real arguments** the ACP event carries:

- A `Reading <path>` title classifies to `filesystem.read` (the read path is in
  the title); `classify_tool_args` also maps `tool_kind == "read"` +
  `raw_params["path"]` → `filesystem.read`.
- `tool_kind == "edit"` + `raw_params["path"]` → `filesystem.write`.
- `tool_kind == "fetch"` + `raw_params["url"]` → `network.egress` (the host is
  extracted from the URL so the `host` matcher applies).

`on_tool_call(..., tool_kind=, raw_params=)` carries these from the ACP event
(`AcpEvent.tool_kind` / `.raw_tool_params`); the call sites thread them
(`llm_helpers`, `subagent`, `task_executor`, `task_planner`, dashboard
`chat_runner`, slack `handler`). **The `EVENT_PERMISSION_REQUEST` event the gate
runs on must carry `raw_tool_params`** — `acp/client.py` caches the structured
rawInput at the ToolCall notification (`_tool_call_params`, keyed by
`toolCallId`) and attaches it to the later permission event, because that
message itself carries only a truncated title. Without this the two arg-derived
scopes would be inert in production.

The `kind` field is **spec-optional**: some ACP backends omit it (it arrives
`""`). `classify_tool_args` therefore falls back to the param SHAPE when the kind
is unknown — a `url` (and no shell `command`) → egress; a `path` (and no
`command`) → BOTH `filesystem.read` and `filesystem.write` (it cannot tell read
from write without the kind, so it applies both ceilings; an ungoverned one
permits, and a `command` param routes to the `commands` scope, never filesystem).

This keeps the existing always-on `is_sensitive_path` keystone (the fixed
credential/trust-root block) in force regardless — **and extends it**: the gate
now runs `is_sensitive_path` on the real `raw_params['path']` too, so an edit to
`~/.ssh`, `~/.aws`, or the governance trust-root files is blocked even when the
display title hides the path. The per-policy path/host rulesets compose **on
top** of this keystone.

> **`folders.*` vs `filesystem.*`.** The profile `folders.read`/`folders.write`
> are **aliases** of the policy `filesystem.read`/`filesystem.write` path scopes
> (the profile schema names them `folders`; the policy names them `filesystem`).
> They are normalized to `filesystem.*` at parse time (`_SCOPE_ALIASES`), so a
> profile's `folders.write` actually narrows the `filesystem.write` ceiling the
> gate queries (both present in one file → intersect). Without the alias they
> would land in separate control keys and silently fail to compose.

### Channels posture (per-transport identity ceiling)

`channels.posture.slack.allowed_enterprise_ids` (policy-only) is enforced in
`slack.enterprise.validate_enterprise`: a workspace must satisfy the governance
posture in ADDITION to the operator's `config.json`
`slack.allowed_enterprise_ids`. The posture is the **agent-unweakenable**
ceiling (the config allowlist is operator-editable; the policy posture is not).
Default-open when no policy posture is configured.

An **empty** id is fail-closed against a *pinned* leaf: Slack returns
`enterprise_id=""` for every non-Enterprise-Grid workspace, and an empty id
cannot satisfy an explicitly-configured allowlist, so it must be DENIED rather
than skipped. `_governance_posture_permits_workspace` distinguishes "leaf is
pinned" from "id is provided" by probing the posture with a sentinel value no
real id can equal: if the leaf is an allow-mode allowlist the sentinel is denied
(pinned → close), otherwise it permits (unpinned → the empty id is fine).

### Audit

Every new chokepoint denial emits a `governance_decision` SEL record (file-
backed, so safe even in the stdio MCP server) via `log_governance_decision`,
matching the host-gate deny path — so cron/script-hook/memory/channel/app
denials leave the same forensic trail.

### Scope boundaries (documented, not gaps)

- **`network.egress` governs the dedicated fetch tool only.** A `fetch`
  tool-kind call is classified to `network.egress` by host. Command-driven
  egress (`curl`/`wget`/`nc` inside a Bash tool) arrives as `tool_kind ==
  "execute"` and is governed by the **`commands`** scope (the command body),
  not `network.egress` — a policy that wants to bound shell egress denies the
  relevant `commands` patterns. This is the same plane split the rest of the
  model uses (a shell command is a `commands` item, never re-parsed into its
  sub-effects).
- **Per-app profile binding via MCP chokepoints is best-effort.** The managed
  `kirocrew-core` MCP server is spawned by kiro-cli, not by an app backend, so
  `KIROCREW_APP_NAME` is absent there — `learn_add`/`send_message` resolve the
  per-SURFACE profile + policy ceiling (the enforced path), not a per-app
  profile. An app's own in-process tool calls (which carry `KIROCREW_APP_NAME`)
  do bind a per-app profile. App blast-radius is contained today by the `apps`
  activation allowlist + per-surface profiles.

### Still-reserved in v1

- **`approval_mode`** — the ordinal is parsed and **boot-floor-checked** (a
  profile looser than the policy mark aborts boot, like `sandbox.min_level`), but
  no approval chokepoint clamps the *live* approval pipeline through it yet: the
  live approval vocabulary (`""`/`auto` in cron; the dashboard trust toggles) is
  not yet reconciled onto the `yolo < auto < interactive` scale. The boot floor
  is the enforced half; the live clamp is the reserved half. Wiring it is the one
  genuinely-architectural follow-up (a single approval-policy resolution point
  fed by `governance_floor_ordinal("approval_mode")`).

> **Capability `profile-absence` semantics (deliberate deviation from spec A.4
> rule 8).** The spec says a profile that OMITS a capability defaults it to
> `false`. KiroCrew instead treats an omitted scope as *not governed by the
> profile* (truth-table "not-governed" → bounded by policy alone), because the
> stricter reading would turn every minimal profile (e.g. one that governs only
> `tools`) into a near-deny-all of all capabilities. To disable a capability a
> profile sets `enabled: false` explicitly, or uses the deny-all built-in. This
> is intentional and documented here rather than silently divergent.

The **enforced** scopes in v1 are: `tools`, `mcp`, `commands` (host gate + cron
command body), `filesystem.read` / `filesystem.write` / `folders.*` and
`network.egress` (host gate via tool kind + args), `channels` (per-transport at
the messaging chokepoint), `apps` (app activation), `sandbox.min_level` (ordinal
floor at `wrap_argv`), `approval_mode` (boot floor only), and every capability
gate — `capabilities.spawn`, `capabilities.messaging`, `capabilities.cron`,
`capabilities.memory_writes`, `capabilities.script_hooks`. Only the live
`approval_mode` clamp remains reserved.

## Audit

`sel.log_governance_decision` records a `governance_decision` event
(`outcome ∈ {allowed, denied}` — the existing permit vocabulary). On-disk SEL is
not redacted by the writer and the HMAC chain signs the bytes as written, so the
operation / item / reason are redacted via `redact_via_context` **before** `log`.

## CLI

`kirocrew policy {show | validate | explain <scope> <item> | profile <name>}` —
read-only operator diagnostics. `explain` traces the rule/layer/reason and the
live gate verdict. Deliberately **not** exposed as an MCP tool: it surfaces
governance internals that the agent (the governed subject) should not enumerate.

## Companion (separate package, separate CR)

The `amazon` companion contributes the restrictive posture as its
**bundled `security_policy.json`** (precedence step 2) rather than as code;
capability providers (Midway/SigV4/tunnels) and the SharePoint redaction
carve-out stay as code. It expects `CONTRACT_VERSION == 1` (pinned pre-launch).

## Files

- `platform/governance.py` — archetypes, catalog, loader, evaluator
  (`resolve`, `resolve_ordinal`, `gate_decision`, `assert_governance_floor`,
  `compose_profiles`).
- `platform/governance_profiles.py` — `ProfileStore` (hot-reload),
  `resolve_active_scope`, `governance_permits`, `governance_floor_ordinal`.
- `security.py` — `_SENSITIVE_HOME_DIRS` keystone entries.
- `hooks.py` — Plane A gate threading.
- `sel.py` — `log_governance_decision`.
- chokepoints: `sandbox.py`, `mcp_cron.py`, `subagent.py`, `mcp_core.py`.
- `cli.py` / `cli_commands.py` — the `policy` command.

## Tests

`test_governance_policy.py` (archetypes + loader + evaluator + E1–E13 vectors +
extensibility), `test_governance_boot.py` (compose at boot), 
`test_governance_self_protection.py` (keystone), `test_governance_profiles.py`
(resolution + binding + hot-reload), `test_governance_gate.py` (Plane A
enforcement + audit), `test_governance_chokepoints.py` (sandbox/cron/spawn/
helpers + egress-reserved).
