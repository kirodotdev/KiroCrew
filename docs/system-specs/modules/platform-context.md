# Platform Context (Composed Platform Providers)

The `kiro_crew.platform` package defines the **Composed Platform Providers
(CPP)** contract: the seam that lets one core serve both the open-source
edition and an enterprise companion without the core ever importing
enterprise-specific code.

> Authoring note: KiroCrew is the public edition of this seam. The daily
> de-branding content sync from the upstream authoring home strips the
> enterprise-tinted Defaults (e.g. the internal git host, `.midway` sandbox dirs)
> down to the public baseline; the enterprise companion re-adds them via overrides.
> The contract (interfaces + consumption-site wiring) is generic core
> infrastructure and survives the sync.

## Model

The core defines a set of **extension points** — interfaces where behavior
differs between editions — and ships a `Default*` adapter for each that
reproduces today's KiroCrew behavior. An enterprise companion package (module
separate from `kiro_crew`) depends on the public wheel and supplies enterprise
adapters for the same interfaces.

The dependency runs one way: **the companion depends on the core; the core never
depends on the companion.** Because the core ships a default for every
interface, the public edition is complete standalone.

## PlatformContext

`kiro_crew.platform.context.PlatformContext` is a frozen dataclass built once at
boot holding the chosen adapter for every extension point, plus three carriers:

| Field | Kind | Default adapter | Companion supplies |
|-------|------|-----------------|--------------------|
| `contract_version` | carrier (int) | `CONTRACT_VERSION` | must match core |
| `profile` | carrier (str) | `"standalone"` | `"enterprise"` |
| `cfg` | carrier (`KiroCrewConfig`) | loaded config | same |
| `providers` | adapter | `DefaultProviderRegistry` (Kiro-CLI-ACP only) | re-registers a companion-registered backend |
| `publish` | adapter | `DefaultPublishRegistry` (registers no provider → publish unavailable) | registers enterprise artifact/publish providers |
| `agent_runtime` | adapter | `DefaultAgentRuntime` (`_MANAGED_MCP_SERVERS`) | internal servers + backend env |
| `agent_executable` | adapter | `DefaultAgentExecutableResolver` (identity) | resolves an edition-managed launcher to its direct executable before core sandboxing |
| `sandbox` | settings | `DefaultSandboxPolicy` (`_STRICT_DIRS`/`_CC_DIRS`) | additional edition-specific credential dirs |
| `credentials` | adapter | `DefaultCredentialPolicy` (AKIA/ASIA redaction; `exempt_exact_hosts()` → `frozenset()`) | internal token regexes + trusted-tenant exempt hosts |
| `security` | **concrete** | `PolicyAuthority()` (baseline only) | `PolicyAuthority(overlay=…)` ADD-only |
| `slack_gate` | adapter | `DefaultSlackEnterpriseGate` (default-open) | fail-closed enterprise allowlist |
| `identity` | adapter | `DefaultIdentityProvider` (`sso_status.py` stub) | enterprise SSO / directory |
| `embeddings` | adapter | `DefaultEmbeddingSource` (dormant seam — the public runtime is the bundled in-process llama-cpp model via `embeddings.register_embedding_backend`; `endpoint_url`/`sign_request` kept for contract stability) | internal source + SigV4 |
| `mcp_tooling` | adapter | `DefaultMcpToolingProvider` (empty) | enterprise MCP server + skills |
| `registry` | adapter | `DefaultAppRegistryPolicy` (public-forge baseline) | internal git hosts |
| `apps_loader` | adapter | `DefaultAppsLoader` (OSS builtins) | internal app sources (code-reviewer; team_manager/mimir follow-on) |
| `package_manager` | adapter | `DefaultPackageManager` (brew/pip) | managed installer |
| `knowledge` | adapter | `DefaultKnowledgeProvider` (no extra connectors) | enterprise doc connector (`extra_connectors`) |
| `tunnel` | adapter | `DefaultTunnelProvider` (no-op) | internal tunnel supervisor |
| `telemetry` | adapter | `DefaultTelemetryProvider` (no-op, RUM off) | RUM/Cognito config |
| `dashboard` | adapter | `DefaultDashboardContributor` (no routes/services, no login handler) | secretary/taskkeeper routes + enterprise SSO PTY login |
| `jail` | adapter | `DefaultJailProvider` (no-op, never jails) | enterprise process isolation |
| `feature_apps` | tuple | `()` | (provenance map only — not consumed; apps register via `apps_loader`) |

