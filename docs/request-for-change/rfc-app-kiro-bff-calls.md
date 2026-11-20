---
title: "Kiro BFF calls for apps: the gateway calls approved operations with the user's Kiro sign-in"
status: draft
author: buluoray, with kirocrew-worker
created: 2026-11-19
last-audited: 2026-11-19
audited-at: 62a48d2381
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Kiro BFF Calls for Apps

> **Status:** `draft`. Nothing in this document is built, and it changes no
> code. Acceptance is requested from a maintainer. The baseline was verified
> at `62a48d2381`: no App Kit seam reaches the Kiro sign-in. The gateway's
> vault is read only by core code (`kiro_crew.auth.bridge.default_token_store`,
> `kiro_crew.auth.provider.KasAuthProvider`), and no grant family for Kiro
> operations is registered in `src/kiro_crew/apps/point_grants.py`.

## Summary

Add one App Kit seam. An app names, in its manifest, the Kiro BFF operations it
needs. The owner approves that list as a staged grant. The gateway then calls an
approved operation for the app, with the user's own Kiro sign-in, and returns
only the operation's output. The token never leaves the gateway process.

```
app backend ──app token──▶ POST /api/kiro/operations/<Op> ─┐
in-gateway hook ──────────▶ await ctx.kiro_call(<Op>, input) ┤
                                                             ▼
                       gateway: grant check → vault token → BFF of the
                       profile's region → output (JSON) back to the app
```

Chat tools reach the seam through the app's own `agentTools`, which the
accepted [rfc-app-agent-tools-third-party-grant.md](rfc-app-agent-tools-third-party-grant.md)
already gates. This document does not change that path.

## Motivation

### Current state

The Kiro BFF is the web API that `app.kiro.dev` serves. It speaks Smithy RPC v2
CBOR: one `POST <origin>/service/<service>/operation/<Op>` with a CBOR body, the
`smithy-protocol: rpc-v2-cbor` header, and the caller's Kiro bearer token. The
origin depends on the profile's region: `https://app.kiro.dev` for
`us-east-1` and `https://eu-central-1.app.kiro.dev` for `eu-central-1`.

Kiro features that serve customers put their API behind this BFF. An app for
such a feature wants to call a fixed set of its operations as the signed-in
user. On main it has three routes, and none of them is supported:

1. **Import core internals.** A `backend.hooks.routes` hook runs in the gateway
   process, so it can import `kiro_crew.auth` and read the vault. App Kit says
   absolute `kiro_crew` imports "are for built-in apps only"
   (`docs/app-kit/manifest-reference.md`). Such a hook holds the user's whole
   Kiro token and can send it anywhere. It also breaks when those internals
   move.
2. **Give the app's agent MCP server a credential.** The stdio `mcpServers`
   that an app's agents declare "get neither a secret nor an app token"
   (`docs/app-kit/api-reference.md`, Backend Environment Variables). That is
   by design: they run in the agent sandbox.
3. **List chat tools on `kirocrew-core`.** `src/kiro_crew/apps/app_agent_tools.py`
   reads `agentTools` for first-party apps only. The third-party path is
   accepted but not built.

### Problems

1. No supported way exists for an app to act as the signed-in user against the
   Kiro BFF.
2. The unsupported way hands an app the raw token, which grants every BFF
   operation, every other Kiro API the token is good for, and any host the app
   sends it to.
3. The owner never sees or approves which Kiro operations an app uses.

## Goals

- An app calls a Kiro BFF operation with the user's Kiro sign-in, and receives
  only that operation's output.
- The token, the refresh token and the identity headers stay in the gateway
  process.
- The owner approves the exact operation names, and an update that adds one is
  held until the owner approves it.
- The gateway forwards only approved operations, by exact name.
- The gateway chooses the endpoint from the signed-in profile. The app never
  chooses a host, a path prefix or a header.
