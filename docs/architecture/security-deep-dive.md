# Security Deep Dive

The security **architecture**: what Kiro Crew defends against, where its trust
boundaries sit, and how the layers compose. Mechanism detail (exact rule tables,
regex shapes, per-function algorithms) lives in the module specs and is linked
from here rather than restated:

- [`../system-specs/modules/security.md`](../system-specs/modules/security.md)
  is the mechanism spec for every control below.
- [`../system-specs/modules/governance.md`](../system-specs/modules/governance.md)
  is the two-level Policy ∩ Profile model.
- [`../system-specs/modules/platform-context.md`](../system-specs/modules/platform-context.md)
  is the edition seam that lets a companion ADD (never remove) deny rules.
- [`resource-protection.md`](resource-protection.md) covers the DoS/resource
  ceilings (cgroup scope, RLIMIT, file descriptors).

Counts are deliberately absent from this document. Every posture count is
derived at runtime by `security_posture.py` and rendered in Settings → Security
from `GET /api/security/posture`; a number written into prose goes stale silently
while the code it describes keeps changing.

## Threat model

Kiro Crew runs an LLM agent with filesystem and shell access on the operator's own
machine. The dominant threat is **prompt injection from content the agent reads**
(web pages, repository files, Slack thread history, imported documents): text
that is data as far as the operator is concerned, but that the model may follow as
instructions. The two payloads that matter are credential exfiltration and
destructive local action.

Three properties shape every control:

1. **The model is untrusted input, not a trusted caller.** Anything the model
   chooses (a tool title, a file path, a command string) is attacker-controllable
   in the injection case. Controls therefore key on ground truth (the real
   `tool_input` command, the resolved filesystem path) and never on model-authored
   display text alone.
2. **The operator is trusted, the agent is not.** The operator may widen their
   own posture; the agent must not be able to widen it for them. That asymmetry is
   what the keystone (below) enforces.
3. **No single layer is assumed to hold.** A credential read has to defeat the OS
   sandbox, the path gate, the command gate, and output redaction; they fail in
   different ways and are not correlated.

The per-threat mitigation table (XPIA credential theft, WebSocket hijack, CSRF,
DNS rebinding, unauthenticated remote access, and the rest) is in
[`security.md` § Threat Model](../system-specs/modules/security.md).

## Trust boundaries

| Boundary | Trusted side | Untrusted side | Enforced by |
|---|---|---|---|
| Gateway process ↔ agent subprocess | Kiro Crew gateway | `kiro-cli` + every tool/MCP descendant | OS sandbox (`sandbox.py`), env scrub, cgroup scope |
| Agent tool request ↔ execution | the PreToolUse gate's decision | the tool call as the model phrased it | `hooks.py:HookManager.on_tool_call` |
| Operator ceiling ↔ agent | keystone files under the data home | every agent read/write path | `security.is_sensitive_path` / `is_sensitive_write_path` |
| Agent output ↔ any human or external service | nothing | all agent-derived text | `redact_credentials` / `redact_exfiltration_urls` / `StreamRedactor` |
| Browser ↔ dashboard | authenticated session | any other origin or host | token auth, CSRF Origin check, Host allowlist |
| Slack workspace ↔ gateway | owner + allowlisted users | every other Slack sender | owner lock, `is_allowed_user`, Enterprise Grid check |

The single most important structural property: **the PreToolUse gate is
Kiro Crew's own gate, not the agent's.** Denied commands and the governance
ceiling are evaluated in `hooks.py` and are never written into a `kiro-cli` agent
JSON, so an agent config that omits or edits its own deny list cannot weaken the
ceiling.

## How the layers compose

```
Layer 5  Audit ........ SEL event log (HMAC-chained, verifiable)
Layer 4  Output ....... credential redaction + URL exfil scan + streaming redactor
Layer 3  Validation ... typed MCP tool schemas, unicode normalization, length caps
Layer 2  Command ...... denied-command rules + sensitive-bash + exfil shapes
Layer 1  Filesystem ... resolved-path gate (read block + wider write block)
Layer 0  OS sandbox ... namespace (Linux) / Seatbelt (macOS), opt-in

Across all layers: request auth (dashboard tokens, CSRF, Host allowlist),
                   Slack owner lock + workspace origin check,
                   governance ceiling (Policy ∩ Profile), SEL audit
```