> `registry` note — the public `DefaultAppRegistryPolicy` encodes the
> public-forge baseline and ships no internal-host set. The enterprise companion
> re-adds the internal git host (and any further internal git hosts) via its
> own override.

Core code reads adapters directly when it has the context, or via
`current_context()` for module-level functions (e.g. `hooks.py` deny path).
`current_context()` lazily builds the standalone default if boot has not run.

## Boot sequence

```python
cfg = KiroCrewConfig.load()
ctx = boot_platform(cfg)      # platform/bootstrap.py (idempotent)
```

`boot_platform` is the single idempotent entry point — `cli.main` and
`run_gateway` both call it; only the first call resolves the profile and
installs the context. `bootstrap_context`:
1. `build_default_context(cfg, profile=resolve_profile(...))` — all `Default*`.
2. If profile != standalone: `discover_companion_context` (fail-closed).
3. Validate `contract_version` and the security floor; `set_context`.
4. `ctx.providers.register_acp_backends()` once (Default no-op).
5. `ctx.publish.register_publish_providers()` once (Default no-op → the
   `publish_provider` registry stays empty and publishing is unavailable).

## Profile resolution

`resolve_profile(cfg, *, entry_points)` precedence (first match wins):
1. `KIROCREW_PROFILE` env (`standalone` | `enterprise`; unknown → standalone).
2. Non-empty `kirocrew.plugins` entry-point group (companion installed).
3. Identity signal: a present `~/.midway` directory (a cheap stat, no
   subprocess) — **only when the opt-in `KIROCREW_MIDWAY_PROFILE_PROBE` env var
   is truthy**. OFF by default so a stray `~/.midway` left by some other tool
   cannot force the public edition into the `enterprise` profile (which has no
   companion to compose and would fail-closed at boot, bricking every command).
   The companion's managed launcher sets `KIROCREW_MIDWAY_PROFILE_PROBE=1`.
4. Otherwise `standalone`.

The profile is a **load trigger, not a security decision**: capability comes
from the installed companion, so a spoofed signal at worst loads a stricter
posture on a host that has nothing to enforce it. The core does NOT spawn a
`whoami` subprocess — entry-point presence + the opt-in `~/.midway` stat cover
the trigger cases; the companion's own identity provider refines the principal
once loaded.

## Fail-closed discovery

`discover_companion_context` (only for non-standalone profiles) looks up the
`kirocrew.plugins` entry-point group via `importlib.metadata`:
- Empty → **raise** `PlatformCompositionError` (refuse to boot with OSS defaults).
- More than one → raise (ambiguous).
- Loads the single entry point (`build_enterprise_context`) and returns its context.

`bootstrap_context` then asserts `contract_version` match and runs
`assert_security_floor` before installing the companion context.

## ADD-only security floor

`PolicyAuthority` (concrete class in `security_authority.py`) is the deny-floor
authority. The invariant — a companion may **add** deny patterns but never
remove or weaken the baseline — is enforced structurally:

- `is_denied` and `effective_patterns` are `@final`. No subclass overrides the
  decision or the union construction.
- The only override surface is the `SecurityOverlay` Protocol, whose
  `extra_deny_patterns()` is **concatenated** to `BASELINE_DENY`. There is no
  method anywhere that subtracts from the baseline.
- `assert_security_floor(authority)` (run at boot) verifies the authority is a
  `PolicyAuthority` and that its effective set ⊇ `BASELINE_DENY`; a weakening
  companion fails composition and boot aborts.
- The actual evaluation (two-pass, git-publish verb anchoring, SEL audit) is
  reused verbatim from `security.is_denied` via the `extra_patterns` parameter.

The enforcement hot path (`hooks.py` tool-deny) reads
`current_context().security.is_denied(...)`, so an enterprise overlay is enforced at
the synchronous tool-call boundary. Standalone overlay is empty → identical.

> ADD-only constrains the **contract boundary** (a plugin/companion). It does
> not constrain a user who edits the open source. For managed fleets, the
> enforced controls live at the device/fleet layer (out of scope here).

