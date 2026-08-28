# AgentCore Identity and Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Crew agent a standalone AgentCore Identity workload
and use AgentCore Gateway as the outbound token-vending plane, behind
default-off CPP seams so the public edition stays byte-identical. A
fleet picks exactly one `security_policy.json` posture, and the
copyable cloud Policy.json pair must match it.

**Architecture:** A new `AgentIdentityProvider` slot on
`PlatformContext` holds workload identity and token vending. The public
`Default` is empty. The companion talks to Identity (`GetWorkloadAccessToken*`)
and contributes the Gateway MCP URL. The core derives a trusted
`SessionPrincipal` and leaves outbound OAuth to Gateway.
`CONTRACT_VERSION` stays `1`.

Two exclusive postures, selected by `capabilities.agentcore.posture`:

- **`workload`** — deployed box. Instance IAM invokes Gateway at
  boot. No login. Kiro MCP defaults still merge. Instance Policy.json
  allows `InvokeGateway` + `GetWorkloadAccessToken` / `ForUserId` and
  Denies `ForJWT`.
- **`login`** — Gateway catalog only. Rebuild withholds Kiro-global,
  seam-global, crew-store, and leftover non-managed MCP. Tools stay
  absent until `ensure` login vends a CUSTOM_JWT. Instance Policy.json
  allows `ForJWT` only and Denies `ForUserId` + `InvokeGateway`.

The launcher `iam.policy_json()` never grows those instance actions.
A new helper emits the instance fragment; a new named boundary
(`kirocrew-ec2-boundary-agentcore`) is the ceiling. The original
`kirocrew-ec2-boundary` is not re-versioned.

**Tech Stack:** Python 3.10+, existing CPP (`platform/`), MCP gateway
rewriter, governance `SCOPE_CATALOG`, pytest-asyncio. Companion-only:
`boto3` / `bedrock-agentcore` (not a public-wheel dependency).

**Spec:**
[`../../request-for-change/rfc-agentcore-identity-gateway.md`](../../request-for-change/rfc-agentcore-identity-gateway.md)

## Global Constraints

- Core never imports `bedrock_agentcore`, `boto3` AgentCore clients, or
  names Cognito / Midway / a discovery URL.
- Do not consume `IdentityProvider.whoami` or `issuer` (RESERVED).
- Do not write tokens into `~/.kiro/agents/kirocrew.json`, transcripts,
  SEL payloads, or `status()`.
- Do not take `userId` from the model, a tool argument, or a query
  string.
- Fail closed: missing companion, expired JWT, vend error, unknown
  posture, or posture-vs-IAM mismatch means the Gateway server is
  absent for that session.
- `capabilities.agentcore` defaults **off**. No new default-on egress.
- Postures are exclusive. Do not layer `workload` IAM inbound on top
  of `login` JWT inbound.
- Do not `CreatePolicyVersion` `kirocrew-ec2-boundary`. AgentCore
  ceiling is a new named boundary, opt-in at launch.
- Do not put `InvokeGateway` / `GetWorkloadAccessToken*` on the
  launcher Policy.json. Those belong on
  `iam.agentcore_instance_policy_document(posture)`.
- `login` withholds Kiro defaults at `rebuild_agent_config()` emit
  time (same control as `kirocrew-computer` `spec_gate`), not only
  at the tool gate.
- `sso_status.py` stays a stub. No `CHANGELOG.md` edit.
- Computer use stays ungoverned. No `computer_use.*` scopes.
- Update the owning spec in the same commit as the code it covers.
- Run `scripts/docs-lint.sh` after every docs change.
- Frontend user-facing strings go through the i18n catalog. No emojis.

---

## Stack and file map