Layers 1 through 4 are always on and are the reason Layer 0 can be optional.
Layers 1, 2 and the governance ceiling all evaluate at the same chokepoint
(`on_tool_call`), in a fixed order that matters: sensitive-path and deny checks
run **before** any auto-approve or trust fast-path, so a user trust decision or an
active YOLO grant can never route around a hard deny.

## Layer 0: OS-level sandbox (`sandbox.py`)

Confines the `kiro-cli` subprocess tree with platform-native isolation, hiding
credential directories by bind-mount (Linux user + mount namespaces) or file-read
denial (macOS Seatbelt), and scrubbing credential-bearing environment variables
on the way in. The parent gateway process is unaffected.

**`agent.sandbox` defaults to `"auto"`, engaging OS-level isolation
(namespace on Linux, sandbox-exec on macOS).** The only alternative value is
`"off"` (`config/loader.py`, `AgentConfig.sandbox`, `enum=["auto", "off"]`;
the same two-value enum gates the dashboard config editor in
`dashboard/handlers/core.py`). `"off"` skips Kiro Crew's own sandbox but still
delegates to `kiro-cli`'s internal agent sandbox on macOS when it is enabled,
which cannot nest inside Kiro Crew's
Seatbelt wrap (the macOS kernel returns EPERM even under an allow-all outer
profile), so exactly one layer can own isolation per spawn. Setting `"auto"`
re-enables Kiro Crew's own sandbox.

`wrap_argv`'s internal tier vocabulary is wider than the config enum: `standard`
(what `auto` resolves to), `cc`, `strict` and `off`. Those extra tiers are reached
by internal callers and by the governance `sandbox.min_level` ordinal floor
(`_ORDINAL_SCALES["sandbox"] = ("off", "standard", "cc", "strict")`), which clamps
a requested mode **up** before resolution, so an enterprise floor confines even a
`mode="off"` call. They are not values an operator writes into `agent.sandbox`.
Per-tier hidden paths, the empirical backend probes, the nested-passthrough rule
and the fail-closed/fail-open flags are specified in
[`security.md` § OS-Level Sandbox](../system-specs/modules/security.md).

Two properties are load-bearing at the architecture level:

- **Failure is refusal, not degradation.** With no sandbox backend available and
  a mode other than `off`, `wrap_argv` raises rather than spawning unconfined.
  Running unconfined is an explicit opt-in (`agent.sandbox_allow_unsandboxed_exec`);
  a separate flag (`agent.sandbox_allow_no_isolation`) only demotes the warning's
  log level and does not permit execution. The opt-in's default is
  **platform-independent** — a platform-derived default would grant unconfined
  execution on every backend-less host with no operator having declared it — so
  the discoverable path is instead a consent step in `kirocrew setup`, which
  prompts (default no) when `detect_backend()` reports `"none"` and writes the
  key only on an explicit yes.
- **Delegation is audited, never silent.** When `kiro-cli`'s internal sandbox owns
  isolation for a spawn, the decision is config-driven (never a reaction to a wrap
  failure), logged once per process, and SEL-audited on an audit-or-deny basis: if
  the audit cannot be written, the delegation is refused and Kiro Crew's own
  Seatbelt takes the spawn.

**Launcher shims are deliberately not bypassed on the delegated path.** On that
path the shim is part of `kiro-cli`'s own sandbox mechanism, so resolving past it
would defeat the delegated layer. Where an edition needs a managed launcher
replaced with the executable it ultimately invokes, that goes through the
`PlatformContext.agent_executable` resolver, whose result is always placed
*inside* the same namespace/Seatbelt wrapper. The capability probe never runs an
edition-resolved or user-writable target; it runs a fixed trusted system binary.

### Why the default is defensible

The sandbox is the only optional layer, so the credential-read threat has to be
covered without it. It is, three times over, at different altitudes:

- A tool read of `~/.aws` or `~/.ssh` is refused by the resolved-path gate
  (Layer 1), which follows symlinks before deciding.
- A shell read of the same paths is refused by `is_sensitive_bash_command`
  (Layer 2), which tokenizes and normalizes the command rather than pattern-
  matching raw text, so quoting and expansion tricks do not evade it.
- Anything that still reaches tool output is caught by redaction (Layer 4) before
  it reaches a human or an external service.

`SSH_AUTH_SOCK` is scrubbed whenever a Kiro Crew sandbox tier is active, so
ssh-agent forwarding is unavailable inside a confined spawn. Operators who depend
on passphrase-protected keys or hardware tokens use key files directly or leave
`agent.sandbox` at `off`.