- Callers are the app's own backend (with its app token) and the app's
  in-gateway hooks (through `ctx`).

## Non-goals

- Any Kiro API other than the BFF's one RPC v2 CBOR service.
- A choice of identity. The seam uses the gateway's own Kiro sign-in, the one
  in Settings → Kiro account. It never uses kiro-cli's login and never the
  `KIRO_API_KEY` environment bypass.
- Calls from the dashboard page with the owner's session. A page calls its own
  backend, and the backend calls this seam.
- A credential for an app's stdio `mcpServers`. They stay without one.
- Redesigning third-party `agentTools`. Section 6 says how the two compose.
- Streaming operations, and input values that JSON cannot carry (CBOR byte
  strings and timestamps). See Open questions.

## Design

### 1. Manifest: `permissions.kiroOperations`

```json
"permissions": {
  "kiroOperations": ["GetWidget", "ListWidgets", "UpdateWidget"]
}
```

- A list of operation names. Each name is 1 to 64 ASCII characters, the first an
  upper-case letter and the rest letters or digits. The parser checks each
  character in turn. It uses no pattern match.
- The parser drops any other entry and keeps each name once, in manifest order.
  It never widens or rewrites a name.
- No wildcard, no prefix form and no service name. The service is fixed by the
  gateway (section 4).

### 2. The grant family

Register one family, `kiroOperations`, with `register_permission_grants`
in `src/kiro_crew/apps/point_grants.py`, `explicit_only=True`. One entry is one
operation name. It joins `STAGED_GRANT_FAMILIES` (`apps/manager_records.py`)
through `point_grant_families()`, so no install, update or approval path needs
new code for the widening rule:

- **Install.** The install screen lists each declared operation under the
  heading "Kiro operations this app calls as you". The install consent approves
  the list shown, as it does for `permissions.api`.
- **Update.** `_approved_grants_after_manifest_change` keeps only names that
  the new manifest still declares. A new name is held back. The app stays
  enabled, and its approved names keep working.
- **Approval.** The owner approves held-back names on the app detail page,
  through `enable_app(grants_consent=...)`, like any staged family.
- **Older records.** `explicit_only=True` means a stored set written before
  this family existed approves no operation. An app that declared the key
  early never reached the owner's eyes, so it gets nothing until approved.
- **Revoke.** The owner can untick a name on the detail page. The next call
  is refused.

The grant file lives in `member-records/app-grants/<app>.json`
(`apps/protected_app_state.py`). The OS sandbox masks it, so an app cannot
approve its own operations.

The reader is one function, `app_kiro_operations(app_name) -> frozenset[str]`,
modelled on `app_bot_scopes` (`apps/bot_grants.py`). It returns the declared
names that the stored set approves, read fresh on each call, and an empty set
when the app is not installed, not enabled, or unreadable.

### 3. Callers

#### 3a. The app's backend

```
POST /api/kiro/operations/{operation}
Content-Type: application/json
Cookie / Authorization: the app's app token

{ ...operation input as a JSON object... }
```

- Only an app token is accepted. A dashboard session token, an internal-secret
  caller and an anonymous caller get 403 `app_token_required`. The app comes from
  the verified token (`request["app"]`), never from the path or the body.
- `app_token_path_allowed` (`dashboard/token_auth_app_scope.py`) admits this one
  path for an app token whose `app_kiro_operations` set is not empty. The app
  does not also declare a `permissions.api` prefix: the family is the grant,
  and a second entry would add nothing the owner could decide.
- The answer is `200` with `{"output": {...}}`, the operation's output map.

#### 3b. In-gateway hooks

```python
output = await ctx.kiro_call("GetWidget", {"widgetId": "w-1"})
```

`AppContext.kiro_call` (`apps/context.py`) runs the same core function as the
route, keyed on `ctx`'s own app name. It raises `KiroCallError` with the same
`code`, `message` and `status` as the route's error body. A hook never needs
`kiro_crew.auth`.