| PR | Branch suffix | Primary files |
|---|---|---|
| 1 | `plan` (this PR) | RFC, this plan, RFC + plans indexes |
| 2 | `seams` | `platform/interfaces.py`, `defaults.py`, `context.py`, `bootstrap.py`, CPP coverage tests, `SCOPE_CATALOG` + `posture`, `platform-context.md`, `governance.md` |
| 2b | `iam` | `cloud/iam.py` helper + new boundary name, launcher CreateRole pin widening, dashboard/CLI copy of the instance fragment, login-mode rebuild withhold, posture-mismatch probe, `cloud.md`, `mcp.md` |
| 3 | `principal` | session-principal derivation, injected-message guard, unit tests, `session.md` |
| 4 | `probe` | Phase 0 verdict written back into the RFC (kiro-cli headers + standalone workload) |
| 5 | `inject` | per-session Gateway header sidecar **or** header-proxy MCP; unpooled Gateway server |
| 6 | `consent-unattended` | 3LO allowlist + dashboard/channel prompt + posture-aware cron/M2M policy + SEL; `security.md` |
| 7 | extra + IaC (this repo) | `kirocrew[agentcore]` (`AwsAgentIdentityProvider`), CFN Gateway URL, `install.sh --agentcore`, systemd env |

PRs 2, 2b, 3, 5, 6, and 7 are this repository. PR 4 is a research commit
that only edits the RFC. Operator IdP JWT annotation and Gateway/target
control-plane stay companion-owned; token fetch on a launched box does not.
PR 2b can land right after PR 2: the IAM helpers and the rebuild
withhold do not need a live vend path.

### Stable interfaces

```python
@dataclass(frozen=True)
class WorkloadIdentity:
    name: str
    arn: str

@dataclass(frozen=True)
class SessionPrincipal:
    surface: str
    subject: str
    session_key: str
    user_jwt: str | None = None

@dataclass(frozen=True)
class InboundToken:
    scheme: str
    token: str
    expires_at: float
    audience: str

class AgentIdentityProvider(Protocol):
    def enabled(self) -> bool: ...
    def workload_identity(self) -> WorkloadIdentity | None: ...
    def status(self) -> dict[str, object]: ...
    def gateway_mcp_spec(self) -> dict[str, object] | None: ...
    async def annotate_principal(
        self, principal: SessionPrincipal
    ) -> SessionPrincipal: ...
    async def vend_workload_access_token(
        self, principal: SessionPrincipal
    ) -> str | None: ...
    async def vend_gateway_inbound_token(
        self, principal: SessionPrincipal
    ) -> InboundToken | None: ...
```

`DefaultAgentIdentityProvider`: `enabled() -> False`; all other methods
return `None` / `{}` / the input principal unchanged.

---

### Task 1: PR 1 — record the design and this plan

**Files:**

- Create: `docs/request-for-change/rfc-agentcore-identity-gateway.md`
- Create: `docs/superpowers/plans/2026-08-27-agentcore-identity-gateway.md`
- Modify: `docs/request-for-change/README.md`
- Modify: `docs/superpowers/plans/README.md`

**Interfaces:**

- Consumes: CPP slot table, RESERVED identity methods, MCP merge rules,
  AgentCore Identity/Gateway product shape.
- Produces: locked design + PR-sized implementation map.

- [x] **Step 1: Write the RFC and this plan.**

- [x] **Step 2: Index both documents.**

  Add a `draft` row to `docs/request-for-change/README.md` naming the
  commit they were verified against (`152c00e99`). Link this plan from
  `docs/superpowers/plans/README.md`.

- [x] **Step 3: Verify documentation.**

  Run: `bash scripts/docs-lint.sh && git diff --check`
  Expected: both exit zero.

- [x] **Step 4: Commit.**

  ```
  docs: add AgentCore identity and gateway plan
  ```

---

### Task 2: PR 2 — CPP slot and governance row (public no-ops)

**Files:**

- Modify: `src/kiro_crew/platform/interfaces.py` (add dataclasses + Protocol)
- Modify: `src/kiro_crew/platform/defaults.py` (add `DefaultAgentIdentityProvider`)
- Modify: `src/kiro_crew/platform/context.py` (add `agent_identity` field)
- Modify: `src/kiro_crew/platform/bootstrap.py` (wire Default)
- Modify: `src/kiro_crew/platform/__init__.py` (exports)
- Modify: `src/kiro_crew/platform/governance.py` (`capabilities.agentcore`)
- Modify: `test/test_platform_cpp_seam_coverage.py` (and any bootstrap/context tests the coverage file names)
- Modify: `docs/system-specs/modules/platform-context.md`
- Modify: `docs/system-specs/modules/governance.md`