## Layer 1: Filesystem gate (`security.py` + `hooks.py`)

`is_sensitive_path()` is the shared read+write block, and
`is_sensitive_write_path()` is its strict superset: it adds paths that stay
readable but must not be modified by an agent tool (the data home's `config.json`
/ `config.local.json`, which carry resource ceilings, and the data-home migration
marker, whose mere presence is a trust signal). Path matching checks the fully
symlink-resolved target as well as the lexically normalized and raw forms, so a
workspace symlink into a blocked directory is refused through the link.

`hooks.safe_read_file()` is the guarded read used by Kiro Crew's own non-tool file
access: it re-checks the resolved target and then opens the canonical path with
`O_NOFOLLOW`, which closes the TOCTOU window where the final component is swapped
for a symlink after the check.

### The keystone: the agent cannot read or rewrite its own ceiling

The governance trust root (`security_policy.json`, `profiles/`,
`admission_policy.json`), the denied-command opt-out state
(`denied_commands.json`), the SEL HMAC key and event log, the dashboard token
signing key, and the channel credential `.env` all sit on the read+write block.
This is a single mechanism with an outsized consequence: it is what makes the
enterprise ceiling **un-disableable from inside the agent**. An agent that could
read these could forge tokens or impersonate internal callers; one that could
write them could set `disable_all: true` and neuter the deny gate after a
restart. Every legitimate reader and writer opens these paths directly rather
than through the shared gate, so real functionality is unaffected.

Each leaf is registered under every known data-home prefix, so a not-yet-migrated
legacy home is fenced identically to the current `~/.kiro/crew`.

**Do not weaken this when editing the path or bash matchers.** Write and extract
verbs must stay covered: a bash command that merely *names* a write-protected
leaf is refused, verb-independently, because an enumerated write-verb allowlist is
inherently bypassable (quoted redirects, `cp`, a Python `open(..., 'w')`, or any
novel verb).

### Audited internal carve-out

`safe_read_file_internal(read_id)` permits a small hardcoded allowlist of
system-internal reads of otherwise-sensitive paths. It re-verifies
`is_sensitive_path()` (a path that has stopped being sensitive means the
configuration drifted, so it refuses rather than silently widening), opens with
`O_NOFOLLOW` on a single descriptor, SEL-audits every outcome, and fails closed:
a `success` whose audit cannot be persisted returns `None`, because a log warning
is not an audit event and the carve-out's validity depends on every successful
read producing one. `read_id` is never constructed from untrusted input.

## Layer 2: Command gate (`security.py` + `hooks.py`)

Three independent checks run on every shell-bearing tool call, each against the
model's title **and** the raw command:

- **Denied-command rules** (`BUILTIN_DENIED_RULES`): first-class
  `DeniedCommandRule` records (stable `id`, regex `pattern`, `category`,
  human `description`) covering credential exfiltration, destructive
  infrastructure and data operations, publishing to a protected branch, and
  self-protection (the agent disabling Kiro Crew or minting its own dashboard
  token). Default-ON, user-configurable from Settings → Security; the governance
  `commands` scope is the enterprise force-pin that cannot be opted out of
  (tightest-wins).
- **Sensitive-bash detection** (`is_sensitive_bash_command`): refuses commands
  that read credential paths, reach the cloud metadata endpoint under any IP
  encoding, or dump credential environment variables. Regex fast-path first, then
  a tokenizing pass that resolves quoting, empty-string concatenation, `$HOME`
  and tilde before routing path-like tokens through `is_sensitive_path()`.
- **Exfiltration shapes** (`audit_bash_exfiltration`): data-egress and
  reverse-shell forms, narrowly scoped so it can be a hard deny at the gate
  without blocking benign local commands.

`SUSPICIOUS_BASH_PATTERNS` / `audit_bash_command()` are a **separate, advisory**
surface: they back the `kirocrew security audit` history scan and the posture
count, and are not enforced at the gate. The gate enforces the narrower checks
above. Conflating the two is the historical error here, so keep the distinction
explicit.

Rule-table contents, the two-pass whole-string/per-segment evaluation, the
verb-anchored git-publish detector, the protected-branch and force-push
semantics, the argv-structural self-protection floor, and the linear-time
ReDoS-safe matcher are all specified in
[`security.md` § Denied Commands](../system-specs/modules/security.md).

