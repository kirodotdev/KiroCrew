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
- `_MATCHERS` — exactly **five**: `identifier` (case-insensitive), `command`
  (case-sensitive `fnmatchcase`), `path`, `host`, and `mcp` (a `@server` grant covers
  `@server/tool`). An earlier revision also listed `bundle_id` and `cu_action` "both
  added for computer use"; they were removed with that governance model and naming
  either in a `ScopedRuleset` raises `PlatformCompositionError: unknown matcher`,
  which under `boot.fail_closed` aborts governance boot — so this list is
  load-bearing, not descriptive. Extend it only through
  `register_matcher`/`register_scope`, which validate the name.
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

## Update pins (`updates`) — policy-only

Replacing the running code is the widest privileged action the host performs: a
self-update rewrites every other ceiling in this document, because the deny
catalog, the sensitive-path list and the evaluator are *code*. Two enterprise
pins ride in the policy file for it:

```json
"updates": {
  "source": "https://git.corp.example/platform/*",
  "min_version": "1.4.0"
}
```

- **`source`** — an fnmatch glob over the git remote URL new code may come from
  (a glob so one pin covers a mirror set, and so non-URL remote shapes —
  SCP-style, local path — are pinnable). Empty = unpinned. A checkout whose
  remote cannot be resolved is **denied when a pin exists**: an admin's pin must
  not be satisfied by "we could not tell".
- **`min_version`** — the minimum version the fleet may run. A host below it
  takes a **mandatory** update, overriding the user's `auto_update=false`
  (user config sits under the enterprise ceiling). It never refuses to *boot*:
  bricking a fleet on a policy typo would remove the surface an admin needs to
  fix it. An unparseable floor imposes none, for the same reason.

**Not an archetype, by design.** Every archetype answers "is X permitted?"; a
remote URL and a version number are *values the core consumes*. So they ride
outside `controls` — no `SCOPE_CATALOG` row, no matcher, no evaluator change.
What makes them enterprise-*pinnable* is the file they live in: the trust-root
`security_policy.json` is on the `security._SENSITIVE_HOME_DIRS` keystone, so the
agent can neither read nor write its own ceiling. A `config.json` field or an env
var would only be a suggestion.

**Policy-only — rejected in a Level-2 profile** (`parse_profile` raises). A
profile is narrow-only and there is no narrower version of *pointing somewhere
else*; a per-app profile that could redirect the update source would be
privilege escalation.

`platform/update_governance.py` is the one seam the three update paths share
(`POST /api/update`, `kirocrew update`, the gateway-boot auto-apply) so they
cannot drift. It resolves the remote git would *actually* fetch from — reading
`branch.<name>.remote` rather than assuming `origin`, via `ls-remote --get-url`
so `url.<base>.insteadOf` rewriting is applied — and returns a blocking reason or
`""`. **A pin blocks; an unresolvable pin does not:** if governance cannot be read
at all the update proceeds, because refusing one would strand a host on a build
that may need a patch. These are a routing constraint for a managed fleet, not a
boundary against a local operator who could edit the checkout directly.

**Roll the build before the pin.** The parser fails closed on an unknown key, so a
build predating `updates` refuses to boot on a pinned policy — which inverts the
`min_version` case, since the stale hosts a floor targets are exactly the ones
that would stop booting. Recovery is a manual `kirocrew update`.

## Policy authenticity (`identity.signature`)

Without a signature check, a policy's integrity rests entirely on **filesystem
permissions** — adequate for the single-user host that owns its own ceiling, but
not for a managed fleet where the operator is not the local user and the local
user can edit the file. `load_security_policy` therefore verifies an optional
detached `identity.signature`, mirroring `admission._signature_valid` rather than
inventing a second scheme.

| Piece | Where | Notes |
|---|---|---|
| Canonical payload | `policy_signing_payload()` | Routes through `admission.canonical_signing_bytes` — the **same** sorted-keys/compact-separators/UTF-8 canonicalization `PluginManifest.signing_payload` uses, so the two trust roots cannot drift |
| Primitive | `admission.hmac_signature` | HMAC-SHA256 + `hmac.compare_digest`. POC symmetric; an asymmetric verify swaps in behind the same helper |
| Trust key | admission policy `trust_keys[<issuer>]` | The **existing** operator-controlled key store — one store, not two |
| Opt-in | admission policy `require_policy_signature` | Separate from the plugin-facing `require_signature` |
| Verdict | `GovernanceCeiling.signature_state` | `verified` / `unverified` / `unsigned` / `unchecked` |

**Coverage** is the whole document minus `identity.signature` (a signature cannot
cover itself). `identity.issuer` **is** covered, so a validly-signed policy cannot
be re-labelled as issued by someone else. Signing the raw document rather than a
projection of the parsed ceiling is deliberate: it covers keys *this build does
not know* — a companion-registered scope, a future schema addition — so removing
or editing one is still detected, and it keeps the payload scope-name-agnostic
(adding a scope stays a `SCOPE_CATALOG` data change). Because coverage is
byte-canonical over the *parsed* JSON, re-indenting or reordering keys does not
break a signature while changing any value or key does.

**Why the trust key comes from the admission policy** and not from
`security_policy.json` itself: a document must not be the authority on whether it
has to be authentic. A `require_signature` flag inside the security policy would
be self-referential — an attacker rewriting the policy would simply clear it. The
admission policy is already this package's fleet-controlled trust root, already
carries `trust_keys`, and is already on the `is_sensitive_path` keystone, so the
governance trust root inherits every protection the plugin trust root has.
`_policy_trust_settings()` reads through **`admission.read_policy_trust_root()`**,
a deliberately side-effect-free reader — *not* `load_admission_policy`, which
records the dashboard admission posture and emits a **critical**
`governance_degraded` SEL on an absent policy. That is correct once per process at
boot and wrong here, because `gatewayd` re-loads the security policy **per app
call** (`mcp_gateway/app_call.py`), so reusing the audited loader would flip the
governance indicator to degraded and append a critical audit record on every app
call. It never raises, and on an absent/unreadable admission policy it yields no
keys and a `False` opt-in: an admission-policy problem is already handled loudly
and fail-closed in admission's own domain, and it must not additionally make the
security ceiling unloadable through a second path.

