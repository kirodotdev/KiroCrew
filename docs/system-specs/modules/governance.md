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
  but otherwise verbatim**. This does two jobs and avoids one trap:
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

> **2026-07-18 governance-seam re-triage.** The re-triage of the 16 upstream CPP
> commit groups added **zero `SCOPE_CATALOG` rows** and **did not touch the
> evaluator** — its seam work was confined to `platform/interfaces.py` /
> `defaults.py` (IdentityProvider / CredentialPolicy / TunnelProvider method
> additions) and their consumption sites, none of which are governed scopes. The
> only capability scope in the catalog that post-dates the original governance
> model, `capabilities.publish` (below), arrived via **PR #14** (artifacts
> mirror), **not** this re-triage. See `platform-context.md` for the design
> record.

## Loading + precedence

`load_security_policy()` precedence (first present wins):

1. `KIROCREW_SECURITY_POLICY` env path — fleet hot-override, highest.
2. companion-bundled resource (the `amazon` edition packages it; the public core
   passes `None`).
3. `~/.kiro/crew/security_policy.json` — standalone operator-authored.
4. none → `None` → editable secure-defaults (ungoverned ceiling).

The home path (step 3) is resolved through the **lazy `_policy_home_path()`
accessor**, never a module-level `config_dir()` capture — so importing
`platform.governance` (or `platform.admission`, whose `_policy_default_path()` /
`_seed_marker_path()` / `_checksum_path()` follow the same pattern) never
triggers `config_dir()` and thus never fires the one-time data-home migration as
an import side effect. The migration runs only at the single chosen point
(`ensure_data_home()` in the CLI prologue, before any `asyncio.run`), keeping the
platform layer side-effect-free load-bearing infrastructure. Tests patch these
accessors, not captured constants.

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
(`~/.kiro/crew/security_policy.json`, `~/.kiro/crew/profiles`,
`~/.kiro/crew/admission_policy.json`) — `is_sensitive_path` is the shared
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
(`apps.manager._app_activation_denied`), Slack workspace admission
(`slack.enterprise`), and non-Slack transport startup
(`slack.gateway._channel_transport_permitted`) — runs under the `_host` sentinel
session key, which classifies to surface `host`. Operators can bind a
`surface:host` profile to narrow these on top of the policy ceiling (e.g. an
`apps` allowlist that further restricts which apps may activate, or a `channels`
allowlist that narrows which transports may connect below what the ceiling
permits). NOTE: these callers used to pass an empty session key, which
mis-classified to `slack` and accidentally picked up `surface:slack` profiles;
they now use the honest `host` surface, so a `surface:slack` profile no longer
governs host-side app activation or transport startup. The two policy-scope
chokepoints (app activation + transport start) audit their decisions via
`sel().log_governance_decision` (`governance_permits` audits only its own
degrade, never a normal permit/deny); Slack workspace admission audits via a
different sink (`log_api_access`, see below). They also differ on the ERROR
disposition:

- **App activation (`apps.manager._app_activation_denied`)** audits a DENY and,
  on an unexpected governance error, **fails open** (degrades to permit + an
  `audit_governance_degraded` record) — the app's own enable guard still applies
  and wedging host boot on a governance hiccup is worse.
- **Inbound message receive (`messaging.identity.channel_inbound_permitted`)**
  gates each transport's per-message dispatch on the SAME `channels` allowlist,
  resolved on the host surface with `fail_closed=True` and run OFF the event loop
  (it walks the ProfileStore). Called at the top of every dispatcher's
  `handle_message` (Slack / Discord / Telegram / Webex / WeCom — Slack is NOT
  exempt), it closes the gap the connect-time gate alone leaves: a host-profile
  deny added AFTER a transport connected would otherwise keep dispatching inbound
  messages until restart. On deny the message is silently dropped
  (no reply), matching how an unauthorized user is ignored; `PlatformCompositionError`
  propagates. Default OSS build (no `channels` policy) permits, so inbound handling
  is byte-identical to today.
  **Audit disposition:** a GOVERNED allow is audit-or-deny (`critical=True` — a SEL
  persistence failure denies the inbound, so a governed channel never receives
  unaudited); every DENY is recorded best-effort. The **ungoverned default-permit
  is deliberately NOT recorded**: this gate is on the per-message hot path of five
  transports (including observe-mode traffic the bot merely sees), so auditing it
  would append one HMAC-chained SEL row per message on every install with no
  governance configured — hot-path write amplification that also drowns real
  governance signal. Nothing was governed, so there is no decision to record.