Both callers go through one function in a new module, `apps/kiro_calls.py`.
Its order is fixed:

1. Resolve the app from the caller (token or `ctx`).
2. `operation in app_kiro_operations(app)`, exact set membership.
3. Read and check the body (section 5).
4. Resolve the sign-in and the endpoint (section 4).
5. Encode, send, decode, answer.

A refusal at any step sends nothing to the BFF.

### 4. Sign-in and endpoint

- **Sign-in.** `KasAuthProvider(default_token_store(), allow_env_api_key=False)`,
  the same vault and refresh that answer the engine's
  `_kiro/auth/getAccessToken` (`docs/system-specs/modules/kas-auth.md`). It
  renews a token inside the refresh margin under the cross-process lock, and it
  never returns the refresh token.
- **Identity headers.** The gateway builds `Authorization: Bearer <token>`,
  `X-Kiro-Idp` (from the stored provider), `X-Kiro-Profile-Arn` (from
  `kas_profile_arn`) and `TokenType` (from the stored auth method). The app
  sends no header that reaches the BFF.
- **Endpoint.** One fixed table in `apps/kiro_calls.py`:

  | Profile region | Origin |
  |---|---|
  | `us-east-1` | `https://app.kiro.dev` |
  | `eu-central-1` | `https://eu-central-1.app.kiro.dev` |

  The region comes from `profile_region(kas_profile_arn(token))`
  (`auth/login/control_plane.py`). A sign-in with no profile ARN uses
  `us-east-1`, the same fallback the engine uses. An ARN whose region is not in
  the table is refused with 503 `kiro_region_unsupported`. It is never sent to
  another region's host, because that would move the user's data across a
  region boundary. Adding a region is a one-row change to the table.
- **Service.** One constant names the BFF service in the path. The operation
  name is the only part of the URL a caller supplies, and step 2 has already
  checked it against the approved set.
- **No override.** No app config, manifest key or environment variable changes
  the origin or the service. Tests inject the table.
- **Transport.** HTTPS only, redirects never followed, 30 seconds per call. A
  reply without `smithy-protocol: rpc-v2-cbor` is refused as `kiro_bad_reply`.
- **Renewal.** On a 401 or 403 from the BFF, the gateway renews once with
  `ensure_fresh(..., rejected_access_token=...)` and retries once. It retries
  nothing else: the gateway cannot know whether an operation is idempotent, so
  a 429 or 5xx goes back to the app.

### 5. Bodies

- **Request.** A JSON object, at most 1 MiB, nested at most 32 levels. JSON
  maps onto CBOR maps, arrays, text, integers, floats, booleans and null.
- **Response.** At most 8 MiB of CBOR, checked on `Content-Length` and again on
  the bytes read. The gateway decodes it to JSON: a byte string becomes base64
  text, and a timestamp becomes ISO-8601 text.
- **Codec.** The gateway already has an RPC v2 CBOR codec for the Kiro Space
  client (`cbor_encode` and `cbor_decode` in `apps/market_client.py`). The
  build moves it to one shared module that both use. It adds no dependency.

### 6. Chat tools

A chat tool reaches a Kiro operation like this:

```
agent ─▶ kirocrew-apps (prompted) ─▶ app's agentTools route ─▶ app handler
                                                  └─▶ ctx.kiro_call / POST /api/kiro/operations/<Op>
```

- The app declares an `agentTools` tool whose route is its own handler. The
  handler calls this seam. The model sees the tool's answer, never a token.
- For a third-party app, the call needs four approvals, all failing closed: the
  owner's `agentTools` grant, the agent mounting `kirocrew-apps`, the person's
  per-call prompt (all three from the agentTools RFC), and this document's
  `kiroOperations` grant.
