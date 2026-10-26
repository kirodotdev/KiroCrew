# Platform Context (Composed Platform Providers)

The `kiro_crew.platform` package defines the **Composed Platform Providers
(CPP)** contract: the seam that lets one core serve both the open-source
edition and an enterprise companion without the core ever importing
enterprise-specific code.

> Authoring note: Kiro Crew is the public edition of this seam. The daily
> de-branding content sync from the upstream authoring home strips the
> enterprise-tinted Defaults (e.g. the internal git host, SSO sandbox dirs)
> down to the public baseline; the enterprise companion re-adds them via overrides.
> The contract (interfaces + consumption-site wiring) is generic core
> infrastructure and survives the sync.

## Model

The core defines a set of **extension points** — interfaces where behavior
differs between editions — and ships a `Default*` adapter for each that
reproduces today's Kiro Crew behavior. An enterprise companion package (module
separate from `kiro_crew`) depends on the public wheel and supplies enterprise
adapters for the same interfaces.

The dependency runs one way: **the companion depends on the core; the core never
depends on the companion.** Because the core ships a default for every
interface, the public edition is complete standalone.

Every extension point is imported from `platform/interfaces.py`, its one import
path. The boot-layer points are defined there; the install / structural points
and the feature apps are defined in `platform/interfaces_structural.py`, and the
runtime-service / frontend points in `platform/interfaces_runtime.py`, both
re-exported by `interfaces.py`. A new extension point goes to the module of its
kind.

The execution catalog reads `ProviderRegistry.agent_runtime_policy(engine_identity)`
through `current_context()` and `safe_context_call` for owner-visible member rows.
The lookup key is the agent the member runs: its `kiro_agent` through
`dispatch_kiro_agent`, so a row that recorded an agent's file name keys on the
name that file declares, falling back to its roster alias when the binding is
empty. Redacted requests neither query nor emit this metadata; template
rows never carry it. The public adapter returns `None`; companion policy is
advisory metadata, not an enforcement boundary or a public picker behavior.
Composition failures propagate, while other lookup failures log at debug and
omit the policy.

## PlatformContext

`kiro_crew.platform.context.PlatformContext` is an immutable dataclass holding the chosen adapter for every extension point, plus four carriers. Boot installs the initial context once; a validated central-governance refresh replaces the active context only to carry a new `governance` value. `policy_distribution.apply_ceiling` performs that replacement through `set_context`, and `governance_generation()` makes dependent profile snapshots refresh rather than serving a profile composed against a retired ceiling:

