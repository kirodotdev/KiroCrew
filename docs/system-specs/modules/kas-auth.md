# KAS-Mode Auth Module

> Status: implemented (pre-integration). The auth subsystem exists under
> `src/kiro_crew/auth/` with unit tests; it is not yet wired into a live KAS-embedded
> runtime (that runtime does not exist in this tree — `bridge.py` is the seam it will
> bind to). Until KAS mode ships, `agent.provider` remains `acp` and kiro-cli owns
> login; this spec describes what replaces that dependency.

## Why this exists

In the kiro-cli runtime, Kiro Crew never held Kiro credentials of its own: every ACP
backend delegated auth to the `kiro-cli` process it spawned, and the KAS bridge
obtained tokens by calling back into `kiro-cli chat _ get-kas-token`. In **KAS mode
there is no kiro-cli**, so Kiro Crew must perform the entire Kiro OIDC lifecycle
itself: interactive login, token refresh, secure storage, and identity
classification — then feed the result to the embedded KAS engine.

This is the last dependency severed when moving from "spawn kiro-cli" to "embed KAS".

## Scope

In scope: obtaining and maintaining a valid Kiro access token for the four identity
types Kiro supports, across the three Kiro Crew install shapes, and handing it to KAS.

Out of scope: the KAS runtime itself, model routing, governance evaluation (KAS owns
that, keyed off the `provider` field this module supplies).

## The KAS contract (what we must produce)

Confirmed against `@kiro/agent` source (`packages/kiro-agent/src/server/`). KAS
consumes a token through one of two injection points; both take the same logical
contract:

1. **Library injection (preferred, in-process KAS).** `KiroAgentOptions.authProvider`
   is a required constructor field of type `IAuthProvider`
   (`src/index.ts` exports `IAuthProvider`). Interface:

   ```ts
   interface IAuthProvider {
     getToken(): Promise<string>;                 // may trigger refresh
     getProfileArn(): Promise<string | undefined>;
     isAuthenticated(): boolean;
     readToken(): { authMethod?: string; provider?: string } | undefined; // no refresh
     region?: string;
   }
   ```

   Built-in providers also implement `RequestCredentialResolver.resolveRequestCredential():
   Promise<{accessToken, profileArn, provider}>` (an atomic snapshot used by the BFF
   header middleware `addKiroAuthHeaders`). Our implementation SHOULD implement it too,
   so the full request path is exercised.

2. **acp-callback.** KAS calls back `_kiro/auth/getAccessToken` (empty request body).
   Response shape (`packages/acp-type-covenant/capabilities/auth/get-access-token.ts`):

   ```ts
   { accessToken: string;            // required
     expiresAt: string;              // required ISO-8601, MUST be > now + 3min
     profileArn?: string;            // SHOULD; missing -> region falls back us-east-1 + warn
     authMethod?: string;            // 'external_idp' -> TokenType header; else omit
     provider?: string; }            // governance classification (see below)
   ```

Required fields either way: **`accessToken`** + **`expiresAt` (delivered ≥ now+3min)**.

### Identity classification — the `provider` field

`provider` drives KAS governance (`governance-service.ts`) and the `X-Kiro-Idp`
header. Getting it wrong misroutes governance and can fail-closed. Mapping:

| Kiro Crew login | `provider` value | governance-enterprise? | notes |
|---|---|---|---|
| AWS Builder ID | `BuilderId` | no | MUST be set; empty → misclassified |
| Social — Google | `Google` | no | |
| Social — GitHub | `Github` | no | |
| IAM Identity Center | `Enterprise` | **yes** | `profileArn` mandatory (region routing) |
| External IdP federation | `ExternalIdp` | **yes** | `authMethod: 'external_idp'`, needs `tokenEndpoint` for refresh |
| Amazon internal | `Internal` | no (but admin-billing) | |

`profileArn`: mandatory for enterprise/IdC (feeds `X-Kiro-Profile-Arn`, and its 4th ARN
segment is the region source). Optional for Builder ID / social (falls back to
us-east-1). Social device-flow uses a shared default profile ARN.

## Login flows (four identities × two transports)

The wire contract below is reverse-engineered from kiro-cli source
(`crates/chat-cli/src/auth/{portal.rs, social.rs, builder_id.rs, external_idp.rs,
oauth_callback.rs, pkce.rs, consts.rs}`) and **validated end-to-end for social device
flow** (a real Google token was obtained with a stdlib-only client, no kiro-cli — see
PoC note below).

### Transport selection (per install shape)

The single most important design rule: **detect the install shape and pick the
transport that can receive the result.** kiro-cli already does this with `is_remote()`.

| Install shape | Browser vs gateway | Transport | Rationale |
|---|---|---|---|
| Desktop app / native local | same machine | **loopback callback** | browser hits `localhost:<port>`, gateway is listening there |
| Local container (Docker) | browser on host, gateway in container | **loopback callback**, requires `-p 127.0.0.1:<port>:<port>` mapping | callback must be port-mapped into the container |
| Remote host (VPS / cloud / remote desk) | browser on laptop, gateway remote | **device code** | remote `localhost` ≠ laptop `localhost`; device flow needs no callback port |

