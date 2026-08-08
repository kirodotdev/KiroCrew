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
| `agent_runtime` | adapter | `DefaultAgentRuntime` (`run_first_run_setup` wired; `managed_mcp_servers` **RESERVED**) | extra one-time first-run provisioning |
| `agent_executable` | adapter | `DefaultAgentExecutableResolver` (identity) | resolves an edition-managed launcher to its direct executable before core sandboxing |
| `sandbox` | settings | `DefaultSandboxPolicy` (`_STRICT_DIRS`/`_CC_DIRS`) | additional edition-specific credential dirs |
| `credentials` | adapter | `DefaultCredentialPolicy` (AKIA/ASIA redaction; `exempt_exact_hosts()` → `frozenset()`) | internal token regexes + trusted-tenant exempt hosts |
| `security` | **concrete** | `PolicyAuthority()` (baseline only) | `PolicyAuthority(overlay=…)` ADD-only |
| `slack_gate` | adapter | `DefaultSlackEnterpriseGate` (default-open) | fail-closed enterprise allowlist |
| `identity` | adapter | `DefaultIdentityProvider` (`sso_status.py` stub; `whoami`/`issuer` **RESERVED**) | enterprise SSO / directory |
| `embeddings` | adapter | **RESERVED** — `DefaultEmbeddingSource`; the public runtime is the bundled in-process llama-cpp model, so no method is consumed (swap via `embeddings.register_embedding_backend`) | — (slot inert) |
| `mcp_tooling` | adapter | `DefaultMcpToolingProvider` (all methods empty) | enterprise MCP server + skills + provider MCP scopes |
| `agent_catalog` | adapter | `DefaultAgentCatalogProvider` (`builtin_agents()` → `[]`) | edition agent-catalog rows |
| `prompt_sources` | adapter | `DefaultPromptSourceProvider` (`prompt_source_roots()` → `[]`) | edition prompt/SOP roots |
| `capability_manager` | adapter | `DefaultCapabilityManager` (`available()` → `False`) | operations-based external package manager: MCP servers, skills, agent packages, and client plugins |
| `registry` | adapter | `DefaultAppRegistryPolicy` (public-forge baseline) | internal git hosts |
| `apps_loader` | adapter | `DefaultAppsLoader` (OSS builtins) | internal app sources (code-reviewer; team_manager/mimir follow-on) |
| `package_manager` | adapter | **RESERVED** — `DefaultPackageManager`; installs are inline in `cli_doctor.py` (use `CapabilityManager`) | — (slot inert) |
| `knowledge` | adapter | `DefaultKnowledgeProvider` (no extra connectors) | enterprise doc connector (`extra_connectors`) |
| `tunnel` | adapter | `DefaultTunnelProvider` (no-op) | internal tunnel supervisor |
| `telemetry` | adapter | `DefaultTelemetryProvider` (no-op, RUM off) | RUM/Cognito config |
| `dashboard` | adapter | `DefaultDashboardContributor` (no routes/services, no login handler) | secretary/taskkeeper routes + enterprise SSO PTY login |
| `jail` | adapter | `DefaultJailProvider` (no-op, never jails) | enterprise process isolation |
| `feature_apps` | tuple | **RESERVED** — `()`; apps register via `apps_loader` (provenance record only) | — (slot inert) |

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
remove or weaken the floor — is enforced structurally:

- `is_denied` and `effective_patterns` are `@final`. No subclass overrides the
  decision or the union construction.
- The only override surface is the `SecurityOverlay` Protocol, whose
  `extra_deny_patterns()` is **concatenated** to `BASELINE_DENY`. There is no
  method anywhere that subtracts from that union.
- `assert_security_floor(authority)` (run at boot) verifies the authority is a
  `PolicyAuthority` and that it has not overridden the `@final` decision methods;
  it also keeps a (now-vacuous) `effective set ⊇ BASELINE_DENY` superset check so
  a future non-empty static floor is auto-enforced. A weakening companion fails
  composition and boot aborts.
- The actual evaluation (two-pass, git-publish verb anchoring, SEL audit) is
  reused verbatim from `security.is_denied` via the `extra_patterns` parameter.