- **Transport start (`slack.gateway._channel_transport_permitted`)**
  audits BOTH the allowed and the denied decision and **fails closed**: it passes
  `governance_permits(fail_closed=True)`, and its outer error branch also denies
  (`return False` + `audit_governance_degraded(failed_closed=True)`), so a
  transport connects ONLY on a positive permit. This deliberately DIVERGES from
  app-activation and `mcp_core._vet_channel_governance` (both fail open) because a
  transport is an externally-reachable network surface — deny-by-default on any
  error is the safer posture there, and a transport that fails to start leaks
  nothing. `fail_closed=True` is the same disposition the authorization/admission
  chokepoints use (e.g. `capabilities.publish` in `handlers/artifacts.py`,
  `capabilities.theme_install` in `handlers/themes.py`, `capabilities.theme_persona`
  in `chat_runner.py`) where a wrong permit lets bytes leave the box or ingests
  untrusted content. The ALLOW audit is disposition-split: a **governed** allow
  (a policy/profile governs `channels`, detected as the Decision's
  `layer ∈ {policy, profile, both}`) is **audit-or-deny** — written
  `critical=True` (synchronous + raising) so a SEL persistence failure propagates
  and DENIES the start (the default background writer swallows disk failures, so
  `critical` is required for the guarantee to be real); an **ungoverned** allow
  (no policy governs `channels` — the default OSS build) is **best-effort** so OSS
  transport availability never depends on SEL disk health. The deny audit is
  best-effort (the transport is not starting either way). The governed check keys
  on `layer`, NOT `rule`: `resolve()` returns `rule="rule2-intersect"` for EVERY
  permit — including the case where a policy exists but does not govern
  `channels` — so a `rule != "default"` test would mis-treat that ungoverned case
  as governed; `layer` names which level actually carried the decision
  (`""` = no policy at all, `"default"` = policy present but this scope
  ungoverned, `policy`/`profile`/`both` = governed).
- **Slack workspace admission (`slack.enterprise`)** audits via `log_api_access`
  (not `log_governance_decision`) and its posture probe fails **closed** (returns
  False + `audit_governance_degraded(failed_closed=True)`) on an error, because
  admitting an unverified workspace is the higher-blast-radius mistake.

The Slack posture check itself stays policy-only (a profile cannot carry
`posture`, Rule 6).

Profiles hot-reload via an mtime fingerprint (`ProfileStore`); a schema-invalid
profile falls back to deny-all (Validation rule 5), **not** the ceiling.
`extends` is monotonic narrowing (`compose_profiles`).

**Present-but-unrecoverable profile — governed fleet fails closed, standalone is
lenient.** The reload reads each file's bytes SEPARATELY from parsing and handles
four on-disk states:

- *Parse error with a salvageable bind* (present, readable, but invalid JSON /
  schema, yet the parsed dict carries a valid `bind`): deny-all, binding
  **salvaged from the parsed content** (`_salvage_bind`) so the bound surface
  still resolves to deny-all, not policy-only.