Fallback rule: if the loopback bind fails or the shape is remote/headless, use device
code. Device code works everywhere, so it is the safe default when detection is
uncertain.

### Social (Google / GitHub)

Brokered entirely by Kiro's servers — there is **no independent OAuth client** we can
stand up, but there is also **no per-app secret to obtain**. The only client
identifier sent is the literal User-Agent `"Kiro-CLI"`. We replicate the flow by
driving Kiro's own portal/service exactly as the CLI does. This works only as long as
Kiro's server keeps accepting the `kirocli` portal contract and the allowlisted ports.

Endpoints (host: `https://prod.us-east-1.auth.desktop.kiro.dev`, portal:
`https://app.kiro.dev`; both overridable — `KIRO_AUTH_PORTAL_URL`, setting
`ApiKiroAuthService`):

- **Loopback:** open
  `GET {portal}/signin?state=…&code_challenge=…&code_challenge_method=S256&redirect_uri=http://localhost:<port>&redirect_from=kirocli`;
  listen on a **Cognito-allowlisted port** (see below); portal redirects to
  `/oauth/callback?login_option=<google|github>&code=…&state=…`; then
  `POST {host}/oauth/token` with `{code, code_verifier, redirect_uri}` where
  `redirect_uri` MUST equal `http://localhost:<port>/oauth/callback?login_option=<p>`
  (path + login_option query, exactly as rebuilt in portal.rs).
- **Device code:** `POST {host}/oauth/device/authorization`
  `{clientId:"Kiro-CLI", loginProvider:"Google"|"Github"}` →
  `{deviceCode, userCode, verificationUri, verificationUriComplete,
  expiresInMilliseconds, intervalInMilliseconds}`; user approves
  `verificationUriComplete`; poll `POST {host}/oauth/device/poll`
  `{deviceCode, clientId:"Kiro-CLI"}` until `status:"authorized"`.

Response (camelCase): `{accessToken, refreshToken, expiresIn, profileArn}`. A non-empty
`profileArn` is mandatory. **Device poll may omit `expiresIn`** — default to 3600s (as
kiro-cli does) or parse the JWT `exp`.

**Allowlisted callback ports (do not change without auth-service coordination):**
`[3128, 4649, 6588, 8008, 9091, 49153, 50153, 51153, 52153, 53153]`.

### AWS Builder ID / IAM Identity Center — fully independent

Standard AWS SSO-OIDC. **No Kiro-controlled gate.** Runtime dynamic client
registration (`RegisterClient`, `client_type=public`, `client_name="Kiro Crew"`) →
returns `client_id` + `client_secret`; PKCE authorization-code on an arbitrary loopback
port (or device code via `StartDeviceAuthorization`); `CreateToken`. Endpoints:
`https://oidc.<region>.amazonaws.com` (Builder ID region `us-east-1`). Scopes:
`codewhisperer:completions|analysis|conversations`. Start URLs: Builder ID
`https://view.awsapps.com/start`; internal `https://amzn.awsapps.com/start`.

### External IdP (enterprise SSO) — independent

Portal returns IdP metadata (`issuer_url`, `client_id`, `scopes`, `login_hint`,
`audience`); run standard authorization-code against the customer's own IdP; refresh at
the token's own `tokenEndpoint` with `grant_type=refresh_token`. Ensure
`offline_access` scope for refresh.

## Refresh

Each identity refreshes on its own endpoint; the module owns the refresh and the
cross-process lock (KAS just re-reads / re-callbacks):

| Identity | Refresh endpoint | Protocol |
|---|---|---|
| Social + Builder ID (social path) | `POST {host}/refreshToken` | JSON `{refreshToken}` → `{accessToken, refreshToken?, expiresIn, profileArn?}` |
| IAM Identity Center | AWS SSO-OIDC `CreateToken` | needs stored `{clientId, clientSecret}` from RegisterClient |
| External IdP | token's `tokenEndpoint` | OAuth `grant_type=refresh_token` |