**Interfaces:**

- Consumes: the Protocol above.
- Produces: a composed `ctx.agent_identity` that is disabled in
  standalone.

- [ ] **Step 1: Write the failing coverage test.**

  Assert `PlatformContext` has `agent_identity`, the default adapter's
  `enabled()` is False, `workload_identity()` is None,
  `gateway_mcp_spec()` is None, `status()` is a dict with no token-like
  keys, and `capabilities.agentcore` exists with
  `capability_default=False`. An `enabled: true` document with a
  missing or unknown `posture` fails closed (treated as disabled,
  or boot-abort when `boot.fail_closed`).

- [ ] **Step 2: Run the test and verify RED.**

  Run: `python -m pytest test/test_platform_cpp_seam_coverage.py -n0 -q -k agent_identity`
  Expected: FAIL because the field / scope does not exist.

- [ ] **Step 3: Implement Default + catalog row.**

  Follow existing v1 addition comments (`no CONTRACT_VERSION bump`).
  `safe_context_call` fallback for every new method must be the disabled
  answer (False / None / `{}` / unchanged principal), never a raised
  error that degrades to "enabled."

- [ ] **Step 4: Grep the public tree for AWS leakage.**

  Run: `rg -n "bedrock.agentcore|bedrock_agentcore|GetWorkloadAccessToken|cognito-idp" src/kiro_crew website/src`
  Expected: no matches.

- [ ] **Step 5: Update specs and run gates.**

  `platform-context.md` table gets an `agent_identity` row.
  `governance.md` documents the new capability as opt-in and names
  the inner `posture` field (`workload` | `login`).
  Run: `black --target-version py310 <touched py>` then
  `python3 scripts/check_black_formatting.py` and
  `mypy --platform linux src/kiro_crew`.

- [ ] **Step 6: Commit.**

  ```
  feat: add agent_identity CPP slot and agentcore capability
  ```

---

### Task 2b: PR 2b — Policy.json pair, boundary successor, login withhold

**Files:**

- Modify: `src/kiro_crew/cloud/iam.py`
  (`agentcore_instance_policy_document(posture)`,
  `AGENTCORE_BOUNDARY_NAME = "kirocrew-ec2-boundary-agentcore"`,
  `agentcore_boundary_policy_document(account, posture)`,
  launcher `CreateRole` / `CreatePolicy` statements accept either
  boundary name)
- Modify: `src/kiro_crew/cloud/source.py` (create-once helper for the
  new boundary name; still never `CreatePolicyVersion`)
- Modify: `src/kiro_crew/cloud/templates/kirocrew-ec2.yaml`
  (`PermissionsBoundaryArn` `AllowedPattern` lists both names;
  `AgentCorePosture` creates `AWS::BedrockAgentCore::WorkloadIdentity`
  named `kirocrew-<StackTag>` and attaches the instance grant)
- Modify: dashboard cloud IAM copy + `cli_cloud` `iam-policy` (labeled
  sibling document; query/flag `--instance --posture`)
- Modify: `src/kiro_crew/agent.py` / MCP merge (`rebuild_agent_config`
  withholds Kiro-global, seam-global, crew-store, leftover non-managed
  when posture is `login` and the capability is on)
- Create: `test/test_agentcore_iam_posture.py`
- Modify: existing `test/test_cloud_*` / rebuild tests that pin
  `BOUNDARY_NAME` or merge order
- Modify: `docs/system-specs/modules/cloud.md`
- Modify: `docs/architecture/mcp.md` (login withhold = emit-time
  `spec_gate`, same as `kirocrew-computer`)

**Interfaces:**

- Consumes: `capabilities.agentcore.{enabled,posture}` from PR 2.
- Produces: posture-correct instance Policy.json; a new immutable
  boundary name; login-mode rebuild that emits only managed
  `kirocrew-*` plus (later) a URL-only Gateway spec.