- *Present but unrecoverable* — an `OSError` on `read_text` (bad perms, IO error)
  OR a `UnicodeError`/`UnicodeDecodeError` (invalid encoding) OR a parse error
  with **no** salvageable bind. The file's intended permissions cannot be read, so
  the profile **FAILS CLOSED**: its surface resolves to a **deny-all**, never to
  its last-known-good permissions. This is deliberate — a profile that was just
  *tightened* and then became unreadable must NOT keep its newly-denied operations
  authorized (the fail-open this closes; it also covers a composed child whose
  parent changed). The reload is still per-file (it always publishes the
  successfully-parsed profiles, so a valid *tightening* of any OTHER profile in the
  same reload is still published — no whole-store rollback). To keep the deny-all
  **bound** to its surface (rather than dropping to policy-only, a fail-open of the
  operator's narrowing), the reload recovers the `bind` — from the parsed dict via
  `_salvage_bind` for a parse error, else from the prior snapshot's entry. When
  **no** bind can be recovered (a first-ever unreadable file, no salvageable dict,
  no prior), the disposition splits on whether the fleet is governed:
  - **Governed fleet** (a policy ceiling is present): boot **fails closed** —
    `assert_profiles_within_ceiling` raises `PlatformCompositionError` and aborts
    boot rather than run with a silently-dropped restrictive profile
    (deny-by-default: refuse to run over run-ungoverned).
  - **Standalone / ungoverned** (no ceiling): **lenient** — the file becomes an
    unbound deny-all that drops out of the bind index, so the surface falls to
    policy-only (matches pre-split standalone behavior; a profile blip never
    crashes an ungoverned install). Catching `UnicodeError` alongside `OSError`
    at the read is required: `UnicodeDecodeError` is not an `OSError`, so without
    it a corrupt-encoding file would escape uncaught and crash boot inside
    `assert_profiles_within_ceiling`.
- *Directory unenumerable* — `iterdir()` on the profiles dir raises. ONLY a
  `FileNotFoundError` is the NORMAL "no profiles configured" case (a fresh data
  home): publish an EMPTY index (policy-only), no warning. Every other `OSError`
  — EACCES/EIO on an existing dir, OR `NotADirectoryError` (a non-directory at the
  `profiles` path, a MISCONFIG where honouring "empty" would silently drop all
  Level-2 narrowing) — is treated as present-but-unreadable, NOT benign absence:
  if a prior snapshot exists it is **preserved untouched** (a transient blip must
  not drop every active profile to policy-only); if there is **no** prior (a cold
  boot with an unreadable/non-directory path) the reload flags the whole dir
  unrecoverable so a governed fleet boot-aborts rather than silently running with
  zero profiles. `_dir_fingerprint` maps this to a distinct `<unreadable>`
  sentinel (vs `<absent>` for a genuinely missing dir) so a later fix/delete busts
  the cache.
- *Absent* (missing file, or one that vanished between `iterdir()` and read):
  **not** a policy — skipped, no manufactured deny. An attended/host surface with
  no profile at all legitimately falls to the policy ceiling (policy-only), per
  `resolve_active_scope`.

**Runtime unrecoverable escalation.** `assert_profiles_within_ceiling` is the
boot floor and runs **once**, so a governed host that hot-loads a *new*
unrecoverable profile after boot (no prior entry to recover a bind from) gets an
unbound deny-all that never matches its intended surface — that surface silently
falls to policy-only until the file is fixed. The reload makes this **loud and
observable** rather than locking the fleet down: an `ERROR` log plus a
`mark_governance_incident("unrecoverable_profile", …)` governance-health incident
(surfaced by the dashboard indicator), and only when a ceiling is actually present
(an ungoverned standalone host has no narrowing to lose). A global deny is
deliberately **not** the response: one stray unreadable file must not DoS every
working surface over a narrowing that was never in effect. Boot differs precisely
because no prior state proves the fleet is within its ceiling, so boot aborts.

Fingerprint + recovery: the dir fingerprint is `st_mtime_ns + st_size +
st_ctime_ns` per file (ctime included so a `chmod` that fixes perms — which
changes ctime, not mtime/size — busts the cache). The store **always commits**
the fingerprint after a reload, even one that produced a deny-all for an
unreadable file, so a persistently-unreadable profile does NOT re-run
`iterdir`+`read_text` on every synchronous `resolve_active_scope` (a slow-FS
event-loop wedge). Recovery is the **normal hot-reload path**: because an
unreadable/malformed profile fails CLOSED (a deny-all — there is nothing STALE
being served), the only transition needed is "file fixed", and every realistic fix
(edit, `chmod`, delete, atomic-rename) changes `mtime`/`size`/`ctime` and busts
the fingerprint, so the next resolve reloads. There is **no** same-metadata bounded
retry — that machinery previously existed only to re-read a *preserved* (stale)
entry; with fail-closed there is no stale entry to recover, so it was removed.

Freshness picks its reload discipline from **one** condition — has this store ever
loaded? — not from which thread is calling. `_Snapshot.loaded` records that
distinction, and it is load-bearing: a never-loaded snapshot is EMPTY, and an empty
snapshot is indistinguishable from a genuine "no profiles configured" host, so a
caller served one resolves `profile=None` and `governance_permits` returns its
`ungoverned` **default-permit** — a fail-OPEN that `fail_closed=True` cannot catch,
because the default-permit is a normal return rather than an exception.

`_ensure_fresh` **never blocks** — it takes the reload lock with
`acquire(blocking=False)` only, because it is reachable on the event loop (the
synchronous PreToolUse gate) and waiting there on another thread's filesystem I/O
would wedge the gateway (a slow first profile load in a worker plus a concurrent
dashboard tool approval is exactly that stall). It returns whether the snapshot is
**resolved**, i.e. safe to authorize against, and a caller that loses the lock
does not wait:

- **Warm** (already loaded): serve the current immutable snapshot, resolved
  `True`. Safe because `_snap` is only ever replaced wholesale (an atomic ref
  swap), so a concurrent reader sees a coherent prior-or-next snapshot — and
  because a prior snapshot *exists*, the worst case is authorizing against the
  last committed state for one call; the next access self-heals.
- **Unprimed**: resolved `False`. There is nothing safe to serve, so
  `resolve_active_scope` returns a **deny-all** for that one call (and logs a
  warning) instead of `None`. Concurrent first-touch is the *expected* case, not
  an exotic one: nothing primes the store on the ungoverned / profile-only boot
  path (`assert_profiles_within_ceiling` early-returns when no ceiling is
  present), so a startup burst across the five transports puts several `mc-gov`
  threads on the first load at once. Regression-locked by
  `test_cold_store_contention_never_serves_ungoverned_permit`. Read-only callers
  (the CLI, the boot floor) may ignore the result; the authorization path may not.

A failed first load commits no fingerprint and leaves `loaded` False, so one
transient read error cannot cache a permanent fail-open
(`test_failed_first_load_does_not_cache_a_permissive_state`).

The lock gives the reload transaction a single owner, so concurrent callers don't
each run the full `iterdir`+`read_text` walk and publish competing snapshots. On a
genuine metadata change a warm reload walks the profiles dir exactly **once**: the
warm caller reuses the pre-lock fingerprint it already computed rather than
re-statting under the lock (a second walk on the loop would be a slow-FS stall for
no freshness gain), while an unprimed caller — which has no pre-lock value —
stats once under it. Either way the fingerprint used for the freshness test is the
one committed, so the committed fingerprint always describes the snapshot actually
published.

mtime hot-reload itself is unchanged: an operator edit to a profile is picked up
without a restart. What the store deliberately does **not** have is a per-thread
"always block" discipline for off-loop callers on a **warm** store. There its only
benefit is closing a staleness window one call wide while a reload is concurrently
in flight — not worth a thread-local plus a dual code path, and it invites a future
caller to reach for the blocking path from the event loop, reintroducing the wedge
the non-blocking rule exists to prevent. A surface that needs strict
read-your-writes should add it deliberately, with its own tests.

## Enforcement planes

- **Plane A — the host gate** (`HookManager.on_tool_call`, the primary
  chokepoint). The deny-floor is now the *effective* denied-command rule set —
  the enabled subset of `BUILTIN_DENIED_RULES` ∪ the user's `user_added`
  patterns from the keystone `denied_commands.json` opt-out state, resolved by
  `HookManager._effective_denied(ctx)` and passed to `PolicyAuthority.is_denied`
  as `denied_regexes` (see `security.md`). Gate order: **sensitive-path
  keystone → effective deny-floor (`is_denied`) → `gate_decision(ceiling,
  profile, title)` (governance, incl. the `commands` scope, and MCP titles
  `mcp__server__tool` converted to `@server/tool`) → read-only auto-approve →
  user `auto_approve_tools` loop**. A governance deny wins over a user
  auto-approve, and the read-only auto-approve fast-path runs strictly AFTER
  both the deny-floor and `gate_decision`, so a read-only classification can
  never re-admit a denied/governed call. The governance `commands` deny is
  evaluated in `gate_decision` **independently of** the user's keystone
  opt-out state, so a rule the operator disabled in `denied_commands.json` is
  STILL denied when the enterprise ceiling pins the equivalent pattern —
  tightest-wins. The call sites thread `session_key`/`agent` (they default to
  `""`, so non-governed callers are unaffected).
- **Plane B — kiro agent JSON**: out of scope (v1). KiroCrew no longer writes
  `deniedCommands` into `~/.kiro/agents/*.json` at all — the
  `agent._enforce_denied_commands` injection path is retired — so the hooks gate
  is the SOLE denied-command enforcement point, not a secondary layer. The gate
  is authoritative; KiroCrew does not regenerate `~/.kiro/agents/*.json`.
- **Plane C — out-of-band executors**: the cron `command` (runs via `sh -c`
  outside the ACP flow) is gated in `mcp_cron._vet_command_governance`; the
  cron *capability* on/off gate in `mcp_cron._vet_cron_capability_governance`
  (at `cron_add`); the sandbox ordinal floor is clamped in `sandbox.wrap_argv`;
  spawn in `subagent._vet_spawn_governance`; outbound messaging in
  `mcp_core._vet_messaging_governance` plus the per-transport `channels` check
  in `mcp_core._vet_channel_governance`; the per-transport **startup** gate in
  `slack.gateway._channel_transport_permitted` (a `channels` deny for a member
  keeps that transport — `slack`/`wecom`/`telegram`/`discord`/`webex` — from
  connecting at boot; resolved under `session_key=HOST_SESSION_KEY` so a
  `surface:host` profile can narrow it; the decisions are computed in an executor
  before any client starts, since the profile-file read is blocking and this runs
  on the gateway loop. **Slack is gated too**, in `_connect_slack` rather than in
  `_start_channel_transports`, because it owns its own socket-client lifecycle: a
  deny must DROP that client, not just skip a start call, so nothing can reconnect
  it later);
  durable memory writes in
  `mcp_core._vet_memory_writes_governance` (at `learn_add`); script-hook
  execution in `hooks._script_hooks_capability_denied` (at `run_script_hook`);
  app activation in `apps.manager._app_activation_denied` (at `enable_app`). All
  route through the same `governance_permits` / `governance_floor_ordinal`
  decision source.

## Foreign-agent import interaction

Foreign-agent import is a data-ingest path, not a third governance level and
not a trusted configuration source. The governing equation remains:

`effective = POLICY ∩ PROFILE`

Import can only narrow its own selectable data projection; it cannot widen what
either level permits. In particular:

- Foreign security policies, profiles, denied-command state, approval/sandbox
  settings, credentials, hooks, native personas/agents, raw instructions, and
  runtime state are never imported.
- The strict settings allowlist excludes governance and security controls.
  Preserving an existing KiroCrew value on collision cannot be overridden by
  foreign precedence.
- Imported workspace references grant no filesystem permission. Any later tool
  use is evaluated by the ordinary filesystem scopes and sensitive-path
  keystone.
- Imported MCP definitions grant no MCP capability. Managed servers remain
  protected, and later calls still pass the effective `mcp`/`tools` gates.
- Imported memory/skills and closed ConversationLog sessions are passive data;
  provenance records are deduplication evidence, never authorization evidence.
- Imported schedules are created disabled. A later explicit resume uses the
  normal cron capability, command, channel, sandbox, and bound-profile
  chokepoints.

The importer must not write the policy/profile/admission trust-root files or
construct an alternate evaluator. Unsupported or policy-incompatible items are
reported/skipped; import success never implies a governance grant.

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

### Channels governance-status surface (read-only) + Settings greying

`GET /api/governance/channels` (`handlers_system.api_governance_channels`,
registered in `dashboard/server.py`, behind the same dashboard token auth as the
sibling `/api/*` GETs) returns the effective per-channel `channels` policy
decision as a `{channel_type: bool | null}` map (`true` = permitted, `false` =
denied by policy, `null` = governance evaluation transiently FAILED → the UI shows
"policy status unavailable", NOT "Off by admin"), e.g. `{"slack": true, "discord":
false, "telegram": false, "webex": false, "wecom": false}`. It calls
`governance_permits("channels", <member>, session_key=HOST_SESSION_KEY,
fail_closed=True)` per member, reading `Decision.permitted`
(default-missing-to-`False`); a fail-closed **evaluation-error** Decision (marked
by `rule == "default"` + a "governance error" reason) is surfaced as `null` rather
than `false`, so a transient failure is never mislabeled as an explicit admin
denial. The offload runs on the dedicated `governance_executor` (browser-
triggerable profile-store I/O must not pin the default DNS pool). This mirrors the **connect-time
host-transport gate** (`slack.gateway._channel_transport_permitted`), which uses
the same `_host` surface and also fails closed — so the viewer agrees with what
the gateway actually started. It is deliberately NOT the same surface as the
**outbound** messaging chokepoint (`mcp_core._vet_channel_governance`): that
chokepoint resolves the CALLER's session and app profile, so its per-send
decision is caller-specific and can differ from this host-surface snapshot (a
narrower app/task profile may deny an outbound send on a channel the host is
otherwise permitted to run). The members are derived from
each transport's `channel_type` class attribute
(`handlers_system._channel_members()`: Slack / Discord / Telegram / Webex /
WeCom), never a hardcoded divergent list. The per-member evaluation runs in a
thread-pool executor (`run_in_executor`) because `governance_permits` can read
profile files off disk — the aiohttp event loop is never blocked.

Read-only and byte-identical by default: with NO policy governing `channels`
(the standard OSS build) `governance_permits` returns `permitted=True` for every
member, so the endpoint returns all-true and the Settings UI is unchanged (every
channel tab fully enabled).

The dashboard Settings UI consumes this map to make the channel tabs
governance-aware: in the single Channels tab (`ChannelsPanel`, a list-detail
view), a policy-denied channel's list row shows an **"Off by admin" chip (greyed,
NOT hidden)** and its detail pane renders a disabled-by-policy state (lock icon +
explanation) instead of the editable bot-token form — so a user isn't confused by
a form that silently does nothing, and cannot save config that would never take
effect. **Slack is governed like every other channel** (it is NOT exempt): its
inbound message + tool-approval + review-action + OPTIONS-choice chokepoints call
`channel_inbound_permitted("slack")`, so a `channels` policy denying `slack`
blocks it and the row is marked "Off by admin" to match. (The connection-time gate
+ the direct cron/heartbeat outbound posts are a separate follow-up; outbound
sends via the messaging tool already pass `_vet_channel_governance`. The non-Slack
transports are additionally gated at connect time by
`slack.gateway._channel_transport_permitted`.) Default OSS build (no policy) →
every channel permitted → nothing greyed.