Every denial emits a `deny_event` SEL record; an exception grant emits
`deny_exception` fail-closed (if the audit cannot be written, the exception is not
granted).

## Layer 3: Input validation (`validation.py`)

Every MCP tool call is checked against a declarative `FieldSpec` + `ToolSchema`
before the handler sees it: NFC unicode normalization with hidden-character
stripping (control, format, private-use and surrogate code points, preserving
`\n`/`\r`/`\t`), enum allow-lists, regex patterns for identifiers, range checks,
unknown-field rejection, tiered length caps (`MAX_TOOL_NAME_LEN` 256,
`MAX_SHORT_STRING` 500, `MAX_MEDIUM_STRING` 5 000, `MAX_LONG_STRING` 50 000), and
response truncation at `MAX_RESPONSE_LEN` (100 000 chars) so unbounded tool output
cannot be a DoS vector.

The schema count is a runtime-derived posture value (`tool_schemas` in
`security_posture.py`), surfaced in Settings; it is not stated here.

## Layer 4: Output redaction

Redaction runs at **every** boundary where agent-derived output reaches a human or
an external service. The authoritative list is the `redaction_paths` control in
`security_posture.py`, whose registry is kept honest by an omission-detecting
test: every redactor call site in the package must be either a registered sink or
on an explicit non-egress allowlist, so a new egress path cannot be added without
someone deciding which bucket it belongs in.

- `redact_credentials()` recognizes credential families in plaintext and
  base64-encoded form (it decodes base64-looking chunks and re-checks the decoded
  bytes), including cloud access keys and secrets, private-key headers, chat and
  forge tokens, package-registry tokens, and database connection URIs carrying
  embedded credentials. Key-value matching is JSON-aware and value classes are
  bounded at JSON structural delimiters, so a match in compact JSON cannot
  over-capture and mask the next credential.
- `redact_exfiltration_urls()` / `scan_exfiltration_urls()` are
  **domain-agnostic**: they flag the payload, not the destination. A credential in
  a URL is an unconditional floor; long query strings, base64 blobs and heavy
  URL-encoding are heuristics. A flagged URL is replaced with a redaction marker.
- `redact()` composes both in order for a single call site.
- `StreamRedactor` handles the case per-chunk redaction structurally cannot: a
  credential split across a streaming boundary, where neither fragment matches on
  its own. It withholds the trailing run of credential-class characters until a
  non-credential terminator arrives or the stream ends, emitting only the
  confirmed-safe prefix, with a bounded hold-back so latency and memory stay
  bounded on a pathologically long unbroken run.

Two ordering rules generalize beyond this layer and are worth stating once:
**screen after decode, not before** (screening an encoded form and then writing
the decoded value makes every escape a bypass), and **redact before truncate**
(truncating first can slice a credential so neither fragment matches).

## Layer 5: Audit (SEL)

The Security Event Log is append-only and HMAC-chained, so tampering is
detectable rather than merely discouraged; `GET /api/sel/verify` reports the
chain's integrity and `GET /api/sel/events` returns recent records. Every event
carries a `source` inferred from the session key (`sel._infer_source`, published
via `sel.audit_sources()`), and a call site may stamp a more specific source, so
the inferred set is a floor rather than a total.

The audit log is itself a user-facing, *durable* surface: string fields are
redacted before they are written or forwarded. A leak into the SEL persists in a
way a response body does not. See
[`../system-specs/modules/sel.md`](../system-specs/modules/sel.md).

Several security decisions are audit-or-deny rather than best-effort: a sandbox
delegation, a deny exception, and an internal sensitive read all refuse to
proceed when their audit cannot be written. The one documented exception is the
nested-sandbox passthrough, which has no safe alternative (the kernel denies a
re-wrap by design) and would otherwise couple every in-sandbox spawn to SEL
health; it logs loudly and proceeds, still confined by the outer boundary.

## Governance: the enterprise ceiling

Governance is a second, orthogonal axis to the layers above:
`effective = POLICY ∩ PROFILE`, tightest-wins. Level 1 POLICY is loaded at boot
from the trust-root path and is never merged from `config.json`; Level 2 PROFILE
is a per-surface, narrow-only ceiling. Both are enforced at Kiro Crew's own
PreToolUse gate, which is what lets a policy deny a tool or MCP call **even when
the `kiro-cli` agent config granted it**.