| Field | Kind | Default adapter | Companion supplies |
|-------|------|-----------------|--------------------|
| `contract_version` | carrier (int) | `CONTRACT_VERSION` | must match core |
| `profile` | carrier (str) | `"standalone"` | `"enterprise"` |
| `cfg` | carrier (`KiroCrewConfig`) | loaded config | same |
| `providers` | adapter | `DefaultProviderRegistry` (the baseline in `agent_sdk/backends.py`; registration is a no-op) | registers an edition backend after the core knows and verifies its routing |
| `publish` | adapter | `DefaultPublishRegistry` (registers the personal cloud drive under `PERSONAL_DRIVE_PROVIDER`; leaves the unnamed default unregistered) | registers enterprise artifact/publish providers |
| `agent_runtime` | adapter | `DefaultAgentRuntime` (`run_first_run_setup` wired; `managed_mcp_servers` **RESERVED**) | extra one-time first-run provisioning |
| `agent_executable` | adapter | `DefaultAgentExecutableResolver` (identity) | resolves an edition-managed launcher to its direct executable before core sandboxing |
| `gateway_lifecycle` | adapter | `DefaultGatewayLifecycleProvider` (`restart_launcher()` → `None`) | stable absolute launcher for package-manager-owned gateway installs |
| `sandbox` | settings | `DefaultSandboxPolicy` (`_STRICT_DIRS`/`_CC_DIRS`) | additional edition-specific credential dirs |
| `credentials` | adapter | `DefaultCredentialPolicy` (AKIA/ASIA redaction; `exempt_exact_hosts()` → `frozenset()`) | internal token regexes + trusted-tenant exempt hosts |
| `security` | **concrete** | `PolicyAuthority()` (baseline only) | `PolicyAuthority(overlay=…)` ADD-only |
| `governance` | **concrete carrier** | `load_security_policy()` result or `None` | bundled Level-1 ceiling |
| `slack_gate` | adapter | `DefaultSlackEnterpriseGate` (default-open) | fail-closed enterprise allowlist |
| `identity` | adapter | `DefaultIdentityProvider` (`sso_status.py` stub; `whoami`/`issuer` **RESERVED**) | enterprise SSO / directory |
| `agent_identity` | adapter | `DefaultAgentIdentityProvider` (disabled: `enabled() -> False`; no workload, no Gateway spec, no tokens). Distinct from operator-SSO `identity`. `IdentityProvider.whoami` / `issuer` stay RESERVED and are not consumed to satisfy this seam. The public edition composes only the Default. | edition workload identity + token vending |
| `embeddings` | adapter | **RESERVED** — `DefaultEmbeddingSource`; the public runtime is the bundled in-process llama-cpp model, so no method is consumed (swap via `embeddings.register_embedding_backend`) | — (slot inert) |
| `mcp_tooling` | adapter | `DefaultMcpToolingProvider` (all methods empty) | enterprise MCP server + skills + provider MCP scopes |
| `agent_catalog` | adapter | `DefaultAgentCatalogProvider` (`builtin_agents()` → `[]`) | edition agent-catalog rows |
| `prompt_sources` | adapter | `DefaultPromptSourceProvider` (`prompt_source_roots()` → `[]`) | edition prompt/SOP roots |
| `skill_discovery` | adapter | `DefaultSkillDiscoveryProvider` (`skill_providers()` → `[]`) | edition skill discovery providers for the multi-provider search |
| `tips` | adapter | `DefaultTipsProvider` (`tips_pool()` → `None`) | the edition's feature-tip pool. The one **REPLACE-capable** seam: a supplied `TipsPool` takes over from the public curated file AND the docs-scan catalog instead of being unioned into them, because a public tip advertises a capability an edition build may not have or may not expose. An empty pool means "this build shows no tips" |
| `denied_rules` | adapter | `DefaultDeniedRuleProvider` (`denied_rules()` → `[]`) | edition denied-command rules that are default-on but USER-DISABLEABLE (distinct from `security`, the un-weakenable overlay floor) |
| `import_sources` | adapter | `DefaultImportSourceProvider` (`import_sources()` → `[]`) | edition onboarding-import sources |
| `capability_manager` | adapter | `DefaultCapabilityManager` (`available()` → `False`) | operations-based external package manager: MCP servers, skills, agent packages, and client plugins |
| `external_access` | adapter | `DefaultExternalAccessPolicy` (`admits_registry()` / `admits_cloud_deployment()` → `True`) | allowlist installable content to an internal registry; withhold cloud deployment |
| `registry` | adapter | `DefaultAppRegistryPolicy` (public-forge baseline) | internal git hosts |
| `apps_loader` | adapter | `DefaultAppsLoader` (OSS builtins) | internal app sources (code-reviewer; team_manager/mimir follow-on) |
| `package_manager` | adapter | **RESERVED** — `DefaultPackageManager`; install hints are inline in `doctor_checks/features.py` (use `CapabilityManager`) | — (slot inert) |
| `knowledge` | adapter | `DefaultKnowledgeProvider` (no extra connectors) | enterprise doc connector (`extra_connectors`) |
| `memory_files` | adapter | `DefaultMemoryFilesProvider` (memory's markdown source text on local disk) | a non-local `MemoryFiles` for memory's markdown source. `memory_files_for` does not catch a provider error: `PlatformCompositionError` propagates and nothing falls back to local disk. Memory's derived state (the FTS index, the SQLite stores, the FAISS vectors) stays local and does not travel through this seam |
| `tunnel` | adapter | `DefaultTunnelProvider` (no-op) | internal tunnel supervisor |
| `telemetry` | adapter | `DefaultTelemetryProvider` (no-op, RUM off; OTLP destination from `telemetry.otlp_endpoint`) | RUM/Cognito config + its own OTLP collector |
| `dashboard` | adapter | `DefaultDashboardContributor` (no routes/services, no login handler, no internal-reachable paths) | secretary/taskkeeper routes + enterprise SSO PTY login |
| `jail` | adapter | `DefaultJailProvider` (no-op, never jails) | enterprise process isolation |
| `mobile_connect` | adapter | `DefaultMobileConnectProvider` (personal-install pair: `tailnet_qr` + `login_link` (id == kind by design)) | edition-specific phone-connection methods (descriptor-only `{id, kind}`; minting stays on each method's own endpoint; an empty list hides the dashboard entry; list + mint governed by `capabilities.mobile_connect`) |
| `remote_provisioners` | adapter | `DefaultRemoteProvisionerProvider` (the built-in `aws_ec2` lane backed by `RealLaunchEngine`, **plus a conditional `aws_fargate` lane** backed by `FargateLaunchEngine` that is offered only when `cloud.json` carries a complete `fargate` block; id == kind by design for both) | edition-specific ways to CREATE a remote instance (a managed dev environment, a container task): descriptor-only `{id, kind, label, posix_only, step_labels, confirm_before_launch}` plus a `LaunchEngine` per id (`confirm_before_launch` carries what the operator must see and confirm before that lane may launch -- `POST /api/cloud/launch` requires `confirm_recipient` to equal it, so the requirement is derived from the row rather than hard-coded to one id, and a lane with nothing to confirm leaves it empty); the core's durable launch job still drives every launch, so cancel, rollback and orphan reaping are inherited rather than reimplemented |
| `feedback` | adapter | `DefaultFeedbackTrackerProvider` (`report_issue_url()` → `None`) | the edition's issue tracker behind **Report a Problem** and `kirocrew doctor`. **REPLACE-capable** like `tips`: a supplied URL takes over from the public pre-filled GitHub bug-report link rather than being appended to it, because that link's query string is GitHub's form. The backend half of the dashboard's `setFeedbackTracker` seam (`website/docs/extension-seams.md` § "Feedback tracker"): the frontend-built links live there; this one is built by the gateway because it carries the bundle's facts |
| `speech` | adapter | `DefaultSpeechProviderRegistry` (no STT or TTS providers, `default_stt()` / `default_tts()` → `None`) | edition-registered speech-to-text and text-to-speech providers and the edition's default choice. **ADD-only**: a registered name joins the built-in `stt.provider` / `voice_reply.provider` sets and can never shadow a built-in, a retired STT name or `off` (boot raises `PlatformCompositionError`). The edition default applies only to an absent key; a stored value always wins. Validation, defaults, STT/TTS dispatch, live dictation and the two capability lists route through it (pluggable speech providers RFC, [#22982](https://github.com/kirodotdev/KiroCrew/issues/22982)) |
| `feature_apps` | tuple | **RESERVED** — `()`; apps register via `apps_loader` (provenance record only) | — (slot inert) |

> `remote_provisioners` note — this seam lets the Set-up tab under Settings →
> Remote Crew offer lanes beyond an EC2 instance in the user's own AWS account:
> `handlers_cloud._engine()` resolves the engine for a `provider_id` through the
> seam (the `state.cloud_launch_engine` hook beside it is a test seam, not a
> contract), so a deployment whose users have no AWS account of their own, or
> whose machines come from a managed dev-environment service, can add a lane
> without shadowing the panel. The seam follows `mobile_connect`
> exactly: the backend contributes descriptors (`GET /api/cloud/provisioners`),
> the frontend draws each `kind` through `registerRemoteProvisionerRenderer()`
> (the `aws_ec2` kind is drawn by the core's own form and cannot be claimed), and
> `POST /api/cloud/launch` resolves the requested `provider_id` against the same
> seam before a job file exists. What the seam deliberately does NOT hand out is
> the launch loop itself: an edition supplies the five-method `LaunchEngine` and
> `cloud/launch_job.py::run_launch` drives it, so one-launch-at-a-time, cancel,
> the two rollback paths and `reap_orphans` apply to every lane. The four step
> KEYS are therefore fixed (rollback branches on them); a descriptor may only
> relabel them. `size_key`, `profile` and `region` are the generic wires: for the
> built-in they are the EC2 ladder, an AWS profile and an AWS region; another
> provisioner reads them as its own shape, credential selector and placement,
> which is why `LaunchJobStore.create()` validates `size_key` against
> `sizes.py` for the built-in id only. The stop/start/delete lifecycle routes
> (`/api/cloud/{tag}/...`) remain EC2-specific: a lane that needs them
> contributes its own through `dashboard.contribute_routes`. No governance scope
> is added here; the existing EC2 lane carries none today, and a
> `capabilities.remote_provisioners` row mirroring `capabilities.mobile_connect`
> is the natural follow-up once a second lane exists to narrow on.
>
> Contributor walkthrough: [adding-a-remote-provisioner.md](../../guides/adding-a-remote-provisioner.md).

> `external_access` note — three surfaces the core offers unconditionally, none of
> which had a composition point. Two are installable-content registries: skill
> discovery (skills.sh) and MCP server discovery (the official registry) hardcoded
> their public provider at registration time, so a managed deployment could not
> restrict where installable code came from without patching the core. The third is
> **cloud deployment**: `kiro_crew/deploy/` provisions S3, CloudFront, IAM roles and
> a reaper Lambda in the operator's own account and carried no capability gate at
> all — `capabilities.publish`, which bounds publish-provider destinations, does not
> reach it.
>
> `admits_registry(kind, name, api_base)` is consulted in both `_build_registry()`
> functions; a refused provider is never registered, so it is ABSENT rather than
> failing per request and no later install path is left to gate.
> `admits_cloud_deployment(target)` is consulted by `deploy/handlers.py`: the read
> at `GET /api/deploy/config` reports `cloudDeploymentEnabled` so the frontend hides
> the console instead of rendering one whose every button 403s, and every mutating
> route is wrapped at registration so a new endpoint is gated by being listed rather
> than by remembering an in-handler check. Read endpoints stay open deliberately —
> a 403 on `config` would leave the page unable to explain itself.
>
> Both decisions take the concrete target as well as a label, because a name is
> self-chosen while the URL or target determines where bytes go; an allowlist pinned
> to the target stops admitting a provider that repoints at a different host.
> `_shared_access.py::admits_registry` / `admits_cloud_deployment` are the single call
> points: they deny on a composed-adapter error (reaching that fallback means an
> operator intended to restrict something), let `PlatformCompositionError`
> propagate, and SEL-audit **both** outcomes — a log carrying only denials cannot
> show whether the permitted path was ever taken.

> `registry` note — the public `DefaultAppRegistryPolicy` encodes the
> public-forge baseline and ships no internal-host set. The enterprise companion
> re-adds the internal git host (and any further internal git hosts) via its
> own override.

Core code reads adapters directly when it has the context, or via
`current_context()` for module-level functions (e.g. `hooks.py` deny path).
`current_context()` lazily builds the standalone default if boot has not run.

`installed_context()` returns the INSTALLED context or `None` as a bare
attribute read — it never resolves, never raises, and does no I/O. Use it ONLY
where the answer for "no context" is already the conservative one, because it
skips the config load and entry-point discovery that `current_context()`
performs on every call while unbooted. A caller that must honour a companion's
policy has to go through `current_context()` and take the fail-closed
`PlatformCompositionError`.

Whether the no-context answer is conservative is a property of the CALLER, so
the accessor cannot check it and this paragraph cannot enforce it — while it was
the only guard, the caller set grew from one to three and the sentence here still
said "the one such caller". Every call site is therefore declared in
`context.PEEK_CALLERS` with that reason, and `test_platform_cpp_seam_coverage.py`
fails the build on an undeclared peek AND on an entry whose call site is gone.
Today's three: the exempt-host lookup (below), `redact_log_via_context` (above),
and `governance.active_policy_distribution`. The gate is a review forcing
function, not a proof — it cannot verify that a written justification is true,
only that one was written where a reviewer will read it.

## Boot sequence

```python
cfg = KiroCrewConfig.load()
ctx = boot_platform(cfg)      # platform/bootstrap.py (idempotent)
```

`boot_platform` is the single idempotent entry point — `cli.main` and
`run_gateway` both call it; only the first call resolves the profile and
installs the context. `bootstrap_context`:
1. `build_default_context(cfg, profile=resolve_profile(...))` composes all `Default*` adapters and selects the applicable Level-1 governance ceiling through `load_security_policy`.
2. If profile != standalone: `discover_companion_context` (fail-closed), then validate `contract_version` and the ADD-only security floor.
3. `assert_governance_paths_protected`, `assert_policy_signature_satisfied`, and `assert_profiles_within_ceiling` validate the final context before `set_context`; these gates prevent an agent-writable trust root, an absent required signature, or a profile looser than its ceiling from becoming active.
4. `ctx.providers.register_acp_backends()` once (Default no-op).
5. `ctx.publish.register_publish_providers()` once (Default registers the personal
   cloud drive under its OWN key, `PERSONAL_DRIVE_PROVIDER`, so the destination is
   listed and selectable but does NOT capture the unnamed default — a publish that
   names no destination still resolves `DEFAULT_PROVIDER`, which stays unregistered,
   and gets a 503; a companion edition registers its own destination instead).

## Profile resolution

`resolve_profile(cfg, *, entry_points)` precedence (first match wins):
0. An enterprise build (`edition.is_enterprise()`, see [Build-time edition constant](#build-time-edition-constant)) → `enterprise`, before every step below; `KIROCREW_PROFILE`, the marker probe and entry points cannot change it. With no companion installed it fails closed at boot. An `oss` build skips this step.
1. `KIROCREW_PROFILE` env (`standalone` | `enterprise`; unknown → standalone).
2. Non-empty `kirocrew.plugins` entry-point group (companion installed).
3. Identity signal: a present SSO-marker directory (a cheap stat, no
   subprocess) — **only when the opt-in `KIROCREW_SSO_PROFILE_PROBE` env var
   is truthy**. The legacy name `KIROCREW_MIDWAY_PROFILE_PROBE` is honored too,
   because already-deployed managed launchers set it: dropping it would let an
   enterprise host with the marker but a missing companion resolve `standalone`
   instead of failing closed. `KIROCREW_SSO_MARKER_PATH` overrides the path that
   is stat'd (default `~/.midway`). The probe is OFF by default so a stray marker
   directory left by some other tool cannot force the public edition into the
   `enterprise` profile (which has no companion to compose and would fail-closed
   at boot, bricking every command). A companion's managed launcher sets the
   opt-in.
4. Otherwise `standalone`.

The profile is a **load trigger, not a security decision**: capability comes
from the installed companion, so a spoofed signal at worst loads a stricter
posture on a host that has nothing to enforce it. The core does NOT spawn a
`whoami` subprocess — entry-point presence + the opt-in marker stat cover
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

## Level-1 governance ceiling and distribution

`PlatformContext.governance` is the optional Level-1 `GovernanceCeiling` that enforcement chokepoints read through `current_context()`. `governance.load_security_policy` composes a **tier ladder**, highest first: the centrally distributed document, then exactly one of the local sources (an explicit `KIROCREW_SECURITY_POLICY` path, a companion-bundled policy, or the data-home policy). The central document is the authority; every tier below it may only **tighten** it, through the same per-scope AND the profile layer uses. There is no local rollback lever above the fleet: recovery from a bad publication is re-publishing a good document at the source (`governance.md` → "Loading + precedence"). No source leaves editable standalone defaults.

`policy_distribution.resolve_distribution` takes the central source from the `distribution` declaration of the highest tier that made one, or from fleet environment settings when none did, with environment settings taking precedence individually. A declared distribution source cannot carry credentials; request headers remain host-local and `cache_only()` prevents child processes from receiving the means to contact the fleet control plane.

`governance._parse_controls` rejects every unknown governed key, including an unrecognised `sandbox` child. Only documented non-governed sandbox flags are accepted in the reserved internal scope. This fails closed instead of recording a misspelled sandbox floor as a valid but unenforced policy control.

`policy_distribution.load_distributed_policy` serves a validated last-known-good cache when the source is unavailable, and its unavailable disposition decides the no-cache case. `refresh_now` rejects an invalid live candidate and retains the running ceiling; `validate_ceiling` applies the same signature and profile-floor checks as installation before `apply_ceiling` replaces the active context. This asymmetry is load-bearing: a transient outage cannot remove governance, and a malformed central update cannot weaken or interrupt a governed running host.

The cache is covered by `assert_governance_paths_protected` and is therefore part of the sensitive-path trust root: write access would let an agent substitute both policy bytes and provenance. `register_policy_fetcher` is append-only, so an edition can add a transport without shadowing a built-in fetcher or bypassing shared validation and cache handling. `distribution_posture` exposes operational state without returning the control-plane URL or request credentials. `governance_profiles.ProfileStore._ceiling_token` includes `governance_generation()` so a profile snapshot is never reused after `apply_ceiling` installs a replacement ceiling.

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

**`BASELINE_DENY` is `()` — the floor definition.** The built-in
denied-command patterns are **default-ON but user-DISABLEABLE** (Settings →
Security; see `security.md`), so they cannot be an unconditional compiled
`BASELINE_DENY = tuple(security.BUILTIN_DENY_PATTERNS)` — that would re-apply
every built-in inside `PolicyAuthority.is_denied` and make user opt-out inert.
`BASELINE_DENY` therefore narrows to the empty tuple: the static, un-weakenable
OSS floor is empty. The un-opt-out-able floor is supplied dynamically by (a)
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
   uses HMAC. Production uses an asymmetric publisher key. The signature covers
   a canonical payload (name/publisher/version/capabilities and non-empty
   `config_keys`), so tampering with declared capabilities or config ownership
   invalidates it. A plugin may declare at most 200 non-empty config keys, each at
   most 200 characters long. A malformed or over-limit declaration owns no keys.
   An absent or empty declaration stays outside the payload, which preserves
   signatures generated before `config_keys` existed. After admission succeeds,
   config validation receives the declaration from the same manifest snapshot
   without importing plugin code or reading the manifest again.
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
An ABSENT policy fails closed the same way, and because the first-run seed is
marker-guarded a restart does not re-create it; the rejection therefore carries
`admission.missing_policy_remedy()`: delete the seed marker
(`.migrations/admission_policy_seeded`) to re-seed the open default when the seed
already ran, or fix the data-home write when it never managed to write the file.

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

**Strict gate-flag reading.** `require_signature` and
`require_policy_signature` are read strictly by `_coerce_flag`: a real JSON
boolean is honoured, an absent key leaves the gate off (the documented
default), and any other present value — explicit `null`, the string
`"false"`, `0`, `1` — is warned about and read as **on**, the fail-closed
direction. A plain `bool()` on the raw value would read any non-empty string
as ON but `null`/`""` as OFF, so a template rendering
`"require_policy_signature": null` would silently disable the gate. Same
strict-read shape as the `boot` gate flags in
[governance](governance.md).

**This policy is also the trust root for the security ceiling.**
`require_policy_signature` (default `false`) additionally demands a *verified*
`identity.signature` on `security_policy.json`, keyed by that document's
`identity.issuer` in `trust_public_keys` (a base64 Ed25519 public key, policy
issuers only) or else in the same `trust_keys` map — one key store, not two. An
issuer with a public key never falls back to its `trust_keys` secret. It is a
**separate** flag from `require_signature` on purpose: a fleet that signs its
plugins has not thereby promised to sign its governance ceiling, and conflating
them would break managed fleets on upgrade. The flag lives here rather than inside
the security policy because a document cannot be the authority on whether it must
be authentic. `canonical_signing_bytes` / `hmac_signature` are shared by both
checks so the two trust roots cannot drift apart. The governance loader reads
these fields through `read_policy_trust_root()` — a **side-effect-free**
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
- the `agent_identity` slot (`AgentIdentityProvider` — agent workload identity
  and token vending, distinct from operator-SSO `identity`);
- wiring an *existing* but unconsumed Protocol method into a call site
  (e.g. `ProviderRegistry.create_factory` going live, `AppsLoader` bundling
  feature apps) — no shape change, so no bump regardless;
- adding `TunnelProvider.register_callbacks` / `status_snapshot` when the tunnel
  lifecycle was routed through the seam — a v1 method addition to an existing
  Protocol.

Start incrementing only after the first public release, when a separately-built
companion can pin against a frozen contract.

## Companion packaging

The companion declares (in its `pyproject.toml`):
```toml
[project]
dependencies = ["kirocrew"]

[project.entry-points."kirocrew.plugins"]
enterprise = "kirocrew_enterprise.compose:build_enterprise_context"

[project.scripts]
kirocrew-enterprise = "kirocrew_enterprise.cli:main"
```
The `kirocrew-enterprise` binary sets `KIROCREW_PROFILE=enterprise` and delegates to the
core `main` — the explicit composition-root path that a security review reads.

**The companion's top-level module MUST be named `kirocrew_<edition>`** — lowercase
letters, digits and underscores only, no dots or hyphens. Two matchers identify a
running gateway from its command line and neither can read the companion's entry
points: `port_resolution._gateway_module_roots()` derives the Python side's set
from the installed `kirocrew.plugins` entry points, while the desktop launcher's
`isKirocrewCommand` (`website/electron/gateway-stop.js`) runs in a process with no
view of that Python environment and matches the name against `KIROCREW_MODULE_RE`
instead. Both also require a server subcommand (`gateway`, `dashboard`, `start`)
as the first positional after the module. A companion named outside the
convention classifies as ours on the Python side but as a foreign port holder on
the desktop side, and the app refuses to start on its own gateway's port.

### Distribution build version

A distribution that repackages one core release as several builds of its own
(an enterprise bundle vending `0.6.0.10`, `0.6.0.11`, … of the same `0.6.0`)
names the build it shipped in a `BUILD_VERSION` file placed **beside
`kiro_crew/__init__.py`** at packaging time (declared as optional package data;
absent in this repo and in the wheels it publishes). `kiro_crew/__init__`
resolves it **at import**, replacing `__version__` for every reader — the About
page's version chip (`version_display`), `/api/status`, `/api/health`,
`kirocrew --version`, the diagnostics report, and the governance minimum-version
floor (`update_required`), which a fleet writes in the distribution's build
numbers. Two readers deliberately do not change: the anonymous usage beacon
(`beacon.release` clamps to `major.minor.patch` for cardinality, so a stamped
build sends the same value as its base), and the About page's changelog, where
`changelog.running_release` folds the stamp back onto its three-segment release
so the build marks the `0.6.0` row rather than opening an empty `0.6.0.12` row. Import time is the only point that works:
`dashboard/handlers/updates_verdict.py`, `dashboard/handlers/updates_check.py`,
`dashboard/ws.py`, `config/loader_document.py` and
`cli_server.py` bind `from kiro_crew import __version__` copies that nothing
later can reach, so this is deliberately not a `PlatformContext` field — a
context is composed after those imports.

A file in the package rather than an environment variable, on purpose: the
version feeds an enterprise ceiling (the minimum-version floor) that the local
operator must not be able to weaken, and an env var is theirs to set. The file
sits in the same site-packages tree as the code and carries exactly its trust —
whoever can write it can write `__init__.py` — so it adds no bypass the bytes did
not already have, and it is the same model the release lanes use when they
rewrite the version literal for a nightly or insider build. Being in the bytes,
it is also what a shadow-venv probe or any fresh interpreter naturally reports.

Fail-closed on shape: only the core base itself, or the base followed by exactly
one `.N` ASCII-numeric segment over a bare numeric base, is honoured (the read is
capped, and an oversized, undecodable or unreadable file falls back to the base).
A different base, a prerelease stamp, a non-numeric or second segment, or a
suffix over an `rc` / nightly base is a build this core is not; it is ignored
with one warning and the base stays, so a process can never claim bytes it does
not run. The contract is the file — its name and that shape — not a Python API;
the helpers in `kiro_crew/__init__` are private. The accepted shape parses as a
PEP 440 release wherever the version is compared (`_is_newer` orders `0.6.0.12`
above `0.6.0` and below `0.6.1`; `release_channel.channel` still reads
`stable`), and `base_version` — the stable-channel display fold — preserves the
stamped string, which is what lets the chip show it. The one reader that does
NOT take the stamp's word is the bug-report resolver
(`release_channel.provenance`, behind `kirocrew doctor --bundle` and the
dashboard's Report-a-problem link): because this rule admits a stamp only over
a bare base, a stamped build has by construction lost whatever prerelease
marker the release pipeline wrote, so the stamp claims no lane there — a
prerelease marker in the installed distribution's metadata version (which the
stamp never rewrites) or the `channel` record decides, and with neither the
report says `Not sure` rather than Stable. The public `version` field is the
release the stamp is a build of (`changelog.release_of_build`); the evidence
the resolver weighed stays in the private bundle, where `versions.txt` and
`manifest.json` both record the raw stamp (`kirocrew_version`), the folded
release (`release`), the distribution's metadata version
(`distribution_version`), the proven lane (`channel`, `unknown` when none) and
the `channel` record (`channel_record`) — the manifest spells a silent source
as JSON `null`, the text file as `unavailable` / `absent`. `collect_bundle`
resolves the provenance exactly ONCE, before it writes the first member, and
keeps that snapshot on `BundleResult.provenance`: `versions.txt`, the manifest
and the issue link are all written from it, and the terminal link `kirocrew
doctor` prints afterwards reads the same field rather than resolving again.
The resolver reads host state that can change mid-collection — the dashboard's
`set_release_channel` rewrites the `channel` record on the event loop while
the collector runs in a worker thread — so a per-member read could leave one
bundle with a `versions.txt` and a `manifest.json` that disagree about the
lane, beside a link that explains neither (pinned by
`test_one_bundle_is_written_from_one_resolution`).

Update lane. A distribution owns its own update path, normally the command
provider selected by `check_command` / `apply_command` update pins, which never
consults the public release feed. Should a stamped build reach the feed check,
`channel_move_pending` compares by **release** (`changelog.release_of_build`
folds `0.6.0.12` to `0.6.0`), so a stamped build on the lane that shipped its
release is on that lane, not permanently "ahead" of it; the `update_available`
verdict needs no fold (`0.6.0` is not newer than `0.6.0.12`). The wheel's
dist-info metadata is not touched by the stamp — `pip` and `importlib.metadata`
still report the base — so a downstream that builds its own wheel and needs
those to differ must bump its own project version as well.

### Build-time edition constant

`kiro_crew/edition.py` holds `EDITION`, one of `EDITIONS = {"oss", "enterprise"}`, and `is_enterprise()`. The checked-in value is always `"oss"`. A packaging build picks the value: `make wheel|backend-bin|desktop EDITION=enterprise` (only a command-line `EDITION=` counts, an exported shell `EDITION` is ignored, even under `make -e`, because the edition variables are `override`; without it, Make takes `KIROCREW_BUILD_EDITION` from the environment, else `oss`; both set and different is refused) passes it as `KIROCREW_BUILD_EDITION` to `setup.py`'s `build_py`, which rewrites the `EDITION` line in the BUILT copy only, through `kiro_crew/_edition_stamp.py`. The same variable works for a direct `python -m build` or `packaging/build-desktop.sh` run. An unknown value fails the build (Make checks first with an exact match, so `%` wildcards are refused; `build_py` checks again); an editable install (which `make.ps1 wheel` runs as a prerequisite) stamps nothing, since it runs the `oss` source tree, but still refuses an unknown value. The stamp is rewritten on every build, `oss` included, so a reused `build/` cannot carry an old enterprise stamp into a default build.

Runtime code reads only the module constant, never the build variable or a config file, for the same reason as `BUILD_VERSION`: an installed build's edition is part of its bytes, not something the local operator sets. Read it at call time (`edition.is_enterprise()`), not as a `from kiro_crew.edition import EDITION` copy, so tests can flip it with `monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")`. Its readers are `resolve_profile` and `kiro_crew.edition_locks` (below). In `resolve_profile`, an enterprise build always resolves the `enterprise` profile, so `PlatformContext.is_enterprise` stays the single property callers read and the edition only feeds it. An enterprise build with no companion installed therefore fails closed at boot (`discover_companion_context` raises); that is intended, not a gap. An `oss` build resolves exactly as before. Tests: `test/test_edition.py`, `test/test_platform_context.py::TestEnterpriseEditionProfile`.

#### Local switches with no governance key (`edition_locks.py`)

[rfc-crew-enterprise-edition](../../request-for-change/rfc-crew-enterprise-edition.md) D3: in an enterprise build only the remote policy can turn a control on, and local config, environment variables, profiles and dashboard settings may only narrow it. A control with a governance key gets that from the evaluator. A control with no key cannot be turned on by any remote rule, so its local switch is ignored: `kiro_crew/edition_locks.py` lists each one in `UNKEYED_LOCKS`, and the feature calls `local_enable_locked(<id>)` where it reads its own switch. The call returns `False` on `oss`, so nothing changes there; on enterprise it returns `True` and logs one warning per control. A later change that adds a real key for a control replaces its row with a governance check.

| Control id | Local source ignored | Read site | Setting API |
|---|---|---|---|
| `computer_use` | `computer_use.json` `enabled` | `computer_use/enable_state.py::is_enabled` | `GET /api/computer-use/config` carries `locked_by_policy`; an enable save answers 409 |
| `tunnel` | `tunnel.enabled`, `slack.use_tunnel_url` | `dashboard/server_runtime/tunnel.py::_start_aea_tunnel` (the edition's own `TunnelProvider` is not a local source); `dashboard/urls.py::slack_tunnel_opt_in`, read by `tunnel_origin_if_opted_in` and by `send_dashboard_link` before its on-demand `ensure_available()` door, so Slack links stay on the local origin | `GET /api/config/schema` marks both paths `lockedByPolicy` |
| `telemetry.otlp_hosts` | `telemetry.otlp_endpoint` | `metrics/provider.py::_edition_locked_view`, read by `_otlp_destinations` and `otlp_egress_active` before any provider sees the config (an edition provider's own destinations stay) | `GET /api/config/schema` marks the path `lockedByPolicy` |
| `developer_mode` | `KIROCREW_DEV_MODE` (and the `-dev` home suffix for the update simulator) | `session_environment.py::session_environments_enabled` (remote placement stays off) and `dashboard/routes/system.py` (`POST /api/update/simulate` is not registered) | none; an environment variable has no setting API. The flag's on-loop persistence strictness only raises more, so it is left as is |

The update channel left this table when it gained its key, `updates.channel` (see governance.md, "Update pins"): an enterprise build follows the policy's channel, or `stable` with none, and still ignores `$KIROCREW_HOME/channel`.

Each locked config path is in `CONFIG_PATH_LOCKS`, which the schema endpoint reads. The dashboard shows a "Locked by policy" badge beside a schema setting reference with `lockedByPolicy` (`settingRef/SettingRef.tsx`) and beside the computer-use toggle, which it greys out, when its GET carries `locked_by_policy` (`settings/ComputerUsePanel.tsx`). A write of a locked value is still stored and is ignored on read, so a saved setting never loops on an error. Tests: `test/test_enterprise_unkeyed_locks.py`.

### Enterprise gate (login half)

The second reader of `is_enterprise()` is the enterprise gate ([RFC](../../request-for-change/rfc-crew-enterprise-edition.md), "Enterprise gate"). `kiro_crew/enterprise_gate.py` holds `EnterpriseGate`: open, or closed while any reason holds it (`close(reason)` / `clear(reason)`, each concern clearing only its own reason; `status()` returns `open` plus the reasons). An enterprise build does not start without a login or without its governance rules (`kiro_crew/enterprise_boot.py`). `GatewayOrchestrator.run` calls `require_kiro_login` after the sandbox warm-up and before any service starts: one fresh `KiroPrerequisiteService.login_verified(max_age_secs=0)` probe, and with no login it prints what failed and the fix (`kiro-cli login`, the install link, or the sandbox error) and exits 1. A `PlatformCompositionError` from `boot_platform` (the policy load) in `cli.main` or `run_gateway` goes to `refuse_policy_boot`, which does the same. Both run before the HTTP server binds, so a failed boot never serves anything; the desktop app shows that stderr in its start-failure window's log pane. Those checks run before the bind with their own limits (`--version` 10 s, `whoami` 30 s, policy fetch 10 s), so an enterprise desktop build waits that much longer for its local gateway: `packaging/build-desktop.sh` stages `website/electron/EDITION` from the same `KIROCREW_BUILD_EDITION` stamp (enterprise only), `website/electron/edition.js` reads it, and `gatewayWaitTimeoutMs` adds 80 s for a spawned local gateway. An `oss` desktop build stages nothing and keeps its 30 s (120 s on Windows) wait. Both gateway entrypoints then call `install_enterprise_gate` (`dashboard/enterprise_gate_middleware.py`) with the service whose probe saw the login (`start_dashboard` / `start_api_server` run the same check themselves when handed none); in an enterprise build it puts a gate on the app that starts OPEN, refuses to install on a service with no login, and an `oss` build gets nothing. `LoginWatch` keeps `login_required` in step with the login while the gateway runs, reusing the setup gate's probe through `KiroPrerequisiteService.login_verified` (re-probes when the last probe is past its age or the identity store names a different account; a probe that raises answers signed out). It checks at once on startup, then every 5 s while closed and every 60 s while open (a close wakes a watch sleeping its open interval, so it moves to the closed cadence at once), with the full probe at most every 60 s while closed and 900 s while open. Each request also reads the latched `authenticated` verdict, which only ever closes the gate, so an ACP auth failure (`mark_signed_out`) closes it on the next request. Only the watch's check reopens it. An enterprise build never assumes readiness: `KiroPrerequisiteService` drops `assume_ready` (`--test-mode`) there, so test mode cannot open the gate without a probe.

While closed (a login lost or expired while running), the gate middleware (inserted right after token auth in both chains) serves only `ALLOWED_WHILE_CLOSED` (method-scoped: the login status `GET /api/kiro-prerequisite` and its two owner-only remedies `POST /api/kiro-prerequisite/repair-specs` and `/update-cli`, the session-cookie routes `GET /api/auth/me` and `POST /api/auth/refresh`, session revocation `POST /api/auth/logout` and `POST /api/logout`, `GET /api/token/local`, the `/api/health|live|ready` probes, `POST /api/shutdown`, and the shell's static files) and answers a SPA document navigation with the static shell. Every other request, an unknown route and any WebSocket upgrade included, is refused 403 `enterprise_gate_closed` with the `reason`. Closing the gate also closes every connection that carried a gated request while it was open (tracked per connection until it closes, since aiohttp sends a returned response such as a download after the middleware returns). While closed, `GET /api/kiro-prerequisite` and both remedy POSTs (the dashboard caches each as its status) report `enterprise_login_required: true` and `initial_setup_complete: false`, and `KiroPrerequisiteGate` skips every bypass that would render the app (`ready`, an established install, another usable harness), so its first-run screens run in order: a missing CLI or a sandbox refusal shows its own remedy, then the sign-in step; the other-agents picker, which writes config, is hidden. That applies until the app has first been shown: a relock of a dashboard already in use keeps the app mounted and on screen, its requests refused until sign-in, and adds `KiroPrerequisiteRelockNotice` beside it: a display-only card (`pointer-events-none`, nothing focusable, no handlers, so it cannot reach an open dialog or popover) in the local pane's overlay container, saying to run `kiro-cli login` in a terminal (a non-owner is told to ask the owner; a bundled or off-PATH CLI gets no command, only a pointer to a new window's sign-in screen for the exact one). The status poll clears it once the gate reopens. A managed MCP server whose tool-policy read meets that 403 records `gateway_locked`, which is in `mcp_shared_policy._UNRESOLVED_REFUSES_CALL`, so its tools refuse the call rather than drop the operator's exclusions. The on-disk marker is untouched. `test/test_enterprise_gate.py` walks the dashboard and MCP route tables and pins which registered routes stay open. A governance fetch failure at boot exits (above); a failed refresh while running is meant to close the same gate under its own reason, and that is not wired yet. Tests: `test/test_enterprise_boot.py`.

#### Enterprise hardcoded Red controls (`edition_locks.py`)

The enterprise edition RFC ([rfc-crew-enterprise-edition](../../request-for-change/rfc-crew-enterprise-edition.md), appendix "Red") lists controls governance cannot reach. An enterprise build hardcodes them at each feature's own on/off read through the same module: `locked_off()` is true in an enterprise build; `follows_policy(decision)` serves the rows the RFC writes as "follows `<key>`; off by default" by accepting a permit only when the policy layer gave it (`layer` `policy` or `both`, not `default` or a `profile` alone); `sandbox_mode_floor()` / `unsandboxed_exec_floor()` are the one sandbox floor. An `oss` build passes every site unchanged. Which policy tier may widen a key (remote versus a local `security_policy.json`) is the RFC's D3 work, not these helpers.

| Red control | Enterprise behaviour | Where |
|---|---|---|
| Hooks by name | Follows `capabilities.script_hooks`, off by default: no script hook runs and no user kiro hook is installed (asked on the host key) until the policy enables it; once on, its `events` list (if named) also drops the user kiro hooks of each refused event | `hook_runtime/governance_gate.py::_script_hooks_capability_denied`, `agent_materialization/kiro_hooks.py::_user_kiro_hooks_locked` and `_drop_refused_kiro_hook_events` |
| Webhooks by source | Follows the `webhooks` scope, off by default (baseline): a source runs only when the policy names it, whatever the stored switch says | `webhook_sources.py::source_refusal`, asked by `webhooks.py::WebhookTokenStore.is_switch_on` (`/api/hooks/agent`, source `agent`) and `apps/builtins/ops_mission_control/backend/providers/webhook.py::enqueue` (the app's `/webhook` ingress, source `ops-mission-control`) |
| Signed extensions only, Extension versions | Follow install: `capabilities.extension_install`, off in the baseline, so both are off until a policy turns install on (see `docs/system-specs/modules/governance.md`, "Extensions, app versions and developer mode") | `platform/capability_bound.py::BoundedCapabilityManager` install ops and `sync_plugins` |
| Workload identity, Identity settings | `capabilities.agentcore` off: no posture, Gateway URL or workload name | `platform/governance_parts/carriers.py::agentcore_posture` |
| Content sent to external decisions | Follows `capabilities.decisions`, off by default: nothing is sent until the policy enables it; the local row holds that deny, as for any hosted deny, and the denial is audited | `decisions/capability.py::_row_denied` |
| Managed update commands | Off; the built-in updater path applies | `platform/update_provider.py::resolve_provider` |
| Minimum sandbox level | a requested `off` tier is clamped to the shipped default before the governance floor, and a declared `sandbox_allow_unsandboxed_exec: true` reads as the platform default; tighter values still apply | `edition_locks.py::sandbox_mode_floor`, read by `sandbox_tiers.py::_clamp_sandbox_mode_to_floor` (the clamp every spawn path shares); `edition_locks.py::unsandboxed_exec_floor`, read by `sandbox_unavailable.py::_allow_unsandboxed_exec` (the one unsandboxed-exec gate) |

Session runtimes, KAS spend cap, boot requirements and image share/copy have no code here: the RFC records that Crew cannot handle them. Tests: `test/test_edition_red_controls.py`.

## Gateway restart launcher

`PlatformContext.gateway_lifecycle` composes a `GatewayLifecycleProvider`.
`restart_launcher() -> str | None` returns an absolute stable launcher pathname;
`DefaultGatewayLifecycleProvider` returns `None`. A companion opts in through
`dataclasses.replace(ctx, gateway_lifecycle=provider)` at its trusted composition
root, only for installs its launcher owns. This is lifecycle selection, not an
update authorization setting: no request, config or environment command selects
it, and update governance is unchanged. `CONTRACT_VERSION` remains 1.

`gateway_restart.resolve_restart_launcher()` is the shared selector for dashboard
restart (including update apply) and the orchestrator's automatic-update restart.
Both import it before an update can retire the running package tree and call it
off-loop before saving or draining sessions. The provider must load its own
dependencies at composition and return cheaply without deferred imports. A
provider error or an explicit empty, relative, missing, non-file or non-executable
target refuses restart; only `None` reaches the interpreter, which normally takes
the existing `respawn_executable()` → `reexec_python_module()` path, including
core-managed virtual environments.

The core executes `[launcher, *sys.argv[1:]]` directly, without a shell or Python
`-m` prefix, and never resolves the launcher's symlinks: dispatch may depend on
its basename. `reexec_launcher` preserves the inherited environment, including
instance home and port, while pinning Python's UTF-8 stream settings. The launcher
owns choosing the current bundle/interpreter and replacing version-specific
`PYTHONPATH` and other environment entries before loading that bundle. Windows
launchers must be native `.exe` files; each argument is quoted for the CRT's
`execv` command-line reconstruction. POSIX receives the original argv unchanged.
The existing restart coalescing, callback fence and drain ordering remain with
their callers. CLI service-manager restarts are independent and unchanged.

Validation establishes availability at selection time, not future execution:
an updater must keep the stable launcher usable through handoff. The core does
not retry a failed explicit launcher with the old Python bundle. When it is the
*interpreter* an update retires, the guard below is what keeps the gateway
serving.

### The pruned-interpreter guard

`resolve_restart_launcher()` returning `None` and the interpreter no longer
existing is a real state: an `apply_command` that installs into a new versioned
tree and prunes the old one deletes the interpreter the running gateway was
launched from.

What made that state costly was the ORDERING, not the missing file. The
orchestrator saved, fenced, closed every session and only then raised `ENOENT`
from `os.execv`. Admission itself does come back —
`_finish_auto_update_apply` calls `resume_turn_admission_after_update()` — but the
sessions `close_all()` tore down do not, and `_pending_update_respawn` is cleared
immediately before the exec, so `_retry_pending_update_restart` can never fire.
What survived was a gateway with no sessions, running a different version from the
install on disk. The dashboard's own path is worse by one step: its `close_all()`
sets the closing flag without owning the update pause, so `resume_...` returns
early and admission stays shut as well.

Both consumers therefore ask before the point of no return. The dashboard's
`_restart_gateway` refuses with `Cannot restart: invalid Python executable path`
while every session is still answerable. The orchestrator defers, retaining
`_pending_update_respawn` so `_retry_pending_update_restart` completes the update
once the install is repaired. Neither drains on the chance that a restart might
still work.

Asking early narrows that failure; it cannot remove it. The target can be
replaced between the check and the call, and a file that is present and carries
the exec bit can still be an image this kernel refuses — a wrong architecture, a
truncated binary, a script whose interpreter is gone. The kernel is the authority
on all of those and reports them as `OSError` from `execv` itself, so the exec
site is the only place they can be answered. Both consumers answer by exiting:
`platform_compat.exit_after_failed_restart_exec()` logs CRITICAL naming the
target and calls `os._exit(1)`. Returning instead is what produced the original
defect — the orchestrator's caller catches `Exception`, logs and sleeps, and the
dashboard's path would fall through to `return True` and report a restart that
did not happen. Exiting makes the failure visible to whatever started the
gateway, frees the port so the operator's own relaunch can bind, and refuses to
serve the version skew. Reopening admission is not an alternative: the closed
sessions do not come back, and the skew would then be served. Because `os._exit`
runs no `atexit` handler, that function repeats the two bounded best-effort
flushes the force-exit signal handler performs — the CRITICAL line is the whole
diagnosis and is queued, not yet on disk. Only the event log's flush is bounded there:
the `gateway.log` tail goes through `cli.drain_log_queue_before_hard_exit()`, the shared
async hard-exit drain for that queue, so every hard-exit path keeps one spelling and one
ceiling for it. The event log's own ceiling is derived the way that function derives
its own — the inner bound plus one second — rather than chosen independently.

There is deliberately NO second pathname for an operator to declare as a fallback
exec target. Validating one is not reachable. A pathname's bytes do not decide
what the kernel execs: a `#!` line delegates to an interpreter the check never
sees, and a header that parses can still belong to a truncated binary. The only
way to learn the answer for certain is to exec the candidate, which means either
running an arbitrary operator-supplied binary or booting a second gateway. So the
recovery is repair-then-relaunch, which an operator can actually perform and the
core can describe honestly.

## Consumption-site wiring

Core consumption sites read the context rather than a module global. Standalone
behavior is unchanged because each Default adapter delegates to that same global.
Wired sites:

- `cli.py:main` / `slack/gateway_runtime/process_entry.py:run_gateway` — `boot_platform(cfg)` once at
  startup (gateway raises fail-closed; cli is defensive — standalone never raises).
- `slack/gateway_runtime/service_boot.py` gateway boot — `AgentRuntime.run_first_run_setup()` through
  `safe_context_call`. Previously this imported `agent.run_first_run_setup`
  directly, bypassing the seam; routing it through the context makes first-run
  provisioning genuinely extensible (an edition adds its own one-time steps).
  The Default adapter delegates to that same function, so standalone behavior is
  byte-identical — asserted in `test_cpp_wiring_standalone.py`
  (`test_default_agent_runtime_delegates_to_agent_first_run_setup` +
  `test_gateway_first_run_setup_routes_through_the_seam`). Best-effort: the
  gateway's surrounding `except` keeps a failure non-fatal to startup, and
  `PlatformCompositionError` still propagates fail-closed.
- `sandbox_argv.py` — `_live_plan_host`, the plan host `_build_launcher_script` /
  `_build_seatbelt_profile` both plan with, sources the sensitive-dir lists from
  `current_context().sandbox` through `sandbox._sandbox_policy` (Seatbelt's
  `.aws`-exclusion at the cc tier is preserved, as that renderer's declared capability). `sandbox_argv.py`'s
  `namespace_argv` / `sandbox_exec_argv` resolve argv[0] through
  `current_context().agent_executable` before applying the core sandbox. The
  public Default is identity; a companion may return the direct
  executable behind an edition-managed launcher to avoid nested isolation, but
  cannot disable or weaken the outer sandbox. A transient adapter error falls
  back to the original executable (outer sandbox still applies); a
  `PlatformCompositionError` propagates fail-closed.
- `hooks.py` — the deny check routes through `current_context().security.is_denied`;
  the kiro-hooks egress (`dashboard/handlers/hooks_scripts.py`) scrubs command/matcher
  through the shared `redact_via_context` shim.
- Credential redaction — all egress scrubs route through the single
  `kiro_crew.platform.redact_via_context` shim (the one canonical
  fail-closed-aware shim; modules import it as `redact`). Covers: `agent_parts/spec_build.py`
  SEL-audit callers, `mcp_core_parts/` chat-history/crew output, `mcp_cron_vet.py`
  deny-reason + script-vet and `mcp_cron.py` timezone messages, and the file-content egress the
  `dashboard/handlers/files.py` facade serves (the outbox's slot append in
  `dashboard/file_api/outbox.py`, file_read in `dashboard/file_api/editor.py`,
  file-watch and the download gate in `dashboard/file_api/transfer.py`) as well as
  the filename/path/description gates.
  Standalone is byte-for-byte the prior
  exfil-then-credential two-pass (the Default `CredentialPolicy.redact` delegates
  to `security.redact`); a loaded companion adds its internal-token regexes
  uniformly across every egress surface.
- Code text (`kiro_crew.platform.context.redact_code_via_context`) — `redact_via_context` run
  inside a `security.redaction_switch.code_text()` scope, where pass 3 of the core credential pass
  (the entropy-and-shape guess at a secret key) spares an ordered alphabet and a data URI payload of
  64 characters or more and judges every other run as it does anywhere, while every other pass, the
  exfiltration-URL pass and a loaded companion's patterns run as they do for `redact_via_context`.
  Its one caller is the member dashboard page scrub, which takes `redact_via_context` over the whole
  page as its baseline and puts what this returns in place of the baseline's for the text of the
  page's `<script>` and `<style>` elements. Like `redact_owner_view_via_context` it is a scope the
  caller must open, so a sink that does not call it keeps the full pass.
- Content scans (`binary_content_is_flagged`, `wide_content_is_flagged`) are serialized
  behind one re-entrant lock (`_SCAN_LOCK`) and their verdicts are kept in a 256-entry LRU
  (`_SCAN_VERDICTS`) keyed by (scan kind, SHA-256 of the bytes, `governance_generation()`),
  so the same file served repeatedly is scanned once. A verdict is kept only if neither the
  governance generation nor this thread's redact-degrade count moved while it ran, so an
  answer the bare baseline gave in the active context's place is never reused; a scan that
  raises caches nothing and still fails closed.
- Gate-side LOG lines (`kiro_crew.platform.redact_log_via_context`) — the same
  companion-aware redaction for an operational log or audit line, which unlike an
  egress sink cannot afford to raise. `redact_via_context` propagates
  `PlatformCompositionError` because refusing to SEND is both safe and the point;
  a log line is not an output boundary, and there the same raise buys nothing
  while costing availability. The two no-companion states are treated
  DIFFERENTLY, and conflating them is the subtle part: a context that IS
  installed whose policy failed to compose withholds the line's TEXT
  (`LOG_WITHHELD_PLACEHOLDER`), because the host is known non-standalone and the
  baseline would be a real downgrade; whereas NO installed context keeps the
  baseline, because nothing there evidences a companion and withholding would
  destroy diagnostics. Transient adapter errors still degrade to baseline
  (inherited), so a merely flaky companion does not blank the logs. Covers
  `platform/update_provider.py` (a failed update command's stderr),
  `task_planner.py` (an unparseable LLM response before the diagnostic ERROR
  line) and `name_grant.py` (a model-authored tool title before the shared SEL
  row — the one that PERSISTS rather than rotating out of a log window).
  Resolves NOTHING: the no-context branch is answered by the bare
  `installed_context()` read, whose contract this fits exactly — the no-context
  answer is the same as the default-context answer, since
  `DefaultCredentialPolicy.redact` delegates to `security.redact` — so a
  per-line caller never re-pays the unmemoized config load described in the
  exemption bullet below.
- `mcp_gateway/backend.py` deliberately stays on the baseline redactor for its
  backend stderr and spawn-argv log lines. `gatewayd` is NOT the composition
  process: it never runs `boot_platform` and never installs a context (see the
  reasoning at `mcp_gateway/app_call.py`, which relies on the same fact for the
  security ceiling; its one telemetry-only composition, under
  `mcp_gateway/gatewayd.py::_compose_and_apply` below, installs nothing). So
  routing those sites through the context could not reach a
  companion policy in the first place, and would only trade the baseline for a
  lazily-composed standalone default resolved per stderr line on the event loop.
  Closing this residual means handing gatewayd a composed context, the same
  remedy that module already names for the ceiling, and is a separate change.
- **Which spelling a gate-side log line uses is gated in CI**
  (`test_security_posture.TestGateSideLogRedactorSpelling`). The egress
  classification (`security_posture._REDACTION_SINKS` vs
  `NON_EGRESS_REDACTION_MODULES`) is a different axis and cannot cover this one:
  all three sites converged above sat correctly bucketed as non-egress, with
  accurate reasons, while reading the companion-blind baseline pass. The gate
  scans for the PROPERTY — a baseline redactor's result reaching a log or audit
  write, either nested in the call or through a local assigned from one in the
  same scope — rather than for a filename or a subject name, because a grep
  scoped to files mentioning stderr is exactly what missed `task_planner.py` and
  `name_grant.py`. `_BASELINE_LOG_SITE_CENSUS` in `test/test_security_posture.py` records the
  per-module residual and owns the counts; it is a census, NOT an approved-exception list and NOT a to-do
  list, since for a process that never composes — gatewayd above — the baseline
  is the honest answer. Growth in any module, or a first site in a module absent
  from it, fails until someone either calls `redact_log_via_context` or raises
  that number and records which PROCESS the site runs in and why the baseline is
  correct there. A count that has fallen must be lowered, so a converted module
  does not leave free slots behind.
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
  companion adapter is the only supplier. EVERY failure degrades to
  `frozenset()` — the empty set means MORE redaction (every host runs the
  heuristics), the safe direction, and stricter than any companion-supplied list
  could be; NO logging on the degrade path (runs inside the stdio MCP servers).
- Pass-4 token-parameter URL prefixes
  (`CredentialPolicy.token_param_exempt_url_prefixes()`) — redaction pass 4
  redacts every `token=` URL value by name. It skips only a literal `?token=`
  immediately after a companion-supplied `https://host/path` prefix whose local
  boundary and preceding text prove it is not nested in another destination,
  whose value is one URL-safe/base64 run plus closers, and which no earlier pass
  claimed. The companion must list the host in `exempt_exact_hosts()` too, or
  earlier base64/query heuristics can redact the URL before pass 4. The
  preceding-text guard rejects URL, backslash, scheme and HTML
  destination signals — any `&` (character reference) or `<` (tag) counts, scanned
  over the raw text and never decoded — after blanking earlier links that already
  passed the exemption together with their own `<` autolink opener, so a list of
  approval links remains usable without making whitespace a trust boundary.
  Context-incomplete streaming windows never apply the exemption; the complete
  stored message may apply it when the destination guards can inspect all prior text.
  While the prefix set is non-empty, the dashboard's per-delta redaction of the text
  it accumulates for that stored message leaves pass 4 to the whole-segment run
  (`redact_credentials(..., defer_token_param=True)`), so a delta cut inside the
  link cannot redact a value the whole text would exempt; every other pass still
  runs per delta, and the live wire window still redacts the value.
  For links the user must open whose token is a lookup key, e.g. a one-time
  approval workflow. Read lazily on the first pass-4 match via
  `installed_context()` (declared in `PEEK_CALLERS`) with the same degrade
  contract as `exempt_exact_hosts`: no context, a pre-method adapter, an
  exception or a member without a path all mean MORE redaction. The display
  redaction memo folds the prefix set into its key. Public default:
  `frozenset()`; never sourced from `config.json`.
  The set is read via `installed_context()`, so a process with no installed
  context takes that same empty set WITHOUT resolving one: resolving would load
  config and discover entry points per call, and on a non-standalone profile
  `current_context()` never memoizes its fail-closed verdict, so a per-line
  caller (`_pump_stderr` redacting backend stderr) would re-pay that synchronous
  I/O for every line on the gateway event loop. This is deliberately INVERTED vs
  `redact_via_context`, which must keep propagating: that seam SUBSTITUTES a
  companion's redaction for the baseline, so a missing context there would fail
  OPEN, whereas here it fails STRICTER. Because this lookup only ever RELAXES the
  heuristics, it can never be the reason a credential reaches a log — the
  credential pass (`redact_credentials`) is independent of it and unchanged by a
  missing context. **Deferred-import exception:** `security` reads the set
  through a FUNCTION-LOCAL import of `kiro_crew.platform.context` (the `sel.py`
  pattern), so the CPP import-direction invariant holds — `platform/defaults.py`
  imports `security` at module load, and `security` never reaches `platform` at
  module-load time (only at call time). v1 method addition to the existing
  `CredentialPolicy` Protocol; no `CONTRACT_VERSION` bump; `DefaultCredentialPolicy`
  returns `frozenset()` so standalone redaction is byte-identical.
- `agent_parts/spec_build.py` and `agent.py` — `current_context().mcp_tooling.extra_mcp_servers()` merged
  additively into the agent config build; the dynamic refresh also re-pins an
  existing entry's `command`/`args` and keeps its other keys.
- `slack/events.py` / `slack/handler_runtime/dispatch.py` / `dashboard/handlers_system.py` —
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
- `mcp_gateway/gatewayd.py::_compose_and_apply`: **the one exception to "the
  daemon never composes": telemetry only.** On a consenting non-standalone host
  the daemon calls `platform/bootstrap.py::compose_telemetry_provider` on a
  background thread, after its startup recorder build. That runs the same
  profile resolution, fleet admission gate, contract check and
  `assert_security_floor` as `bootstrap_context`, builds the edition's whole
  context, keeps ONLY `.telemetry` for OTLP destination resolution, and never
  calls `set_context`: every other `current_context()` reader in the daemon,
  including the security ceiling (`app_call.py`) and the backend log redactor
  (`backend.py`), keeps its no-context behavior. The companion's construction
  side effects therefore also run in the daemon; measured on an edition install
  against a data home the main process had already set up, that was about 1.1 s,
  no file created or changed, and two helper threads. Rule for later seams: a
  SECURITY seam must never be resolved through `compose_telemetry_provider` (it
  discards everything but `.telemetry`); it keeps the parent-resolves-and-passes
  channel above. **Why telemetry does not use that channel:** an
  `OtlpDestination` carries `session`, an authenticated transport whose
  `Session.auth` is re-evaluated on every export so a short-lived rotating
  credential is re-read each time (see `OtlpDestination` in
  `platform/interfaces_runtime.py`). A live session cannot cross a process
  boundary. Passing the endpoint plus a credential by argv, environment or file
  would freeze that credential at spawn in a long-lived daemon, which is exactly
  what the `session` seam exists to avoid, and would place a bearer secret in the
  daemon's environment or on disk. A watch path (the credential-watch flag above)
  is not secret and does not rotate, which is why argv suits it.
- `apps/manager_builtins.py` — builtin discovery + orphan detection merge
  `current_context().apps_loader` sources.
- `apps/registry_pipeline/sources.py` / `apps/routes_art.py` — clone-sandbox-mode decision routes
  through `current_context().registry` (`_context_clone_sandbox_mode`).
- Telemetry `record_event` sites — `dashboard/server_runtime/diagnostics.py` records `gateway_start`
  at boot; `_turn_context_usage` (`dashboard/chat_turn/turn_stats.py`) and
  `slack/handler_runtime/turn.py` record one `interaction` event per successful chat turn
  (immediately after the `record_success` call, non-cancelled / non-retrying branch only;
  cancelled turns emit nothing). **The interaction payload is strictly metadata —
  `session_key`, `surface` (`"dashboard"` / `"slack"`), and `model` — never
  prompt/response text or file contents.** All sites are best-effort
  (try/except-Exception, debug log); the Default provider is a no-op so
  standalone is byte-identical. Phase-1 scope is dashboard + slack only
  (cli_chat/cron/subagent/task_executor sites are deliberately not wired).
- Preflight checks (`IdentityProvider.preflight_checks()`) —
  `kiro_crew.preflight.run_preflight_checks()` runs seam-supplied pre-launch
  checks at exactly two sites: the `gateway` dispatch in `cli.py` (before
  faulthandler/lock/`asyncio.run`) and `_token` in `cli_server_requests.py` (before TTL
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
  `dashboard/server_runtime/tunnel.py::_wire_tunnel_shutdown`** — an `app.on_cleanup` hook that
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
- `dashboard/server.py` (the gate in `dashboard/server_runtime/tunnel.py`) — tunnel enable-gate
  ORs in `current_context().tunnel.enabled()`. **Dashboard contributor:**
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
- `dashboard/server_runtime/security_middleware.py` `_mixed_internal_api_paths()` — unions
  `dashboard.mixed_internal_api_paths()` into the module-level
  `_MIXED_INTERNAL_API_PATHS` at BOTH `token_auth_middleware` construction sites
  (the dashboard chain and the headless `--slack-only` one), so the two cannot
  gate different route sets. It exists because an edition mounts its routes
  through `contribute_routes`, so the core cannot name them in a frozenset —
  without it an edition's own MCP tool authenticating with the loopback
  `X-Internal-Secret` handshake is not recognized as internal at all and answers
  `Token required` on every call. ADD-ONLY with two core-enforced limits: a
  contributed path matching a CORE STRICT entry is DROPPED and SEL-audited
  (strict hard-denies off-loopback where mixed accepts a validated cookie, so
  admitting one would soften a deliberately loopback-only route), and the result
  is a union so a contribution can never remove a core entry. BOTH outcomes are
  recorded — the admitted set is logged and SEL-audited at composition time
  alongside the drop audit, because a dropped contribution is invisible to the
  EDITION while an honoured one is invisible to the OPERATOR, and SEL is what has
  to distinguish a widened deployment from stock; a public build contributes
  nothing and stays silent. The degraded-contributor path goes through
  `safe_context_call`, not a hand-written `try/except`: a
  `PlatformCompositionError` is RE-RAISED (a host that could not compose its
  companion must abort, never fall back to open-source defaults) while any other
  contributor failure degrades to no contribution. The contribution is also
  MATERIALIZED inside that thunk, so a generator raising part-way through
  iteration degrades instead of escaping middleware construction and stopping the
  gateway from binding at all. The overlap is checked in BOTH directions by
  `_would_soften_a_strict_path`: the request is what
  gets prefix-matched, so a contributed ANCESTOR of a strict entry reclassifies it
  exactly as a child does — a request for the strict path then matches both sets,
  and token_auth's off-loopback arm tests `_matches_mixed` first. Every degraded
  contributor shape — raising, absent method, non-iterable, non-path entries —
  contributes nothing rather than widening the set on a value the core could not
  check; a contributor predating the seam is NOT logged as a fault, while one
  that raises is.
- `dashboard/handlers_system.py` — `frontend_rum_config()` added to the status
  payload only when non-None.
- `config/loader.py` `build_provider_factory(cfg)` — the
  LLM-provider factory build sites (`cli_chat`, `cli_server`,
  `session.reload_provider_factory`, `slack/gateway`, `cli`, `cli_commands_eval`) route
  through `current_context().providers.create_factory(cfg)` instead of
  `cfg.create_provider_factory()` directly. The Default returns exactly
  `cfg.create_provider_factory()` (identity), so the public edition is unchanged;
  the companion selects its Bedrock-hosted backend only when opted in. The
  fallback is passed as a lazy `fallback_factory` so the happy path builds the
  factory exactly once (no eager double-build) and a failure inside the fallback
  is still caught by the shim.
- `dashboard/handlers/knowledge_lifecycle.py` — the `SyncScheduler` connector map merges
  `current_context().knowledge.extra_connectors(cfg)` after the built-ins
  (`local_folder`/`obsidian_vault`); Default returns `{}` so standalone is
  unchanged.
- `cli.py` `main` (jail gate, factored into `cli_jail._jail_reexec_gate`) +
  `cli_doctor.py` — for `_JAILED_COMMANDS`
  (`chat`/`run`/`consolidate`/`eval` — the rule is "every command that
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


### Edition seam additions (v1, no `CONTRACT_VERSION` bump)

Existing-Protocol methods added / wired so a companion can re-introduce behavior
the public fork dropped without the core importing it. All are v1 additions (a
`Default*` no-op reproduces today's OSS behavior exactly — a standalone process
is byte-identical) with no `CONTRACT_VERSION` bump.

- `SlackEnterpriseGate.heartbeat_safe_tools() -> frozenset[str]` — consulted by
  `slack/gateway_runtime/tool_policy.py::_is_heartbeat_safe_tool` after the core
  branches, matched only as the exact trusted `@server/tool` identity. Default
  `frozenset()`. ADD-only; never sourced from config. The `@server/` prefixes of
  its pins also decide which default-spec servers
  `worker_agent.heartbeat_mirrored_servers()` mounts on the heartbeat agent after
  `HEARTBEAT_MIRRORED_SERVERS`: each is copied verbatim (narrowing filters
  included) minus `autoApprove`, which would let kiro-cli approve a call before
  `_heartbeat_approval` and its SEL audit saw it; a name the default spec does not
  mount is skipped; the spawn-path fingerprint and freshness check read the same
  list. The read goes through `safe_context_call`, so an older companion's
  `AttributeError` degrades to the core list and `PlatformCompositionError`
  re-raises.
- `AppsLoader.registry_rows() -> List[Dict]` — ADD-only merged by
  `apps/registry_pipeline/sources.py::_load_registry_file` after bundled `app-registry.json`
  (same-`name` core row wins). Default `[]`.
- `AppsLoader.default_registries() -> List[Dict]` — external app registries the
  edition pins, merged with the operator's `config.registries` by
  `apps/registry_pipeline/sources.py::_effective_registries`, which is the single list every
  registry consumer reads (index fetch/refresh, the trusted-host allowlist, row
  lookup, install, the blob-proxy allowlist). Rows are the field shape of
  `ExternalRegistryConfig` (`{name, repo, branch, label, review, trust}`).
  `label` (a display name shown instead of the `name` id) and `review` (`""` /
  `"curated"` / `"community"`, which badge the dashboard renders) are display-only
  and change no security posture; an unrecognised `review` degrades to `""` (no
  claim) and is logged, never dropping the row, since a display field must not be
  able to remove a registry from install and the security gates. `label` never replaces
  the id: cache paths and installed apps' `_registry` tags are keyed by it. Unlike
  `registry_rows`, the **edition row wins** a `name` collision — and when the two
  rows name DIFFERENT repositories, **neither** is served, because the index cache
  is keyed by name and the displaced row's cache would otherwise be read under the
  winner's identity. Merged at the
  consumption sites, never inside `KiroCrewConfig`, so a config save can never
  persist an edition default into the operator's file. Default `[]`.
  A row may carry `allowDefaultInstall: true`, which lets that registry's index
  mark apps `defaultInstall` for a one-time install and trust grant on first
  boot. Honoured only for a pinned row in force at the `owner` tier; contract in
  [app-kit-platform](app-kit-platform.md) §15.1.
- `TelemetryProvider.otlp_destinations(cfg) -> Sequence[OtlpDestination]` — the
  OTLP collectors this edition sends telemetry to. Read by
  `metrics/provider.py::_otlp_destinations` once per recorder build (through
  `safe_context_call`, fallback `()`), which attaches one
  `PeriodicExportingMetricReader` per destination naming the `"metrics"` signal,
  AFTER the built-in local JSONL reader. ADD-only: an edition can add a
  destination but can never remove or replace the local sink, relax consent or
  attribute sanitisation, or set the export cadence. `OtlpDestination` carries
  `name` (non-secret, log-safe — the endpoint value is never logged), `endpoint`,
  `signals`, and an optional authenticated `session`; the session is the seam a
  ROTATING credential composes against, since `requests` re-evaluates
  `Session.auth` per export where `OTEL_EXPORTER_OTLP_HEADERS` freezes at
  construction (static per-destination headers ride on `Session.headers`).
  Must be cheap and side-effect-free per call: it is read once per recorder build
  AND on every egress-posture read (Privacy panel status, each `telemetry.enabled`
  write), so an edition builds its transport once rather than minting a credential
  inside it. A provider that does not implement the method answers "no
  destinations" rather than "unknown", so a pre-seam edition keeps the two
  surfaces agreeing. Signal-agnostic on purpose: the OTLP metric, span and log
  exporters take the same constructor arguments, so a later core that emits traces
  or logs reads the same descriptor instead of needing another method. Deny-by-
  default — an empty `endpoint`, or a signal this core does not emit, is dropped.
  Default: one destination for a non-empty `telemetry.otlp_endpoint`, none
  otherwise (byte-identical to the endpoint-only exporter it replaced).
- `DashboardContributor.on_user_message(app, message)` — fired once per user
  message by `dashboard/chat_api/chat_send.py::api_chat` before the turn, inside a
  fail-safe `safe_context_call`. OBSERVER only. Default no-op.
- `McpToolingProvider.extra_skills()` — WIRED: `SkillsLoader.__init__`
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
- `CapabilityManager` (operations-based external package/capability manager).
  The seam names OPERATIONS, not a binary whose exact CLI grammar the core would
  hardcode: the
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
  row outside every `extra_skills()` root. Two further constraints bind the keys
  an edition may hand out. **A root the core already keys itself is not
  `package/` territory:** `~/.kiro/skills`, the data home skills dir, configured
  `skills.extra_paths`, and the active project's `.kiro/skills` are keyed
  `kiro-user/`, `kiro-workspace/`, or unprefixed, so advertising one of them from
  `extra_skills()` (legitimate — it makes the loader index it) does NOT also
  expose it under `package/`; `_edition_package_roots()` computes that difference
  once and both catalog enumeration and path resolution read it, so the two
  cannot drift. **Resolution is exact-first and refuses ambiguity:** a
  `package/<name>` request prefers `<root>/<name>/SKILL.md` over a nested
  `<root>/<Pkg>/<name>/SKILL.md`, and when two DISTINCT files tie within a tier
  it resolves to `None` — HTTP 404, with the competing candidates logged — rather
  than picking one, because the key cannot express which was meant (paths that
  merely symlink to the same file are not a tie). An edition that wants both of
  two same-named skills reachable MUST therefore key them distinguishably);
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
  cross-process coordination with `bridges_mcp_config.py`;

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

  `async registry(query: str | None = None) -> List[Dict]` (the manager parses its
  own registry output into entries; the core passes them through as
  `{"servers": [...]}`). The public
  `DefaultCapabilityManager.available()` is `False` → the handlers return HTTP 503;
  a companion implements registry-backed management. This is the operations-based
  Protocol the prior binary-name seam's contract note anticipated — chosen now,
  pre-launch, so no external CLI grammar fossilizes in the core.

  > `query` is an optional free-text filter HINT, sent only by MCP discovery
  > search (`mcp_providers/capability.py`); the browse endpoint
  > `GET /api/capability/mcp/registry` omits it and gets the full listing. The
  > provider hands on at most `_LIST_LIMIT_GUARD` (500) entries, and when a query
  > is present that cap applies to the MATCHES: it bounds the fan-out, never the
  > searchable window. So a manager whose registry is larger than the cap does NOT
  > have to filter server-side to stay searchable, and ignoring the hint costs
  > nothing but the work of returning its own catalog — it is a cost hint, not a
  > correctness one. The hint is feature-detected on the signature
  > (`mcp_utils.registry_accepts_query`) and forwarded by
  > `BoundedCapabilityManager`, so an edition still on the zero-arg signature
  > keeps working.

  **Second consumer — App Kit dependency resolution.** `apps/dependencies.py`
  resolves an app manifest's `dependencies.capabilities.{mcp,skills}` through
  `install_mcp`/`install_skill` (and `uninstall_*` on cleanup) rather than
  shelling out to a named binary, reading the manager via `current_context()` so
  it inherits the `BoundedCapabilityManager` timeout wrapper. The seam is probed
  **lazily** — a commands-only manifest never touches it. When `available()` is
  `False` the entries are recorded as `failed` (unresolved) and the app still
  installs, so a public install surfaces the unmet dependency instead of silently
  reporting success. `dependencies.capabilities.agents` is **declarable but never
  gateway-installed**: `CapabilityManager` does expose `install_agent` /
  `uninstall_agent` (and `/api/capability/agents/{install,uninstall}` routes),
  but `apps/dependencies.py` does not call `install_agent`, so those entries
  always report unresolved — declare them `managedBy: app` or install them out
  of band. The wire key `aim`
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
- `SkillDiscoveryProvider.skill_providers() -> List[SkillProvider]` — WIRED: the
  dashboard discover registry (`handlers/discover._build_registry`) registers each
  returned provider after the built-in one, gated per provider by the same
  `external_access.admits_registry("skill", name, api_base)` decision the built-in
  provider passes through — a managed allowlist applies uniformly, with no
  edition-specific carve-out. ADD-only and de-duped by name (the built-in wins a
  collision, so an edition cannot silently replace an identity the policy
  admitted). Each returned object implements the
  `kiro_crew.skill_providers.base.SkillProvider` protocol and SHOULD expose an
  `api_base` naming its catalog endpoint (the identity an allowlist is written
  against; a provider without one is gated on an empty base). Provider-sourced
  result fields flow through the existing `_redact_external` scrub before
  reaching the dashboard. Read fail-closed through `safe_context_call` (fallback
  `[]`). Default `[]`, so the public edition searches only the built-in catalog.
  v1 addition; `Default` returns `[]`.
- `ImportSourceProvider.import_sources() -> List[ImportSource]` — WIRED:
  `onboarding_import._sources()` unions the returned descriptors over the core
  builtins for every scan, apply, and id-validation path, so a registered source
  is accepted by `/api/onboarding/import/*` and rendered by the import wizard with
  no core branching. Default `[]`, so the public edition offers only the foreign
  agents it ships. One descriptor carries id, display name, root resolution, and
  the agent's own managed MCP server names, so a registration cannot
  half-work; a malformed one (no id, a reserved id, no
  resolvable root, an id that reuses a builtin, a traversing `home_dir`, or a
  `stale_mcp_binaries` entry naming a shared runtime) is dropped with a warning
  rather than shadowing a builtin or reclaiming unrelated MCP servers. A
  descriptor names neither a reader nor a layout: the engine does all reading, so a
  registered source cannot bypass the content gates inside the engine's readers.
  `superseded` is a separate opt-in: only an agent this product REPLACES has its
  leftover MCP entries reclaimed from the user's global provider config, because a
  live foreign agent's servers are still in use. A registered source is read as an
  install of this product's own on-disk layout — a predecessor, a rename, or a
  fork. v1 addition; `Default` returns `[]`.
- `FeedbackTrackerProvider.report_issue_url(bundle, note, *, prefill) -> Optional[str]`
  — WIRED: `diagnostics._tracker_issue_url`, once per bundle for the dashboard's
  `BundleResult.github_issue_url` (`prefill=True`) and once per `kirocrew doctor`
  print (`terminal_issue_url`, `prefill=False`). `None` (the Default) keeps the
  public pre-filled GitHub link byte-for-byte; a string REPLACES it. `prefill`
  names the surface: the dashboard renders the URL from a JSON field no redactor
  scans, while a terminal print crosses `redact_exfiltration_urls`, which
  replaces any URL whose query is 200 characters or longer, so an edition keeps
  that variant's query short. Read through `safe_context_call` (fallback `None`):
  diagnostics catches `PlatformCompositionError`, logs a warning and returns an
  empty issue link while keeping the bundle usable, without a standalone fallback.
  `kirocrew doctor --bundle` prints the bundle path and explains why the link is
  skipped. Any other error, a non-string or an empty string degrades to the public
  link. The wire field keeps its `github_issue_url` name (a rename is a take-away
  change). v1
  addition; the second half of the frontend `setFeedbackTracker` seam.
- `SpeechProviderRegistry.stt_providers()` / `tts_providers()` / `default_stt()` / `default_tts()` -- WIRED through `kiro_crew.speech_providers`: `config.sections_stt.valid_stt_providers()` (the `stt.provider` loader, `stt_provider_is_coerced`, `kirocrew config set stt.provider`) and `voice_reply.valid_providers()` / `default_provider()` (`resolve_configured_provider`) union the registered names with the built-ins, and an absent key takes the edition default. An unusable stored value keeps its built-in fallback (STT fails closed to `off`, TTS falls back to `system`), so an edition default is never reached through a typo. Read through `installed_context()` (declared in `PEEK_CALLERS`): the STT half runs inside `KiroCrewConfig.load()`, where `current_context()` would re-enter the load; the no-context answer is the built-in sets. Because the boot config is loaded before any context exists, `bootstrap_context` re-reads `stt` once after install when the registry adds an STT provider or default (a no-op for the Default). The schema's `stt.provider` enum still lists only the built-ins; validation holds a registered value aside across the enum check. `validate_speech_registry` refuses, at boot, a name that collides with a built-in, retired or `off` provider, a duplicate or blank name, or a default naming no provider. Dispatch: every site keeps its built-in branches first and asks the registry only for a name that is not a built-in (`speech_providers.stt_provider()` / `tts_provider()` answer `None` for every built-in). `transcribe.availability_detail` / `is_available` answer through the provider's `check()`, `transcribe_audio` calls `transcribe_file`, `voice_reply.is_available` calls `check()`, `voice_reply.synthesize_speech` and `/api/voice/synthesize` call `synthesize` (one format per stream, `audio/mpeg` or `audio/wav`, at most 64 MiB, joined into one replay clip), and `/api/ws/stt` grants `stt_stream.streaming_providers()`: the built-ins plus a registered provider whose `streaming` is exactly `True`, whose `open_stream()` session runs through the same loop as `apple`. Core calls every provider method on the caller's own event loop and pins none. Provider rule: a provider must not keep a loop-bound client (an aiohttp or httpx session) across calls; it creates one per call or keeps one per loop, because the gateway, a channel and a blocking caller may each call it from a different loop. Blocking callers (`transcribe.is_available`, `voice_reply.is_available`, run off-loop by Slack, Discord, Telegram, meetings and voice replies) use `check_status_sync`, which runs the check on a fresh loop in a daemon thread and waits at most `CHECK_TIMEOUT_SECS` plus a one-second grace, so a provider that ignores cancellation holds only that thread. A `check()` is bounded by `CHECK_TIMEOUT_SECS` and fail-closed: a timeout, a raise or a malformed status reads as unavailable, and an `action` survives only as `{kind: "login", label}`. Capabilities: `GET /api/config/stt` and `GET /api/voice/config` each serve `providers`, one row per selectable provider (a built-in is `{name, builtin: true}` with no probe; a registered one is `{name, label, builtin, available, reason, action, error}`; `error` is true only when the check timed out or raised, never for an answered "not available"), built-ins first in the picker's order, then the registered ones (STT keeps `off` last). Saving: `KiroCrewConfig.save()` writes a stored, non-built-in `stt.provider` back as stored when the snapshot degraded it to `off` and the registry is still pending or registers the name (`_restore_stored_stt_provider`), so a config loaded before boot never persists `off` over an edition choice; standalone still saves an unknown name as `off`. v1 addition; `Default` registers nothing.
- `speech` capability `providers` list -- the dashboard half of the slot. `GET /api/config/stt` and `GET /api/voice/config` each serve `providers`: one entry per selectable provider, built-ins first in today's order. A built-in entry is `{name, builtin: true}` and runs no probe; a registered entry is `{name, label, builtin, available, reason, action, error}`, where `available` / `reason` come from the provider's `check()`, `action` is `{"kind": "login", "label": <account>}` or null, and `error` is true only when `check_status` timed out or raised (false for a missing login or install). The Settings pickers (`SttSettings`, `VoicePanel`) render their options from it through `website/src/lib/speechProviders.ts`, and fall back to their hardcoded built-in options when an older gateway serves no list. An unavailable registered provider stays selectable and shows its `reason` under the picker (`SpeechProviderNotice`): an `error` row through `ErrorNotice`, any other as status text with its sign-in hint; built-ins keep their existing availability UI, so a standalone install shows no new text. Field contract: `stt-streaming.md` § "The capability `providers` list"; TTS precedence: `voice-streaming.md` § "Registered providers".

**`McpToolingProvider` is intentionally scoped to MCP tooling only** —
`extra_mcp_servers()`, `extra_skills()`, and `extra_mcp_scopes()`. Agent
catalogs, prompt sources and capability management each own a dedicated Protocol
(`AgentCatalogProvider`, `PromptSourceProvider`, `CapabilityManager`) so the CPP
layer keeps its "one adapter per concern" shape; every edition hook lands on its
own interface rather than accreting onto the nearest existing one.

**Agent discovery.** `agent_discovery.py`, the facade of the `agent_discovery_parts/` owners,
owns it, and `AgentInfo` is the record
it yields. The `source` classification is generic — no product-specific hardcode:
`"kirocrew"` for `kirocrew.json` / `kirocrew-lite.json`, `"package"` for a
package-installed agent, `"builtin"` for the rest. It has many consumers —
`agent`, `session`, `context`, `subagent`, `mcp_core`, `cron_script`,
`slack/handler` and several dashboard modules among them — so treat any list here as
representative rather than exhaustive.
- **There is no browser-auth provider hook.** Browsing runs through the external
  `playwright-cli` binary (`kiro_crew/browser_cli/`), and a logged-in session
  reaches that binary through the CLI's own surfaces (`state-save` /
  `state-load`, `attach --extension`) rather than through an in-process
  provider.
- `hooks.register_internal_read_path(read_id, rel_path)` — guarded seam adding a
  fixed-path entry to `_INTERNAL_READ_ALLOWLIST` (rejects `..`/absolute/
  non-sensitive/repoint).
- `security._SENSITIVE_HOME_DIRS` gains the SSO cookie directory (live SSO
  bearer cookie; inert on a host without it).
- `config.knowledge.doc_ingest_hosts` (list) — SSRF-safe allowlist for the
  server-side fetch path only; empty = deny-by-default. The agent-driven
  `auto_add_documents` path is NOT gated
  on it: the agent hands over text it already fetched, Kiro Crew fetches nothing.
- `KiroCrewConfig._extra_sections` (private) — unknown top-level config.json
  sections captured at `load()`, re-emitted by `to_dict()`, so an edition
  section is not dropped on `save()`/PATCH. Excluded from the JSON schema
  (`build_json_schema` skips leading-underscore fields). Data-preservation half
  of the eventual `ConfigSchemaContributor`; Settings-visibility half is TODO.
- ACP claude seam (inert on kiro-cli, and implemented in the core because CC is
  selectable): `AcpClient._session_mcp_servers()` translates the materialized
  kiro agent spec (`acp/session_mcp.py`) and feeds both `session/new` +
  `session/load` `mcpServers` — gated on membership in
  `ACP_BACKENDS_SESSION_MCP_ARRAY`, not on `_is_claude`, since reading no agent
  file is a property of the transport rather than of Anthropic; the claude adapter's
  launch (`ClaudeLaunch.resolve_spawn`) calls `_write_claude_local_settings()` (still identity-gated — it writes a literal
  `.claude/` path) on the PRIMARY
  spawn path (permission mode, `availableModels`, resolved model — snapshotted and
  restored by `_reset_state`); `AcpClient`/`AcpProvider` take a `permission_mode`
  kwarg (Default `None`); `acp/types.py` adds `CC_PERMISSION_MODE_DEFAULT` /
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
    thread_ts)` — fired in `dashboard/token_auth.py` after `bind_token_peer` on the
    first (non-cookie) exchange, with `channel`/`thread_ts` read from the token's
    signed `extra` payload. OBSERVER only; the anchor a challenge auth-window
    opens on. Default no-op. `safe_context_call(fallback=None)` re-raises
    `PlatformCompositionError` — no outer `except` wraps it (that would swallow
    the invariant).
  - `DashboardContributor.decorate_reply(text, *, channel, user_id) -> str` —
    outbound-reply transform, AFTER the redaction passes. Default identity. Wired
    on BOTH Slack reply paths: native `slack/handler_runtime/delivery.py::_deliver_answer` AND the
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
- `apps/routes_art.py` — `_fetch_git_blob`'s per-URL clone-sandbox-mode decision
  routes through `_context_clone_sandbox_mode` (same as the registry's clone
  sites in `apps/registry_pipeline/`), so a companion's extended trusted-host set
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
| `package_manager` (whole slot) | external-tool install hints (ffmpeg, faiss, the `voice-aws` extra) are inline brew/winget/pip text in `doctor_checks/features.py`, not a single plan-resolution point | `CapabilityManager` for registry-backed MCP/skill/agent installs |
| `feature_apps` (whole slot) | bundled apps are discovered via `AppsLoader` and registered by `apps/manager_builtins.py`; the tuple is a provenance record only | `AppsLoader.manifest_sources()` / `bundled_app_names()` |
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