- [ ] **Step 1: Write failing IAM + rebuild tests.**

  ```python
  def test_launcher_policy_json_has_no_invoke_gateway():
      assert "InvokeGateway" not in iam.policy_json()
      assert "GetWorkloadAccessToken" not in iam.policy_json()

  def test_workload_instance_document_denies_for_jwt():
      doc = iam.agentcore_instance_policy_document("workload")
      ...

  def test_login_instance_document_denies_userid_and_invoke():
      doc = iam.agentcore_instance_policy_document("login")
      ...

  def test_original_boundary_document_unchanged():
      # byte-compare the SSM-core + source-bucket shape; no AgentCore

  def test_login_rebuild_withholds_kiro_global(tmp_path):
      # seed ~/.kiro/settings/mcp.json with a dummy server
      # posture=login + capability on → rebuilt kirocrew.json
      # has kirocrew-core / kirocrew-cron, no dummy, no Gateway yet

  def test_workload_rebuild_still_merges_kiro_global(tmp_path):
      ...

  def test_login_probe_succeeds_iam_invoke_fails_closed():
      # posture=login + mock IAM InvokeGateway success
      # → Gateway spec absent, SEL agentcore.posture_mismatch
  ```

- [ ] **Step 2: Run tests, verify RED.**

  Run: `python -m pytest test/test_agentcore_iam_posture.py -n0 -q`

- [ ] **Step 3: Implement helper, successor boundary, withhold, probe.**

  Keep `BOUNDARY_NAME = "kirocrew-ec2-boundary"` as the default
  launch path. AgentCore launches pass the successor ARN. The
  successor document is the **union** ceiling (SSM-core +
  source-bucket + every AgentCore action either posture may grant).
  Resource ARNs in the instance fragment are fleet-pinned (workload
  name `kirocrew`, gateway `kirocrew-*`), never `*`, except on the
  explicit Deny SIDs.

- [ ] **Step 4: Update cloud.md + mcp.md and run gates.**

  Run: `bash scripts/docs-lint.sh` then
  `black --target-version py310 <touched py>` then
  `python3 scripts/check_black_formatting.py`.

- [ ] **Step 5: Commit.**

  ```
  feat: add agentcore policy.json postures and login mcp withhold
  ```

---

### Task 3: PR 3 — trusted session principal

**Files:**

- Create: `src/kiro_crew/platform/agent_identity.py` (dataclasses if not
  already in interfaces; `derive_session_principal(slot_or_session)`)
- Create: `test/test_agent_identity_principal.py`
- Modify: session start site(s) — the smallest existing hook that already
  knows `session_key` + surface (likely `session` / chat runner / channel
  dispatch). Do **not** invent a second session key.
- Modify: `docs/system-specs/modules/session.md`

**Interfaces:**

- Consumes: existing session key and channel identity.
- Produces: `SessionPrincipal` with partitioned `subject`.

- [ ] **Step 1: Write failing derivation tests.**

  ```python
  def test_dashboard_owner_is_partitioned():
      p = derive_session_principal(surface="dashboard", raw_id="alice", session_key="dashboard:1")
      assert p.subject == "dashboard+alice"
      assert p.user_jwt is None

  def test_tool_input_cannot_supply_subject():
      # whatever helper rejects kwargs from tool_input
      ...

  def test_injected_cron_envelope_does_not_derive_a_user():
      assert derive_session_principal_for_injected("[Cron notification from \"job\"]") is None
  ```

- [ ] **Step 2: Run tests, verify RED, then implement.**

  Run: `python -m pytest test/test_agent_identity_principal.py -n0 -q`

- [ ] **Step 3: Call `annotate_principal` through `safe_context_call`.**

  Fallback = the core-derived principal unchanged. Companion may set
  `user_jwt`. It must not change `subject`. Add a test that a stub
  adapter attempting to rewrite `subject` is ignored or rejected.

- [ ] **Step 4: Commit.**

  ```
  feat: derive partitioned AgentCore session principals
  ```

---

### Task 4: PR 4 — Phase 0 probe verdict

**Files:**

- Modify: `docs/request-for-change/rfc-agentcore-identity-gateway.md`
  (Open question 1 + Phase 0)

