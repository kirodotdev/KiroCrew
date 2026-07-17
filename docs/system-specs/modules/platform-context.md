# Platform Context (Composed Platform Providers)

The `kiro_crew.platform` package defines the **Composed Platform Providers
(CPP)** contract: the seam that lets one core serve both the open-source
edition and an Amazon-internal companion without the core ever importing
Amazon-specific code.

> Authoring note: KiroCrew is the public edition of this seam. The daily
> de-amazon content sync from the upstream authoring home strips the
> Amazon-tinted Defaults (e.g. the internal git host, `.midway` sandbox dirs)
> down to the public baseline; the Amazon companion re-adds them via overrides.
> The contract (interfaces + consumption-site wiring) is generic core
> infrastructure and survives the sync.

## Model

The core defines a set of **extension points** — interfaces where behavior
differs between editions — and ships a `Default*` adapter for each that
reproduces today's KiroCrew behavior. An internal companion package (module
separate from `kiro_crew`) depends on the public wheel and supplies Amazon
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
| `profile` | carrier (str) | `"standalone"` | `"amazon"` |
| `cfg` | carrier (`KiroCrewConfig`) | loaded config | same |
| `providers` | adapter | `DefaultProviderRegistry` (Kiro-CLI-ACP only) | re-registers Claude Code |
| `agent_runtime` | adapter | `DefaultAgentRuntime` (`_MANAGED_MCP_SERVERS`) | internal servers + Bedrock env |
| `sandbox` | settings | `DefaultSandboxPolicy` (`_STRICT_DIRS`/`_CC_DIRS`) | `.midway`/`.ada`/`.krb5` dirs |
| `credentials` | adapter | `DefaultCredentialPolicy` (AKIA/ASIA redaction) | internal token regexes |
| `security` | **concrete** | `PolicyAuthority()` (baseline only) | `PolicyAuthority(overlay=…)` ADD-only |
| `slack_gate` | adapter | `DefaultSlackEnterpriseGate` (default-open) | fail-closed Amazon allowlist |
| `identity` | adapter | `DefaultIdentityProvider` (`midway.py` stub) | Midway / MCS |
| `embeddings` | adapter | `DefaultEmbeddingSource` (Ollama, unsigned) | internal source + SigV4 |
| `mcp_tooling` | adapter | `DefaultMcpToolingProvider` (empty) | builder-mcp + AIM skills |
| `registry` | adapter | `DefaultAppRegistryPolicy` (public-forge baseline) | internal git hosts |
| `apps_loader` | adapter | `DefaultAppsLoader` (OSS builtins) | internal app sources (code-reviewer; team_manager/mimir follow-on) |
| `package_manager` | adapter | `DefaultPackageManager` (brew/pip) | toolbox installer |
| `knowledge` | adapter | `DefaultKnowledgeProvider` (no extra connectors) | Quip connector (`extra_connectors`) |
| `tunnel` | adapter | `DefaultTunnelProvider` (no-op) | internal tunnel supervisor |
| `telemetry` | adapter | `DefaultTelemetryProvider` (no-op, RUM off) | RUM/Cognito config |
| `dashboard` | adapter | `DefaultDashboardContributor` (no routes/services, no login handler) | secretary/taskkeeper routes + mwinit PTY login |
| `jail` | adapter | `DefaultJailProvider` (no-op, never jails) | MCS-Jail process isolation |
| `feature_apps` | tuple | `()` | (provenance map only — not consumed; apps register via `apps_loader`) |

> `registry` note — the public `DefaultAppRegistryPolicy` encodes the
> public-forge baseline and ships no internal-host set. The Amazon companion
> re-adds the internal GitFarm host (and any further internal git hosts) via its
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

## Profile resolution