**Gate placement — BEFORE side effects, not just before the turn.** The inbound
gate for the native Slack path lives in `slack.events._route_message`, placed
right after the auth / interceptor / activation-off checks and BEFORE the first
observable side effect: display-name lookups, audio transcription, image/file
download, `channel_history.push` (denied content must never be recorded — a later
ALLOWED turn in the channel could otherwise pull it into agent context), the
`!restart` bang alias (a gateway restart), and session queueing/dispatch.
`handle_message` keeps its own gate as defense-in-depth for its OTHER entry points
(interaction re-dispatch, synthetic sends). **`!stop` (cancellation) is the sole
exemption** — a denied channel must still be able to halt a runaway session it
previously started; `!restart` is NOT cancellation and stays gated. The OPTIONS
Send / legacy-choice buttons are gated at dispatch BEFORE they edit/post the
selection to the channel (their re-dispatched turn is gated too, but the message
edit precedes it); the spent-marker `_done_` no-op posts nothing and stays exempt.

**Tool-approval REJECT is honored, not dropped.** A `channels` deny blocks
APPROVE/TRUST presses outright, but an explicit REJECT press (Slack transport +
native `reject_tool`, Discord `a:…:0`, Telegram `a:…:0`) is allowed through to
RESOLVE the pending approval as refused (`False`). A reject is itself a denial —
exactly what the policy wants — and silently dropping it would strand the kiro-cli
approval future until it times out (~300s) with the tool neither run nor cleanly
refused. So a blocked APPROVE on a governed-off channel also resolves the future
as denied (prompt refusal) rather than returning without resolving.