Architecturally the important properties are that the evaluator is
scope-name-agnostic (adding a scope is a `SCOPE_CATALOG` data change, never an
evaluator edit), that governance runs before the auto-approve path so it cannot be
bypassed by a trust decision, and that its trust-root files are on the keystone
floor so the agent cannot read or rewrite its own ceiling. Archetypes,
composition algebra, scope boundaries and the signed-policy authenticity model
are in [`../system-specs/modules/governance.md`](../system-specs/modules/governance.md).

**Computer use is deliberately not governed.** It is one operator opt-in on a
keystone file, with refusals enforced in band on the tool dispatch path rather
than at the fail-open PreToolUse gate. See
[`../system-specs/modules/computer-use.md`](../system-specs/modules/computer-use.md).

## Authentication and authorization

### Dashboard requests

HMAC-signed tokens with dual expiry: a short link-click window
(`LINK_WINDOW_SECS`, 5 minutes) and a longer cookie session TTL capped at
`MAX_SESSION_TTL_SECS` (20 hours), IP-pinned on first use. Every request requires
a valid token, with a small set of deliberate, secret-free exceptions: static
assets and same-origin vendored JS (the SPA and sandboxed-iframe bootstrap), the
local-bootstrap endpoints that authenticate with a loopback peer plus a
filesystem secret, the three liveness probes, and self-authenticating external
webhooks that validate their own signatures.

Supporting controls: per-session logout via a cookie nonce recorded in a revoked
set (so one session is revoked without affecting others); app tokens confined to
their manifest-declared API allowlist, deny-by-default even on internal paths; a
path-restricted refresh cookie so the app self-recovers after access-cookie
expiry; and the `Secure` cookie attribute when the gateway is behind TLS.

### CSRF and DNS rebinding are two different barriers

Origin/Referer validation covers state-changing methods. The `Host`-header
allowlist runs on **every** method, because GET-based exfiltration is the
DNS-rebinding payload, and it deliberately does **not** trust a loopback
`request.remote`: a rebound request *is* loopback at the socket while its `Host`
carries the attacker's domain. Both derive their allowlists from one source
(`check_origin` / `check_host` over `allowed_origins`, plus a canonical-loopback
floor from `build_allowed_hosts`) so the two layers cannot drift. Host validation
is deny-by-default (an empty `allowed_origins` denies, never fails open) and
rejects with 403 plus a SEL event. The sole exemption is the three liveness
probes, whose handlers compensate by stripping build-identity fields unless the
caller is direct-local, so a rebound request learns only the liveness bit.

### Slack

Deny-by-default owner lock: socket mode refuses to connect without an owner id,
and event handling rejects messages when it is missing. Trust and YOLO buttons
are DM-gated and suppressed in group channels, with a non-owner receiving an
ephemeral rejection.

Slack messages are processed **inline** and reach the agent directly, gated by
`is_allowed_user` and the workspace origin check. There is no challenge-and-
redirect interception; `send_channel_challenge()` does not exist and must not be
reintroduced on an upstream sync. The generic signed-token helpers remain and
back the explicit `/kirocrew dashboard` link command.

Enterprise Grid validation is a two-layer, **default-open** control: with no
`slack.allowed_enterprise_ids` configured, every reachable workspace is allowed.
`auth.test` caches the workspace `team_id` (plus the org-level enterprise id on
Grid) at startup, and each inbound event's `team` is compared against the cached
allowlist. A governance `channels.posture` policy is the agent-unweakenable
ceiling on top of the operator-editable config allowlist.

### Interactive trust escalation

Dashboard tool approvals offer four decisions, in widening scope: `trust_command`
(this exact command, session-scoped), `trust_base` (the base command glob, e.g.
`ls *`, plus the bare binary, session-scoped), `trust_reads` (read-only bash for
the slot), and `trust` (all tools for the slot). `yolo` is the global escalation.

The security-relevant property is what the pattern is derived from: the **actual
command in `tool_input`**, not the model-authored display title. Trust patterns
are per-slot fnmatch globs; a multi-command title yields one pattern per binary.
Trust never outranks a deny: the gate's deny and governance checks run before the
trust and auto-approve paths.

### Auto-approve (YOLO) has one duration

Auto-approve is time-bounded by a **single** duration shared by every ad-hoc
surface (`agent.yolo_duration`, default `6h`, hard ceiling 24 h, or
`until_shutdown` for an in-memory grant with no timed expiry that a restart
clears). There are deliberately no per-surface TTLs: giving the same grant a
different lifetime depending on which surface enabled it is unpredictable for the
operator without buying any security.