`resolve_profile(cfg, *, entry_points)` precedence (first match wins):
1. `KIROCREW_PROFILE` env (`standalone` | `amazon`; unknown → standalone).
2. Non-empty `kirocrew.plugins` entry-point group (companion installed).
3. Identity signal: a present `~/.midway` directory (a cheap stat, no
   subprocess) — **only when the opt-in `KIROCREW_MIDWAY_PROFILE_PROBE` env var
   is truthy**. OFF by default so a stray `~/.midway` left by some other tool
   cannot force the public edition into the `amazon` profile (which has no
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
- Loads the single entry point (`build_amazon_context`) and returns its context.

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
`current_context().security.is_denied(...)`, so an Amazon overlay is enforced at
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
  "approved": ["amazon"],
  "banned": ["some-rogue-plugin"],
  "capability_ceiling": {"egress": ["*.amazon.com"], "tools": ["builder-mcp"]}
}
```

> What admission does NOT do: it gates the *plugin contract boundary*, not a
> source-editing user. For a managed fleet the enforced root of trust is the
> signed, fleet-distributed policy + the device layer; admission is the
> in-process enforcement point that consumes them.

## Contract versioning

`CONTRACT_VERSION` bumps on any field add/rename or interface-semantics change.
A companion built against a different version refuses to compose. Because the
companion's `build_amazon_context` starts from `build_default_context` and only
`dataclasses.replace`s the fields it overrides, any extension point the core
later adds is inherited at its default until the companion writes an override.

**Pinned at `1` pre-launch.** There is no shipped release yet and the companion
is rebuilt in lockstep with the core from the same source, so the
composition-time mismatch guard always compares `1 == 1`. Bumping per-field
would only churn the seam without protecting any deployed companion. Every seam
added pre-launch landed under this same `1`, with no bump:

- the `governance` carrier (the enterprise security ceiling);
- the `knowledge` (connector registry), `dashboard` (route/service/login-handler
  contributor), and `jail` (process-isolation) extension points;
- wiring an *existing* but previously-unconsumed Protocol method into a call site
  (e.g. `ProviderRegistry.create_factory` going live, `AppsLoader` bundling
  feature apps) — no shape change, so no bump regardless.

Start incrementing only after the first public release, when a separately-built
companion can pin against a frozen contract.

## Companion packaging

The companion declares (in its `pyproject.toml`):
```toml
[project.entry-points."kirocrew.plugins"]
amazon = "kirocrew_amazon.compose:build_amazon_context"
[project.scripts]
kirocrew-amazon = "kirocrew_amazon.cli:main"
dependencies = ["kirocrew"]
```
The `kirocrew-amazon` binary sets `KIROCREW_PROFILE=amazon` and delegates to the
core `main` — the explicit composition-root path that a security review reads.

## Consumption-site wiring

Core consumption sites read the context rather than the module global they
previously used. Standalone behavior is preserved because each Default adapter
delegates to that same global. Wired sites:

- `cli.py:main` / `slack/gateway.py:run_gateway` — `boot_platform(cfg)` once at
  startup (gateway raises fail-closed; cli is defensive — standalone never raises).
- `sandbox.py` — `_build_launcher_script` / `_build_seatbelt_profile` source the
  sensitive-dir lists from `current_context().sandbox` (the `.aws`-exclusion at
  the cc branch is preserved).
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
- `agent.py` — `current_context().mcp_tooling.extra_mcp_servers()` merged
  additively (`setdefault`) into the agent config build + dynamic refresh.
- `slack/events.py` / `slack/handler.py` / `dashboard/handlers_system.py` —
  Slack enterprise gate + Midway status route through `slack_gate` / `identity`.
- `apps/manager.py` — builtin discovery + orphan detection merge
  `current_context().apps_loader` sources.
- `apps/registry.py` / `apps/routes.py` — clone-sandbox-mode decision routes
  through `current_context().registry` (`_context_clone_sandbox_mode`).
- `embeddings.py` — model/endpoint/sign_request source from
  `current_context().embeddings` (explicit caller args win); BOTH the async
  `EmbeddingClient` and the sync `make_sync_embed_fn` vector-memory path.
- `dashboard/server.py` — telemetry `record_event` at boot; tunnel enable-gate
  ORs in `current_context().tunnel.enabled()`. **Dashboard contributor (wave 3):**
  in `start_dashboard` only, the `/api/mwinit` route binds
  `dashboard.mwinit_handler()` (or the built-in stub when `None`),
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
  the companion selects the Claude-Code-on-Bedrock backend only when opted in. The
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
  `jail.available()` / `status_detail()`.

### Deferred / non-mapping sites

- `agent.py` — `mcp_tooling.extra_skills()` (skill catalog) is **not** wired:
  skill discovery lives in `SkillsLoader`, not the agent config (TODO).
- `dashboard/server.py` — the tunnel **lifecycle** (start/stop/public_url) still
  runs through `setup_tunnel`; only the enable *gate* is wired (TODO).
- `cli_doctor.py` — `package_manager` is **not** wired: the ollama-install
  diagnostic is inline step-by-step logic, not a single plan-resolution site
  (TODO; Default `install_plan` returns `[]` = today's inline behavior).
- `apps/routes.py` — `_fetch_git_blob`'s per-URL clone-sandbox-mode decision IS
  wired: it routes through `_context_clone_sandbox_mode` (same as the
  `apps/registry.py` clone sites), so a companion's extended trusted-host set
  applies to registry-blob fetches too. The other `wrap_argv` sites run local
  lifecycle scripts (no per-URL git host), so they have no clone decision to
  route.