### Governance policy viewer (`GET /api/governance/policy`)

`GET /api/governance/policy` (`handlers/security.build_governance_policy_snapshot`,
registered in `dashboard/server.py`, same dashboard-token auth) returns the
effective ceiling across ALL scopes on the **host surface**, for the read-only
Settings → Security viewer. It iterates `SCOPE_CATALOG` (so it auto-covers any
scope a release or the companion registers), intersects each boot-frozen POLICY
control with the host-surface PROFILE control using the model's own
`_compose_controls`, and reports `{scope, archetype, governed, source, detail}`
per scope plus `{version, has_policy, profile, unavailable}`.

**Posture, not contents (security boundary).** The serialized `detail` carries
only POSTURE — set `mode`, entry COUNTS (`allow_count`/`deny_count`),
capability `enabled` + inner scope names, ordinal `floor` — and NEVER the rule
CONTENTS (the allow/deny globs, command patterns). This is deliberate: the
dashboard is reachable by the agent's own browser tooling (Playwright extension
mode attaches to the user's authenticated Chrome), and `security_policy.json` /
`profiles` are on the `is_sensitive_path` keystone precisely so the agent cannot
read the ceiling it is fenced by — knowing the exact deny patterns is what would
let it craft an evasion. The human operator reads the authoritative contents from
the policy files directly (outside the sandbox); the viewer shows only which
scopes are governed and how strict they are. The snapshot is **host-surface
scoped** — narrower profiles bound to a specific surface/app/task can tighten a
scope further at runtime, which the viewer states explicitly. Fail-SAFE for
DISPLAY: any resolution error yields a well-formed `unavailable: true` response
(the frontend also treats a fetch error as unavailable) rather than raising or
mislabeling the ceiling as absent — enforcement is server-side and unaffected.

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
command body + the enterprise force-pin for built-in denied-command rules, see
below), `filesystem.read` / `filesystem.write` / `folders.*` and
`network.egress` (host gate via tool kind + args), `channels` (per-transport at
the messaging chokepoint AND at non-Slack transport startup), `apps` (app
activation), `sandbox.min_level` (ordinal
floor at `wrap_argv`), `approval_mode` (boot floor only), and every capability
gate — `capabilities.spawn`, `capabilities.messaging`, `capabilities.cron`,
`capabilities.memory_writes`, `capabilities.script_hooks`, and
`capabilities.publish` (artifact publish chokepoint — see below). Only the live
`approval_mode` clamp remains reserved.