- A first-party app's tools are listed on `kirocrew-core` today and need only
  the `kiroOperations` grant here. If [#39500](https://github.com/kirodotdev/KiroCrew/pull/39500)
  merges, a first-party app's own agent calls its own tools without a prompt.
  An operation that writes then runs unprompted in that app's own chats and
  crons, which matches what that PR grants for every other app tool.
- The app's stdio `mcpServers` still get no credential, and this document adds
  none.

The third-party listing depends on [#17015](https://github.com/kirodotdev/KiroCrew/pull/17015)
and phase 1 of the agentTools RFC. This seam does not wait on them: a backend
and a hook can call it as soon as it ships.

### 7. Errors

The body is always `{"error": {"code", "message"}}`, plus `kiro` for a BFF
error. No status here is 401, because an app client renews its own app token
on a 401 (`on_auth_expired`), and a Kiro sign-in problem is not an app token
problem.

| Case | Status | `code` |
|---|---|---|
| Caller is not an app token | 403 | `app_token_required` |
| App not enabled | 403 | `app_disabled` |
| Name not declared | 404 | `unknown_operation` |
| Declared, not approved | 403 | `operation_not_approved` |
| Body too large / too deep | 413 | `body_too_large` |
| Body not a JSON object | 400 | `invalid_json` |
| No Kiro sign-in stored | 409 | `kiro_sign_in_required` |
| Sign-in could not be read or renewed | 503 | `kiro_sign_in_unavailable` |
| BFF refused the renewed token | 409 | `kiro_sign_in_refused` |
| Profile region not in the table | 503 | `kiro_region_unsupported` |
| BFF answered an error | 502 | `kiro_operation_failed` |
| Timeout or network error | 504 / 502 | `kiro_unreachable` |
| Reply too large or not RPC v2 CBOR | 502 | `kiro_bad_reply` |

- The three sign-in errors carry the same hint: "Sign in to Kiro in Settings →
  Kiro account." They name no value from the vault. A refresh error names only
  its exception type, because its text can quote response bytes.
- `kiro_operation_failed` carries `kiro: {status, type, message}`: the BFF's
  HTTP status, the short name of its `__type`, and its `message`, cut to 300
  characters and passed through the credential redactor
  (`security/text_redaction.py`). The app can act on a conflict or a throttle.
- At most 8 calls of one app are in flight. The ninth gets 429 `too_many_calls`
  with `Retry-After: 1`. This bounds one app's load on the user's Kiro account.

### 8. Logs and audit

- One log line per call: app, operation, BFF status, duration, request and
  reply byte counts. Never the token, any header, the body, the output or the
  BFF's message.
- One audit row per call: caller kind (backend or hook), app, operation, and
  the grant entry that allowed it, as section 7 of
  [rfc-app-contribution-points.md](rfc-app-contribution-points.md) asks.
- `KiroCallError` and every error body hold no token. A test plants a synthetic
  token from `kiro_crew.testing.synthetic_credentials` and asserts it is absent
  from every log record, error body and audit row.

## Migration plan

Each phase is one PR.

| Phase | Change | Exit criteria |
|---|---|---|
| 1 | `permissions.kiroOperations`, the `kiroOperations` family, `app_kiro_operations`, the route, `ctx.kiro_call`, the shared CBOR codec, the region table, the install and detail-page lists, logs and audit. Spec updates in `app-kit-platform.md`, `api-reference.md` and `manifest-reference.md`. App Kit seam level +1. | Against a fake BFF: an approved operation returns its output, and the fake saw the vault's token and identity headers. An undeclared name gets 404, a held-back name 403, and neither reaches the fake. An update that adds a name keeps the old names working and holds the new one. A record written before the family approves nothing. A dashboard token gets 403. An `eu-central-1` profile reaches only the EU origin, and an unknown region gets 503 with no request sent. A 401 from the fake renews once and retries once. A 9th concurrent call gets 429. Bodies over 1 MiB and replies over 8 MiB are refused. The planted token appears in no log, error body or audit row. |
| 2 | Docs: a section in `docs/app-kit/api-reference.md` showing an `agentTools` handler that calls `ctx.kiro_call`. | It lands after phase 1 of the agentTools RFC, and it shows the four approvals of section 6. |

## Backward compatibility

- Additive. No manifest key, route or family that main accepts changes.
- An app that imports `kiro_crew.auth` today keeps working until those
  internals move, as now. It moves to `ctx.kiro_call` at its own pace.
- An installed app whose manifest already lists `kiroOperations` gets nothing
  until the owner approves, because the family is explicit-only.

## Security considerations

- **What the app holds.** Only operation outputs. The token stays in the
  gateway, so the app cannot replay it against another operation, another
  Kiro API or another host.
- **What the owner approved.** The exact names, shown at install and on each
  widening update. A token-holding app would have every operation the token
  allows. This seam narrows that to the approved names.
- **Residual: the operation itself.** An approved write operation can change
  the user's data in that Kiro feature, as the user. The owner approves that by
  name. The gateway cannot judge what an operation does, and the name is the
  unit the owner can read.
- **Residual: the app token.** A process that holds the app's token can call
  the app's approved operations. The gateway gives that token to the app's
  backend and its in-gateway hooks only. The stdio servers of the app's agents
  get none (`docs/app-kit/api-reference.md`). The agentTools RFC notes that a
  process in the agent sandbox can read the gateway's internal secret. This
  route refuses an internal-secret caller, so that secret alone does not
  reach it.
- **Endpoint pinning.** The origin comes from the profile region and a fixed
  table, the path from one constant, and redirects are not followed. An app
  cannot point the token at a host it controls.
- **No new matcher.** The decision is set membership on an exact name. The
  manifest parser checks characters one by one and keeps only valid names.
- **Logs.** No token, header, body or output reaches a log. Errors pass the
  BFF's message through the redactor and a length cap.
- **Load.** The in-flight cap and the body caps bound one app's use of the
  user's account and of the gateway's memory.

## Alternatives considered

### Give the app the raw token

A `ctx.kiro_token()` or a route that returns the access token. Simple, and the
shape apps already copy. Rejected: the token grants every operation of every
Kiro API it is good for, the app can send it to any host, and the owner cannot
see or limit what it does. It also makes the token's lifetime the app's
problem.

### Put the token in the environment of the app's MCP server

Mint a token at session start and pass it to the app's stdio server. Rejected
for the reasons the agentTools RFC rejects an app token there: the server runs
in the agent's process tree, so an injected prompt can make the agent read the
token and use it with no prompt. It also returns a credential to the sandbox
that #34762 removed credentials from.

### A per-app API key

The app asks the user for a Kiro API key and stores it. Rejected: the user
already signed in to Kiro in Kiro Crew. A second credential has its own
lifetime, its own leak path and a manual setup step, and it still grants more
than the app's operations. It is also tied to one account type.

### A general authenticated HTTP proxy

Let an app send any request to an approved Kiro host with the sign-in attached.
Rejected: the owner would approve a host, not an action, and the gateway would
need path and header rules that a pattern match decides.

### Require `permissions.api: ["/api/kiro/operations"]` as well

The `permissions.bots` seam asks for both a path prefix and a family. Rejected
here: the prefix grants nothing the family does not already decide, and a
second approval of the same thing gives the owner nothing to choose.

## Open questions

1. Is the install-time approval right, or must the owner approve operations in
   a separate step after install, as the agentTools RFC chose for tools? This
   draft approves at install, because the list is short, readable and shown on
   the install screen, and the app does nothing without it.
2. Should a later revision carry CBOR byte strings and timestamps in the input,
   for example as `{"$bytes": "<base64>"}`? No known operation needs them yet,
   so this draft leaves them out.
3. Is 8 in-flight calls per app the right cap? It covers one app page plus a
   few chat tools at once, and it is one constant.
