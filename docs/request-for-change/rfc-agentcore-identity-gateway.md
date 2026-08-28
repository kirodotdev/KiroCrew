---
title: AgentCore Identity and Gateway — Crew agent identity and token vending
status: draft
author: kyle
created: 2026-08-27
last-audited: 2026-08-27
audited-at: 152c00e99
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: AgentCore Identity and Gateway — Crew agent identity and token vending

## Summary

Give each Kiro Crew agent a first-class **Amazon Bedrock AgentCore
Identity** workload identity, and use **AgentCore Gateway** as the
outbound token-vending plane for MCP tools. Crew does not implement
OAuth, RFC 8693, or a token vault. The companion edition registers the
workload, vends tokens, and contributes the Gateway MCP endpoint. The
public core grows generic, default-off seams so a standalone install
stays byte-identical and never imports AWS.

A fleet picks **exactly one** posture in `security_policy.json`, and
that posture is what the copyable cloud **Policy.json pair** must
match — the launcher document (`iam.policy_json()` / `kirocrew cloud
iam-policy`) plus the instance-role fragment
(`iam.agentcore_instance_policy_document`):

- **`workload`** — a deployed Crew instance has an IAM identity and
  can invoke Gateway at boot. No user login required to start.
- **`login`** — only Gateway-approved MCP is vended. That catalog
  **overrides Kiro's default MCP merge**. Tools stay absent until
  the operator logs in.

This is the **identity and credential** plane. It is not the sandbox /
execution plane in the sibling AgentCore sandboxes design.

**Implementation plan:**
[`../superpowers/plans/2026-08-27-agentcore-identity-gateway.md`](../superpowers/plans/2026-08-27-agentcore-identity-gateway.md)

## Motivation

### Current state (verified at `152c00e99`)

Crew has an operator-identity seam and no agent-identity or token-vending
seam.

| Surface | What exists | What it is not |
|---|---|---|
| `IdentityProvider` (`platform/interfaces.py:306`) | SSO status, preflight, MCP credential-watch paths | Not consumed as a principal. `whoami` / `issuer` are **RESERVED** (`platform/context.py:150`) |
| `DefaultIdentityProvider` | Delegates to `sso_status.py` stubs (`available: false`) | No SSO, no JWT, no workload |
| `McpToolingProvider.extra_mcp_servers()` | ADD-only MCP specs merged into `kirocrew.json` | Static at rebuild time. No per-session `Authorization` |
| `kiro_oauth_wire_entry` (`mcp_utils.py:160`) | Translates remote MCP OAuth hints for kiro-cli | Operator-managed client credentials, not AgentCore |
| MCP `headers` on URL servers (`mcp_discovery.py`) | Static headers from config | A baked bearer is a live credential in an agent-readable file |
| Dashboard tokens (`dashboard/token_auth.py`) | HMAC-SHA256, IP-pinned, single-use | Not an OIDC JWT. Cannot satisfy Gateway `CUSTOM_JWT` |
| Channel session keys (`session.md`) | `slack:…`, `dashboard:…`, cron keys | Surface routing, not a cryptographic user identity |
| Governance `SCOPE_CATALOG` | `mcp`, `network.egress`, `capabilities.*` | No AgentCore / token-vend row |
| Credential redaction | AKIA/ASIA floor + bearer-header heuristics | No AgentCore workload-token shape |

Grep of `src/`, `website/src/`, and `docs/` at `152c00e99` finds **no**
`AgentCore`, `bedrock-agentcore`, `GetWorkloadAccessToken`, or
`GetResourceOauth2Token` symbol.

### Problems

1. **The Crew agent has no identity AgentCore can name.** Gateway policy,
   CloudTrail, and the Identity token vault key on a workload identity.
   Today the caller is "whatever IAM role the host has," which cannot
   distinguish `kirocrew` from a sibling agent, a cron job, or a
   compromised local process.
2. **Outbound tool credentials are operator-local secrets.** Slack bot
   tokens, GitHub PATs, and static MCP `Authorization` headers live on
   disk or in env. There is no on-behalf-of exchange, no per-user vault
   binding, and no short-lived scoped token.
3. **A static Gateway URL is not an integration.** An operator can paste
   a Gateway MCP URL into `~/.kiro/crew/mcp.json` today. That carries no
   agent identity, no refresh, no per-session principal, and no 3LO
   consent surface. It also puts a bearer in an agent-readable spec.
4. **Unattended work has no honest principal.** Cron, TaskRunner, and
   subagents are not the operator at a keyboard. ForUserId without a
   trusted derivation is impersonation.

### What AgentCore actually is (product shape)

Two AWS services, not one:

**AgentCore Identity** is the workload identity directory and token
vault.

- A **workload identity** names an agent (`kirocrew`, later
  `kirocrew-<agent_id>`).
- `GetWorkloadAccessToken` / `ForJWT` / `ForUserId` mint an opaque
  **workload access token** bound to `(workload, user)`.
- That token is **first-party only**. It authorizes AgentCore Identity
  APIs (`GetResourceOauth2Token`, vault reads). It is not a Gateway
  inbound credential and must never be sent to a downstream MCP server.
- `GetResourceOauth2Token` vends an OAuth token for a named credential
  provider (`M2M`, authorization-code / 3LO, or `TOKEN_EXCHANGE` /
  on-behalf-of).
- Runtime-managed and Gateway-managed workload identities **cannot**
  call `GetWorkloadAccessToken` themselves. A Crew-owned identity must
  be a **standalone** workload, not the Gateway's.

**AgentCore Gateway** is a hosted MCP endpoint.

- Inbound: `CUSTOM_JWT` (OIDC discovery + JWKS) or `AWS_IAM`. JWT is
  required for OBO; IAM is machine-to-machine and has no user `sub`.
- The Gateway obtains its **own** workload access token and asks
  Identity to vend the outbound credential for the target.
- Outbound grant types: client credentials, authorization code, RFC 8693
  token exchange (`TOKEN_EXCHANGE`).
- Optional Cedar policy engine and request interceptors.