The `commands` scope now **doubles as the enterprise force-pin** for built-in
denied-command rules. A deny-mode `commands` ScopedRuleset's `deny` patterns are
projected as force-pins via `GovernanceCeiling.pinned_command_patterns()` /
`Profile.pinned_command_patterns()`, unioned by `resolve_pinned_commands(ceiling,
profile)` (order-preserving, deduped — deny composes by union, tightest-wins).
`hooks.py` unions these into the effective denied set, so an operator's
`security_policy.json` `commands.deny` patterns are **un-opt-out-able**: they
apply regardless of the user's `denied_commands.json` `disable_all` /
`disabled_ids`, because governance is Level-1 POLICY and the keystone opt-out is
operator-editable (agent-unwritable) state. This is `effective = POLICY ∩ PROFILE`,
tightest-wins, applied to command denials. Only deny-mode entries become pins;
an allow-mode `commands` allowlist is a deny-by-default gate enforced solely by
`gate_decision` and is NOT projected as a pin (the accessor returns `()`).
Because `security_policy.json` is on the `_SENSITIVE_HOME_DIRS` keystone (the
agent cannot write it — `assert_governance_paths_protected`), a pin is
un-opt-out-able by construction. NOTE: the governance `command` matcher is
case-sensitive `fnmatchcase` while the security union matches case-insensitively;
a pin is an independent ceiling that *covers the same command*, not literally the
same rule string. Double coverage (gate + security union) is intended and
harmless — both only deny. New public surface (reflected in `__all__`):
`COMMANDS_SCOPE`, `resolve_pinned_commands`; purely additive — no new
`SCOPE_CATALOG` row and no change to `resolve`/`gate_decision`/`load_security_policy`.