No product code. Record:

1. Whether kiro-cli accepts per-session MCP headers without persisting
   them in the rendered agent file (experiment against current kiro-cli;
   cite version).
2. Whether a standalone workload identity accepts
   `GetWorkloadAccessTokenForJWT` from the companion IAM role (companion
   repo or a scratch account; paste only the error string, no tokens).

- [ ] **Step 1: Run the two probes.**

- [ ] **Step 2: Write the verdict into the RFC Open questions section.**

- [ ] **Step 3: Commit.**

  ```
  docs: record AgentCore Phase 0 header and workload verdicts
  ```

Phase 5 implements **exactly one** of: kiro-cli sidecar headers, or the
local header-proxy MCP. Do not implement both.

---

### Task 5: PR 5 — Gateway attach and inbound injection

**Files (sidecar path, if Phase 0 said yes):**

- Modify: `src/kiro_crew/mcp_gateway/` rewriter / session spawn
- Modify: `src/kiro_crew/agent.py` (`_extra_mcp_servers` merge of
  `gateway_mcp_spec()`, URL only)
- Create: `test/test_agentcore_gateway_inject.py`
- Modify: `docs/architecture/mcp.md` only if the inject path needs a
  new header-sidecar note (login withhold already landed in PR 2b)

**Files (proxy path, if Phase 0 said no):**

- Create: `src/kiro_crew/mcp_gateway/agentcore_proxy.py` (stdio MCP that
  forwards to the Gateway URL with the session inbound token)
- Same tests and `mcp.md` update

**Either path:**

- [x] **Step 1: Write failing tests.**

  - `enabled() is False` → no Gateway server in the rebuilt agent config.
  - `enabled() is True` / posture `login` but `vend_gateway_inbound_token`
    returns None → server still absent (fail closed). Operator must
    complete ensure-login.
  - `enabled() is True` / posture `workload` → IAM inbound, no JWT
    sidecar. Gateway URL-only spec is present at boot.
  - Successful `login` vend → transport has `Authorization: Bearer …`
    and `~/.kiro/agents/kirocrew.json` does not.
  - Two sessions with different principals do not share a backend
    (unpooled).
  - Token bytes never appear in a captured log / SEL fixture.

- [x] **Step 2: Implement the Phase 0-chosen path only.**

  Gate contribution on `capabilities.agentcore` (fail closed when the
  capability is off, even if the companion `enabled()` is True).

- [x] **Step 3: Commit.**

  ```
  feat: inject per-session AgentCore Gateway inbound tokens
  ```

---

### Task 6: PR 6 — consent surface and unattended policy

**Files:**

- Modify: `src/kiro_crew/security.py` (consent-host allowlist reuse of
  `oauth_endpoints.json`; do not add a second file unless the existing
  keystone cannot express AgentCore consent hosts)
- Modify: dashboard handler + a small Settings / modal component
  (`website/src/…`) with i18n keys
- Modify: cron / task start path: `login` never attaches Gateway
  (already withheld). `workload` refuses OBO targets without a
  vaulted owner token (companion reports "m2m" vs "user" on
  `gateway_mcp_spec()` or status)
- Modify: SEL event names from the RFC
- Modify: `docs/system-specs/modules/security.md`
- Test: `test/test_agentcore_consent.py`, `test/test_agentcore_unattended.py`

- [x] **Step 1: Write failing tests for unknown consent host, injected
  envelope, and cron-without-JWT.**

- [x] **Step 2: Implement allowlist + fail-closed unattended policy.**

  User-facing copy is cataloged. Backend errors include `code`.
  No model-visible "click this URL" injection.

- [x] **Step 3: Verify dashboard strings and the unattended path.**

  `cd website && npm run test` for the new modal/copy.
  Backend: `python -m pytest test/test_agentcore_consent.py test/test_agentcore_unattended.py -n0 -q`

- [x] **Step 4: Commit.**

  ```
  feat: gate AgentCore 3LO consent and unattended vending
  ```

---

### Task 7: In-repo extra + IaC (this repository)

Landed in Kiro Crew as an opt-in extra, not a `kirocrew.plugins`
companion. A launched box that opted into AgentCore actually vends.