Refresh window: KAS enters its refresh buffer at **now + 3min**. The module MUST ensure
a token satisfying `expiresAt ≥ now+3min` whenever queried. Use a **single-flight
cross-process lock** (mirror kiro-cli's `refresh_coordinator`) so concurrent sessions
don't stampede the refresh endpoint; re-read the store inside the lock and skip the
HTTP call if a peer already refreshed.

## Storage

Context: kiro-cli does **not** encrypt its tokens — they live plaintext in a SQLite
`auth_kv` table protected only by `0600` file perms. Kiro Crew deliberately does
better, because it faces a threat kiro-cli does not: its own AI agent reads files on
the same machine.

KAS tokens live in a **dedicated `SecretVault` instance** rooted at
`<data_home>/kas` (store `kas/.vault/secrets.enc`, key `kas/.vault/.vault_key`):
per-entry **AES-256-GCM** with per-entry AAD (entry transplant fails decryption),
atomic replace + fsync, cross-process flock, owner-only modes / Windows ACLs — all
provided by `kiro_crew.secrets.SecretVault`, not reimplemented. Entry name = the
identity kind (`social` / `builder_id` / `identity_center` / `external_idp`), value
= the token JSON.

This is deliberately NOT the user-facing secrets vault (`config_dir()`): login
credentials are auto-refreshed session state managed by the login/logout UI, not
user-provided integration secrets. A separate store path and key keeps them out of
the `/api/secrets` panel and keeps delete semantics distinct (logout vs
disconnect-integration).

Defense in depth: the whole `kas` directory (vault included) is a keystone leaf in
`security._CREW_SECRET_LEAVES`, so the agent can neither read nor write its own
credential store — the denylist blocks the tools, and encryption at rest means a
ciphertext-only leak (backup, sync, accidental read) discloses nothing without the
key file. Do not weaken either layer. The threat model is honest about its limits:
a same-UID attacker who can read the key file can decrypt — the vault defends
against agent reads and ciphertext-only leaks, not same-UID malware (same position
as sops/age key files and Ansible vault-password files).

Error split at the store API: an unknown identity kind raises `ValueError` (HTTP
400 at the API layer); a vault read/write failure raises `TokenStoreError` (coded
HTTP 500) — a logout that could not remove the credential never reports success.

Three-source priority when multiple credentials exist (from kiro-cli `auth/mod.rs`):
**External IdP > Builder ID > Social**, then `KIRO_API_KEY` env as final fallback.

## Module shape (proposed)

New subsystem `src/kiro_crew/auth/` (implemented):

```
auth/
  __init__.py
  provider.py        # KasAuthProvider: implements the IAuthProvider contract
  bridge.py          # KAS seam: acp-callback handler + IAuthProvider-shaped mapping
  refresh.py         # per-identity refresh + cross-process single-flight lock
  store.py           # 0600 token file store, three-source priority resolver
  shape.py           # install-shape detection -> transport selection
  login/
    endpoints.py     # endpoint constants (mirror consts.rs; env-overridable)
    portal.py        # loopback callback flow (PKCE + allowlisted-port listener)
    device.py        # social device-code flow (no callback)
    builder_id.py    # AWS SSO-OIDC RegisterClient + device-code
    external_idp.py  # customer IdP authorization-code
```

Wiring: the KAS bridge answers `_kiro/auth/getAccessToken` (acp-callback) from
`KasAuthProvider`, or hands `KasAuthProvider` directly to `KiroAgentOptions.authProvider`
when KAS runs in-process. Refresh tokens never leave Kiro Crew; KAS only ever sees an
access token.

## Security invariants

- Token store paths join `security._SENSITIVE_HOME_DIRS`; agent cannot read/write them.
- Refresh tokens and access tokens never appear in chat surfaces or logs (extend the
  existing credential-redaction floor).
- The child process running any browser-open MUST bind the callback listener to
  `127.0.0.1` only.
- `KIRO_API_KEY` remains a supported headless path (matches KAS `EnvAuthProvider`), and
  its ApiKey identity must fail-fast on usage/credit APIs (existing lesson).

## Validation status

- Social **device-code** flow: **validated end-to-end** — a stdlib-only Python client
  (no kiro-cli) obtained a real Google `{accessToken, refreshToken, profileArn}` via
  `/oauth/device/authorization` → user browser approval → `/oauth/device/poll`. Confirms
  the portal/service accept a non-CLI caller identified only by `User-Agent: Kiro-CLI`.
- Social **loopback** flow: portal `GET /signin` accepts our params (HTTP 200) and
  renders the login page; full loopback token exchange not yet run end-to-end.
- Builder ID / IdC / External IdP: contract implemented from source and covered by unit
  tests with a mocked HTTP session; **not yet exercised against the live endpoints**.
- Unit tests: `test/test_kas_auth_{store,device,flows,helpers,shape_idp}.py` cover the
  store (perms, priority, expiry, invalid-token drops), the device state machine, SSO-OIDC
  polling, all three refresh paths + the single-flight lock + peer recovery, the
  KasAuthProvider contract (incl. the refresh-token-never-leaks assertion), PKCE/URL
  construction, shape→transport selection, external-IdP authorization/exchange, and the
  bridge seam. black / isort / flake8 / mypy clean.

## Not yet done

- Live-endpoint validation of Builder ID / IdC / External IdP (only social device flow
  is proven against the real service).
- The loopback flow's dashboard driver is not built yet: `portal.py` ships the URL/
  port/exchange primitives **and** the callback listener (`wait_for_callback`), but no
  `/api/kas-login` endpoint starts the loopback flow — only the device-code flow has
  begin/poll routes. A desktop (loopback-transport) sign-in therefore has no server
  entry point yet; the chooser must force device transport, or a begin-loopback route
  must land, before the loopback path is wired into the app root.
- Wiring `KasAuthProvider` into an actual embedded-KAS runtime (the runtime and its ACP
  bridge do not exist in this tree yet; `bridge.py` is the seam).