Two `security.py` accessors keep enforcement and display correctly scoped:
`pinned_builtin_command_ids()` (ENFORCEMENT) resolves the **active ceiling
only** — the hooks gate force-re-adds these so a user opt-out can't weaken a
*ceiling* pin, but it does NOT union other profiles' pins (a profile-A pin must
not force-enforce for profile B / a no-profile session; per-profile command
enforcement is the gate's bound-profile `_governance_denial` deny plane).
`pinned_builtin_command_ids_for_snapshot()` (DISPLAY) unions the ceiling pins
with **all** loaded profiles' pins (`all_profile_pinned_commands()`) for the
surface-agnostic Settings > Security snapshot + the builtin-toggle 409 check, so
a rule pinned by any profile renders locked and rejects a disable rather than
surfacing a no-op opt-out (UI success while the bound-profile gate still denies).
Display-only union — it does not widen enforcement.

`capabilities.publish` is a `CapabilityGate` (opt-in: `capability_default=False`)
with an inner `destinations` `ScopedRuleset` (`identifier` matcher) bounding
which publish-provider ids are allowed once the capability is on — the direct
analogue of `capabilities.spawn`'s `agents` ruleset. It is enforced at a Plane-C
out-of-band chokepoint in the artifact publish handler (`api_artifact_publish`),
NOT at the host PreToolUse gate: publishing is a user-driven dashboard HTTP
action ("NOT LLM tools"), so the title-gate never sees it. The chokepoint calls
`governance_permits("capabilities.publish", "destinations:<provider_id>", …)`
BEFORE dispatching to the provider, and additionally honours the standalone
operator's `publish.allowed_destinations` config allowlist (default-open,
narrow-only — config can never widen past the ceiling, mirroring the Slack
enterprise allowlist). This scope is distinct from the `git push` deny FLOOR and
from `network.egress`: `capabilities.publish.enabled: true` never re-enables git
publish (the floor is ADD-only and unconditional) nor a fetch host. WHO
implements a destination is the orthogonal CPP `PublishRegistry` seam; governance
decides only WHETHER + to WHERE, and runs first.