- Extra: `kirocrew[agentcore]` = `boto3>=1.34,<2` (`setup.cfg`)
- Adapter: `platform/agentcore_aws.py` `AwsAgentIdentityProvider`
- Bootstrap (standalone only): `dataclasses.replace` of
  `agent_identity` when `opted_in()` — home-policy or env posture
  `workload`/`login`, or a named workload plus
  `KIROCREW_AGENTCORE_AWS=1`. `ensure_extra()` pips
  `kirocrew[agentcore]` on that path and on Settings PUT
  (not on GET; not uninstalled when posture returns to `none`)
- Settings/policy `capabilities.agentcore.gateway_url` is the
  this-crew Gateway MCP URL (https); runtime prefers that over
  `KIROCREW_AGENTCORE_GATEWAY_URL`
- IaC: `install.sh --agentcore`; CFN `AgentCoreGatewayUrl`; systemd
  `KIROCREW_AGENTCORE_GATEWAY_URL`
- CLI: `kirocrew cloud launch --agentcore-gateway-url https://…`
- Calls: `GetWorkloadAccessToken` / `GetWorkloadAccessTokenForJWT`
  via boto3 client name `bedrock-agentcore`
- Gateway spec: URL-only; never a WAT bearer. Workload posture
  rewrites the URL to a `127.0.0.1` SigV4 proxy
  (`platform/agentcore_sigv4.py`, service `bedrock-agentcore`)
- Login inbound: companion IdP JWT on `principal.user_jwt` when
  present; otherwise a URL-only sidecar so kiro-cli can start its
  MCP OAuth challenge (`_kiro.dev/mcp/oauth_request`)
- `status()` contains no token material
- Settings catalog: `platform/agentcore_inspect.py` + owner-only
  `GET`/`POST /api/agentcore/gateway` (verify, targets, tools/list,
  optional Sync). Instance inspect SID on `gateway/*`. Launcher
  Policy.json still has no inspect / Invoke verbs.
- Do **not** register `kirocrew.plugins` (that flips enterprise
  profile / fail-closed)

Still companion-owned (other repo):

- Operator IdP JWT → `annotate_principal.user_jwt` (optional now;
  public login uses kiro-cli MCP OAuth when the JWT is absent)
- Gateway + target + OAuth provider control plane

---

## Execution order and stop points

1. Land PR 1 (this documentation).
2. Land PR 2 before anything consumes the slot.
3. Land PR 2b next (Policy.json + boundary + login withhold). It
   does not wait on a vend path.
4. Land PR 3 before any vend call.
5. Run PR 4 **before** writing PR 5 code. If the header probe is
   inconclusive, stop and update the RFC rather than guessing.
   PR 5's `workload` IAM path does not depend on the header probe;
   only the `login` JWT sidecar / proxy does.
6. PR 6 can overlap PR 5 once the injection tests exist, but do not
   merge consent UI that points at a server the inject path cannot
   attach.
7. Task 7 (this extra) can land on the same Protocol as PR 2. Do not
   start a companion package to fetch tokens.

**Do not implement Phase 5 of the RFC** (Crew-direct
`GetResourceOauth2Token`) in this stack.

## Verification before calling a phase done

- Public tree still has zero `bedrock-agentcore` SDK imports (boto3
  in the extra module only, lazy).
- `DefaultAgentIdentityProvider.enabled()` is False and no Gateway
  server appears in a standalone `rebuild_agent_config()`.
- `iam.policy_json()` still has no `InvokeGateway` /
  `GetWorkloadAccessToken*` action.
- `iam.boundary_policy_document()` is byte-stable vs the pre-PR
  SSM-core + source-bucket shape.
- Login-posture rebuild fixtures contain managed `kirocrew-*` only
  (no Kiro global, no crew-store leftover).
- `kirocrew.json` fixtures contain no `Authorization` header.
- Injected cron / subagent envelopes cannot vend a user token.
- `scripts/docs-lint.sh` and
  `BRAND_BASE_REF=origin/main python3 scripts/check_brand_name.py`
  are clean on the files you added.