**Advisory by default, fail-closed on opt-in.** With `require_policy_signature`
unset (the default, and the seeded value), an unsigned or unverifiable policy
still loads and still governs — every existing standalone install and every
existing policy file keeps working unchanged, with no key to provision. This is
the compatibility contract: verification adds *reporting*, not a new way for a
working install to stop booting. With the flag set, a non-`verified` verdict
raises `PlatformCompositionError` and **aborts boot** (plus a `failed_closed`
governance-health mark), matching the module's existing fail-closed discipline for
a wrong version, a missing `boot` object, or an unknown governed key.

**All three tiers are verified — none is exempt.** When `require_policy_signature`
is OFF (the default, and what the `amazon` edition ships), verification is advisory
at every tier: an unsigned policy — bundled or on disk — still loads and still
governs, so existing installs are unchanged. When it is ON, every tier must present
a signature that verifies against a trust key, or boot aborts.

The companion-bundled tier is **not** exempt: the plugin-admission manifest
signature covers only the manifest fields (`name` / `publisher` / `version` /
`capabilities` — see `admission.PluginManifest.signing_payload`), **not** the bytes
of the packaged `security_policy.json`. So "covered by admission" never actually
protected the resource — a tampered bundled policy would have loaded unchecked. An
edition that opts into `require_policy_signature` therefore signs its bundled policy
like any other governed tier.

And a **missing** policy does not satisfy the requirement: with
`require_policy_signature` ON and no policy present at any tier, boot aborts rather
than returning an ungoverned host — otherwise a mandated-signature fleet that lost
or never shipped its policy file would silently run with no ceiling at all, the
exact failure the flag exists to prevent.

**Load computes the verdict; one gate enforces it.** `_verify_policy_signature`
records each tier's `signature_state` as it loads, and never raises.
`assert_policy_signature_satisfied` is the single enforcement point, called by boot
on the **final composed context** alongside the other governance floor gates. It
rejects both failure shapes: a surviving ceiling whose state is not `verified`, and
no ceiling at all.

The split is what makes tier precedence work. `load_security_policy` walks
env → companion bundle → operator home and runs more than once per boot with
different arguments — the core calls it with no `bundled_loader`, a companion
edition re-invokes it with one. A raise inside the loader fires on whichever tier
that particular pass happened to reach, so an enterprise host with an unsigned home
file and a correctly signed companion bundle aborted on the *lower-precedence* tier
the core's pass fell through to, even though the bundle is what the final ceiling
comes from. Only the composed result knows which tier won, so only the composed
result can be judged.

`gatewayd`'s per-app-call reload calls the gate too, on the ceiling that reload
produced, so a policy tampered with *after* boot cannot widen an app callback — boot
verified the original bytes, which says nothing about what the reload just read.

**Residual gap — absence is not decidable in `gatewayd`.** That gate is applied only
when the reload returns a ceiling. The daemon is not the composition process: it
never runs `boot_platform`, so it loads with no `bundled_loader` and cannot see a
companion-bundled ceiling. `None` there is the *normal* result on a bundle-only
enterprise host, not evidence the policy is gone, so refusing on it would deny every
app callback. Deletion is therefore caught at boot but not mid-session; closing that
means handing `gatewayd` the composed ceiling instead of re-reading the file, which
is pre-existing behavior and a separate change.

**A broken trust root reads as no opt-in, on purpose.** An admission file that is
absent, unreadable, or not a JSON object leaves verification advisory rather than
failing closed. That is not a gap: an attacker who can write `admission_policy.json`
is outside this threat model (see below) and would simply set the flag to `false`,
which parses fine — so fail-closing on a *malformed* file would catch only a clumsy
variant of an attack the design already concedes, while turning a non-atomic fleet
push or a hand-edit typo into an unbootable host. Corruption there is a reliability
event: it is logged at WARNING, plugin admission independently fails closed on the
same file, and `kirocrew doctor` reports it.


**Threat model.** This detects **offline / at-rest tampering and substitution** of
a policy file by anyone without the issuer's key: a widened ceiling, a stripped
scope, a swapped file, a policy re-labelled to a different issuer. It does **not**
defend against an attacker who holds the trust key, and it is **not** a
confinement boundary for a local process running as the operator — such a process
can edit the admission policy (clearing the opt-in) as easily as the security
policy. The `is_sensitive_path` keystone remains the control that stops the
*agent* from reaching either file; signing is what makes a fleet-pushed ceiling
tamper-**evident** to the host that loads it. Symmetric HMAC also means the
verifier holds a secret capable of *producing* signatures, so key distribution is
the residual weakness an asymmetric successor removes.

`kirocrew policy show` prints the verdict verbatim
(`GovernanceCeiling.signature_summary()`) so an operator can tell an established
issuer from a decorative one — it previously printed a bare `issuer` that no check
had ever established.

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