The official [Kiro IDE + AgentCore Gateway](https://builder.aws.com/content/3CS1jTWHngGW3IxFXCjcP2T9l8B/govern-mcp-tools-at-scale-with-kiro-and-agentcore-gateway)
pattern is "IDE presents a developer OIDC JWT; Gateway vends outbound
tokens." Crew is a **local orchestrator**, not Kiro IDE and not AgentCore
Runtime. Runtime auto-injects `WorkloadAccessToken` into hosted agent
code; Crew never runs there, so it must mint its own workload token when
it calls Identity APIs.

## Goals

- Register one standalone AgentCore workload identity for the Crew
  agent, visible in Identity status, CloudTrail, and vault bindings.
- Use AgentCore Gateway as the token-vending plane for approved MCP
  targets. Crew does not implement RFC 8693.
- Bind vault entries to a **trusted** `(workload, user)` pair. Prefer
  `GetWorkloadAccessTokenForJWT`. Use `ForUserId` only with a
  core-derived, partitioned subject.
- Inject Gateway inbound credentials **per session**, never into
  `~/.kiro/agents/kirocrew.json`.
- Public edition remains complete standalone: no AWS SDK, no AgentCore
  import, no new default-on egress, no Cognito/SSO reintroduction.
- Fail closed. A missing companion, expired JWT, or Identity error
  denies the Gateway call. It does not fall back to a shared token.
- Work with the existing cloud Policy.json pair: the launcher
  document (`iam.policy_json`) and the instance permissions
  boundary (`iam.boundary_policy_document`). A deployed Crew can
  only reach Gateway when **both** the grant and the boundary
  ceiling allow it.
- Two exclusive postures (`workload` | `login`), selected by
  `security_policy.json`. `login` withholds Kiro-global and
  crew-store MCP at rebuild time, not only at the tool gate.

## Non-goals

- Hosting Crew on AgentCore Runtime, or treating Gateway as an
  `agent.provider`. `agent.provider` stays `acp`.
- A new OS-sandbox backend or Instances `connection_method`. That is
  the sibling sandboxes RFC.
- Re-adding enterprise SSO, Cognito, Midway, or device-posture tunnels
  to the public core. `sso_status.py` stays a stub. Companion SSO lands
  through the existing `IdentityProvider` slot.
- Crew-side `GetResourceOauth2Token` for arbitrary local tools in v1.
  Gateway vends. Direct Identity vending is a later, narrowly-scoped
  follow-on.
- Making `IdentityProvider.whoami` / `issuer` live. Those stay
  RESERVED. Surface principal data through wired `status()` payloads.
- Bumping `CONTRACT_VERSION`. Pre-launch field and method adds stay at
  `1`, matching `knowledge` / `dashboard` / `jail`.
- A public `config.json` AgentCore block. Agent-writable config cannot
  be the trust root for workload name, gateway URL, or region.
- Re-versioning the immutable `kirocrew-ec2-boundary` in place.
  Widening that document is a new named boundary, opt-in at launch.
- Running both postures at once. `workload` and `login` are
  alternatives, not layers.

## Design

### Target architecture

```
Operator / channel user
        │  dashboard token, Slack user id, companion SSO JWT
        ▼
Kiro Crew gateway (local, ACP → kiro-cli)
        │
        ├─ AgentIdentityProvider.workload_identity()
        │     standalone AgentCore workload "kirocrew"
        │
        ├─ AgentIdentityProvider.vend_workload_access_token(principal)
        │     Identity: GetWorkloadAccessToken*  (see posture)
        │     first-party token; never leaves the gateway process
        │
        └─ inbound to AgentCore Gateway  (MCP)
              posture=workload → instance IAM  (InvokeGateway)
              posture=login    → user OIDC JWT after ensure-login
                    │
                    ▼
         AgentCore Gateway
                    │  Gateway's own workload token
                    ▼
         AgentCore Identity token vault
                    │  GetResourceOauth2Token (M2M / 3LO / OBO)
                    ▼
         Gateway target (Slack, GitHub, internal API, MCP server)
```

The diagram branches on `capabilities.agentcore.posture`. `workload`
is the deployed-box path (IAM inbound, no login). `login` is the
ensure-login path (CUSTOM_JWT, Gateway catalog only).

Two tokens, two audiences, never interchangeable:

| Token | Audience | Who mints it | Who holds it |
|---|---|---|---|
| Workload access token | AgentCore Identity APIs only | Identity (`GetWorkloadAccessToken*`) | Crew gateway process, in memory |
| Gateway inbound JWT | Gateway `customJWTAuthorizer` | Companion IdP / SSO | Injected as `Authorization` on the Gateway MCP transport for that session |
| Outbound resource token | Downstream API | Identity, **called by Gateway** | Gateway; Crew never sees it in v1 |

### Two postures, one Policy.json

The operator-facing document today is `iam.policy_json()` — the
indented JSON the dashboard copies (`GET /api/cloud/iam-policy`) and
`kirocrew cloud iam-policy` prints. It is the **launcher** policy
(CloudFormation, tagged EC2, PassRole, SSM, source bucket, STS). It
is **not** what the running instance can do.

A deployed Crew process assumes the instance role. That role is
capped by the content-fixed permissions boundary
`kirocrew-ec2-boundary` (`iam.boundary_policy_document`): SSM-core +
`s3:GetObject` on `kirocrew-src-<account>-*`. A boundary is a
ceiling, not a grant. Adding `bedrock-agentcore:InvokeGateway` to
the launcher Policy.json alone does nothing for a box that is
already running: the boundary still denies it. The boundary is
create-once and never re-versioned on purpose (a leaked launcher
credential must not be able to `CreatePolicyVersion` it permissive).

So "works with our Policy.json" means three documents stay in
agreement, not one:

| Document | Who applies it | What it is |
|---|---|---|
| `iam.policy_json()` | Operator pastes into IAM for the **launch** principal | Must be able to PassRole an instance role that *may* carry AgentCore |
| Instance role grant (inline or attached) | Launch template / companion | The actual `InvokeGateway` / `GetWorkloadAccessToken*` allow |
| `kirocrew-ec2-boundary` (or a new named successor) | Admin, once per account | Ceiling. Must list any AgentCore action the grant uses |
| `security_policy.json` | Fleet trust root (keystone) | Picks the posture. Agent cannot weaken it |

`capabilities.agentcore` grows an inner posture, still a data row
(`SCOPE_CATALOG` append-only, evaluator untouched):

```json
{
  "capabilities": {
    "agentcore": {
      "enabled": true,
      "posture": "workload"
    }
  }
}
```

`posture` is `"workload"` or `"login"`. Absent / unknown with
`enabled: true` fails closed (boot abort if `boot.fail_closed`,
else treat as disabled). The two postures are exclusive.

#### Deploy assigns the AgentCore identity

A remote Crew launch **creates** the standalone AgentCore identity
in the same CloudFormation stack that creates the instance.

- Resource: `AWS::BedrockAgentCore::WorkloadIdentity` (CDK L2:
  `aws_bedrockagentcore.WorkloadIdentity`). Same resource, two
  front-ends. The public wheel does not depend on `aws-cdk-lib`.
- Name: `kirocrew-<StackTag>` so two crews in one account do not
  collide. Hand-rolled fleets may still use the bare name
  `kirocrew`.
- The instance role receives the posture grant automatically
  (`AWS::IAM::Policy`). Operators do not paste the instance
  Policy.json onto a CFN-launched box.
- systemd gets `KIROCREW_AGENTCORE_POSTURE`,
  `KIROCREW_AGENTCORE_WORKLOAD_NAME`, and optional
  `KIROCREW_AGENTCORE_GATEWAY_URL`. A non-`none` posture installs
  `kirocrew[agentcore]` (`boto3`) via `install.sh --agentcore`.
  Settings PUT and standalone boot also `ensure_extra()` when the
  home policy or env posture is already `workload`/`login`, so a
  box that skipped the CFN flag still gets boto3. The in-repo extra
  (`platform/agentcore_aws.py`) reads those env vars (workload name
  defaults to `kirocrew` when only the policy is set) and the
  Gateway MCP URL from `capabilities.agentcore.gateway_url` (Settings
  / policy first) or `KIROCREW_AGENTCORE_GATEWAY_URL`. It calls
  `GetWorkloadAccessToken*` through a lazy boto3 client.
  Public core never imports the `bedrock-agentcore` SDK package and
  never registers `kirocrew.plugins`.
- Opt-in: `AgentCorePosture=none` (default) is the historical
  launch. `kirocrew cloud launch --agentcore-posture workload`
  creates the AWS identity at deploy time. See-and-configure
  lives on **that crew's** Settings → Security → Agent identity
  (`GET`/`PUT /api/agentcore/identity`), not on the hub's Remote
  Crew launcher. The same pane verifies the Gateway, lists targets
  and the data-plane tool catalog, and can Sync a DEFAULT target
  (`GET`/`POST /api/agentcore/gateway`, owner-only). Instance IAM
  grants inspect on `gateway/*`; Invoke stays on `kirocrew-*`.
  `ListOauth2CredentialProviders` is not on this pane. A fleet
  override or signed policy is refused.
- Stack delete removes the identity. The launcher Policy.json
  grows `CreateWorkloadIdentity` / `DeleteWorkloadIdentity` /
  `GetWorkloadIdentity` / tag verbs, still scoped to
  `kirocrew` / `kirocrew-*`. It still does **not** grow
  `InvokeGateway`.

#### Posture `workload` — deployed Crew can start

Use this on a `kirocrew cloud launch` box (or any companion-managed
host) that should reach Gateway **without** an interactive login.

- Register a standalone workload identity `kirocrew-<StackTag>`
  (not Gateway-managed) via the CloudFormation resource above.
- Gateway inbound is **IAM** (`authorizerType: AWS_IAM`). The
  instance role is the caller. Action:
  `bedrock-agentcore:InvokeGateway` on the Gateway ARN.
- The instance role also needs
  `bedrock-agentcore:GetWorkloadAccessToken` (and, for unattended
  job-owner binding only,
  `GetWorkloadAccessTokenForUserId`) scoped to
  `workload-identity-directory/default/workload-identity/kirocrew`.
- **Deny** `GetWorkloadAccessTokenForJWT` on this role: there is no
  user JWT in this posture, and leaving ForJWT allowed invites a
  planted-token confused deputy.
- Kiro MCP defaults **still merge** (`~/.kiro/settings/mcp.json`,
  crew store, apps). The fleet can still deny them with the
  existing `mcp` ruleset. This posture answers "can the box start,"
  not "is the catalog empty."
- Cron / TaskRunner / injected envelopes run as the workload
  identity. They may call M2M Gateway targets. They must not OBO as
  an arbitrary user.

The copyable **launcher** Policy.json (`iam.policy_json()`) does
**not** grow `InvokeGateway` or `GetWorkloadAccessToken*`. Those
belong on the instance role, not the laptop/CI principal that
pastes the document. The launcher document grows only the verbs
needed to *stand up* an AgentCore-capable box:

- `iam:CreatePolicy` + `iam:GetPolicy` on the **new** boundary
  ARN (`kirocrew-ec2-boundary-agentcore`), still no
  `CreatePolicyVersion` / `Delete*`.
- `iam:CreateRole` `ArnLike` on **either**
  `kirocrew-ec2-boundary` or `kirocrew-ec2-boundary-agentcore`.
- `bedrock-agentcore:CreateWorkloadIdentity` (and Get/Delete/Tag)
  on `workload-identity/kirocrew` and `kirocrew-*`.
- Existing tag-gated `PassRole` of `kirocrew-ec2-*` roles.

The dashboard / `kirocrew cloud iam-policy` grows a **second,
labeled** document — the instance-role fragment — from
`iam.agentcore_instance_policy_document(posture)`. Copying the
launcher JSON onto the instance role, or the instance JSON onto
the launch principal, is a misconfiguration the copy UI must
not invite.

The instance boundary does **not** silently grow. A new named
boundary (`kirocrew-ec2-boundary-agentcore`) is the **union**
ceiling: existing SSM-core + source-bucket read, plus every
AgentCore action either posture's instance grant may use
(`GetWorkloadAccessToken`, `ForUserId`, `ForJWT`,
`InvokeGateway`). A boundary does not grant; the instance
document is still what turns a verb on. One successor name
covers both postures so a fleet can switch `security_policy.json`
without minting a third boundary. New launches opt in; existing
instances stay on `kirocrew-ec2-boundary` and cannot reach
Gateway. No `CreatePolicyVersion` of the original document.

#### Posture `login` — Gateway catalog only, user must sign in

Use this when the approved tool catalog *is* the Gateway, and a
human has to be present.

- Gateway inbound is **CUSTOM_JWT**. Crew attaches the operator's
  IdP JWT after login. The instance role does **not** receive
  `InvokeGateway` or `GetWorkloadAccessTokenForUserId`.
- If Crew must mint a workload token after login, the role may have
  `GetWorkloadAccessTokenForJWT` only, scoped to `kirocrew`.
  ForUserId is **denied** in IAM.
- **Override Kiro defaults at rebuild time.** When posture is
  `login` and `capabilities.agentcore` is enabled,
  `rebuild_agent_config()` withholds:

  - `~/.kiro/settings/mcp.json` (Kiro global)
  - seam-contributed provider globals
  - `~/.kiro/crew/mcp.json` user servers
  - leftover non-managed entries in the merge base

  It still emits the managed Crew servers (`kirocrew-core`,
  `kirocrew-cron`, `kirocrew-computer` when its spec_gate is open)
  and the Gateway URL-only spec. Those are not "Kiro defaults";
  they are Crew's own process. A `@server` ref whose entry was
  withheld mounts nothing — same control as the computer-use
  spec_gate (`mcp.md`).
- Until `vend_gateway_inbound_token` returns a live JWT, the
  Gateway server is **absent** (fail closed). The dashboard/CLI
  login prompt is the only way to make it appear. Companion SSO
  already owns `IdentityProvider` / `/api/sso-login`; this posture
  consumes that login, it does not re-add Cognito to the core.
- Unattended jobs cannot see Gateway tools. Cron on a login-posture
  host is M2M-less and stays on managed Crew servers only.

The login-mode instance document **omits** `InvokeGateway` and
`GetWorkloadAccessTokenForUserId`. Copying the workload-mode
fragment onto a login-mode fleet is a misconfiguration: the
instance could IAM-invoke Gateway and skip the login. The core
detects the mismatch (posture `login` but a successful IAM
InvokeGateway probe, or vice versa) and refuses to emit the
Gateway server, SEL-audited.

#### How the two Policy.json shapes differ

Workload-mode excerpt (instance grant + matching boundary ceiling;
ARNs are fleet-pinned, never `*`):

```json
{
  "Sid": "AgentCoreIdentity",
  "Effect": "Allow",
  "Action": [
    "bedrock-agentcore:GetWorkloadAccessToken",
    "bedrock-agentcore:GetWorkloadAccessTokenForUserId"
  ],
  "Resource": [
    "arn:aws:bedrock-agentcore:*:*:workload-identity-directory/default",
    "arn:aws:bedrock-agentcore:*:*:workload-identity-directory/default/workload-identity/kirocrew"
  ]
},
{
  "Sid": "AgentCoreGateway",
  "Effect": "Allow",
  "Action": ["bedrock-agentcore:InvokeGateway"],
  "Resource": "arn:aws:bedrock-agentcore:*:*:gateway/kirocrew-*"
},
{
  "Sid": "DenyJwtPathOnWorkloadPosture",
  "Effect": "Deny",
  "Action": "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
  "Resource": "*"
}
```

Login-mode excerpt:

```json
{
  "Sid": "AgentCoreIdentityForJwt",
  "Effect": "Allow",
  "Action": ["bedrock-agentcore:GetWorkloadAccessTokenForJWT"],
  "Resource": [
    "arn:aws:bedrock-agentcore:*:*:workload-identity-directory/default",
    "arn:aws:bedrock-agentcore:*:*:workload-identity-directory/default/workload-identity/kirocrew"
  ]
},
{
  "Sid": "DenyUserIdAndIamGateway",
  "Effect": "Deny",
  "Action": [
    "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
    "bedrock-agentcore:InvokeGateway"
  ],
  "Resource": "*"
}
```

`iam.policy_json()` stays the launcher document. A new helper
`iam.agentcore_instance_policy_document(posture)` emits the
instance-role fragment above so the dashboard can copy **the
right Policy.json for the posture in `security_policy.json`**,
not a single kitchen-sink document.

### Edition split

The public core defines the protocol, the session-principal derivation,
the MCP header-injection site, governance, redaction, and SEL. It ships
`DefaultAgentIdentityProvider` (all methods empty / `enabled() ==
False`).

An **opt-in extra** (`kirocrew[agentcore]`, boto3 only) implements
`AgentIdentityProvider` in-tree as `AwsAgentIdentityProvider` so a
deployed box can vend. IaC (`install.sh --agentcore`, the EC2
template) installs it; a later policy or Settings configure
force-installs the same extra into the gateway interpreter.
Bootstrap swaps only `agent_identity` on a standalone profile — it
does not flip to a companion and does not register
`kirocrew.plugins`.

The enterprise companion (separate package, `kirocrew.plugins` entry
point) remains the home for operator IdP JWT annotation, Cognito /
SSO, and Gateway/target control-plane registration.

Dependency stays one-way: companion depends on core. Core never imports
`bedrock_agentcore`, never names Cognito, never hardcodes a discovery
URL. boto3 is imported only inside the extra module's methods.

### New CPP slot: `agent_identity`

A new `AgentIdentityProvider` Protocol on `PlatformContext`, not more
methods on `IdentityProvider`.

`IdentityProvider` is operator SSO (status line, preflight, credential
watch). Agent workload identity and token vending are a different
edition concern. The same reason `AgentCatalogProvider` is not folded
into `McpToolingProvider` applies here.

Pre-launch, a new `PlatformContext` field does **not** bump
`CONTRACT_VERSION` (pinned at `1`; `knowledge` / `dashboard` / `jail`
landed the same way). `DefaultAgentIdentityProvider` keeps a standalone
process byte-identical.

```python
@dataclass(frozen=True)
class WorkloadIdentity:
    name: str
    arn: str

@dataclass(frozen=True)
class SessionPrincipal:
    """Trusted caller. Core-derived; never taken from tool input."""
    surface: str          # dashboard | slack | discord | telegram | …
    subject: str          # already partitioned: "{surface}+{id}"
    session_key: str
    user_jwt: str | None  # set only by the companion after IdP verify

@dataclass(frozen=True)
class InboundToken:
    scheme: str           # "bearer"
    token: str
    expires_at: float     # unix epoch seconds
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

`status()` is display-only: `{enabled, workloadName, gatewayConfigured,
principalBound}` and never token material. The dashboard merges it next
to the existing `IdentityProvider.status()` payload (or a sibling
`GET /api/agent-identity` if merging would confuse the SSO TTL probe).

`whoami` / `issuer` on `IdentityProvider` stay RESERVED. Do not consume
them to satisfy this RFC.

### Session principal (core, trusted)

The core builds `SessionPrincipal` from ground truth it already has.
The adapter may **annotate** (attach a verified JWT). It may not
replace `subject` with a client-supplied user id.

| Surface | `subject` | JWT available? |
|---|---|---|
| Dashboard (companion SSO) | `dashboard+{idp_sub}` | Yes — `ForJWT` (`login` posture) |
| Dashboard (OSS token auth) | `dashboard+{local_owner}` | No JWT. `ForUserId` only in `workload` posture, and only if the local owner is the host principal |
| Slack / Discord / … | `{channel}+{provider_user_id}` | JWT only if companion SSO has bound that channel user (`login`). Else `ForUserId` only in `workload` |
| CLI | `cli+{os_user}` | Same as local owner |
| Cron / TaskRunner | `cron+{job_owner}` (the operator who created the job, persisted at create time) | No interactive JWT. `workload`: M2M Gateway only. `login`: Gateway absent |
| Subagent | inherit parent principal | Same token audience as parent |
| Injected cron / subagent-completion envelopes | **not a user** | Do not mint a user-bound token for an injected message |

`ForUserId` subjects are partitioned `provider_id+user_id` per the
Identity docs, so `slack+U0123` and `dashboard+U0123` cannot collide in
the vault.

IAM on the instance role denies the path the other posture uses
(`ForJWT` denied in `workload`; `ForUserId` and `InvokeGateway`
denied in `login`). The core still prefers `user_jwt` when
`annotate_principal` set one.

### Gateway attach and per-session header injection

`gateway_mcp_spec()` / `extra_mcp_servers()` contribute a **URL-only**
remote MCP entry: endpoint, protocol, no `Authorization`. In
`login` posture the rest of the MCP merge is withheld first
(see *Two postures, one Policy.json*); Gateway is then the only
non-managed remote server that rebuild may emit.

The missing core seam is per-session header injection at MCP spawn,
analogous to `mcp_gateway` declared-env forwarding
(`security.md` § pooled-backend declared-env):

1. Session start resolves `SessionPrincipal` and calls
   `vend_gateway_inbound_token`.
2. On miss or expiry, the Gateway server is **absent** for that session
   (not present with an empty header). Fail closed.
3. The bearer is written to a `0600` session sidecar that kiro-cli /
   the MCP stub reads as transport headers. It is never merged into
   `~/.kiro/agents/kirocrew.json`.
4. The Gateway server is **unpooled** in v1 (`pool_identity` would have
   to include the bearer, which re-partitions on every refresh and
   leaks a credential into a hash input). Per-session spawn is the
   honest cost.
5. `IdentityProvider.credential_watch_paths()` stays the mcp_gateway
   pooled-backend watcher. Gateway is unpooled, so inbound-token expiry
   drains that session's ACP child (`drain_expired_gateway_transport` →
   `SessionManager.remove`) instead of `pool.drain_all_to_bluegreen`.

A local token-proxy MCP (Crew attaches the header, then forwards) is
the unused fallback. Phase 0 did not run a live kiro-cli header probe;
v1 ships the `0600` session sidecar and injects it on `session/new`,
the same pattern as MCP-gateway env sidecars. Tokens never enter the
agent file. The header-proxy MCP is not implemented.

### Token vending path (Gateway, not Crew)

v1 outbound vending is entirely Gateway + Identity:

- Companion registers Gateway targets and OAuth credential providers
  (M2M, 3LO, `TOKEN_EXCHANGE`) in the AgentCore control plane.
- Crew presents the inbound JWT. Gateway exchanges and calls the
  target.
- Crew never calls `GetResourceOauth2Token` in v1.
- Crew never logs, transcripts, or redacts the outbound token because
  it never holds it.

3LO / consent: when Identity returns `authorizationUrl` + `sessionUri`
instead of a token, Gateway fails the MCP call. The companion must
surface that URL on a **human** channel (dashboard modal or the
originating chat thread), never as model-visible "click this" text.
Reuse the existing operator-OAuth allowlist
(`security.py` `_load_operator_oauth_endpoints` /
`oauth_endpoints.json` keystone) so a consent URL to an unknown host
is refused.

### Unattended jobs

Cron and TaskRunner have no interactive JWT. Posture decides whether
they can see Gateway at all.

v1 policy:

- **`workload`.** Gateway targets whose credential provider is
  **M2M** (client credentials, no user) may run unattended, under
  the job-owner / instance identity. OBO / 3LO targets are
  **denied** on unattended sessions unless a still-valid vaulted
  user token already exists for that `(workload, job_owner)` pair.
  There is no silent refresh via a guessed user id.
- **`login`.** Unattended sessions never receive a Gateway inbound
  JWT, so the Gateway server stays absent. Cron stays on managed
  Crew servers only. That is the point of the posture: a human had
  to log in.
- Injected `[Cron notification]` / `[Subagent completion event]`
  messages do not mint a new principal. They run as the job/parent
  already bound.

### Governance, keystone, redaction

- New `SCOPE_CATALOG` row `capabilities.agentcore` with
  `capability_default=False` (opt-in, like `capabilities.publish` /
  `capabilities.messaging`). The inner `posture` field
  (`workload` | `login`) is policy data, not a second scope.
  Data row only; evaluator untouched; `CONTRACT_VERSION` untouched.
- The capability gates: contributing the Gateway MCP server, calling
  either vend method, and surfacing 3LO consent. `login` also
  withholds the default MCP merge at rebuild. `network.egress` still
  bounds the Gateway host. `mcp` still bounds the server/tool identity.
- No `agentcore.json` in public `config.json`. Companion configuration
  lives in the companion. If a later public opt-in needs a file, it is
  a keystone path in `security._SENSITIVE_HOME_DIRS` (read **and**
  write, including extract verbs), next to `oauth_endpoints.json`.
- Workload access tokens and Gateway inbound JWTs are bearer material.
  Existing HTTP-bearer redaction covers the wire shape; the companion
  `CredentialPolicy` overlay may add AgentCore-specific prefixes. Tokens
  never enter SEL payloads, transcripts, or `status()`.
- SEL events (grant and deny): `agentcore.workload_token`,
  `agentcore.gateway_inbound`, `agentcore.consent_url`,
  `agentcore.unattended_denied`, `agentcore.posture_mismatch`.
  No token bytes, no raw JWT.

### Dashboard and CLI

- Status only in v1: workload name, enabled, posture, whether this
  session has a bound principal, Gateway configured. No token
  display, no "copy bearer."
- Copy Policy JSON offers the launcher document and, when
  `capabilities.agentcore` is enabled, the posture-correct
  instance fragment. Never one kitchen-sink document.
- 3LO consent is a modal / channel prompt with the allowlisted URL.
- User-facing strings go through the i18n catalog
  (`website/docs/i18n-catalog.md`). Backend non-2xx bodies carry a
  machine-readable `code`.
- No emojis. `lucide-react` + `lucide-inline`.

## Migration plan

Each phase is independently shippable and abandonable. Exit criteria
are assertions, not dates.

### Phase 0 — Probe (this repo, no product surface)

Answer two questions and write the verdict into this RFC:

1. Can kiro-cli take per-session `Authorization` headers for a URL MCP
   server without writing them into the rendered agent JSON?
2. Does a standalone (non-Gateway-managed) workload identity in the
   target account accept `GetWorkloadAccessTokenForJWT` from the
   companion's IAM principal?

**Verdict (1):** sidecar path. A live kiro-cli per-session header probe
did not run. Crew already writes `0600` env sidecars for pooled MCP
and injects servers on `session/new` without touching
`~/.kiro/agents/`. Inbound JWTs use that same contract: URL-only spec
at rebuild for `workload`; login vends onto
`<data home>/agentcore-inbound/<digest>.json` and
`AcpClient._pooled_mcp_servers` appends the entry. The local
header-proxy MCP is not implemented.

**Verdict (2):** in-repo extra when opted in. Public Default stays a
no-op. `AwsAgentIdentityProvider` calls `GetWorkloadAccessToken` /
`GetWorkloadAccessTokenForJWT` through boto3 (`bedrock-agentcore`
client name, not the `bedrock-agentcore` SDK). CFN creates the
standalone `AWS::BedrockAgentCore::WorkloadIdentity`. WAT is still
first-party only — never Gateway inbound. Login inbound is the
operator IdP JWT on `principal.user_jwt`. Workload Gateway inbound
is IAM: `gateway_mcp_spec()` returns a `127.0.0.1` SigV4 proxy
(`platform/agentcore_sigv4.py`, service `bedrock-agentcore`) so
kiro-cli never presents an unsigned Gateway URL. A WAT is still
never a Gateway bearer. Login without a companion JWT attaches a
URL-only sidecar so kiro-cli can run its MCP OAuth challenge.

Exit: both answers recorded here. Phase 3 is blocked on (1). Phase 2
is blocked on (2). If (1) is no, implement the local header-proxy MCP
instead of kiro-cli header injection.

### Phase 1 — Core seams, public no-ops

Add `AgentIdentityProvider`, `DefaultAgentIdentityProvider`,
`PlatformContext.agent_identity`, bootstrap wiring, CPP coverage tests,
`capabilities.agentcore` (default off, with a `posture` field), and
spec updates (`platform-context.md`, `governance.md`). No AWS
dependency. Standalone behavior byte-identical.

Exit: `test_platform_cpp_seam_coverage.py` lists the new slot;
`enabled()` is False; unknown/absent posture with `enabled: true`
fails closed; no `bedrock-agentcore` SDK import under
`src/kiro_crew/` (boto3 stays inside the opt-in extra).

### Phase 1b — Policy.json pair, boundary successor, login withhold

Public-core JSON only (no AgentCore SDK):

- `iam.agentcore_instance_policy_document(posture)` emits the
  instance-role fragment for `workload` or `login`.
- New named boundary `kirocrew-ec2-boundary-agentcore` (SSM-core +
  source-bucket read + the AgentCore ceiling for that posture).
  Original `kirocrew-ec2-boundary` stays byte-identical.
- Launcher Policy.json / CloudFormation `AllowedPattern` accept
  **either** boundary name. Still no `CreatePolicyVersion`.
- Dashboard / `kirocrew cloud iam-policy` can copy the
  posture-correct instance document as a labeled sibling of the
  launcher document.
- `rebuild_agent_config()` withholds Kiro-global, seam-global,
  crew-store, and leftover non-managed servers when posture is
  `login` and the capability is on. Managed `kirocrew-*` still
  emit. Gateway stays out of the agent file; attach writes a session
  sidecar (bearer JWT or URL-only OAuth challenge).
- Posture-vs-IAM mismatch probe fails closed (no Gateway emit,
  SEL-audited).

Exit: copying `iam.policy_json()` never grants `InvokeGateway`;
the instance helper for `login` denies `InvokeGateway` and
`ForUserId`; a login-posture rebuild fixture contains no Kiro
global server; the original boundary document is unchanged.

### Phase 2 — Session principal + Identity vend (in-repo extra)

Core derives `SessionPrincipal` at session start and never accepts a
tool-supplied user id. `AwsAgentIdentityProvider` (opt-in extra)
implements `vend_workload_access_token` / `status()` /
`gateway_mcp_spec()`. `annotate_principal` stays a pass-through
until a companion fills `user_jwt`.

Exit: extra tests (mocked boto3) mint a workload token; `status()`
has no token keys; injected messages do not vend.

### Phase 3 — Gateway MCP attach + inbound token injection

Companion (or the in-repo extra) contributes the Gateway URL. Core
injects inbound per session: a JWT sidecar, a URL-only OAuth-challenge
sidecar, or — on workload — the localhost SigV4 proxy URL in the
rebuilt agent file. Unpooled. Expiry drains a bearer sidecar's ACP
child (not the mcp_gateway pool). Fail closed on a missing URL.

Exit: a login session with a Gateway URL lists the server (bearer or
OAuth challenge); a session without a URL does not; `kirocrew.json`
contains no `Authorization` header.

### Phase 4 — Human 3LO consent + unattended policy

Consent URL allowlist + dashboard/channel prompt. Unattended jobs
restricted to M2M or vaulted-owner tokens. SEL events land.

v1 implementation: `security.allow_agentcore_consent_url` reuses
`oauth_endpoints.json` (no second keystone). Settings → Security shows
an allowlisted GET `/api/agentcore/consent` link; unknown hosts return
403 `consent_host_refused`. Login never attaches Gateway for `cron:` /
`taskrunner:` keys. Workload user/OBO without
`status().vaultedOwnerToken` writes a deny sidecar (`disabled: true`).
SEL `agentcore.consent_url` / `agentcore.unattended_denied` log
host+path / session+subject only — never token bytes.
`GetWorkloadAccessToken*` is the `kirocrew[agentcore]` extra
(Task 7), not Phase 5 `GetResourceOauth2Token`.

Exit: an unknown consent host is refused; a cron job cannot OBO as an
arbitrary user.

### Phase 5 (follow-on, not v1) — Crew-direct `GetResourceOauth2Token`

Only if a local tool cannot sit behind Gateway. Same workload token,
same principal rules, same redaction. Do not start this phase to
"complete" v1.

## Backward compatibility

- Standalone / public wheel: no new default imports, no new MCP server,
  no new default-on capability, no config migration. The `agentcore`
  extra is opt-in (`pip install kirocrew[agentcore]` / IaC).
- Companion: additive. A companion that does not override
  `agent_identity` inherits the Default (disabled).
- Existing static remote MCP servers and `kiro_oauth_wire_entry` are
  unchanged.
- `IdentityProvider` signatures unchanged. RESERVED methods stay
  reserved.

## Security considerations

- **Do not send a workload access token to Gateway.** Identity docs:
  first-party only. Gateway inbound is a user JWT or IAM.
- **Do not register a Gateway-managed workload as the Crew identity.**
  Those identities refuse `GetWorkloadAccessToken` from the caller
  ("WorkloadIdentity is linked to a service…").
- **Do not put tokens in agent JSON, transcripts, SEL, or `status()`.**
  Sidecar `0600`, process memory, then drop.
- **Do not take `userId` from the model, a tool argument, or a query
  string.** Core-derived principal only.
- **Do not fail open** to a shared service-account token when JWT
  vending fails.
- **Do not re-introduce Cognito / RUM ids / enterprise SSO** into
  `src/kiro_crew/`. Discovery URLs live in the companion.
- Computer use stays ungoverned and in-band. This RFC does not add
  `computer_use.*` scopes.
- Sensitive-path matchers must cover any later keystone file on both
  read and write/extract verbs.

## Live verification findings (2026-08-28)

Verified in a scratch AWS account in `us-east-1` against a tagged
`kirocrew:e2e=true` stack: an AWS_IAM Gateway (READY), a standalone
workload identity, a Lambda target, and an MCP_SERVER target.

Verified live:

- Control-plane catalog + Settings verify: READY, AWS_IAM, URL match,
  `kirocrew-*` invoke scope, tools/list `echo-hello___echo_hello`.
- Workload SigV4 localhost proxy: MCP initialize, `tools/list`, and
  `tools/call` (`{"message":"hello proxy"}`).
- Unsigned Gateway and WAT-as-`Authorization` both 401. WAT vends
  (opaque). `vend_gateway_inbound_token` without a user JWT is `None`.
- Policy `gateway_url` wins over `KIROCREW_AGENTCORE_GATEWAY_URL`.
- Identity GET/PUT and catalog GET/verify (owner); app tokens 403.
  Lambda Sync returns `not_syncable`.
- Workload rebuild emits the proxy URL only. Login rebuild withholds.
  Unattended login does not attach.

Closed after the run:

- GetGatewayTarget omits `targetType`; `mcp.lambda` was labeled `MCP`.
  Catalog now infers `LAMBDA`.
- Settings-only default name `kirocrew` does not match a created
  `kirocrew-e2e` (`AccessDenied`: identity does not belong to caller
  account). Settings now authors `capabilities.agentcore.workload_name`.
- Login attach used `gateway_mcp_spec()`, which rewrites to the proxy
  when env posture is still `workload`. Login sidecars now always use
  the https Gateway URL.
- Catalog/verify now probes `GetWorkloadAccessToken` and discards the
  body. A standalone `kirocrew-e2e` is `ok`; a Gateway-linked
  service identity is `service_linked`; the default
  `kirocrew` is `identity_denied`. Login skips the probe
  (`login_needs_sign_in`). The token never enters the snapshot.
- Login sidecar `Authorization` is RFC 6750 `Bearer` (live-checked).
- `resolved_posture()` is policy-first like URL and name. Leftover
  env `workload` no longer hides a Settings `login` from catalog
  (authorizer mismatch against this AWS_IAM Gateway is now visible).

Closed on the production-ready pass:

- Owner-dashboard PUT hot-applies the home file onto the running
  ceiling and AWS adapter (`apply_agentcore_runtime`) and rebuilds
  the agent config. `restart_required` stays true only when that
  apply cannot attach the extra.
- Settings-only empty no longer invents `kirocrew`. PUT posture-on
  without a name is 400 `workload_name_required`; the UI disables
  Save until a name is set. Launch env posture without a systemd
  name still uses the RFC default.
- Successor-boundary Invoke stays `kirocrew-*`. Catalog
  `invoke_scope` is now credential-proved: tools/list through the
  SigV4 proxy greens any Gateway this crew can actually call, so a
  pasted existing Gateway is not falsely red. Prefix is the
  fallback when tools were not proved. A data-plane 401/403 is
  `invoke_denied` even on a `kirocrew-*` id.
- Instance-role-only IAM (`kirocrew-e2e-instance` assumed from a
  named IAM user, policy from `agentcore_instance_policy_document(
  "workload")`): WAT `ok`, all nine catalog checks green, tools/list
  and `SynchronizeGatewayTargets` accepted. Admin keys were not in
  that process.
- MCP_SERVER target `public-docs` (DEFAULT listing)
  is READY. GetGatewayTarget still omits `targetType`; catalog
  infers `MCP_SERVER`, marks it `syncable`, and lists
  `public-docs___ask_question` / `read_wiki_contents` /
  `read_wiki_structure` next to the Lambda tool. A hostname that
  does not resolve fails as `FAILED` with `statusReasons`; Settings
  now shows those reasons on the target row.

Closed on the agent-path honesty pass:

- Catalog / Verify `tools/list` uses `ensure_workload_proxy` and an
  unsigned localhost POST — the same path rebuild writes into
  `kirocrew.json`. A green tools check is no longer a direct SigV4
  to the Gateway hostname. Proxy start failure is
  `proxy_unavailable`. Isolated rebuild of a workload spec persists
  `http://127.0.0.1:<port>/mcp`. Live under assumed-role
  `kirocrew-e2e-instance`: snapshot `via=proxy`, both targets'
  tools listed, `gateway_mcp_spec()` listen URL
  `http://127.0.0.1:<port>/mcp`, and `tools/call`
  `echo-hello___echo_hello` succeeded through that listener.

Closed on the OOTB session-inject pass:

- Workload `attach_gateway_inbound` still clears the sidecar (IAM,
  no JWT). `session_gateway_servers` now injects the live loopback
  SigV4 listen URL when identity is on, so `session/new` outranks a
  stale agent-file port after a gateway restart. A companion https
  spec (unsigned Gateway hostname) still injects `[]`.
- The proxy prefers `127.0.0.1:18765` so a rebuilt `kirocrew.json`
  survives a restart when that port is free.
  `KIROCREW_AGENTCORE_PROXY_PORT` overrides; bind failure falls
  back to ephemeral and session inject carries the live URL.
- Isolated kiro-cli 2.20.1 `session/new` rejects `{name, url}` and
  `{name, disabled: true}` (`untagged enum McpServer`). HTTP inject
  is now `{name, type: "http", url, headers}` (empty headers when
  there is no bearer). Deny retracts with a disabled HTTP
  placeholder. Confirmed: `type: http` + `headers: []` accepts
  `session/new`; the earlier shape closed the connection. Live
  inject+MCP under assumed-role `kirocrew-e2e-instance` bound
  `127.0.0.1:18765`, listed both targets' tools, and
  `echo-hello___echo_hello` returned `hello ootb`.   Isolated
  `KIRO_HOME` did not write `~/.kiro/agents`. Isolated kiro-cli
  2.20.1 then accepted `session/new`, overrode the stale agent-file
  port, launched `agentcore-gateway` over unauthenticated HTTP to
  the SigV4 proxy, and emitted
  `_kiro.dev/mcp/server_initialized`.

Closed on the existing-Gateway honesty pass:

- Catalog `invoke_scope` no longer fails a working non-`kirocrew-*`
  Gateway. If tools/list just succeeded via the SigV4 proxy, this
  credential invoked it. Prefix remains the unproved fallback.
  Settings copy is credential-honest (this crew's AWS credentials),
  not "instance role only." `invoke_denied` names a 401/403 on
  Invoke. IAM documents are unchanged.

Still open (not v1 blockers):

- kiro-cli's hosted MCP OAuth challenge (`Authorize` /
  `_kiro.dev/mcp/oauth_request`) against a `CUSTOM_JWT` Gateway was
  not driven. The product sidecar + a real IdP JWT were.
- MCP initialize on this Gateway does not return `mcp-session-id`;
  `tools/list` still works without one.
- Creating a Gateway also creates a service-linked identity that
  refuses caller WAT. Crew must use a standalone workload identity.
  Settings now names that failure `service_linked`.
- Phase 5 `GetResourceOauth2Token` stays follow-on.
- Lambda targets stay `not_syncable`.
- Desktop/PEP 668 extra install and widening Invoke to `gateway/*`
  stay out of v1.

Closed on the leftover-validation pass (live, 2026-08-28):

- **Desktop / PEP 668.** Host Homebrew Python 3.14 is
  `EXTERNALLY-MANAGED`, not a venv, and has no boto3.
  `python3 -m pip install kirocrew[agentcore]` exits 1 with the
  PEP 668 error (no `--break-system-packages`). The worktree venv
  reports `extra_available` / `extra_code=ok`. Settings
  `no_install_channel` is the correct desktop answer.
- **Phase 5 `GetResourceOauth2Token`.**
  `ListOauth2CredentialProviders` is empty in this account. A WAT
  for standalone `kirocrew-e2e` vends. `GetResourceOauth2Token`
  against a missing provider is `ValidationException` ("invalid
  type or does not exist"). Gateway MCP tools already list and
  call without Crew becoming a token broker. Phase 5 stays out.
- **Invoke `gateway/*`.** Created IAM Gateway
  an existing-customer Gateway (not `kirocrew-*`) + public MCP
  target. Admin/laptop credentials: catalog `invoke_scope` green
  via proxy (`via=proxy`, 3 tools). Assumed
  `kirocrew-e2e-instance`: inspect `ok`, tools `tools_denied`,
  `invoke_denied`. Settings customers with their own creds do not
  need a wider CFN grant. A CFN-launched box still cannot Invoke a
  non-`kirocrew-*` Gateway unless the operator attaches that ARN.
  Successor boundary stays `kirocrew-*`.
- **Login / CUSTOM_JWT.** Tagged Cognito pool `kirocrew-e2e-oidc`
  (test IdP, not product SSO) + Gateway `kirocrew-e2e-jwt`.
  Discovery URL is public. Unsigned initialize is 401. A Cognito
  **IdToken** with Gateway `allowedAudience` = app client id
  initializes 200 and `tools/list` returns the three
  `public-docs___` tools. A Cognito **access** token (no `aud`,
  `token_use=access`) is 403 `insufficient_scope`. Product login
  catalog: authorizer `CUSTOM_JWT`, tools `login_needs_sign_in`.
  Attach without JWT: https sidecar + `oauth_challenge`. Attach
  with the IdToken: `Authorization: Bearer` + session/new HTTP
  inject carries the header. The operator IdP must issue a JWT
  the Gateway's audience/client allow-list accepts — Cognito's
  IdToken does; its access token does not.

## Alternatives considered

### A. Companion-only, no core seam (rejected as the long-term shape)

The companion could inject a Gateway MCP server with a static header
via `extra_mcp_servers()` today. That cannot do per-session OBO,
refresh, or keep the bearer out of `kirocrew.json`. Acceptable as a
manual operator escape hatch; not the integration.

### B. boto3 AgentCore client in the public core (rejected)

Violates the de-Amazoned fork rule, adds an AWS dependency to the
public wheel, and invites hardcoded Cognito/discovery values. The CPP
seam exists so the core never imports this.

### C. Deploy Crew onto AgentCore Runtime (rejected for this RFC)

Runtime auto-vends `WorkloadAccessToken` in the invocation payload.
Crew is a local multi-surface gateway (dashboard, Slack, cron,
subagents) that drives kiro-cli over ACP. Runtime has no inbound
dashboard TCP and is a different product. Revisit only if a hosted
Crew edition is separately designed.

### D. Extend `IdentityProvider` instead of a new slot (viable, not recommended)

v1 method adds on `IdentityProvider` would work and avoid a
`PlatformContext` field. The slot is already SSO-shaped
(`status_line`, `preflight_checks`). Token vending would overload it.
A dedicated protocol matches "one edition concern, one interface."

### E. Crew calls `GetResourceOauth2Token` and skips Gateway (rejected for v1)

Puts OAuth, 3LO, and per-target credential-provider config in Crew.
Gateway already does this, plus Cedar policy and interceptors. Crew
should present identity, not become a token broker.

### F. IAM inbound to Gateway, no JWT (adopted as posture `workload`)

This is no longer a fallback: it **is** the deployed-box posture.
IAM inbound lets a `kirocrew cloud launch` instance invoke Gateway
at boot with no login. The public-core `probe_instance_invoke_gateway()`
is a no-op that returns False; a companion must override it.
Fail-closed there means no mismatch was detected, not that IAM inbound
is impossible. It drops user `sub`, so OBO and per-user
vault binding are unavailable on that path — cron is M2M-only, and
the instance Policy.json **Denies** `ForJWT`. Interactive
per-user vending is the other posture (`login` / CUSTOM_JWT),
not a layer on top of this one. A fleet that wants both must
run two hosts (or switch `security_policy.json` and relaunch
with the matching boundary + instance grant).

## Open questions

1. **kiro-cli per-session headers (Phase 0).** Resolved: sidecar path.
   Tokens never enter the rendered agent JSON. `session/new` injects
   the inbound sidecar. Header-proxy MCP not implemented.
2. **One workload vs per-agent-config workloads.** v1 is one
   `kirocrew` workload. A later `kirocrew-<agent_id>` split is
   additive (new identities, same protocol).
3. **Dashboard route.** Resolved: sibling
   `GET`/`PUT /api/agentcore/identity` on **this crew's** Settings →
   Security. Not `GET /api/sso-ttl`, and not the hub Remote Crew
   launcher. SSO TTL stays an SSO probe.
4. **Channel-user SSO binding.** How the companion proves a Slack user
   *is* the IdP `sub` is a companion concern. This RFC only requires
   that the proof happen before `user_jwt` is set.
5. **Sibling sandboxes RFC.** If that design lands a Crew-driven
   Code Interpreter session, it must use this RFC's workload identity
   rather than minting a second one.
6. **Boundary successor vs parameterized name.** v1 uses a second
   exact name (`kirocrew-ec2-boundary-agentcore`) so the original
   document stays immutable. Confirm the CloudFormation
   `PermissionsBoundaryArn` `AllowedPattern` can list both names
   without weakening the create-once story.
7. **Launcher document vs instance document in the dashboard.**
   Today `GET /api/cloud/iam-policy` returns one blob. v1 adds a
   labeled sibling rather than merging AgentCore actions into the
   launcher JSON. Confirm the copy UI copy can show two documents
   without operators pasting the instance grant onto the launch
   principal.

## Related

- [`platform-context.md`](../system-specs/modules/platform-context.md) —
  CPP seam, RESERVED methods, `CONTRACT_VERSION` pin
- [`security.md`](../system-specs/modules/security.md) — keystone,
  redaction, MCP env forwarding
- [`governance.md`](../system-specs/modules/governance.md) —
  `SCOPE_CATALOG` append-only
- [`mcp.md`](../architecture/mcp.md) — MCP merge, `spec_gate`
  withhold, stateless tools
- [`cloud.md`](../system-specs/modules/cloud.md) — launcher
  Policy.json, immutable `kirocrew-ec2-boundary`, create-once
  `CreatePolicy` (never `CreatePolicyVersion`)
- [`injected-messages.md`](../system-specs/common/injected-messages.md)
  — cron / subagent envelopes are not the user
- [Get workload access token](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/get-workload-access-token.html)
- [GetResourceOauth2Token](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_GetResourceOauth2Token.html)
- [On-behalf-of token exchange](https://aws.amazon.com/blogs/machine-learning/implement-on-behalf-of-token-exchange-for-multi-tenant-agents-with-amazon-bedrock-agentcore-gateway/)