The duration is resolved from live config at activation time, so a value saved in
Settings applies to the next activation without a restart. A 5-minute grace
window after expiry allows renewal instead of a fresh activation.

The one non-expiring grant is `agent.dangerously_skip_permissions` in
operator-owned config: a standing instruction, deliberately config-file-only with
no dashboard toggle, re-established and re-audited on every startup. An
enterprise policy can deny it via the `yolo_duration` scope's `permanent` member,
which downgrades it to the ordinary ad-hoc duration.

Every lifecycle transition (`activate`, `renew`, `expired`, `deactivate`) is
SEL-audited, and fleet-visibility endpoints expose the live state
(`/api/status` reports `yolo_active` / `yolo_expires_at`;
`/api/admin/compliance/yolo-status` carries the full override status).

## Context isolation

Observe-mode channel history is gated on sender authorization: only owner or
allowlisted messages are recorded, so a non-owner cannot influence LLM context by
posting into shared channel traffic. Slack thread-root content, which any thread
participant can author, is injection-screened and dropped on match, and surviving
text is framed as explicitly untrusted data with a SEL event on every drop.

## Frontend

| Control | Implementation |
|---|---|
| XSS prevention | DOMPurify on all rendered HTML content |
| Safe DOM APIs | `createElement` + `textContent` for error fallbacks |
| Mermaid | `securityLevel: 'strict'` (iframe sandbox), so an injected diagram cannot execute JS |
| No `innerHTML` | React text children rather than HTML string construction |
| No regex linkification | React elements via `.split()` |

## Credential file handling

`load_credentials()` tightens `~/.kiro/crew/.env` to owner-only mode at load time
and warns if it cannot (for example when the file is owned by another user). The
file is also on the keystone read+write block, so the agent cannot reach it
through any tool or shell form regardless of its filesystem mode: owner-only
permissions do not isolate another process running as the same uid, which is
exactly the agent's situation.

---

## Known gaps

Each gap below is a real residual, stated with why the obvious fix is not already
in place.

**No network egress control by default.** The sandbox hides credential files but
does not restrict outbound network access, so a compromised agent can post
non-credential data to an arbitrary host. Redaction blunts the credential case
and the `network.egress` governance scope can bound hosts where a policy is
configured, but there is no default-on egress boundary. A network namespace
(Linux) or host firewall rules with a trusted-destination allowlist would close
it.

**Regex and tokenizer command matching is not a shell parser.** The command gate
normalizes aggressively (quoting, empty-string concatenation, `$HOME`/tilde,
mid-word empty substitutions, local assignment inlining, literal interpreter
payloads) and adds argv-structural floors for the self-protection rules, which
closes the well-known evasion families. It is still not a bash AST: a payload
assembled at runtime (string concatenation, a base64 blob, an indirect `eval
"$CMD"`) contains nothing for a pattern to find. The un-disableable guarantee for
the signing credential is the keystone path floor, which these rules do not
replace.

**No audit dashboard.** SEL events are queryable over the API
(`/api/sel/events`, `/api/sel/verify`) but there is no UI to browse, filter or
alert on them, so tamper detection and anomaly spotting are manual.

**No in-agent sandbox-escape detection.** The gateway decides fail-closed whether
a backend exists before spawning, but nothing verifies from *inside* the agent
process that confinement actually took effect (for example by attempting to read
a canary that should be hidden). A confinement that loads but does not enforce
would not be noticed.

**Base64 credential detection has a floor.** Only base64 chunks at or above the
minimum length are decoded and re-checked, so a shorter encoded fragment, or one
split across messages, can pass. Cross-message correlation and entropy-based
detection would extend it.

**Write protection covers Kiro Crew's own trust root, not the user's shell
startup files.** Credential directories and the keystone are read+write blocked,
and `config.json` plus the migration marker are write-blocked, but ordinary
persistence targets such as `~/.bashrc` or `~/.zshrc` are not: they are not
credential stores, and blocking the whole home directory would make the agent
useless for its normal work. An agent write there is therefore a real persistence
vector, mitigated only by the approval gate and the destructive-command rules.

**Resource ceilings depend on the platform.** The cgroup v2 scope that bounds
fork bombs and memory balloons requires Linux with cgroup delegation; where it is
unavailable (macOS, older Linux, no user session) it is a no-op with a loud
warning and only the file-descriptor limit applies. See
[`resource-protection.md`](resource-protection.md).