**`~/.kiro/agents/*.json` and `~/.kiro/settings/mcp.json` are NOT on the floor
today** — an honest gap worth stating here because it bounds what the ceiling can
claim. Verified on the current tree: `is_sensitive_path("~/.kiro/agents/kirocrew.json")`
is `False` and `echo x > ~/.kiro/agents/kirocrew.json` is not blocked. Since
`hooks.on_tool_call` runs **only** from the `EVENT_PERMISSION_REQUEST` branch (the
`EVENT_TOOL_CALL` branch is documented informational-only — "the tool is already
running (auto-approved by kiro-cli). Hook results cannot block execution"), an
agent that writes itself into `allowedTools` makes kiro-cli stop sending
permission requests and **Plane A never runs at all** for that tool. This affects
every governed capability, not just computer use. Hardening those paths is
deliberately deferred to its own PR (it changes a path floor every legitimate
writer — `agent.py`, `handlers/mcp.py`, `handlers/agents.py`, `apps/backend.py` —
touches, and deserves review on its own merits). Until it lands, a capability
whose only enforcement is Plane A can be skipped this way. Computer use no longer
relies on either plane — it is not governed at all, and rests on the keystone enable
being agent-unwritable. See [computer-use.md](computer-use.md) → "Known limitations".

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

**`identity_proven` is true for ANY non-empty session key**, so an unattended
surface that *does* carry a key — `cron:<job>`, `subagent:<id>`, `taskrunner` —
resolves to `None` (policy-ceiling-only), **not** `deny_all_profile`; only `_bg`
and `_hb` fall to deny-all. That is correct for every scope that remains.

An earlier revision continued: "…and wrong for computer use", and described a
feature-local unattended refusal in `computer_use.gate` plus shipped `cu-off`
profiles bound to the unattended surfaces. **Neither exists.** Computer use is
deliberately ungoverned — no `computer_use*` row in `SCOPE_CATALOG`, no
unattended-surface rule, and no shipped profile of that name — so cron, subagent,
taskrunner, webhook, workflow and channel sessions all drive the desktop once the
operator has flipped the keystone. That is the product decision recorded in
[computer-use.md](computer-use.md); the containment is the keystone the agent cannot
write plus the SEL audit trail, not a surface ceiling. Do not re-document the refusal
without re-implementing it.

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
  chokepoints use (e.g. `capabilities.publish` in `publish_governance.py`,
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
profile falls back to deny-all (Validation rule 5), **not** the ceiling — unless
the policy declares a top-level `fallback` profile (see *Configurable fallback*
below), which is substituted instead. `extends` is monotonic narrowing
(`compose_profiles`).

**Configurable fallback (policy-only).** By default the substitute for an unusable
profile is the most-restrictive deny-all. A policy MAY declare a top-level
`fallback` object — parsed as a narrow-only profile (same scope validation; an
unknown scope in it fails closed at boot) — which the loader substitutes at all
three unusable-file sites instead of deny-all. Intersected with the ceiling like
any profile, it can only narrow it: it lets an operator keep the basic operational
planes available (subagent/cron/heartbeat/taskrunner) while still denying the
sensitive ones (for example `channels`/`apps`) when a profile file cannot be
loaded. Absent the key the fallback stays deny-all (the default is unchanged). A
profile may NOT declare `fallback` (policy-only, rejected at parse). The chosen
fallback is resolved against the composed ceiling, and the profile-store freshness
key folds in whether a fallback is declared, so a store first-touched before the
ceiling composed reloads once it does rather than baking deny-all permanently.

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

> **MCP App-originated tool calls.** The MCP Apps callback path
> (`mcp_gateway/app_call.py::handle_app_call`, reached via
> `POST /api/mcp-apps/call`) evaluates the governance ceiling ∩ active
> profile for the canonical `@server/tool` reference (`mcp` scope) before
> forwarding — the same decision Plane A applies to model-originated MCP
> calls, so an enterprise deny binds both invocation authorities. Its
> polarity differs deliberately: evaluation errors DENY (fail-closed),
> because the app path does not traverse the always-on deny floor that
> backstops Plane A's soft fail-open. The spool capability tokens
> themselves sit on the sensitive-path floor (`mcp-apps` in
> `security._SENSITIVE_HOME_DIRS`) so the agent cannot harvest them.
> Remaining Plane A parity refinements are tracked in
> [issue #418](https://github.com/kirodotdev/KiroCrew/issues/418) — see
> [mcp-apps.md](mcp-apps.md).

- **Plane A — the host gate** (`HookManager.on_tool_call`, the primary
  chokepoint). The deny-floor is now the *effective* denied-command rule set —
  the enabled subset of `BUILTIN_DENIED_RULES` ∪ the user's `user_added`
  patterns from the keystone `denied_commands.json` opt-out state, resolved by
  `HookManager._effective_denied(ctx)` and passed to `PolicyAuthority.is_denied`
  as `denied_regexes` (see `security.md`). Gate order: **sensitive-path
  keystone → effective deny-floor (`is_denied`) → `gate_decision(ceiling,
  profile, title)` (governance, incl. the `commands` scope, and MCP titles
  `mcp__server__tool` converted to `@server/tool`) → first-party app-own MCP
  server auto-approve → read-only auto-approve →
  user `auto_approve_tools` loop**. A governance deny wins over a user
  auto-approve, and the read-only auto-approve fast-path runs strictly AFTER
  both the deny-floor and `gate_decision`, so a read-only classification can
  never re-admit a denied/governed call. **First-party app-own MCP server
  auto-approve** (`_app_owns_mcp_server` ∧ `_is_first_party_app`) sits
  immediately after `gate_decision`, so a ceiling/profile still denies it: a
  **builtin** app agent calling its OWN app-scoped server (registered
  `<app>:<server>`) is intra-app — the app talking to its own gateway-shipped
  code, not a host surface — and is auto-approved without re-widening any host
  grant, independent of the Normal/Read/Trust tier (that tier governs the HOST
  tools an app may reach, not the app talking to itself). It keys on the trusted,
  non-model-authored `mcp_server_name` (the ACP `_meta.kiro.mcpServerName`), NEVER
  the LLM-authored title: kiro-cli sets that field only for a genuine MCP-served
  call, so a prompt-injected agent that titles a Bash call `mcp__<app>:srv__x`
  carries an empty server name and never matches (fail-closed). Restricted to
  builtins on purpose: only a builtin's server is provably first-party. A
  THIRD-PARTY app's own server is arbitrary installed code whose internals the
  gate cannot see, so its own-server calls are NOT auto-approved here — the OS
  sandbox it runs under and the third-party admission gate bound its behavior,
  not this prompt. **Inside**
  the read-only fast-path the semantic
  `tool_kind` is authoritative and is tested first, as an ALLOW-list: only
  `read`/`fetch` auto-approve, and every other non-empty kind falls through to
  interactive approval before any title-keyed branch (including the computer-use one)
  is consulted — the title is the agent-authored `description`, and `tool_kind`
  itself is a verbatim ACP string, so a denylist of mutating kinds cannot be
  complete. See `security.md`, "Read-only auto-approve". The governance `commands` deny is
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
  cron *capability* on/off gate in `mcp_cron._vet_cron_capability_governance`.
  Both run at `cron_add` (authoring) AND again at fire time — for EVERY job
  kind — via the shared `mcp_cron.vet_job_at_fire_time(job)` entry point
  called from `slack.gateway._cron_callback` immediately before execution:
  `command` jobs re-run the capability gate + the `commands` ceiling, `script`
  jobs re-run the capability gate + the script-body scan
  (`mcp_cron._vet_script_file`) on the freshly re-resolved path (so a script
  file edited on disk after authoring is re-checked too), and `message` (LLM)
  jobs re-run the capability gate before the session dispatch. A policy
  tightened after a job was scheduled therefore denies that job's next run
  instead of only affecting jobs authored after the change. Denial at
  fire time marks the run `last_status="error"`, emits a SEL
  `outcome="denied"` event keyed `cron:<job.id>`, and does not delete or pause
  a RECURRING job — deliberately including the consecutive-failure auto-pause
  counter, which a policy denial must not feed (five denied fires would
  otherwise auto-pause the job permanently) — so a later policy loosening
  lets it resume on its own at its next slot. A denied one-shot `at` job is
  parked DISABLED instead (never deleted, even with `delete_after_run`): its
  due time has passed, so leaving it enabled would refire it on every timer
  tick; the operator re-enables it after loosening; the sandbox
  ordinal floor is clamped in `sandbox.wrap_argv`;
  spawn in `subagent._vet_spawn_governance`; outbound messaging in
  `mcp_core._vet_messaging_governance` plus the per-transport `channels` check
  in `mcp_core._vet_channel_governance`; dashboard cross-surface mirror creation
  in `dashboard.chat_mirror` reuses the fail-closed
  `dashboard.chat_runner._resolve_channel_target` ladder before opaque target
  resolution and at every outbound send boundary; the per-transport **startup** gate in
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
  app activation in `apps.manager._app_activation_denied` (at `enable_app`).

Plane A carries **no live ordinal clamp**. It used to: a computer-use title under a
`computer_use.approval: interactive` floor had both auto-approve branches suppressed,
so the call fell through to interactive approval. That row and its clamp were removed
along with the rest of the computer-use governance model — see [Computer use is NOT
governed](#computer-use-is-not-governed-deliberately). The global `approval_mode`
row's live clamp remains reserved (see "Still-reserved in v1").

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

### `vet_and_audit` — the audited-decision seam for governed outbound messaging

`governance_profiles.vet_and_audit(scope, item, *, session_key, tool_name,
app="", fail_closed=False, log_warning=True)` evaluates ONE permission
decision via `governance_permits` AND writes its SEL
`log_governance_decision` record — **grant and denial alike** — from a
single code path, then returns the Decision. Any chokepoint whose outcome
must land in the audit trail with a consistent shape calls this seam
instead of pairing `governance_permits` with hand-rolled SEL writes.
Current caller: `mcp_core._vet_messaging_governance` (governed outbound
messaging, shared by `send_message` and `send_notification`, single
`capabilities.messaging` check). Contract details: `fail_closed` passes
through to `governance_permits` unchanged (a degraded evaluation returns a
denying Decision instead of raising); exceptions from evaluation propagate
to the caller so each site keeps its documented degrade posture; SEL write
failures never raise (best-effort audit must not block or unblock the
send). **A new governed caller MUST use this seam** — hand-rolling
`governance_permits` + SEL at a new outbound-messaging chokepoint (e.g. a
future notification delivery-routing fanout) reissues the
record-shape/fail-closed drift this seam exists to prevent.

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
`_compose_controls`, and reports
`{scope, archetype, governed, source, scope_note, detail}` per scope plus
`{version, has_policy, profile, surface, other_bound_surfaces, fallback_profiles,
unavailable}`.

**A row describes ONE surface, and must say which.** The host profile governs
in-process host actions, so it legitimately pins capabilities the host process
never performs — `cron`, `messaging`, `spawn` — OFF, while the surfaces that do
perform them enable them under their own profiles. Rendering such a row as an
unqualified "disabled by policy" therefore reports a *working* feature as
switched off. Two fields keep that honest:

- **`scope_note`** — `host_profile` when the host-surface profile contributes to
  the row (`source` of `profile` or `policy+profile`), so the value is that one
  surface's posture; `policy_wide` when POLICY alone governs, which does apply to
  every surface; `""` when ungoverned. A string enum, not a rendered sentence, so
  the frontend maps it to a translated string and no English ships in a JSON body.
- **`other_bound_surfaces`** — surface ids other than `host` that carry their own
  bound profile (from `governance_profiles.bound_surfaces()`). **Names only**, no
  control, count, or rule from those profiles, so the POSTURE-only boundary below
  is unchanged. This answers the question a host-scoped row provokes: *is cron
  really off, or is that just the host's ceiling?*
- **`fallback_profiles`** — file stems of every profile currently replaced by the
  fallback built-in — deny-all by default, or the policy's declared `fallback`
  profile (from `governance_profiles.fallback_profile_names()`), sorted.
  **Names only**, same exposure contract as `other_bound_surfaces`: a stem is not
  rule content, so the POSTURE-only boundary is unchanged. `_reload` substitutes
  that built-in at three sites — a present-but-unreadable file, a file whose JSON
  or schema does not parse, and a file that parses but `extends` a parent that is
  missing or chained — and enforcement is correct in all three. What is NOT correct
  without this field is the *display*: the substitute reports `source: "profile"`,
  so an unusable file renders identically to a deliberate operator lockdown, and an
  operator has to read server logs to tell them apart.

  Reported as the whole SET rather than a flag for the host-resolved profile, for
  two reasons. A bound non-host profile deny-alls its own surface just as silently
  while appearing in `other_bound_surfaces` looking healthy. And a flag derived by
  matching a resolved profile's declared `name` against file stems mislabels a
  profile whose name collides with a broken sibling's stem, whereas these ARE the
  stems.

  One documented gap: a first-ever load of an unreadable file has no salvageable
  bind, so it is an UNBOUND deny-all. Its stem appears here, but no surface
  resolves to it — that surface falls back to the ceiling rather than being denied.

**Posture, not contents (security boundary).** The serialized `detail` carries
only POSTURE — set `mode`, entry COUNTS (`allow_count`/`deny_count`),
capability `enabled` + inner scope names, ordinal `floor` — and NEVER the rule
CONTENTS (the allow/deny globs, command patterns). This is deliberate: the
dashboard is reachable by the agent's own browser tooling (`playwright-cli attach
--extension` drives the user's authenticated Chrome), and `security_policy.json` /
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
  [`docs/guides/assets/security-policy.example.json`](../../guides/assets/security-policy.example.json)
  shows both scopes set together, but read it as **egress defense-in-depth,
  not a bounded egress guarantee**: a `commands` deny list is a finite set of
  known patterns, not an allow-shaped ceiling, so it cannot enumerate every
  network-capable tool (`python`, `ssh`, `git`, `pip`, `openssl s_client`, a
  `curl` invocation with no `://` in it, or a piped/absolute-path
  invocation of any of the above), and it says nothing about the web terminal
  PTY, which is an ungoverned plane by design (see below). A deployment that
  needs an actual bound on where the host can reach should treat the example
  as a starting point for defense-in-depth, not as sufficient on its own.
  Separately: once a `commands` deny pattern is adopted into policy, it
  becomes a force-pin via `resolve_pinned_commands` (ceiling pins ∪ profile
  pins, union not override) — a user cannot locally opt out of a pinned rule
  the way they can an unpinned one, so an operator copying the example should
  expect its deny rows to be effectively permanent for anyone bound by that
  policy, not something end users can narrow per-rule.
- **Per-app profile binding via MCP chokepoints is best-effort.** The managed
  `kirocrew-core` MCP server is spawned by kiro-cli, not by an app backend, so
  `KIROCREW_APP_NAME` is absent there — `learn_add`/`send_message` resolve the
  per-SURFACE profile + policy ceiling (the enforced path), not a per-app
  profile. An app's own in-process tool calls (which carry `KIROCREW_APP_NAME`)
  do bind a per-app profile. App blast-radius is contained today by the `apps`
  activation allowlist + per-surface profiles.
- **Shell GUI automation is a `commands` item, never re-parsed.** `osascript`,
  `cliclick`, `xdotool`, `ydotool`, `wtype`, `screencapture`, `scrot`, `grim`,
  `import -window` and `nircmd` inside a Bash tool are governed by the
  **`commands`** scope on the command body — no `computer_use.*` scope applies to
  them, because a shell command is never decomposed into its GUI sub-effects. A
  fleet banning computer use must also deny those `commands` patterns (see the
  copy-pasteable fleet-ban policy below); a deny-mode `commands` pattern also
  becomes an un-opt-out-able force-pin via `resolve_pinned_commands`.
- **The web terminal PTY is an ungoverned plane today.**
  `dashboard/handlers/terminal.py` spawns a real PTY and contains **no**
  `is_denied` / `is_sensitive_bash_command` / governance call, so
  `screencapture` typed into it is bounded by neither the `commands` scope nor
  any `computer_use.*` scope. It is an operator-only surface. Routing PTY input
  through the same effective-deny floor as `on_tool_call` is tracked as its own
  follow-up; do not describe computer-use governance as covering it.
- **Raster capture has two channels and neither is governed.** Computer use has no
  `observations` scope any more, and `playwright-cli screenshot` is a shell command
  rather than a tool call, so an `mcp` deny cannot reach it at all. A fleet that
  means "no raster capture" must deny `@kirocrew-computer` via the `mcp` scope
  **and** the browser CLI via the `commands` scope.
- **The `mcp`-scope deny is now the ONLY governance lever over computer use, and it
  is keyed on a renameable alias.** `mcp.deny: ["@kirocrew-computer"]` works on
  unmodified shipped code, but the server key is derived by `mcp_server_alias()` from
  an agent-mutable config: verified `mcp__kirocrew-computer2__click` and
  `mcp__cu__click` both PERMIT under that deny. With the `capabilities.computer_use`
  row removed there is no authoritative ban behind it — a fleet that must guarantee
  the feature is off should not ship the keystone enable, and should treat the alias
  deny as best-effort. See [Computer use is NOT
  governed](#computer-use-is-not-governed-deliberately).
- **Cursor Motion has no governance row, and deliberately gets none.** The
  fake-cursor desktop overlay (`computer_use/overlay*.py`) grants the agent
  *nothing*: it draws an image, it does not move the pointer, it cannot deliver
  input, and it is invisible to `screencapture` so it cannot even alter what the
  model reads. It is a `config.json` display preference
  (`computer_use.cursor_motion`, default OFF), and adding a scope for it would
  imply an authorization decision where there is no capability to authorize.
  The real pointer path (`click_method: "global"`, which warps the operator's
  physical cursor) has no row either — it is reachable whenever the feature is on,
  and is audited under its own SEL `tool_kind` rather than gated.
- **`kirocrew computer call` is subject to the same checks as an agent call.** The
  CLI harness routes through the same `computer_use.tools.dispatch_tool` chokepoint,
  so the keystone enable and the target policy apply to it, bound to the attended
  `cli` surface (session key `cli_chat`). There is nothing governance-side left for a
  policy author to bind to it.
- **`approval_mode`** — the ordinal is parsed and **boot-floor-checked** (a
  profile looser than the policy mark aborts boot, like `sandbox.min_level`), but
  no approval chokepoint clamps the *live* approval pipeline through it yet: the
  live approval vocabulary (`""`/`auto` in cron; the dashboard trust toggles) is
  not yet reconciled onto the `yolo < auto < interactive` scale. The boot floor
  is the enforced half; the live clamp is the reserved half. Wiring it is the one
  genuinely-architectural follow-up (a single approval-policy resolution point
  fed by `governance_floor_ordinal("approval_mode")`).

  There is no longer a second, live-clamped `approval` row to contrast this with:
  `computer_use.approval` was removed with the rest of the computer-use governance
  model, so `approval_mode` is once again the only row on the `approval` scale and
  its live clamp is still the reserved half.

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
`capabilities.memory_writes`, `capabilities.script_hooks`,
`capabilities.browse` (the native `browser` MCP tool's dispatch chokepoint —
default on; a deny makes the tool refuse outright, and it does NOT fall back to
`playwright-cli`. The `playwright-cli` fallback path itself remains governed by
the `commands` scope, so denying browsing wholesale means denying both this
capability AND the `playwright-cli` command),
`capabilities.publish` (artifact publish chokepoint — see below),
`capabilities.theme_persona` / `capabilities.theme_install`, and
`capabilities.telemetry` (the anonymous beacon: send gate + both write
chokepoints — **policy layer only**, see below). Only the live `approval_mode`
clamp remains reserved.

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
out-of-band chokepoint — `publish_governance.publish_denied_reason` — NOT at the
host PreToolUse gate: publishing is a user-driven dashboard HTTP action ("NOT LLM
tools"), so the title-gate never sees it. The chokepoint calls
`governance_permits("capabilities.publish", "destinations:<provider_id>", …)`
BEFORE dispatching to the provider, and additionally honours the standalone
operator's `publish.allowed_destinations` config allowlist (default-open,
narrow-only — config can never widen past the ceiling, mirroring the Slack
enterprise allowlist). This scope is distinct from the `git push` deny FLOOR and
from `network.egress`: `capabilities.publish.enabled: true` never re-enables git
publish (the floor is ADD-only and unconditional) nor a fetch host. WHO
implements a destination is the orthogonal CPP `PublishRegistry` seam; governance
decides only WHETHER + to WHERE, and runs first.

Callers (one decision, several surfaces — the helper lives in its own module so a
second surface cannot grow a drifting copy):

| Surface | Destination id | On deny |
|---|---|---|
| `api_artifact_publish` + its sharing/review siblings (`handlers/artifacts.py`, via the module-local `_publish_governance_denied` alias) | the requested/effective `publication.provider` | 403 |
| `GET /api/publish-providers` (`apps/routes.py`) | `deploy-web-aws` | the row is omitted, so the button never renders |
| `POST /api/deploy/deploy` (`deploy/handlers.py`) | `deploy-web-aws` | 403, audited `deploy/denied` |
| `POST /api/deploy/pending/{id}/confirm` | `deploy-web-aws` | 403 BEFORE `claim_pending`, so a denied confirm does not consume the entry |

The deploy-path callers are what make the public-web destination genuinely
closable: hiding the provider row alone would be presentation, not a control
(the endpoint is reachable directly, and the `deploy_artifact` MCP preview goes
through `/api/deploy/deploy` too), and gating only the initial deploy would leave
a pending entry created before the ceiling changed still confirmable. Because the
deploy path shares `publish.allowed_destinations`, an operator who had already
narrowed that list for the registry must add `deploy-web-aws` to keep deploying —
intentional: the list states which destinations are permitted, and the core
deploy provider was previously the one destination exempt from it.

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
  `~/.kiro/crew/themes/`) plus a per-content sha grant — an actor with that
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

### Anonymous telemetry — `capabilities.telemetry`

The anonymous daily heartbeat and official-app install receipt (`beacon.py` and
`apps/install_receipt.py`; full spec in [metrics.md](metrics.md) → "Anonymous
outbound telemetry"), together with the in-app session-pulse survey
(`dashboard/handlers/feedback.py`), are the repo's **only default-on egress
family**. All three gate on the same `beacon.telemetry_permitted` effective-enable
ladder. The heartbeat and install receipt send fixed anonymous payloads; the
survey egresses the user's own submitted answers plus an anonymous per-install
id (`beacon.install_id`), and only once that same ladder — including the
first-run privacy disclosure — permits it. They are governed
by the `capabilities.telemetry` `SCOPE_CATALOG` capability row
(`capability_default=True`, data-only shape — no `CONTRACT_VERSION` or evaluator
change, mirroring the theme rows above).

**Why a governance row when a Settings toggle already exists.** The toggle, the CLI
and the `KIROCREW_TELEMETRY_DISABLED` env var are all *operator* controls: anyone on
the machine can flip them, and the agent can reach the first two. A managed fleet
frequently may not egress to a vendor endpoint at all, which needs a control the
running app cannot undo. Because the row is read from the trust-root
`security_policy.json` — inside `security._SENSITIVE_HOME_DIRS`, so the agent can
neither read nor rewrite its own ceiling — this is genuinely un-opt-out-able where a
`config.json` field would only be a suggestion.

Consulted at **four** chokepoints — the send gate plus EVERY write path to
`telemetry.beacon_enabled`; any one alone would be a half-control:

| Chokepoint | Pinned-off behavior |
|---|---|
| `beacon.telemetry_permitted()` | Refuses both heartbeat and receipt egress. Ranked **above** the config flag so the reported reason names the policy, not the (now irrelevant) local value |
| `PATCH /api/config/kirocrew` (`handlers/core.py`) | **403** on `telemetry.beacon_enabled=true` |
| `kirocrew telemetry enable` (`cli_commands.py`) | Exits **1** without writing config.json |
| `kirocrew config set [--local] …` (`cli_config.py`) | Exits **1** without writing. The *generic* setter reaches the same key, and `--local` writes the overlay that takes PRECEDENCE over the base file |

Writing `false` is **always** permitted at both write chokepoints. The ceiling is a
floor on privacy, so a narrower local choice composes with it (tightest-wins), and
refusing it would leave a user unable to record a stricter preference they already
have in effect — and strand them if the policy were later lifted.

The write refusals exist so a pinned host cannot sit storing `beacon_enabled: true`
behind a control that does nothing: `should_send` already blocks the egress, so
without them the config file and the UI would both claim "on" while nothing is sent.

**Fails CLOSED** (`fail_closed=True`), joining `capabilities.theme_install` /
`capabilities.publish` rather than diverging from them. An earlier revision of this
row failed open on the reasoning that "a wrong deny only loses a heartbeat"; that
reasoning describes the wrong-DENY and quietly ignores the wrong-PERMIT, which is
an **egress on a fleet that explicitly forbade egress** — the one thing this scope
exists to prevent, on a payload that leaves the machine. `fail_closed` also
promotes the degrade to a critical SEL event, so an unevaluable ceiling is visible
rather than silently permissive.

**Audited at the enforcement call, not on the probe.** `should_send` (the decision
that actually stops an egress) routes through `vet_and_audit` — the existing
audited seam — so a suppressed heartbeat lands a `governance_decision` SEL record
with the same shape as the messaging chokepoints, grant or deny. The **read-only**
path (`status()` → `GET /api/telemetry/beacon`, which the Privacy panel refetches)
passes `audit=False`: auditing an *inspection* would append HMAC-chained rows at a
multiple of the one decision per boot that governs anything. This is the same
disposition the channels gate applies to its hot-path default-permit — audit the
decision that does something, not the question.

The probe is `beacon.is_governance_pinned_off()`, surfaced as
`governance_override` on `GET /api/telemetry/beacon`; the Privacy panel shows it as
the strongest of three pinned-notes (it outranks the env-var and overlay notes,
which would otherwise suggest remedies the ceiling makes pointless).

**POLICY LAYER ONLY — this row is Level-1 in a way the others are not.** The probe
requires `layer == "policy"`, so a **Level-2 profile** setting
`capabilities.telemetry.enabled: false` does **not** suppress the beacon, even
though the read-only viewer will render that row as governed with a `profile`
source. Two reasons, and the narrowing is what makes the control trustworthy
rather than weaker:

- The probe is **process-wide and carries no session**. It runs from the beacon's
  detached boot thread, so `_infer_surface("")` classifies to `unknown` and matches
  no bind — a per-surface ceiling is simply not the question "should this
  installation send a daily heartbeat" asks.
- A bare not-permitted test is **wrong in a way no `except` can catch**:
  `resolve_active_scope` returns a synthetic deny-all *profile*
  (`_deny_all_unloaded:…`) when the profile store is unprimed and another thread
  holds its non-blocking reload lock. That is a transient race on a host with **no
  policy at all**, and it arrives as an ordinary `Decision`, not an exception — so
  reading it as a pin would make the CLI, the 403, and the UI note all blame an
  administrator who does not exist. `TestGovernancePin` pins both directions.

So the probe reads **three** outcomes, not two: a policy-layer deny is a pin, a
degrade (`reason` prefixed `GOVERNANCE_ERROR_REASON`) is a pin (fail-closed), and a
profile-layer deny is not.

This mirrors the policy-only treatment `ScopedMap.posture` and the Slack posture
check already get. A profile-layer telemetry suppression would need its own
session-bearing chokepoint, not this probe.

The Security panel picks the row up automatically — `api_governance_policy` iterates
`SCOPE_CATALOG` — and labels it **"Anonymous telemetry"** rather than the leaf's
bare "Telemetry", because this scope governs only the outbound heartbeat and NOT the
unrelated local-only `telemetry.enabled` OTEL collection.

### Tailnet origin derivation — `capabilities.tailnet_origin`

`dashboard.tailscale.enabled` lets the gateway ask the local Tailscale daemon for
this machine's MagicDNS name at startup and add `https://<name>` to the CSRF origin
allowlist and the DNS-rebinding `Host` barrier, so `tailscale serve` reaches the
dashboard without a hand-written `dashboard.url` (`dashboard/tailnet.py`; RFC
`request-for-change/rfc-tailnet-dashboard-access.md`). Governed by the
`capabilities.tailnet_origin` `SCOPE_CATALOG` capability row
(`capability_default=True`, data-only shape — no `CONTRACT_VERSION` or evaluator
change, mirroring the telemetry and theme rows above).

**Why a governance row.** The config switch is an *operator* control that the agent
can reach through the generic config setter. What a managed fleet objects to is not
a preference but two effects it may forbid outright: **running the tailnet CLI on a
managed host**, and **widening the set of origins the gateway accepts
authenticated, state-changing requests from**. Read from the trust-root
`security_policy.json` (inside `security._SENSITIVE_HOME_DIRS`, so the agent can
neither read nor rewrite its own ceiling), the row is a control the running app
cannot undo.

Consulted at **four** chokepoints — the derivation, the publish action, and every
write path to `dashboard.tailscale.enabled`; any one alone would be a
half-control:

| Chokepoint | Pinned-off behavior |
|---|---|
| `tailnet.resolve_tailnet_host()` | Contributes no origin **and does not spawn the CLI**, so the pin closes both halves an administrator objects to. Checked ahead of the daemon call |
| `tailnet_serve.publish()` | Refuses to run `tailscale serve`, so the dashboard is never put on the tailnet in the first place. Checked before the spawn — refusing after publishing would be theatre |
| `PATCH /api/config/kirocrew` (`handlers/core.py`) | **403** on `dashboard.tailscale.enabled=true` |
| `kirocrew config set [--local] …` (`cli_config.py`) | Exits **1** without writing. The generic setter reaches the same key, and `--local` writes the overlay that takes PRECEDENCE over the base file |

`tailnet_serve.unpublish()` is deliberately **not** gated, and the asymmetry is
load-bearing rather than an oversight. `is_governance_pinned_off` returns true both
for a real policy deny and for a ceiling it could not evaluate, so gating withdrawal
would mean a transient policy-read failure leaves a dashboard published on a tailnet
with no supported way to take it down — a fail-closed control failing open in
effect. Removing exposure is always permitted, the same direction that lets a config
write of `false` through while `true` is refused.

Writing `false` is **always** permitted, for the reason the telemetry row gives:
the ceiling is a floor, so a narrower local choice composes with it and refusing it
would strand a user who wants to record a stricter preference already in effect.

The write refusals exist so a pinned host cannot sit storing `enabled: true` behind
a switch that does nothing — the derivation is already suppressed, so without them
the config file and the Security panel card would both claim "on" while no origin is
trusted and `tailscale serve` still fails the Origin check.

**Fails CLOSED** (`fail_closed=True`), joining `capabilities.telemetry` /
`theme_install` / `publish`. The two dispositions are not symmetric: a wrong-DENY
costs a convenience and leaves the explicit-`dashboard.url` path exactly as it is
today, while a wrong-PERMIT **widens a security boundary on a fleet that forbade
it**. `fail_closed` also promotes the degrade to a critical SEL event, so an
unevaluable ceiling is visible rather than silently permissive.

**Audited at the enforcement call, not on the probe** — the same disposition
telemetry documents, for the same reason. `resolve_tailnet_host` and both write
chokepoints pass an `audit_tool`, so a suppressed derivation and a refused write
each leave a `governance_decision` SEL record. `GET /api/tailnet/status`, which the
Security panel's card refetches, passes none: auditing an *inspection* would append
HMAC-chained rows for a question rather than a decision.

**POLICY LAYER ONLY**, and the probe reads **three** outcomes rather than two, both
exactly as the telemetry row above spells out: a policy-layer deny is a pin, a
degrade (`reason` prefixed `GOVERNANCE_ERROR_REASON`) is a pin, and a profile-layer
deny is **not** — because `resolve_active_scope` returns a synthetic deny-all
profile during an unprimed-store race, and reading that as a pin would make the
startup warning, the 403 and the CLI refusal all blame an administrator who does not
exist. The probe also runs once at gateway startup carrying no session, so a
per-surface Level-2 ceiling is not the question it asks.

The Security panel picks the row up automatically (`api_governance_policy` iterates
`SCOPE_CATALOG`), and the tailnet card additionally renders `governance_pinned` as a
distinct `pinned` state — the card must separate "off because the operator left the
switch off" (flippable) from "off because an administrator pinned it" (a config
write returns 403), since offering a working-looking toggle for the second is the
half-control this row exists to avoid.

### Computer use is NOT governed (deliberately)

Computer use (see [computer-use.md](computer-use.md)) has **no scope rows in
`SCOPE_CATALOG`** and no governance decision anywhere in its dispatch path. That is
a product decision, not an oversight, and it is a reversal: an earlier revision
shipped eight rows here (`capabilities.computer_use`, `computer_use.actions`,
`.apps`, `.app_names`, `.observations`, `.targets`, `.approval`, and
`capabilities.computer_use_pointer`) plus two custom matchers (`bundle_id`,
`cu_action`). All of it was removed — neither matcher is registered, and naming
either one now aborts governance boot (see the `_MATCHERS` note above).

**What replaced it.** One operator opt-in on the keystone `computer_use.json`,
which `security._SENSITIVE_HOME_DIRS` fences the agent away from. The agent cannot
read or write that file, so it cannot enable its own desktop automation — and it
cannot drive KiroCrew's own window either (`computer_use/policy.py`), so it cannot
click the toggle in the UI. Those two facts are the entire boundary.

**What this costs, stated plainly.** There is no way to express "computer use is
allowed but only for Preview", "read-only desktop access", "never type into a
password field" (beyond the always-on floor), or "every action must be approved" as
policy. A fleet that needs any of those should not enable the feature. The
`mcp` scope still works as a blunt instrument: denying `@kirocrew-computer` removes
the tools entirely, which is the one governance lever that remains.

**If it is ever re-governed**, the rows belong back in this file's `SCOPE_CATALOG`
inline (never `register_scope()`d from the feature package): `load_security_policy()`
runs at boot before any feature import, and a policy naming an unregistered scope
raises "unknown governed key … (fail-closed)" — so a lazy registration would abort
boot on every governed host the day a fleet adds the row.

Two things computer use still shares with this module, neither of them a decision:

* `_CU_ACTION_CLASSES` — the code-owned `observe` / `mutate` / `pointer` /
  `keyboard` / `text_entry` / `control` labels. `hooks` reads them for the
  read-only auto-approve — the one live consumer. `gate.is_mutating_action` reads
  them too so "which verbs synthesize input" has one definition, but it currently has
  no caller in the package: it is retained as the accessor an edition would use
  rather than re-deriving the classes, not as a control on the dispatch path;
* `CU_MCP_SERVER` / `is_computer_use_title` — the server key and title prefix, used
  by `classify_tool_title` to route a computer-use title to the ordinary `mcp` pair.

## Audit

`sel.log_governance_decision` records a `governance_decision` event
(`outcome ∈ {allowed, denied}` — the existing permit vocabulary). On-disk SEL is
not redacted by the writer and the HMAC chain signs the bytes as written, so the
operation / item / reason are redacted via `redact_via_context` **before** `log`.

## CLI

`kirocrew policy {show | validate | explain <scope> <item> | profile <name>}` —
read-only operator diagnostics. `show` reports the ceiling's **proven** provenance
(`signed and verified` / `signed but UNVERIFIED` / `unsigned`) rather than a bare
issuer string. `explain` traces the rule/layer/reason and the live gate verdict. Deliberately **not** exposed as an MCP tool: it surfaces
governance internals that the agent (the governed subject) should not enumerate.

(The two `validate` warnings that used to be listed here were specific to the
computer-use `bundle_id` matcher and the `capabilities.computer_use` row, both of
which are gone.)

## Companion (separate package, separate CR)

The `amazon` companion contributes the restrictive posture as its
**bundled `security_policy.json`** (precedence step 2) rather than as code;
capability providers (Midway/SigV4/tunnels) and the SharePoint redaction
carve-out stay as code. It expects `CONTRACT_VERSION == 1` (pinned pre-launch).

## Files

- `platform/governance.py` — archetypes, catalog, loader, evaluator
  (`resolve`, `resolve_ordinal`, `gate_decision`, `assert_governance_floor`,
  `compose_profiles`, `resolve_pinned_commands` + `COMMANDS_SCOPE` force-pins,
  `policy_signing_payload` + the `identity.signature` verification path).
- `platform/admission.py` — `canonical_signing_bytes` / `hmac_signature` (shared
  by both trust roots), `require_policy_signature` / `trust_keys`, and
  `read_policy_trust_root` (the side-effect-free trust-root reader).
- `platform/update_governance.py` — the shared update seam (`resolve_remote_url`,
  `update_blocked_reason`, `update_required`, `min_version`) called by
  `dashboard/handlers/updates.py`, `cli_server.py` and `slack/gateway.py`.
- `platform/governance_profiles.py` — `ProfileStore` (hot-reload),
  `resolve_active_scope`, `governance_permits`, `governance_floor_ordinal`,
  `GOVERNANCE_ERROR_REASON` (the eval-error marker consumers match on),
  `vet_and_audit`.
- `security.py` — `_SENSITIVE_HOME_DIRS` keystone entries.
- `hooks.py` — Plane A gate threading + the computer-use read-only auto-approve
  (`_cu_read_only_auto_approve`, which reads the action-class table rather than a
  governance row).
- `sel.py` — `log_governance_decision`.
- chokepoints: `sandbox.py`, `mcp_cron.py`, `subagent.py`, `mcp_core.py`.
- `messaging/identity.py` — `channel_inbound_permitted` (the per-message inbound
  `channels` gate) + its SEL audit disposition.
- `executors.py` — `governance_executor` (`mc-gov`), the bounded pool the
  externally-paced governance checks run on.
- `dashboard/handlers_system.py` — `GET /api/governance/channels`.
- `dashboard/handlers/security.py` — `GET /api/governance/policy` (posture-only
  serialization).
- chokepoints: `sandbox.py`, `mcp_cron.py`, `subagent.py`, `mcp_core.py`,
  `computer_use/gate.py` (`require_computer_use` fail-closed +
  `apply_observation_ceiling`).
- `cli.py` / `cli_commands.py` — the `policy` command.

## Tests

`test_governance_policy.py` (archetypes + loader + evaluator + E1–E13 vectors +
extensibility + the `identity.signature` states, the opt-in fail-closed gate, and
the `policy show` provenance reporting), `test_platform_admission.py`
(`require_policy_signature` / shared signing primitives),
`test_governance_boot.py` (compose at boot), 
`test_governance_self_protection.py` (keystone), `test_governance_profiles.py`
(resolution + binding + hot-reload + fail-closed reload dispositions),
`test_governance_gate.py` (Plane A enforcement + audit),
`test_governance_chokepoints.py` (sandbox/cron/spawn/helpers + egress-reserved +
the per-transport inbound gates), `test_governance_channels_endpoint.py`
(`/api/governance/channels`, incl. the eval-error→`null` distinction),
`test_governance_policy_viewer.py` (`/api/governance/policy` posture-only, incl.
`test_detail_never_leaks_rule_contents` and `TestScopeAttribution` — that a
host-profile pin is reported as surface-scoped, not install-wide),
`test_governance_updates.py` (the
`updates` pins, the shared seam's fail-open-on-error disposition, and the
tracked-remote resolution), and `test_computer_use_gate.py` (that the
computer-use gate is audit-only and permits — see the section above).