## Plugin admission control

The structural gates above reject a plugin for being *wrong* (no plugin, bad
contract version, weakened floor). **Plugin admission** (`admission.py`) is the
policy layer that lets a managed fleet reject a plugin for not being *trusted* —
the control surface for a plugin marketplace and a ban capability. It runs
inside `discover_companion_context` **before `ep.load()`** (verify-before-run),
so a rejected plugin's code never executes.

Defense in depth, evaluated by `evaluate_admission(ep, policy)`:

1. **Kill-switch** (`banned`) — a fleet bans a plugin by name; the ban always
   wins, in any mode (R-08 / M-09 remote-disable).
2. **Marketplace allowlist** (`approved`) — when present, only listed plugins
   are admitted. Adding a plugin to the list *is* the marketplace review gate.
3. **Verify-before-run signature** (`require_signature`) — the plugin ships a
   signed `kirocrew_plugin.json` manifest; admission verifies the signature
   against a trust key the **policy** carries (R-11 / M-12 supply chain). POC
   uses HMAC; production uses an asymmetric publisher key. The signature covers
   a canonical payload (name/publisher/version/capabilities), so tampering with
   declared capabilities invalidates it.
4. **Capability ceiling** (`capability_ceiling`) — the manifest declares
   requested capabilities (tools, egress, credential paths); admission rejects a
   plugin whose declared capabilities exceed the fleet ceiling, or that requests
   a capability category the fleet doesn't grant at all.

**Trust-root invariant:** the policy loads from a fleet-controlled source
(`KIROCREW_ADMISSION_POLICY` env path, else `~/.kirocrew/admission_policy.json`),
**never from the plugin** — a plugin cannot approve, sign, or un-ban itself. The
manifest is read **import-free** from the plugin's installed distribution files,
so plugin code never runs before the decision.

**Default-open / fail-closed:** the public edition ships no policy → admit
everything (standalone unchanged). A present-but-unreadable policy fails closed
(enforce + signature + empty allowlist = admit nothing). A rejected plugin
raises `PluginAdmissionError` (a `PlatformCompositionError`), aborting boot.

Policy shape (`admission_policy.json`):
```json
{
  "mode": "enforce",
  "require_signature": true,
  "trust_keys": {"p13n": "<publisher key>"},
  "approved": ["enterprise"],
  "banned": ["some-rogue-plugin"],
  "capability_ceiling": {"egress": ["*.example.com"], "tools": ["enterprise-mcp"]}
}
```

> What admission does NOT do: it gates the *plugin contract boundary*, not a
> source-editing user. For a managed fleet the enforced root of trust is the
> signed, fleet-distributed policy + the device layer; admission is the
> in-process enforcement point that consumes them.

## Contract versioning

`CONTRACT_VERSION` bumps on any field add/rename or interface-semantics change.
A companion built against a different version refuses to compose. Because the
companion's `build_enterprise_context` starts from `build_default_context` and only
`dataclasses.replace`s the fields it overrides, any extension point the core
later adds is inherited at its default until the companion writes an override.

**Pinned at `1` pre-launch.** There is no shipped release yet and the companion
is rebuilt in lockstep with the core from the same source, so the
composition-time mismatch guard always compares `1 == 1`. Bumping per-field
would only churn the seam without protecting any deployed companion. Every seam
added pre-launch landed under this same `1`, with no bump:

- the `governance` carrier (the enterprise security ceiling);
- the `agent_executable` resolver (edition-neutral direct-executable resolution
  before the core applies its sandbox);
- the `knowledge` (connector registry), `dashboard` (route/service/login-handler
  contributor), and `jail` (process-isolation) extension points;
- wiring an *existing* but previously-unconsumed Protocol method into a call site
  (e.g. `ProviderRegistry.create_factory` going live, `AppsLoader` bundling
  feature apps) — no shape change, so no bump regardless;
- adding `TunnelProvider.register_callbacks` / `status_snapshot` when the tunnel
  lifecycle was routed through the seam — a v1 method addition to an existing
  Protocol.

Start incrementing only after the first public release, when a separately-built
companion can pin against a frozen contract.