Unlike the messaging/cron chokepoints (which degrade-to-permit on a transient
governance-evaluation error so a latent regression can't wedge the surface),
publish is an **authorization** decision whose wrong-permit is a data
exfiltration — so it fails **CLOSED**. Because `governance_permits` catches its
OWN internal errors (and would otherwise return a permissive "no opinion"
Decision), the handler passes `fail_closed=True`: an error raised *inside*
`governance_permits` then returns a DENYING Decision (audited `failed_closed`),
not a permit. The chokepoint also evaluates the **effective** destination — for
an already-published artifact `publish_sync.publish` dispatches to the existing
`publication.provider`, so the gate resolves that provider (not the requested/
default one) before deciding, or a re-publish with no explicit provider could be
gated against the wrong destination.

### Governed capability: theme-pack persona injection

Installed theme packs (see `themes.md`) can carry a `persona.md` that
`_maybe_inject_persona` prepends to the first user turn — the first
user-installed content path that shapes agent behavior. This surface is
**governed by the `capabilities.theme_persona` `SCOPE_CATALOG` capability
row** (`capability_default=True`): standalone it defaults to allow, but an
enterprise POLICY can force-disable **installed-pack persona injection** —
the scope this row enforces today. (It does NOT gate L2 asset serving —
overlays/topbar/audio keep working under a denying policy; if wholesale L2
disablement is wanted it will be its own row or an extension of this one,
tracked with kirodotdev/KiroCrew#312.) The decision is consulted at the
injection site
(`chat_runner.py`, via `governance_permits("capabilities.theme_persona",
"", session_key=...)`); a denying policy skips injection silently (info log).
It is a **data row only** — `CONTRACT_VERSION` is unchanged and the evaluator
(`resolve`/`gate_decision`/`load_security_policy`) is untouched, per this
spec's design.

**Companion row — pack installation.** The wider content-ingestion surface
(`POST /api/themes/install`, including a server-side `git clone` of a remote
pack, then serving its sandboxed JS + assets into the dashboard) is governed by
a sibling `capabilities.theme_install` `SCOPE_CATALOG` capability row
(`capability_default=True`, same data-only shape — no `CONTRACT_VERSION` or
evaluator change). Standalone it defaults to allow; a managed-fleet POLICY can
ban pack installation wholesale. Consulted in `api_themes_install`
(`handlers/themes.py`, via `governance_permits("capabilities.theme_install",
"", fail_closed=True)`) **before any fetch/clone**; a denying policy — or a
governance-evaluation error (admission chokepoint fails closed) — returns `403`
and ingests nothing.

Rationale for the tone-only surface (context, not a reason to leave it
ungoverned):

- The persona is **tone-only by construction**: it is injected as message
  text, not policy — it cannot grant tools, change refusals, alter the deny
  patterns, or move any governance ceiling. Every tool call the persona-styled
  agent makes still passes the full PreToolUse gate, so the Level-1 POLICY
  ceiling continues to bind all agent *actions* regardless of persona.
- Activation requires a locally installed pack (filesystem access to
  `~/.kirocrew/themes/`) plus a per-content sha grant — an actor with that
  access is already inside the trust boundary the POLICY ceiling models.
- The persona-injection force-disable that a plain in-boundary actor could
  not otherwise get is now available to an enterprise POLICY via the
  capability row above (this supersedes the earlier "deferred to a follow-up
  row" decision for the persona surface).

**Recorded maintainer decision (2026-07-24, PR #107):** "consent =
surprise-prevention UX, not authorization" is **accepted as the v1
contract** for installed-pack personas, and `capabilities.theme_persona`
ships `capability_default=True`. Rationale: KiroCrew is a single-user,
self-hosted tool where the pack installer is the machine owner; the persona is
tone-only, content-bound (sha256), and enterprise-disableable via the row
above — while a default-off would make every installed persona silently dead
on arrival. The considered stronger alternatives (server-recorded grants,
default-off until a headless consent story exists) were explicitly declined
for v1; server-side grant persistence remains the optional half of
kirodotdev/KiroCrew#312 and MAY tighten the model later without breaking this
contract (a stricter server is backward-compatible with consenting clients).
**Revisit trigger:** #312 MUST be revisited before any persona-scope
expansion (longer length bound, per-turn injection, or richer pack tiers) —
scope growth without server-recorded grants is not covered by this decision.

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
  `compose_profiles`, `resolve_pinned_commands` + `COMMANDS_SCOPE` force-pins).
- `platform/governance_profiles.py` — `ProfileStore` (hot-reload),
  `resolve_active_scope`, `governance_permits`, `governance_floor_ordinal`,
  `GOVERNANCE_ERROR_REASON` (the eval-error marker consumers match on).
- `security.py` — `_SENSITIVE_HOME_DIRS` keystone entries.
- `hooks.py` — Plane A gate threading.
- `sel.py` — `log_governance_decision`.
- chokepoints: `sandbox.py`, `mcp_cron.py`, `subagent.py`, `mcp_core.py`.
- `messaging/identity.py` — `channel_inbound_permitted` (the per-message inbound
  `channels` gate) + its SEL audit disposition.
- `executors.py` — `governance_executor` (`mc-gov`), the bounded pool the
  externally-paced governance checks run on.
- `dashboard/handlers_system.py` — `GET /api/governance/channels`.
- `dashboard/handlers/security.py` — `GET /api/governance/policy` (posture-only
  serialization).
- `cli.py` / `cli_commands.py` — the `policy` command.

## Tests

`test_governance_policy.py` (archetypes + loader + evaluator + E1–E13 vectors +
extensibility), `test_governance_boot.py` (compose at boot), 
`test_governance_self_protection.py` (keystone), `test_governance_profiles.py`
(resolution + binding + hot-reload + fail-closed reload dispositions),
`test_governance_gate.py` (Plane A enforcement + audit),
`test_governance_chokepoints.py` (sandbox/cron/spawn/helpers + egress-reserved +
the per-transport inbound gates), `test_governance_channels_endpoint.py`
(`/api/governance/channels`, incl. the eval-error→`null` distinction),
`test_governance_policy_viewer.py` (`/api/governance/policy` posture-only, incl.
`test_detail_never_leaks_rule_contents`).