**`BASELINE_DENY` is now `()` — the floor redefinition.** The built-in
denied-command patterns are **default-ON but user-DISABLEABLE** (Settings →
Security; see `security.md`), so they can no longer be an unconditional compiled
`BASELINE_DENY = tuple(security.BUILTIN_DENY_PATTERNS)` — that would re-apply
every built-in inside `PolicyAuthority.is_denied` and make user opt-out inert.
`BASELINE_DENY` therefore narrows to the empty tuple: the static, un-weakenable
OSS floor is now empty. The un-opt-out-able floor is supplied dynamically by (a)
the companion's ADD-only `SecurityOverlay` (structurally un-removable via the
`@final` union) and (b) the governance `commands`-scope **pins**
(`resolve_pinned_commands`, applied tightest-wins in `hooks.py` — see
`governance.md`). The disableable built-in rules ride in through the resolved
**effective set** (`denied_regexes`): the hooks layer computes
`compute_effective_denied(...)` and passes it into
`current_context().security.is_denied(target, extra_patterns=…,
denied_regexes=…)`. The always-on keystone denials that are NOT rule-toggleable
(git-publish / protected-branch, exfiltration shapes, sensitive-path) run
unconditionally inside `security.is_denied`, independent of the tiers. A user
opt-out of a built-in is orthogonal to — and can never weaken — the companion
overlay or the governance ceiling: the overlay travels via `extra_patterns` and
is never routed through the opt-out, and a governance pin re-adds a rule the user
disabled.