**2026-07-18 governance-seam re-triage.** A re-triage of the CPP seam against the
16 upstream commit groups landed four of the above seam additions on this branch,
each in its own commit — `IdentityProvider.preflight_checks()` (G1, "Preflight
checks" below), `CredentialPolicy.exempt_exact_hosts()` (G3, "Exfil exact-host
heuristic exemption" below), `TunnelProvider.register_callbacks` /
`status_snapshot` (G2, "tunnel/manager.py" below), and
`IdentityProvider.credential_watch_paths()` (G6, blue-green pooled-backend drain
on credential rotation, "mcp_gateway/manager.py" below) — plus the metadata-only
`interaction` telemetry event (G8, "Telemetry record_event sites" below, no
Protocol change). All are v1 additions with **no `CONTRACT_VERSION` bump**. G6
was first built on the stacked branch `feat/govseam-post-pr18` (it depends on
PR #18's `mcp_gateway/` reshape) and was consolidated onto this branch once that
work merged. The re-triage added **no new Protocols and no new `SCOPE_CATALOG`
rows**; the full per-SHA verdict record is kept with the upstream sync tooling.

## Companion packaging

The companion declares (in its `pyproject.toml`):
```toml
[project.entry-points."kirocrew.plugins"]
enterprise = "kirocrew_enterprise.compose:build_enterprise_context"
[project.scripts]
kirocrew-enterprise = "kirocrew_enterprise.cli:main"
dependencies = ["kirocrew"]
```
The `kirocrew-enterprise` binary sets `KIROCREW_PROFILE=enterprise` and delegates to the
core `main` — the explicit composition-root path that a security review reads.

## Consumption-site wiring

Core consumption sites read the context rather than the module global they
previously used. Standalone behavior is preserved because each Default adapter
delegates to that same global. Wired sites:

- `cli.py:main` / `slack/gateway.py:run_gateway` — `boot_platform(cfg)` once at
  startup (gateway raises fail-closed; cli is defensive — standalone never raises).
- `sandbox.py` — `_build_launcher_script` / `_build_seatbelt_profile` source the
  sensitive-dir lists from `current_context().sandbox` (the `.aws`-exclusion at
  the cc branch is preserved). `namespace_argv` / `sandbox_exec_argv` resolve
  argv[0] through `current_context().agent_executable` before applying the core
  sandbox. The public Default is identity; a companion may return the direct
  executable behind an edition-managed launcher to avoid nested isolation, but
  cannot disable or weaken the outer sandbox. A transient adapter error falls
  back to the original executable (outer sandbox still applies); a
  `PlatformCompositionError` propagates fail-closed.
- `hooks.py` — the deny check routes through `current_context().security.is_denied`;
  the kiro-hooks egress (`dashboard/handlers/hooks.py`) scrubs command/matcher
  through the shared `redact_via_context` shim.
- Credential redaction — all egress scrubs route through the single
  `kiro_crew.platform.redact_via_context` shim (the one canonical
  fail-closed-aware shim; modules import it as `redact`). Covers: `agent.py`
  SEL-audit callers, `mcp_core.py` chat-history/spawn output, `mcp_cron.py`
  deny-reason + script-vet + timezone messages, and `dashboard/handlers/files.py`
  file-content egress (slot append, file-watch, file_read, download gate) as well
  as the filename/path/description gates. Standalone is byte-for-byte the prior
  exfil-then-credential two-pass (the Default `CredentialPolicy.redact` delegates
  to `security.redact`); a loaded companion adds its internal-token regexes
  uniformly across every egress surface.
- Exfil exact-host heuristic exemption (`CredentialPolicy.exempt_exact_hosts()`) —
  `security.scan_exfiltration_urls` / `redact_exfiltration_urls` read the
  companion-supplied exact-host set and, for a URL whose domain is an EXACT
  member, skip ONLY the base64-blob / query-length heuristics (which
  false-positive on legitimate long base64 document pointers, e.g. SharePoint
  `:fl:` / Loop `nav=<base64>` links). **Narrow-only:** the exemption can only
  relax the heuristics, NEVER the hard-credential floor — the S3-presigned
  fast-path and the unconditional `_HARD_CREDENTIAL_RE` path+query scan run FIRST
  (before the exemption is consulted), so a real AWS key / SSH-or-PEM header /
  Slack token on an exempted host — including one embedded in the URL PATH — is
  still flagged and redacted. Matched EXACTLY (not by suffix) so a shared
  multi-tenant domain does not exempt every tenant. The set is guarded with
  `getattr(policy, "exempt_exact_hosts", None)` (a pre-method companion adapter
  degrades to the empty set) and is NEVER sourced from `config.json` — an
  agent-writable exemption would be a hole in the redaction ceiling, so the
  companion adapter is the only supplier. Degrade semantics are INVERTED vs
  `redact_via_context`'s baseline-redact fallback: a `PlatformCompositionError`
  propagates fail-closed, but any other adapter failure degrades to
  `frozenset()` — the empty set means MORE redaction (every host runs the
  heuristics), the safe direction; NO logging on the degrade path (runs inside
  the stdio MCP servers). **Deferred-import exception:** `security` reads the set
  through a FUNCTION-LOCAL import of `kiro_crew.platform.context` (the `sel.py`
  pattern), so the CPP import-direction invariant holds — `platform/defaults.py`
  imports `security` at module load, and `security` never reaches `platform` at
  module-load time (only at call time). v1 method addition to the existing
  `CredentialPolicy` Protocol; no `CONTRACT_VERSION` bump; `DefaultCredentialPolicy`
  returns `frozenset()` so standalone redaction is byte-identical.
- `agent.py` — `current_context().mcp_tooling.extra_mcp_servers()` merged
  additively (`setdefault`) into the agent config build + dynamic refresh.
- `slack/events.py` / `slack/handler.py` / `dashboard/handlers_system.py` —
  Slack enterprise gate + SSO status route through `slack_gate` / `identity`.
- `mcp_gateway/manager.py` — `GatewayManager._spawn_once` resolves
  `current_context().identity.credential_watch_paths()` (v1 method addition to
  `IdentityProvider`; Default returns `[]`) and threads each path to the
  gateway daemon as a repeatable `--credential-watch-path` argv flag. The seam
  is resolved in the **already-booted gateway process**, never in the daemon:
  gatewayd is a separately spawned subprocess that does not call
  `boot_platform`, and `current_context()`'s lazy default fails closed on
  non-standalone profiles — so the argv flag is the only channel. Absent flag
  (the public default) ⇒ the daemon creates no watcher task and its run flow is
  byte-identical. With a flag, `mcp_gateway/credwatch.py` polls the file and
  fires only on a **content-digest** change (an mtime bump with byte-identical
  content — the no-op-rewrite storm — never fires; the first observation is the
  silent baseline, whether the file is present OR **absent**). An absent
  baseline that later **appears** DOES fire (a "no credential -> credential"
  transition drains any backend prewarmed during the absent startup window —
  prewarm is scheduled before the watcher's first probe), and a **present ->
  absent** deletion fires too (credential *revocation* — otherwise pooled
  backends keep the revoked credential until deadline/restart); the baseline
  moves to absent so a re-appearance fires again. Genuine absence only — a
  transient stat/read `OSError` is skipped without firing. Firing triggers a
  blue-green drain (`pool.drain_all_to_bluegreen`) + re-warm so pooled backends
  respawn with the rotated credential. The core
  never hardcodes or interprets any credential path/content — the bytes are
  only hashed. Read through `safe_context_call` (fallback `[]`), so a
  pre-method companion adapter degrades to no-watcher instead of raising.
- `apps/manager.py` — builtin discovery + orphan detection merge
  `current_context().apps_loader` sources.
- `apps/registry.py` / `apps/routes.py` — clone-sandbox-mode decision routes
  through `current_context().registry` (`_context_clone_sandbox_mode`).
- `embeddings.py` — model/endpoint/sign_request source from
  `current_context().embeddings` (explicit caller args win); BOTH the async
  `EmbeddingClient` and the sync `make_sync_embed_fn` vector-memory path.
- Telemetry `record_event` sites — `dashboard/server.py` records `gateway_start`
  at boot; `dashboard/chat_runner.py` and `slack/handler.py` record one
  `interaction` event per successful chat turn (immediately after the
  `record_success` call, non-cancelled / non-retrying branch only; cancelled
  turns emit nothing). **The interaction payload is strictly metadata —
  `session_key`, `surface` (`"dashboard"` / `"slack"`), and `model` — never
  prompt/response text or file contents.** All sites are best-effort
  (try/except-Exception, debug log); the Default provider is a no-op so
  standalone is byte-identical. Phase-1 scope is dashboard + slack only
  (cli_chat/cron/subagent/task_executor sites are deliberately not wired).
- Preflight checks (`IdentityProvider.preflight_checks()`) —
  `kiro_crew.preflight.run_preflight_checks()` runs seam-supplied pre-launch
  checks at exactly two sites: the `gateway` dispatch in `cli.py` (before
  faulthandler/lock/`asyncio.run`) and `_token` in `cli_server.py` (before TTL
  parsing). The method returns **already-resolved callables** — checks are
  never `module:function` strings resolved from config (an agent-writable
  config importing arbitrary callables at next start would be a code-exec
  escalation). `SystemExit` from a check propagates so a check can abort the
  launch; every other exception is logged and swallowed per check. When called
  with no explicit list, the runner resolves the checks through
  `safe_context_call` (fallback `[]`), so a transient context failure can never
  block standalone startup while `PlatformCompositionError` still propagates
  fail-closed. `DefaultIdentityProvider.preflight_checks()` returns `[]` —
  standalone startup is byte-identical; the companion returns e.g. an
  SSO-session freshness prompt. Placement rationale: the checks cannot live in
  `boot_platform` (it runs for every subcommand, incl. the mcp-core/mcp-cron
  stdio servers where an interactive prompt would corrupt the JSON-RPC stream)
  nor in `DashboardContributor.start_services` (it never runs for `token` and
  fires only inside gateway async startup) — so the two command dispatch sites
  host the call. v1 method addition to the existing `IdentityProvider`
  Protocol; no `CONTRACT_VERSION` bump.
- `tunnel/manager.py` — the tunnel **lifecycle** routes through the seam. The
  stub `TunnelManager` delegates `start` / `stop` / `public_url` UNCONDITIONALLY
  to `current_context().tunnel` (via `safe_context_call` / `async_safe_context_call`
  — re-raise `PlatformCompositionError`, degrade other errors); there is **no**
  `isinstance`/identity check against `DefaultTunnelProvider` (that would be an
  edition branch by proxy). `start()` first registers the connect/disconnect
  CORS-reflection callbacks with the provider (`register_callbacks`), then
  delegates `start()`; when the provider is not enabled (the public Default) it
  falls through to the byte-identical "not available in OSS" disabled notice. The
  `status` property prefers the provider's `status_snapshot()` and otherwise
  reports its own local `TunnelStatus` — the Default returns `None`, so the
  standalone `/api/tunnel/status` payload and `test_tunnel_manager.py` assertions
  are unchanged. Precedence: an explicit local lifecycle write wins — `stop()`
  (STOPPED) and the OSS-disabled `start()` (DISABLED) pin the local status so a
  stale/lagging companion snapshot cannot resurrect a "connected" state after
  teardown; the next `start()` clears the pin. The snapshot is projected onto a
  FRESH `TunnelStatus` each read, so a key a later snapshot omits (e.g. a cleared
  `error`/`url`) resets to its default rather than persisting a stale value.
  `public_url` returns the provider URL only while state is CONNECTED (mirrors
  the pre-seam stub), so a companion that keeps its last URL while
  RECONNECTING/ERROR is not reported as live. `register_callbacks` +
  `status_snapshot` are a v1 addition to the
  existing `TunnelProvider` Protocol (no `CONTRACT_VERSION` bump). The token-auth
  deny gate in `tunnel/setup.py` is evaluated BEFORE the manager is constructed or
  `start()` reached, so a companion tunnel cannot start without dashboard token
  auth; the connect/disconnect callbacks and `/api/tunnel/status` stay wrapped
  AROUND the provider. Import direction: `tunnel/` imports
  `kiro_crew.platform.context`; `platform/` keeps zero imports of `kiro_crew.tunnel`.
- `dashboard/server.py` — tunnel enable-gate
  ORs in `current_context().tunnel.enabled()`. **Dashboard contributor (wave 3):**
  in `start_dashboard` only, the `/api/sso-login` route binds
  `dashboard.sso_login_handler()` (or the built-in stub when `None`),
  `dashboard.contribute_routes(app)` mounts edition routes before the SPA
  catch-all + `AppRunner.setup()`, and `dashboard.start_services(app)` /
  `stop_services(app)` ride `app.on_startup` / `app.on_cleanup` — appended BEFORE
  `runner.setup()` freezes the signal lists. The sync calls fail-closed via
  `safe_context_call`; the two async lifecycle hooks via `async_safe_context_call`
  (the async sibling — same re-raise-`PlatformCompositionError` / degrade-other
  contract, centralized so the fail-closed policy cannot diverge). `stop_services`
  takes the same `app` handle as `start_services` (symmetric) so a companion need
  not stash services in process-global state.
- `dashboard/handlers_system.py` — `frontend_rum_config()` added to the status
  payload only when non-None.
- `config/loader.py` `build_provider_factory(cfg)` (wave 3 wiring) — the
  LLM-provider factory build sites (`cli_chat`, `cli_server`,
  `session.reload_provider_factory`, `slack/gateway`, `cli`, `cli_commands`) route
  through `current_context().providers.create_factory(cfg)` instead of
  `cfg.create_provider_factory()` directly. The Default returns exactly
  `cfg.create_provider_factory()` (identity), so the public edition is unchanged;
  the companion selects its Bedrock-hosted backend only when opted in. The
  fallback is passed as a lazy `fallback_factory` so the happy path builds the
  factory exactly once (no eager double-build) and a failure inside the fallback
  is still caught by the shim.
- `dashboard/handlers/knowledge.py` — the `SyncScheduler` connector map merges
  `current_context().knowledge.extra_connectors(cfg)` after the built-ins
  (`local_folder`/`obsidian_vault`); Default returns `{}` so standalone is
  unchanged.
- `cli.py` `main` (wave 3 jail gate, factored into `_jail_reexec_gate`) +
  `cli_doctor.py` — for `_JAILED_COMMANDS`
  (`chat`/`tui`/`run`/`consolidate`/`eval` — the rule is "every command that
  builds a provider factory / runs in-process agent work"; `gateway` is excluded
  so its execv self-update path is never nested in a jail). Order: (0) **re-entry
  guard** — if the `KIROCREW_JAILED` marker is PRESENT (any non-empty value) we
  are already the jailed child, so return immediately (no re-probe / re-jail).
  The gate sets this marker right before invoking the backend so the re-exec'd
  child inherits it; a `try/finally` restores the prior value on the no-re-exec
  paths so it never leaks into an in-process run. A companion that re-execs with
  a fresh environment MUST set the marker to any non-empty value (detection is by
  presence, not truthiness) or the on-mode child would re-probe, get an "already
  jailed" `None`, and deadlock on the fail-closed floor. (1) if `off` this
  invocation (`--no-jail` OR `KIROCREW_NO_JAIL` truthy — `1`/`true`/`yes`/`on`
  via the shared `env_flag_enabled`, so a `=0`/`=false` typo does NOT bypass
  isolation), or the re-normalized `agent.jail` mode is `off`, return and run
  in-process (no probe). (2) Probe `current_context().jail.available()`: a clean
  `False` (the public Default) is a pure no-op even under `mode == "on"` (exactly
  as the help text promises) and `_child_argv()` is not even built; a
  `PlatformCompositionError` always propagates; a *transient probe error* degrades
  to no-op under `auto` but FAILS CLOSED (`exit 2`) under `on` (availability
  unknown ≠ absent — an on-mode host must not run un-jailed on a flaky probe).
  (3) With a backend present, `jail.maybe_reexec_into_jail(_child_argv(), mode)`
  runs; a non-`None` return is the jailed child's exit code (propagated via
  `sys.exit`). Single fail-closed floor: under `mode == "on"`, anything other than
  a real re-exec (`None` return OR a swallowed backend error) refuses to run
  un-jailed (`exit 2`). The mode is re-normalized at the gate via `_normalize_jail`
  (so a programmatically-set off-spec value is handled like the load-time path);
  `--no-jail` is accepted on every jailed subparser. `cli_doctor` reports
  `jail.available()` / `status_detail()`. The host probes a companion backend
  builds on — `sandbox.userns_available()` / `sandbox.is_wsl()` — are CACHED
  and never block on a running event loop (`userns_available()` delegates to
  the probe-cache machinery; a cold on-loop call defers to the background warm
  and returns `False` with a transient classification). Boot code should call
  `sandbox.prewarm_backend()` before companion composition so the cache is warm
  by the time a jail backend probes it.


### Amazon-edition seam additions (v1, no `CONTRACT_VERSION` bump)

Existing-Protocol methods added / wired so the Amazon companion can re-introduce
dropped MeshClaw behavior without the core importing it. All are v1 additions (a
`Default*` no-op reproduces today's OSS behavior exactly — a standalone process
is byte-identical) with no `CONTRACT_VERSION` bump.

- `SlackEnterpriseGate.heartbeat_safe_tools() -> frozenset[str]` — unioned into
  `slack/gateway.py::_is_heartbeat_safe_tool` after the core `HEARTBEAT_SAFE_TOOLS`
  exact-match. Default `frozenset()`. ADD-only; never sourced from config.
- `AppsLoader.registry_rows() -> List[Dict]` — ADD-only merged by
  `apps/registry.py::_load_registry_file` after bundled `app-registry.json`
  (same-`name` core row wins). Default `[]`.
- `DashboardContributor.on_user_message(app, message)` — fired once per user
  message by `dashboard/chat_handlers.py::api_chat` before the turn, inside a
  fail-safe `safe_context_call`. OBSERVER only. Default no-op.
- `McpToolingProvider.extra_skills()` — now WIRED: `SkillsLoader.__init__`
  appends returned paths as lowest-precedence extra skill roots (sensitivity- +
  existence-checked). Default `[]`.
- `browser/auth.py::register_browser_auth_provider(provider)` — module-level
  registration hook (twin of `register_acp_backends`); every `browser/auth`
  helper delegates to it when present, else the OSS default. `browser/cli.py`
  auth subcommands now delegate through the helpers.
- `hooks.register_internal_read_path(read_id, rel_path)` — guarded seam adding a
  fixed-path entry to `_INTERNAL_READ_ALLOWLIST` (rejects `..`/absolute/
  non-sensitive/repoint).
- `security._SENSITIVE_HOME_DIRS` gains `.midway` (live SSO bearer cookie;
  inert on a host without `~/.midway`).
- `config.dashboard.mwinit_flags` (str) + `_EDITABLE_CONFIG` PATCH entry.
- `config.knowledge.auto_ingest_doc_links` (bool) + `doc_ingest_hosts` (list) —
  SSRF-safe, empty allowlist = deny-by-default.
- `KiroCrewConfig._extra_sections` (private) — unknown top-level config.json
  sections captured at `load()`, re-emitted by `to_dict()`, so an edition
  section is not dropped on `save()`/PATCH. Excluded from the JSON schema
  (`build_json_schema` skips leading-underscore fields). Data-preservation half
  of the eventual `ConfigSchemaContributor`; Settings-visibility half is TODO.
- ACP claude seam (all inside the dormant `_is_claude` path, inert on kiro-cli):
  `AcpClient._claude_session_mcp_servers()` (Default `[]`) feeds both
  `session/new` + `session/load` `mcpServers`; `_spawn` calls the
  companion-attached `_write_claude_local_settings()` (via `getattr`) on the
  PRIMARY spawn path; `AcpClient`/`AcpProvider` take a `permission_mode` kwarg
  (Default `None`); `acp/types.py` adds `CC_PERMISSION_MODE_DEFAULT` /
  `CC_PERMISSION_MODE_AUTO`.

### Deferred / non-mapping sites

- `cli_doctor.py` — `package_manager` is **not** wired: the ollama-install
  diagnostic is inline step-by-step logic, not a single plan-resolution site
  (TODO; Default `install_plan` returns `[]` = today's inline behavior).
- `apps/routes.py` — `_fetch_git_blob`'s per-URL clone-sandbox-mode decision IS
  wired: it routes through `_context_clone_sandbox_mode` (same as the
  `apps/registry.py` clone sites), so a companion's extended trusted-host set
  applies to registry-blob fetches too. The other `wrap_argv` sites run local
  lifecycle scripts (no per-URL git host), so they have no clone decision to
  route.