The enforcement hot path (`hooks.py` tool-deny) reads
`current_context().security.is_denied(target, extra_patterns=…,
denied_regexes=…)`, passing the resolved *effective* denied-command set (enabled
built-ins ∪ user `user_added`, with governance-pinned ids force-re-added) as
`denied_regexes`, on top of the companion overlay (glob tier) and the redefined
empty baseline. A standalone install with no opt-out and an empty overlay
resolves to today's full built-in list → behavior identical for the default
install.

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
(`KIROCREW_ADMISSION_POLICY` env path, else `~/.kiro/crew/admission_policy.json`),
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
  "require_policy_signature": true,
  "trust_keys": {"p13n": "<publisher key>", "fleet-control": "<issuer key>"},
  "approved": ["enterprise"],
  "banned": ["some-rogue-plugin"],
  "capability_ceiling": {"egress": ["*.example.com"], "tools": ["enterprise-mcp"]}
}
```

**This policy is also the trust root for the security ceiling.**
`require_policy_signature` (default `false`) additionally demands a *verified*
`identity.signature` on `security_policy.json`, keyed by that document's
`identity.issuer` in the same `trust_keys` map — one key store, not two. It is a
**separate** flag from `require_signature` on purpose: a fleet that signs its
plugins has not thereby promised to sign its governance ceiling, and conflating
them would break managed fleets on upgrade. The flag lives here rather than inside
the security policy because a document cannot be the authority on whether it must
be authentic. `canonical_signing_bytes` / `hmac_signature` are shared by both
checks so the two trust roots cannot drift apart. The governance loader reads
these two fields through `read_policy_trust_root()` — a **side-effect-free**
reader that records no posture and emits no SEL, because unlike
`load_admission_policy` (once per process at boot) it runs on a repeating path.
See `governance.md` → "Policy authenticity".

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
- `slack/gateway.py` gateway boot — `AgentRuntime.run_first_run_setup()` through
  `safe_context_call`. Previously this imported `agent.run_first_run_setup`
  directly, bypassing the seam; routing it through the context makes first-run
  provisioning genuinely extensible (an edition adds its own one-time steps).
  The Default adapter delegates to that same function, so standalone behavior is
  byte-identical — asserted in `test_cpp_wiring_standalone.py`
  (`test_default_agent_runtime_delegates_to_agent_first_run_setup` +
  `test_gateway_first_run_setup_routes_through_the_seam`). Best-effort: the
  gateway's surrounding `except` keeps a failure non-fatal to startup, and
  `PlatformCompositionError` still propagates fail-closed.
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
  existing `TunnelProvider` Protocol (no `CONTRACT_VERSION` bump).
  **`ensure_available(*, install=True) -> str`** is a further v1 addition (no
  bump): an idempotent "make the tunnel reachable, provisioning on demand" entry
  point returning one of `"connected"` / `"starting"` / `"disabled"` /
  `"unavailable"`. WIRED at `slack/allowlist.py::send_dashboard_link`, which
  calls it (via `async_safe_context_call`) only when a tunnel URL is wanted for a
  Slack dashboard link but none is live yet; the `DefaultTunnelProvider` returns
  `"disabled"`, so the standalone path is unchanged (it still falls back to the
  local URL). Narrow-only: the method can start/provision a companion tunnel but
  never bypasses the `tunnel/setup.py` token-auth deny gate. The token-auth
  deny gate in `tunnel/setup.py` is evaluated BEFORE the manager is constructed or
  `start()` reached, so a companion tunnel cannot start without dashboard token
  auth; the connect/disconnect callbacks and `/api/tunnel/status` stay wrapped
  AROUND the provider. **Teardown is wired at
  `dashboard/server.py::_wire_tunnel_shutdown`** — an `app.on_cleanup` hook that
  reads `state.tunnel_manager` lazily (the manager is assigned later, by
  `setup_tunnel`). It covers BOTH start paths, because a live tunnel does not
  imply a manager: with a manager it calls `TunnelManager.stop()`; with
  `state.tunnel_manager` still `None` — the on-demand link path
  (`slack.use_tunnel_url` → `ensure_available()`) provisions and starts a tunnel
  straight on the provider and never constructs a manager — it stops
  `current_context().tunnel` directly. Exactly one path runs per shutdown (the
  manager delegates to the same provider), so nothing is stopped twice, and both
  are idempotent, so a shutdown path that runs twice is harmless. Both paths go
  through one guard: bounded by `_TUNNEL_STOP_TIMEOUT_SECS`, every failure logged
  and swallowed — including a fail-closed `current_context()` lookup, which is
  evaluated INSIDE the guard — so a hanging or raising provider cannot block or
  abort the remaining `on_cleanup` handlers. **Registration order is
  load-bearing:** the hook is appended immediately after the `web.Application` is
  created, ahead of every other cleanup registration and well before
  `runner.setup()` freezes the signal lists. aiohttp dispatches `on_cleanup` in
  registration order under a hard shutdown deadline, so a tunnel hook queued
  behind the other subsystems can be starved (instances cleanup waiting on SSH
  children that ignore SIGTERM eats the deadline, the gateway force-exits, and
  the tunnel is never stopped); the lazy `state.tunnel_manager` read is what makes
  the early registration safe. Because the manager is edition-neutral, one hook
  tears down EVERY provider (the Default's `stop()` is a no-op).
  `start_api_server` (the `--slack-only`/headless path) never calls
  `setup_tunnel`, so it needs no hook.
  Import direction: `tunnel/` imports
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
- `AgentCatalogProvider.builtin_agents() -> List[Dict[str, Any]]` — ADD-only
  agent-catalog rows merged by `agent_discovery.list_agents()` AFTER the on-disk
  scan of `~/.kiro/agents` and, when the caller supplies a `project_dir`,
  `<project>/.kiro/agents` (via `_with_edition_agents`, through
  `safe_context_call`), de-duped by name so an on-disk agent of the same name
  wins. Within the on-disk scan a **project** agent shadows a user-level one of
  the same name (and the shadowing is logged), mirroring kiro-cli — which resolves
  `--agent` against its cwd first, and which Kiro Crew spawns with the session's
  project directory as that cwd, so the project entry is the one that would
  actually run. Kiro Crew's legacy `<project>/.kiro/*.agent-spec.json` convention is
  deliberately NOT scanned here (only the Slack handler opts into it): kiro-cli
  cannot activate such a name, and this list is a dispatch surface. Each row is a
  plain dict of `AgentInfo` fields (`name` required;
  `filename`/`description`/`model`/`skills`/`mcp_servers`/`source`/`package`/`scope`
  optional). **EXECUTABLE INVARIANT:** every returned row MUST be spawnable —
  the edition guarantees a resolvable agent config exists for its `name`
  (materialized under `~/.kiro/agents` or otherwise resolvable by the ACP
  backend). `list_agents()` is the single executable-agent allowlist consumed by
  `_do_agents_sync()` (which PERSISTS rows into `config.json`'s `cfg.agents`),
  `subagent._validate_agent()` (spawn), and conductor generation, so a
  catalog-only row with no config behind it would be persisted and offered for
  spawning yet fail at ACP `session/set_mode` — do NOT return non-executable
  rows. Default `[]` (discovery is the on-disk scan only). **Split out of
  `McpToolingProvider` into its own Protocol** — agent-catalog contribution is a
  distinct concern from MCP tooling; each edition hook lands on its own interface
  rather than accreting onto the nearest existing one.
- **Agent packages + plugins (the symmetry completion).** `list_agents()` was
  originally READ-only, so an edition could show installed agent packages but not
  manage them — the dashboard had install/uninstall for MCP servers and skills and
  a dead end for agents. `install_agent/uninstall_agent(package)` closes that
  asymmetry (`POST /api/capability/agents/{install,uninstall}`); both rebuild the
  agent config and clear the `list_agents()` cache, because an agent package
  carries agents PLUS its own skills and prompt sources.
  Alongside them, three ops cover **plugin packages** — agent-client integrations
  an edition's package manager installs next to the agent packages themselves:
  `async list_plugins() -> List[Dict[str, Any]]` (informational rows),
  `async plugins_out_of_sync() -> List[str]` (the DRIFT set: packages installed as
  agents but missing their plugin counterpart — which presents to a user as an
  agent their client cannot see), and `async sync_plugins() -> CapabilityResult`
  (the writer that reconciles the drift). `GET /api/capability/plugins` returns the
  rows and the drift set in ONE response so the UI cannot render a list and a
  reconcile affordance that disagree mid-install. All three are deliberately
  client-agnostic: the core neither knows nor names any particular editor or CLI,
  and an edition with no plugin concept returns `[]`/`[]` — for
  `plugins_out_of_sync` an empty list genuinely means "in sync", which is why its
  Default is not a fail-closed error like the mutation stubs.
  **Implementer note (learned wiring a real edition):** package managers commonly
  publish one plugin per package SUBSET (`<Package>-<subset>`) while agent
  packages carry the bare `<Package>`. Comparing the two name spaces directly
  reports EVERY package as drifted. Resolve a plugin's owner by matching against
  the known installed package set (longest match wins) rather than splitting on a
  separator — a split silently misattributes any package whose own name contains
  that separator, which makes the package permanently "drifted" so every
  `sync_plugins` reinstalls a plugin that already exists.
- `CapabilityManager` (operations-based external package/capability manager) —
  **replaces the former `external_capability_bin()` binary-name seam.** Rather
  than naming a binary whose exact CLI grammar the core then hardcodes, the
  edition implements OPERATIONS and OWNS its own invocation grammar, output
  parsing, and error translation; the core (`/api/capability/*` handlers +
  `mcp.py` uninstall) calls an operation and only serializes the result / applies
  side effects (config sync, agent rebuild). Ops: `available() -> bool`;
  `async list_mcp()/list_skills()/list_agents() -> List[Dict[str, Any]]`
  (structured entries — the manager parses its own output; the core keeps no
  text grammar; **`list_skills()` containment invariant:** every skill row MUST
  live under an `McpToolingProvider.extra_skills()` root, because the skill
  browser (`/api/skills/package/<name>/tree` + detail) resolves paths by
  searching those roots — a row outside them lists but 404s on tree/detail, so an
  edition satisfying both Protocols MUST keep them consistent; the core enforces
  this at runtime — `collect_skills_blocking` logs a loud warning for any listed
  row outside every `extra_skills()` root);
  `async install_mcp/uninstall_mcp(server_id)`,
  `async install_skill/uninstall_skill(package)`,
  `async install_agent/uninstall_agent(package)` → `CapabilityResult(ok, message)`
  (the manager translates its own errors — the core never matches
  package-manager error strings, and **no Amazon-internal `version_set` field is
  exposed** on the op or the public `/api/capability/skills/install` schema;
  **LIVENESS:** operations MUST be internally time-bounded — a slow companion
  op must not stall MCP handlers. As defense-in-depth the core also wraps every
  mutation op with `asyncio.wait_for` via
  `platform.capability_bound.BoundedCapabilityManager`, applied at **context
  composition** (`PlatformContext.__post_init__` binds every `CapabilityManager`
  once, idempotently — so the companion's `dataclasses.replace` path is not
  double-wrapped). Applying it at the seam — not at the dashboard accessor
  `_capability_manager()` — means EVERY reader of
  `current_context().capability_manager` inherits the bound, whether it goes
  through that accessor or reads the context directly (a subagent, conductor,
  MCP-tool, or apps-backend consumer), so a future non-dashboard call site cannot
  silently obtain an unbounded manager and reintroduce the hang class. Bounds are
  DIFFERENTIATED: tight `CAPABILITY_UNINSTALL_TIMEOUT` (60s) for uninstall,
  generous `CAPABILITY_INSTALL_TIMEOUT` (600s) for install so a legitimate cold
  package-manager download is not cancelled mid-mutation (which could leave
  partial state), and a tight `CAPABILITY_READ_TIMEOUT` (30s) on the async READ
  ops (`list_mcp`/`list_skills`/`list_agents`/`registry`/`list_plugins`/
  `plugins_out_of_sync`) — the dashboard POLLS those list endpoints, so a stalled
  unbounded read would accumulate pending gateway tasks on every poll (the same
  wedge class the bound exists to prevent), even though reads mutate nothing.
  `sync_plugins` takes the INSTALL bound (it may shell a package manager once per
  drifted package). Only the synchronous `available()` probe is unwrapped (no I/O). `/api/mcp/apply` orders the two mutations to be both
  lock-safe and race-safe: it runs the companion `uninstall_mcp` calls FIRST, in
  a phase BEFORE acquiring the process-wide MCP file lock (`_get_mcp_lock`) —
  deduped by name, bounded-concurrent (`_MCP_DEFERRED_UNINSTALL_CONCURRENCY`)
  under ONE phase-level `asyncio.wait_for` deadline — then removes the scope-file
  config entries under the lock. So no slow companion op is awaited while the
  lock is held (no timeout×N stall, and the batch is capped by
  `_MCP_APPLY_MAX_CHANGES`), AND the package is removed before its config is
  (config removal is the last, lock-serialized step). Because the apply is a
  two-phase TRANSACTION and the file lock only serializes individual writes (not
  the phase boundary), the whole apply additionally runs under a process-wide
  async apply mutex (`_get_apply_lock`, spanning BOTH phases) so two concurrent
  applies cannot interleave (one re-adding a server from a preserved spec after
  another removed its package); the narrower file lock is retained inside for
  cross-process coordination with `bridges.py`;

  > **Uninstall ordering & the crash window.** Package-first ordering (chosen
  > for the concurrent-re-add TOCTOU fix above) flips the partial-failure
  > DIRECTION from benign (config removed, package orphaned-but-harmless) to
  > harmful (package removed, config persists → the server fails at every
  > subsequent session start until re-applied). The core closes this: BOTH phases
  > run inside one outer `try`, whose `finally` calls a guaranteed-cleanup sweep
  > (`_sweep_dangling_uninstalls`) that re-purges — via the shared, idempotent
  > `_purge_server_config` — the config of every REQUESTED uninstall the locked
  > loop did not reach. It sweeps by REQUEST, matching Phase 2's own uninstall
  > branch (which removes config unconditionally, without consulting the companion
  > outcome): keying on the request rather than on a `capability_results` entry
  > closes the window where a cancellation lands the instant AFTER the companion
  > removed a package but BEFORE its result was recorded (which would otherwise
  > leave config dangling). Removing config for a requested uninstall is the user's
  > intent and errs toward the BENIGN failure direction (config gone, package
  > possibly orphaned-but-harmless — recoverable by reinstall) rather than the
  > harmful one. Wrapping BOTH phases (not just the locked loop) is load-bearing: a
  > `CancelledError` raised DURING Phase 1 — gateway shutdown / client disconnect,
  > before the Phase-2 lock is ever taken — must still trigger the sweep. The sweep
  > is a blocking acquire→purge→release (its own `_McpFileLockSync`) dispatched to
  > a **worker thread** via `run_in_executor`, and the `finally` awaits that future
  > to completion (shielded) before re-raising. Running it off the event loop
  > satisfies two constraints at once: (1) **deadlock-free** — a loop-blocking
  > acquire here would wedge any other task that holds the MCP lock (it could never
  > resume to release it), violating `no-blocking-call-on-event-loop`; off the loop
  > the lock-holder resumes normally; and (2) **runs-to-completion** — a worker
  > thread is not cancelled when the request task is, and awaiting the future
  > before re-raising means the purge finishes even on cancellation (an
  > un-awaited/shield-only future would run orphaned and loop teardown could
  > destroy it mid-write). This narrows the window to the irreducible hard-kill
  > case (SIGKILL / power loss between the Phase-1 op and the config write), which
  > is itself self-healing: the same package-then-config apply is idempotent, so a
  > re-apply (or a manual uninstall) converges the state. `list_mcp` is a pass-
  > through read and does NOT auto-reconcile — the sweep + idempotent re-apply are
  > the recovery path.

  `async registry() -> List[Dict]` (the manager parses its own registry output
  into entries; the core passes them through as `{"servers": [...]}`). The public
  `DefaultCapabilityManager.available()` is `False` → the handlers return HTTP 503;
  a companion implements registry-backed management. This is the operations-based
  Protocol the prior binary-name seam's contract note anticipated — chosen now,
  pre-launch, so no external CLI grammar fossilizes in the core.

  **Second consumer — App Kit dependency resolution.** `apps/dependencies.py`
  resolves an app manifest's `dependencies.capabilities.{mcp,skills}` through
  `install_mcp`/`install_skill` (and `uninstall_*` on cleanup) rather than
  shelling out to a named binary, reading the manager via `current_context()` so
  it inherits the `BoundedCapabilityManager` timeout wrapper. The seam is probed
  **lazily** — a commands-only manifest never touches it. When `available()` is
  `False` the entries are recorded as `failed` (unresolved) and the app still
  installs, so a public install surfaces the unmet dependency instead of silently
  reporting success. `dependencies.capabilities.agents` is **declarable but never
  gateway-installed**: the Protocol exposes `list_agents` only (package/agent
  install routes were removed), so those entries always report unresolved —
  declare them `managedBy: app` or install them out of band. The wire key `aim`
  is a deprecated READ alias (`Dependencies.from_dict`) that is never
  re-emitted, so a manifest round-trip migrates it; ledger keys/types likewise
  resolve the pre-rename `aim/*` / `aim.*` spellings so an upgraded install does
  not orphan tracked dependencies.
- `McpToolingProvider.extra_mcp_scopes() -> List[McpScope]` — provider-specific
  GLOBAL MCP config scopes. `/api/mcp/apply` and the MCP uninstall path write
  each returned scope's `global_json` (and strip its `agent_mcp_file`) IN
  ADDITION to the core Kiro global, keyed by `f"{scope.id}Global"` in the request
  body. Default `[]` → **the core writes the Kiro global ONLY**; a companion
  returns e.g. the Claude Code scope (`~/.claude.json`) to keep that provider's
  config in sync. Backed by the new frozen dataclass
  `McpScope(id: str, global_json: Path, agent_mcp_file: Optional[Path] = None, label: str = "")`
  in `interfaces.py`: `id` is the short scope key used in the request body
  (`f"{id}Global"`, e.g. `ccGlobal`), `global_json` is the provider's global MCP
  config file, `agent_mcp_file` is the rendered per-agent MCP file to strip
  on uninstall (or `None`), and `label` is the human display name the dashboard
  shows on the scope badge (e.g. `Claude`; defaults to `id`). All are v1 additions
  to the existing `McpToolingProvider` Protocol; the `Default*` returns empty/`None`
  so a standalone process is byte-identical, and no `CONTRACT_VERSION` bump.
  MCP **discovery** (`mcp_discovery._extra_scope_sources`) reads this SAME seam,
  so the scopes discovery scans are exactly the scopes apply/uninstall manage:
  the core scans the Kiro globals only, and a companion's provider scope
  (`~/.claude.json` → `ccGlobal`) is both scanned AND managed — no
  discover-but-can't-uninstall "zombie" servers. The dashboard is seam-aware too:
  `GET /api/mcp/scopes` (`api_mcp_global_scopes`) returns the configured extra
  scopes as `[{id: "<id>Global", label}]`, and the Installed-Integrations
  "Globals" column renders the core **Kiro** badge unconditionally plus one badge
  per returned scope — so a companion re-surfaces its Claude toggle with no core
  edit, and the public build (empty list) shows Kiro only.
  - **`/api/mcp/apply` omitted-field semantics (contract).** A per-server change
    object carries a presence boolean per scope. The two scope families
    deliberately differ on an **omitted** key: the core `kiroGlobal` key is
    `omit → delete` (defaults to `False` — the bundled SPA always sends it
    explicitly, so an omission means "not present"), while every seam scope
    `f"{id}Global"` is `omit → preserve` (defaults to the scope's *current*
    on-disk presence via `_scope_has_entry`). The asymmetry is intentional and
    load-bearing: the OSS SPA does not know a companion's scope keys, so it omits
    them, and omit-preserve prevents an unrelated apply from silently deleting a
    companion-managed server from its provider global (e.g. `~/.claude.json`).
    A companion frontend that toggles a seam scope MUST send that scope's boolean
    explicitly to change it. (Equivalent alternative, not chosen: unify all scopes
    on preserve-on-omit and have every client always send explicit booleans.)
- `PromptSourceProvider.prompt_source_roots() -> List[Path]` — WIRED: the dashboard
  prompt listing (`handlers/__init__._list_aim_prompts`) walks each returned root
  generically (`rglob('*.sop.md')`) for prompt/SOP markdown, replacing the former
  hardcoded `~/.aim/packages` + eventId/`.version-manifest.json` layout. Default
  `[]` so the standalone edition lists only `~/.kiro/prompts` user prompts; a
  companion returns its resolved package prompt roots. **Split out of
  `McpToolingProvider` into its own Protocol** (a distinct concern). v1 addition;
  `Default` returns `[]`.

**`McpToolingProvider` is intentionally scoped to MCP tooling only** —
`extra_mcp_servers()`, `extra_skills()`, and `extra_mcp_scopes()`. The former
grab-bag members were split into dedicated Protocols this session
(`AgentCatalogProvider`, `PromptSourceProvider`, `CapabilityManager`) so the CPP
layer keeps its "one adapter per concern" shape; every future edition hook lands
on its own interface rather than accreting onto the nearest existing one.

**Agent-discovery module rename (this session).** `aim_agents.py` →
`agent_discovery.py`; the `AimAgent` dataclass → `AgentInfo`. The agent `source`
classification was generalized: the old `KiroCrewAICapabilities`-specific
hardcode was removed, so a package-installed agent is now classified
`source="package"` (alongside `"kirocrew"` for `kirocrew.json`/`kirocrew-lite.json`
and `"builtin"` for the rest) rather than the former `"aim"` literal. Importers
(`subagent`, `mcp_core`, `conductor_skill`, dashboard agents) were updated to the
new module/class names.
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
- `config.knowledge.doc_ingest_hosts` (list) — SSRF-safe allowlist for the
  server-side fetch path only; empty = deny-by-default. The agent-driven
  `auto_add_documents` path (renamed from `auto_ingest_doc_links`) is NOT gated
  on it: the agent hands over text it already fetched, Kiro Crew fetches nothing.
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
- Slack message-gate seams (let an edition compose a fail-closed
  challenge-and-redirect posture without editing the core; `InterceptDecision`
  enum = `PROCESS | REDIRECTED | DROPPED`):
  - `SlackEnterpriseGate.intercept_message(orch, *, channel, sender_id,
    clean_text, thread_ts, msg_ts) -> InterceptDecision` — wired in
    `slack/events.py::_route_message`. Default `PROCESS` (inline, OSS-identical).
    **Ordering is security-critical:** the call site is placed immediately after
    the user-allowlist check and BEFORE any content is recorded or processed
    (observe-mode `channel_history.push`, audio transcription, image/file
    download, and the non-observe history push all follow it), so an unverified
    sender's content can never be persisted to channel history where a later
    verified turn could pull it into agent context and bypass the gate. Gated on
    `_user_authorized` (an unauthorized sender is not a challenge candidate and
    keeps its ephemeral-rejection UX). Invoked through
    `safe_context_call(..., fallback=DROPPED)`: a raised adapter error degrades
    deny-by-default, a `PlatformCompositionError` is re-raised. The public
    Default cannot raise, so standalone never reaches the fallback.
  - `DashboardContributor.on_token_consumed(user_id, channel, session_exp,
    thread_ts)` — fired in `dashboard/token_auth.py` after `bind_token_ip` on the
    first (non-cookie) exchange, with `channel`/`thread_ts` read from the token's
    signed `extra` payload. OBSERVER only; the anchor a challenge auth-window
    opens on. Default no-op. `safe_context_call(fallback=None)` re-raises
    `PlatformCompositionError` — no outer `except` wraps it (that would swallow
    the invariant).
  - `DashboardContributor.decorate_reply(text, *, channel, user_id) -> str` —
    outbound-reply transform, AFTER the redaction passes. Default identity. Wired
    on BOTH Slack reply paths: native `slack/handler.py::handle_message` AND the
    default `slack/renderer.py::SlackRenderer.on_done` (the `handle_message_transport`
    path that normal non-review traffic uses — `SlackRenderer` takes a `user_id`
    kwarg for it). Decorator-introduced text is re-scanned (`redact_exfiltration_urls`
    + `redact_credentials`) on both paths so a decorator cannot smuggle a URL/
    credential past redaction; the native path logs only the redaction COUNT
    (never the warning strings, which embed a truncated credential prefix).

  Interceptor ordering + dedup + audit (security-critical, all in `_route_message`):
  the `intercept_message` call runs after the user-allowlist check and after the
  interceptor-specific retry-dedup guard, but BEFORE any content recording/
  processing. A non-`PROCESS` decision (1) records an `intercept:<msg_ts>` key in
  the `SeenCache` so a Slack ack-timeout re-delivery does not re-mint the challenge
  (PROCESS never records, preserving the paired `app_mention`/`message` dual-event
  and the standalone inline path), and (2) emits a
  `sel().log_api_access(operation="slack.message.intercept")` audit event — the
  interceptor is a permission decision distinct from the allowlist check, so its
  verdict reaches the SEL trail.

### Deferred / non-mapping sites

- `apps/routes.py` — `_fetch_git_blob`'s per-URL clone-sandbox-mode decision IS
  wired: it routes through `_context_clone_sandbox_mode` (same as the
  `apps/registry.py` clone sites), so a companion's extended trusted-host set
  applies to registry-blob fetches too. The other `wrap_argv` sites run local
  lifecycle scripts (no per-URL git host), so they have no clone decision to
  route.

## Reserved (declared-inert) contract surface

Some published extension points have **no consumption site** in the core:
overriding them changes nothing. That is legitimate — the contract is kept stable
pre-launch — but it MUST NOT be silent. An implementor reads `PlatformContext`,
writes an adapter, `dataclasses.replace`s it in, and needs to find out
immediately, not after a debugging session.

Three arms make the inertness impossible to miss:

1. **Declaration at the dataclass.** `context.RESERVED_SLOTS` (whole fields) and
   `context.RESERVED_METHODS` (individual methods on otherwise-live fields) each
   map a name → the reason it is inert **and the wired alternative**. Every
   reserved field also carries a `[RESERVED]` marker comment on its declaration
   in `PlatformContext`, so the fact is visible in the source an edition author
   actually reads — not only in an `interfaces.py` docstring.
2. **A loud runtime signal.** `PlatformContext.__post_init__` calls
   `_warn_reserved_slots`, which logs ONE warning per reserved slot carrying a
   non-default value, naming the offending adapter and the alternative. It
   **warns rather than raises**: an edition may compose an adapter in
   anticipation of a slot being wired, and refusing to compose would turn a
   harmless forward-looking override into a boot failure (a breaking change for
   an existing companion). Deduped per `(slot, adapter type)` per process, so a
   composition root that rebuilds the context does not spam the log. Class
   identity (not `isinstance`) decides "is default", so a companion **subclass**
   of a `Default*` adapter still warns — it can change behavior.
3. **An anti-rot gate.** `test/test_platform_cpp_seam_coverage.py` drives off
   `dataclasses.fields(PlatformContext)` and discovers real consumption sites by
   `ast` analysis of `src/kiro_crew` (excluding `platform/` itself and
   `_vendor/`). It asserts, in both directions:
   - every field is EITHER consumed by non-`platform` core code OR listed in
     `RESERVED_SLOTS` — so a **new** field with no call site fails the build
     instead of becoming the next dead seam;
   - every `RESERVED_SLOTS`/`RESERVED_METHODS` entry has NO consumption site — so
     **wiring** a reserved slot fails the build until its entry is deliberately
     removed, and a marker can never go stale and mislead the next reader.

   Two boot-protocol carriers (`contract_version`, `publish`) are consumed only
   by `bootstrap.py` itself and are enumerated in the test's
   `_BOOT_CONSUMED_IN_PLATFORM`; a companion test proves each is genuinely read
   there, so the allowance cannot be used to park a dead field.

Current reserved surface:

| Slot / method | Why inert | Use instead |
|---|---|---|
| `embeddings` (whole slot) | the public embedding runtime is the bundled in-process llama.cpp model — there is no HTTP embed path to source a model/endpoint/signature from | `embeddings.register_embedding_backend()` |
| `package_manager` (whole slot) | external-tool installs (ollama, ffmpeg, whisper) are inline step-by-step brew/curl/pip logic in `cli_doctor.py`, not a single plan-resolution point | `CapabilityManager` for registry-backed MCP/skill/agent installs |
| `feature_apps` (whole slot) | bundled apps are discovered via `AppsLoader` and registered by `apps/manager.py`; the tuple is a provenance record only | `AppsLoader.manifest_sources()` / `bundled_app_names()` |
| `AgentRuntime.managed_mcp_servers` | the agent config is built from the `agent._MANAGED_MCP_SERVERS` global directly | `McpToolingProvider.extra_mcp_servers()` (wired, ADD-only) |
| `IdentityProvider.whoami` / `.issuer` | nothing in the core displays the principal or branches on the issuer | return them in the wired `status()` payload |

`FeatureApp` is deliberately the ONE Protocol with no `Default*` adapter: it
describes a single app (an item), not a policy the core queries, so there is no
behavior for a default to reproduce — the default for the *slot* is the empty
tuple `build_default_context` already composes. A `DefaultFeatureApp` would have
to invent a fictional app to be instantiable.

Removing a reserved slot outright would be a contract-narrowing change (an
out-of-tree companion that composes it today would fail to compose), so the
slots are kept and marked rather than deleted. None of this warrants a
`CONTRACT_VERSION` bump: no field is added, removed, or renamed, and no
interface semantics change — the version stays pinned at 1 pre-launch.
