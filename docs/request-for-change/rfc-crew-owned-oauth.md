---
title: Crew-owned OAuth for Connections -- the gateway runs OAuth and holds the tokens, harnesses get a stdio bridge
status: draft
author: zejiangg, with kirocrew agent session
created: 2026-11-04
last-audited: 2026-11-04
audited-at: 4055dcd61e
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Crew-owned OAuth for Connections

- Status: draft. Proposed for team review; the team has not decided. Nothing is built, and this document changes no code.
- Would amend [mcp-oauth-ownership.md](../architecture/design-notes/mcp-oauth-ownership.md) (the rule "Kiro Crew never reads or holds the credential") and the invariant in [connections.md](../system-specs/modules/connections.md). It picks up the "OAuth custody under the Agent SDK" option left open in [rfc-crew-agent-sdk-boundary.md](rfc-crew-agent-sdk-boundary.md).
- Measured at Kiro Crew `origin/main` `4055dcd61e` (first written at `aff16f9886`; every cited Crew file and line was re-checked at `4055dcd61e`) and KAS `kiro-team/kiro-agent` `main` `d05183bc8d`.

Crew paths are under `src/kiro_crew/` unless they start with `docs/`. KAS paths are under `packages/kiro-agent/src/`. A claim marked *(earlier research)* comes from the author's earlier research notes, which are not in this repository; it was not re-verified for this document.

---

## Executive summary (one page)

**Problem.** Every Connection (GitHub, Asana, six Google products) needs a pre-registered OAuth client with a client secret. Today the agent harness runs OAuth, so every harness must get it right. None does:

- **KAS (the default backend)** cannot sign in to any of the eight pre-registered providers. Its `customAgents` MCP schema has no `oauth` slot, and its `oauth` schema drops `clientSecret`.
- **kiro-cli v2** signs in, but loses GitHub every ~8 h: it refreshes single-use tokens in memory only, then deletes the file. v2 is being removed anyway.
- **claude, codex, opencode, goose, pi, copilot, strands, deepseek**: Crew's own capability table says none honours a Connection's `oauth` block.
- Google also needs `access_type=offline` + `prompt=consent`. No engine sends them.

**Proposal.** Kiro Crew runs OAuth itself, in the gateway: consent URL, code swap with the secret, refresh, revoke. Tokens live in Crew's existing encrypted vault. Each harness gets a plain **stdio** MCP server (a tiny bridge) that forwards to the gateway over Crew's owner-only, kernel-attested Unix socket (on Windows, verified loopback TCP; see [Amendment 2026-11-07](#amendment-2026-11-07-windows-bridge-over-verified-loopback-tcp-ahead-of-phase-2)). The gateway adds `Authorization: Bearer` and forwards to the vendor. The token never enters the harness, the sandbox, the wire or the transcript. Any harness that can start a stdio MCP server works with no OAuth code of its own.

**Most of it exists.** Crew already ships each half:

- `kas_http_bridge.py` (#28615): a stdio MCP server that relays to a remote streamable-HTTP server with headers kept local.
- `mcp_secrets.py` + `dashboard/handlers/mediated_secrets.py`: a sandboxed tool asks the gateway, over the attested Unix socket, to make an HTTPS call with a vault secret injected gateway-side. SEL-audited.
- `secrets/vault.py`: AES-256-GCM per entry, `.vault` on the agent's sensitive-path floor.

**Size.** About 1,300-2,000 lines of product code and 1,500-2,300 lines of tests for all eight providers on all harnesses. **MVP** (GitHub on KAS, default-off flag): about 900-1,300 code + 1,100-1,600 tests. Reuse saves an estimated 1,200+ lines versus building storage, auth gates and transport attestation fresh.

**Top security risk.** An agent's own shell runs in the same process tree as the bridge. It can reach the same Unix socket and pass the same ancestry attestation. It cannot read the token, but it could drive the vendor's MCP tools directly and skip Crew's per-tool approval. Mitigations: the gateway proxy enforces the governed tool policy and audits every `tools/call` itself, and pins one bridge per session and server. This needs a security review before the flag goes on.

**Decisions needed.** (1) Retire the design rule "Kiro Crew never reads or holds the credential" (`docs/architecture/design-notes/mcp-oauth-ownership.md`, "The boundary in one sentence"). It is already untrue on KAS: Crew's vault stores KAS's MCP tokens today. (2) Who signs off the security review. (3) Whether DCR providers move too.

**Recommendation.** Ship the small KAS fix (alternative a) now if KAS will take it. Build this proposal as the long-term path, MVP first, behind a flag.

---

## 1. Problem

### 1.1 What a Connection needs

A Connection is a vendor-hosted remote MCP server behind OAuth. Every request needs `Authorization: Bearer <access token>`. The eight pre-registered providers on main all need a confidential client: the token endpoint wants `client_secret` even with PKCE.

| Slug | `confidential` | Callback port | MCP URL |
|---|---|---|---|
| github | true | 48101 | `https://api.githubcopilot.com/mcp/` |
| asana | true | 48102 | `https://mcp.asana.com/v2/mcp` |
| google-drive | true | 48103 | `https://drivemcp.googleapis.com/mcp/v1` |
| gmail | true | 48104 | `https://gmailmcp.googleapis.com/mcp/v1` |
| google-calendar | true | 48105 | `https://calendarmcp.googleapis.com/mcp/v1` |
| google-sheets | true | 48109 | `https://sheetsmcp.googleapis.com/mcp/v1` |
| google-docs | true | 48110 | `https://docsmcp.googleapis.com/mcp/v1` |
| google-slides | true | 48111 | `https://slidesmcp.googleapis.com/mcp/v1` |

Read from `connections/registry.json` at `4055dcd61e` (35 entries total; these are the `auth.mode = preregistered` ones). The six Google entries landed in #27921, merged 2026-11-03 09:04 UTC. `confidential` means "the vendor requires a `client_secret` at the token endpoint" (`AuthConfig` docstring in `connections/registry.py`).

Vendor rules *(earlier research, vendor docs)*: GitHub OAuth apps need `client_secret` at `POST /login/oauth/access_token` even with PKCE. Asana needs it for code and refresh. Google Web-application clients need it.

### 1.2 Gaps, per harness

| Harness | Gap | Evidence |
|---|---|---|
| **KAS (v3), default** | `customAgents` MCP schema has no `oauth` field. `z.object` strips unknown keys, so `clientId`, `clientSecret`, `redirectUri` all vanish. KAS then tries DCR, which none of the eight offer. | KAS `ClientAgentMcpServerSchema` in `services/custom-agents/resolve-client-agents.ts` (schema: `command, args, env, cwd, url, headers, disabled, autoApprove, disabledTools, timeout`); its `mapClientMcpServers` passes only `url` + `headers` for a remote server |
| KAS, `mcp.json`/profile path | `OAuthConfigSchema` = `clientId`, `redirectUri`, `clientMetadataUrl`. No `clientSecret`. Pre-registered client is `{client_id}` only, so the SDK picks auth method `none`. | KAS `OAuthConfigSchema` in `mcp/server-config.ts`; the `MCPAuthProvider` constructor (`_preregisteredClientInfo`) in `mcp/oauth/mcp-auth-provider.ts` |
| KAS, Google | Never sends `access_type`. Sends `prompt=consent` only if scope has `offline_access`. | *(earlier research, read from the 0.66.70 bundle)* |
| KAS on Crew today | Crew also strips every remote entry's `headers` before projecting to KAS, then (since #28615) relays them through a stdio bridge | Crew `_CREDENTIAL_BEARING_FIELDS`, `_project_mcp_servers` (the `relay_withheld_headers` call) and `_withhold_credential_fields` in `acp/kas_agents.py` |
| **kiro-cli v2** | Sends the secret. Refreshes in memory only (rmcp `InMemoryCredentialStore`), never writes `token.json`. GitHub refresh tokens are single-use, so the next process loads a dead pair, fails refresh and **deletes** the file. Result: re-login about every 8 h. | *(earlier research: kiro-cli `mcp_client/oauth_util.rs`)*. v2 is being removed; migration context only |
| v2, Google | No `access_type`; Crew works around it by rewriting Google's consent URL | Crew `acp/oauth_offline.py` (58 lines, #27906) |
| **claude, codex, opencode, goose, deepseek, pi, copilot, strands** | Crew's capability table lists only kiro-cli as honouring a remote entry's `oauth` block | Crew `ACP_BACKENDS_REMOTE_MCP_OAUTH = frozenset({ACP_BACKEND_KIRO})` in `agent_sdk/backends_mcp.py`. Whether each vendor harness could do pre-registered OAuth if Crew passed the block another way is **unverified** |

The default backend is KAS (`DEFAULT_ACP_BACKEND` in `agent_sdk/backends.py`). So a default user sees GitHub/Asana/Google cards they cannot use in chat. Crew already shows a `backendLacksOauth` caveat on the card for this (`backendLacksOauth` in `connections/status.py`).

### 1.3 Who is affected

- **Now:** all eight pre-registered providers above, on every harness except v2. On v2, GitHub breaks every ~8 h.
- **Next:** every provider in the add list that has no DCR: Slack, HubSpot, Box, Zoom, PagerDuty, DocuSign *(earlier research)*.
- **DCR providers** (Notion, Linear, Atlassian, Stripe, Vercel, GitLab, the visible one-click set) work on harnesses that do DCR. KAS does DCR. Whether claude/codex/etc. do DCR for a Crew-projected entry is **unverified**. They are not in scope for the MVP (see §7 Q3).

### 1.4 Why fixing each harness does not converge

Each harness has its own config schema, its own token store, its own refresh logic and its own release train. The gaps above are all in the same three places: passing the secret, extra authorize parameters, and refresh persistence. Every new harness Crew adds re-opens all three. Crew also cannot add the secret itself today: the code swap happens inside the engine, and Crew sees only the consent URL *(earlier research)*.

---

## 2. Current architecture

### 2.1 How Crew writes Connection config today

1. Operator saves a client in Settings → OAuth Apps. Client id goes to `config.json` `connections.oauth_clients.<slug>.client_id`; secret goes to the vault as `CONNECTIONS_<SLUG>_CLIENT_SECRET`; env `KIROCREW_CONNECTIONS_<SLUG>_CLIENT_ID/_SECRET` overrides (module docstring, `_SECRET_NAME_SUFFIX` and `client_secret_env_name` in `connections/oauth_clients.py`).
2. `apply_preregistered_oauth_client` writes `oauth.clientId`, `oauth.clientSecret` (when present) and `oauth.redirectUri` into the agent's MCP entry (`connections/oauth_clients.py`). `strip_preregistered_oauth_client` removes them.
3. The Connections card sign-in ("mint") runs one dedicated `kiro-cli acp` process, which is v2 *(earlier research; `connections/mint.py`, 1,316 lines)*.
4. Grant presence is read by `stat` on v2's `~/.aws/sso/cache/<sha256>.token.json` pair, never opened (`grant_presence` in `mcp_grant.py`; `revoke_local_grant` unlinks a pair).
5. On KAS, the engine stores its MCP OAuth state through `_kiro/secret/get|store|delete`, which Crew answers from a dedicated vault at `<data_home>/kas/mcp-grants/.vault/` (on by default since #28630) (module docstring of `acp/kas_secret_storage.py`).

### 2.2 Where the rule "Crew never holds credentials" is stated

The authoritative statement is the design note:

> kiro-cli owns the OAuth chain end to end and Kiro Crew never reads or holds the credential. … A design that would move custody into Kiro Crew is a proposal, not current behaviour, and is argued in `rfc-crew-agent-sdk-boundary.md`.

`docs/architecture/design-notes/mcp-oauth-ownership.md`, "The boundary in one sentence" (last changed `a13c55c5e2`, 2026-09-22; created `cdf2a1055c`, 2026-08-04).

The rule is repeated as an invariant in:

- `docs/system-specs/modules/connections.md`, "What a green L1 run actually proves" ("**Kiro Crew holds no token.** kiro-cli owns the OAuth chain and injects the bearer…").
- the module docstring of `connections/l1_smoke.py` (L1 verdicts are built on it: a healthy managed provider answers with a challenge, graded `GRANT_HELD`).
- `_runtime_grant_present` in `mcp_discovery_remote.py` ("The probe holds no token of its own (Kiro Crew stores no credentials)").

The custody move was already sketched as a future option in [rfc-crew-agent-sdk-boundary.md](rfc-crew-agent-sdk-boundary.md), section "OAuth custody under the Agent SDK" ( "Kiro Crew owns the OAuth chain end-to-end", refresh in a background task, sign-out revokes upstream). That RFC is `status: partial` and did not decide the OAuth part.

### 2.3 Why the rule was chosen

From `mcp-oauth-ownership.md`, "How this works today (kiro-cli backend)":

- The only alternative on kiro-cli was "Path B": put `Authorization: Bearer` into `agent.json` `headers`. It had two showstoppers: no refresh (a static token 401s mid-turn), and plaintext in a file the agent itself can read.
- So Crew lived on "Path A": kiro-cli runs OAuth, stores tokens, refreshes, injects. Crew only renders UI for a flow it does not own.

The rule is a consequence of kiro-cli being the only harness, not a security goal on its own. The same note lists what it costs: no refresh control, no "connected as octocat", no per-agent accounts, no upstream revoke.

### 2.4 The rule is already partly untrue

- On KAS, Crew's vault holds the engine's MCP `client` and `tokens` values (module docstring of `acp/kas_secret_storage.py`). Crew never interprets them, but it holds them, encrypted, gateway-wide (same docstring).
- Crew already holds third-party credentials and injects them gateway-side: Custom secrets via `kirocrew-secrets` (§3.3). WakaTime OAuth (client secret + tokens in the vault, single-flight refresh) is designed in `docs/architecture/design-notes/wakatime-oauth-read-auth.md`, "Credentials and storage" (design note, #19809; not found implemented on main: `build_client` in `wakatime/service.py` has no token code).

So the team is not choosing "hold credentials or not". It is choosing whether Crew holds them **opaquely, per harness** (today) or **owns them, once** (this RFC).

### 2.5 Where tokens sit today

| Path | Where | At rest |
|---|---|---|
| v2 grant | `~/.aws/sso/cache/<sha256>.token.json` | Plaintext JSON *(earlier research)*. File mode **unverified**. The agent sandbox masks `~/.aws` except `config` (`docs/system-specs/modules/agent-host-contract.md`, section 4 "Sandbox") |
| KAS grant | `<data_home>/kas/mcp-grants/.vault/` | AES-256-GCM vault, own key file; `kas` dir is a keystone leaf, hidden from the sandbox (module docstring of `acp/kas_secret_storage.py`) |
| Client secret | user vault `CONNECTIONS_<SLUG>_CLIENT_SECRET` | encrypted; but **written in plaintext into the v2 agent spec** `oauth.clientSecret` (`apply_preregistered_oauth_client` in `oauth_clients.py`) |

---

## 3. Proposed design

### 3.1 Overview

```
 Browser                Gateway (unsandboxed, owner-only)                 Vendor
 ───────                ─────────────────────────────────                 ──────
  Connect ─────────▶ OAuthClient.start(slug)
                       PKCE + state, provider extras (Google offline)
  consent  ◀─────────  authorize URL (via existing banner gate)
  vendor ──redirect──▶ callback listener 127.0.0.1:<registry port>/callback
                       (or paste-back on remote gateway)
                       code + verifier + client_secret ───────────────▶ token endpoint
                       ◀────────────────────────────── access + refresh + expiry
                       TokenStore.put(slug)  →  secrets/vault.py

 Harness (KAS, claude, codex, …), inside the sandbox
 ───────────────────────────────────────────────────
  session/new: mcpServers.github = { command: python -P -m kiro_crew.kas_env_shim
                                     --connection github --session S … }   (stdio, no url, no headers)
  harness ⇄ stdio JSON-RPC ⇄ bridge ──AF_UNIX, SO_PEERCRED attested──▶ ConnectionProxy (gateway)
                                                                         policy check, SEL audit
                                                                         TokenStore.get → Bearer
                                                                         (refresh if near expiry,
                                                                          single-flight)
                                                                         POST ───────────────────▶ MCP URL
                                                     ◀── JSON / event-stream, relayed ──────────
```

The harness sees a stdio server. It never gets a URL to the vendor, a header, or a token.

### 3.2 Components

| # | Component | Job | New / reused |
|---|---|---|---|
| C1 | **OAuthClient** (gateway) | Build authorize URL (PKCE S256, `state`, scopes, per-provider extra params such as Google `access_type=offline&prompt=consent`). Run a short-lived callback listener on the provider's registry port. Swap code for tokens with `client_secret` (Basic or post, from the AS metadata). Refresh. Revoke on disconnect. | New logic. Reuses registry (ports, `confidential`, `l0_expectations.authorization_server`), `oauth_clients.py` (id/secret resolution), `mcp_oauth_discovery.py` (RFC 9728/8414 discovery chain, 307 lines), `security/exfil_oauth.py` (consent-URL banner allowlist), `acp/oauth_offline.py` (Google params) |
| C2 | **TokenStore** | One vault entry per connection, e.g. `CONNECTIONS_<SLUG>_OAUTH_TOKENS` = JSON `{access, refresh, expires_at, scope, account}`. One atomic replace commits all fields together (the WakaTime note's reasoning, `wakatime-oauth-read-auth.md`, "Credentials and storage") | Reuses `secrets/vault.py` (606 lines): `get_with_revision`, `set_with_revision`, `delete_if_revision`. **New:** a `set_if_revision` compare-and-set (~30 lines); today `set_with_revision` overwrites |
| C3 | **Refresher** | Refresh before expiry (on demand in C4, plus an optional background pass). **Single-flight per connection:** an `asyncio.Lock` per slug inside the gateway, and a file lock (`platform_compat.file_lock`, already imported by `secrets/vault.py`) for a second gateway on the same data home. Inside the lock: re-read the entry; refresh only if still the same revision and still expiring; write with `set_if_revision`. On `invalid_grant`/`bad_refresh_token`: re-read once (another writer may have rotated), else mark `needs_auth`. Never delete on a refresh failure. | New. Fixes the exact v2 failure (§1.2) by design |
| C4 | **ConnectionProxy** (gateway route on the owner-only Unix socket) | Admit only an attested session caller. Accept MCP JSON-RPC from the bridge. Apply the governed tool policy and write one SEL event per `tools/call`. Add `Authorization: Bearer`. POST to the registry `mcp_url` only; no redirects. Relay `Mcp-Session-Id`, `MCP-Protocol-Version`, JSON or event-stream answers. On vendor 401: refresh once and retry; else return a JSON-RPC error with a "reconnect on the Connections page" message. **Never return HTTP 401 to the harness.** | New streaming route. Reuses the admission gates of `_admit` in `dashboard/handlers/mediated_secrets.py` (`internal_auth`, `X-Session-Key`, `member_request_scope`), SEL `log_api_access` (via `_audit`), `loopback_http.unix_socket_urlopen` |
| C5 | **Bridge** (inside the harness/sandbox) | Stdio MCP server; forwards each message to C4 over the Unix socket. Holds no token and no vendor URL. | Adapt `kas_http_bridge.py` (269 lines, #28615): today it POSTs to an `https://` URL with headers from a launch snapshot; change the target to the gateway socket. Launched through `kas_env_shim.py` (234 lines). Stays lean: no config or platform composition per bridge; only the opener picks the transport |
| C6 | **Per-harness config writer** | Replace each Connection entry's `url` + `oauth` with the bridge stdio entry. KAS: in the `kas_agents.py` projection. claude/codex/opencode/goose/deepseek/pi/strands: the `session/new` MCP array. copilot: its `--additional-mcp-config` file, since it rejects stdio on `session/new`. | Mostly reuse: `acp/kas_env_launch.py` (372 lines), `ACP_BACKENDS_SESSION_MCP_ARRAY` in `backends_mcp.py` (and the copilot/strands file routes beside it). Stop writing `oauth.clientSecret` into any spec |
| C7 | **Connections status/UI** | Read state from C2 (connected, account, expiry, `needs_auth`). Disconnect = revoke upstream + delete entry. | Edits to `connections/status.py`, `mint.py` retirement path, `mcp_grant.py` kept only for migration |

### 3.3 The closest precedent: mediated secrets

`kirocrew-secrets` already does gateway-side injection of a vault secret into an outbound HTTPS call, so the value never reaches the agent:

- The tool runs inside the agent sandbox, holds only the secret **name**, and forwards non-secret intent to `POST /api/mediated-secret-request` (module docstring of `mcp_secrets.py`).
- Transport is the owner-only dashboard Unix socket, **no TCP fallback**; the client checks the answering peer is the genuine gateway pid before sending credential bytes (`_call_tool_inner` in `mcp_secrets.py`). Windows is refused outright (same function).
- The gateway admits only internal authority + `X-Session-Key` + an **attested** member scope (`_admit` in `mediated_secrets.py`). Attestation is either Unix-socket kernel peer ancestry or a gateway-signed per-session token (`session_key_is_attested` in `member_memory_auth.py`).
- Each call needs a single-use capability minted by the ACP approval of that tool call (the `X-Mediated-Secret-Capability` check in `mediated_secrets.py`, `mediated_request_capability.py`, 423 lines).
- Dispatch SSRF-vets and DNS-pins the destination and resolves the secret only at send time (module docstring of `mcp_secrets.py`; `secrets_mediation/dispatch.py`, 440 lines). Everything is SEL-audited (`_audit` in `mediated_secrets.py`).

What this RFC **reuses** from it: the vault, the Unix-socket transport and peer check, `member_request_scope` / `session_key_is_attested`, the SEL audit call, the "value never leaves the gateway" discipline, and the no-TCP-fallback stance on POSIX. On Windows the bridge leg uses verified loopback TCP instead (Amendment 2026-11-07).

What is **new**:

| Mediated secret today | Connection proxy |
|---|---|
| One-shot request, bounded body, response withheld (blind write) | Long-lived MCP session, streaming event-stream answers, response must reach the harness |
| Static secret | OAuth flow, expiry, refresh with single-flight lock, rotation |
| One capability per approved call | MCP has many calls per session; per-call capabilities would need the harness's approval to mint one per `tools/call` (see §4.3 for what replaces it) |
| Owner-authorized origin + placement per secret | Origin fixed by the registry `mcp_url`; header fixed to `Authorization` |

This reuse is why §5's estimate is ~1,200 lines smaller than a fresh build (vault 606 + attestation/scope 331 + 228 + transport checks).

### 3.4 Data flow, step by step

**Connect.**
1. User clicks Connect. Gateway C1 resolves the client (`oauth_clients.py`), builds the URL, records `{state, verifier, slug, session-of-user}` in memory (verifier never stored on disk).
2. URL goes through the existing consent-URL gate (`security/exfil_oauth.py`, `mcp_oauth_discovery.py`) and opens in the browser.
3. Vendor redirects to `http://127.0.0.1:<port>/callback`. C1 binds that exact host and port only while a sign-in is pending, checks `state`, swaps the code with the secret. On a remote gateway, the existing paste-back path delivers the URL instead (*earlier research*; current paste-back code in `mint.py` **unverified** here).
4. C2 writes the token entry. Status flips to Connected, with account if the vendor exposes it.

**Use.**
1. Session start: C6 writes the bridge stdio entry for each connected provider the agent is granted.
2. Harness starts the bridge. KAS gives a stdio server a no-op OAuth provider (the `MCPConnection` constructor in KAS `mcp/mcp-connection.ts`), so KAS never starts its own sign-in.
3. Bridge connects to C4 over the socket; C4 attests the session (ancestry) and pins the bridge pid (§4.3).
4. Each message: C4 checks policy, logs `tools/call`, gets a fresh token from C2/C3, POSTs to the vendor, relays the answer.

**Disconnect.** C1 revokes upstream where the vendor has an endpoint (GitHub, Google, Asana: endpoint names per vendor docs, **unverified live**), C2 deletes the entry with `delete_if_revision`. C4 keeps open sessions' bridges registered: with no stored pair each call is answered 409 "reconnect it" and nothing is sent upstream, and a later Connect makes them work again (#31149 item 1). Only when the cleanup itself fails does C4 drop every bridge of the slug, so a pair that may survive is never reachable.

### 3.5 Verified key unknown: can KAS use a plain local MCP endpoint?

| Question | Answer | KAS evidence (`d05183bc8d`) |
|---|---|---|
| Plain `http://127.0.0.1:<port>/mcp`, no auth? | **Yes.** URL must be https or localhost; `localhost`, `127.0.0.1`, `::1` pass | `isHttpsOrLocalhost` in `mcp/url-validation.ts`, called from `parseMcpServerConfig` in `mcp/server-config.ts` |
| Custom headers on that URL? | **Yes.** `headers` survives the `customAgents` schema and reaches the transport `requestInit.headers` | `ClientAgentMcpServerSchema` and `mapClientMcpServers` in `services/custom-agents/resolve-client-agents.ts`; `constructStreamableHTTPTransport` and `constructSSETransport` in `mcp/mcp-connection.ts` |
| What if that local URL answers 401? | KAS **always** builds an OAuth redirect server and auth provider for an HTTP server, so a 401 starts KAS's own sign-in | the `MCPConnection` constructor in `mcp/mcp-connection.ts` (`OAuthRedirectServer` for every HTTP server) |
| Unix-socket MCP URL? | **No.** No socket path option found in `mcp-connection.ts` or `server-config.ts` (grep for `socketPath`, `unix:`, `.sock`: no hits) | — |
| Stdio server? | **Yes**, and it gets a no-op OAuth provider | `mapClientMcpServers` in `resolve-client-agents.ts`; the `MCPConnection` constructor in `mcp-connection.ts` |

Two more Crew facts decide the transport:

- KAS stores the `customAgents` batch **verbatim** in the session's `session.json` (module docstring of `acp/kas_env_launch.py`). Any header Crew puts on the wire (a per-session proxy secret) is persisted there.
- Every non-KAS harness Crew supports takes stdio MCP servers on `session/new`, except copilot, which takes them from a launch config file (`ACP_BACKENDS_SESSION_MCP_ARRAY` and the copilot notes beside it in `agent_sdk/backends_mcp.py`).

**Decision: the harness-facing endpoint is stdio, not a loopback HTTP URL.** A loopback URL would work on KAS today, but it brings a TCP listener, a secret on the wire and in `session.json`, and a 401 trap. Stdio brings none of these and works on every harness. A loopback HTTP mode is kept only as a fallback for a future harness that cannot start stdio servers (§4.2).

---

## 4. Security analysis

### 4.1 Assets and trust boundaries

| Asset | Value | Today | Proposed |
|---|---|---|---|
| Refresh token | Long-lived (GitHub: 6 months unused *(earlier research)*); can mint access tokens off-host with the client secret | v2: plaintext `~/.aws/sso/cache`; KAS: Crew KAS vault | Crew user vault only; never leaves the gateway process |
| Access token | 1-8 h of API access | same as above, plus in the engine's memory | gateway memory + vault only |
| Client secret | Lets anyone holding a refresh token or code redeem it | vault, **and plaintext in the v2 agent spec** (`apply_preregistered_oauth_client` in `oauth_clients.py`) | vault only; spec no longer carries it |
| Ability to call vendor tools | Acts as the user at the vendor | through the harness, gated by Crew's tool gate | through the proxy; see §4.3 |

Boundaries: (1) the gateway process (trusted); (2) the harness and the agent sandbox (untrusted for credentials); (3) other OS processes as the same user; (4) other OS users on the host; (5) the browser.

### 4.2 Loopback HTTP proxy threat model (why it is not the default)

| Threat | Loopback TCP proxy | Stdio bridge → Unix socket |
|---|---|---|
| Another OS user connects | Possible: TCP on 127.0.0.1 has no owner check; needs a secret in every request | Socket in owner-only data home (0600) with `SO_PEERCRED` / `LOCAL_PEERCRED` check (module docstring of `mcp_gateway/socketsec.py`) |
| Another user binds the port first (gateway restart) | Possible; the harness would send its secret to them. `_call_tool_inner` in `mcp_secrets.py` refuses TCP for exactly this reason | Client verifies the answering pid is the real gateway (same function) |
| Browser DNS rebinding | Needs the dashboard's `Host` allowlist on this listener too (`check_host` in `dashboard/origin.py`, `_make_host_validation_middleware` in `dashboard/server_runtime/security_middleware.py`) | Browsers cannot reach a Unix socket |
| CSRF from a web page | Needs Origin checks and a non-cookie secret | Not reachable |
| Secret persisted | Per-session header written into KAS `session.json` | Nothing secret on the wire; bridge args name only session/agent/server/version (module docstring of `kas_env_shim.py`) |
| Harness starts its own OAuth on 401 | Yes on KAS (`MCPConnection` constructor) | No (stdio no-op provider, same constructor) |

This table covers the harness-facing URL. The Windows bridge→gateway leg is covered in Amendment 2026-11-07.

If a loopback HTTP fallback is ever needed: bind `127.0.0.1` only, random port, per-session secret header compared with `hmac.compare_digest`, `Host` allowlist = exact `127.0.0.1:<port>`, reject any `Origin`, never answer 401, secret rotates per session. It stays off unless a harness needs it.

### 4.3 The agent inside its own session (top risk)

The bridge is a child of the harness. So is the agent's shell tool. Kernel ancestry attestation therefore admits **both** as "session S". A comment in `_call_tool_inner` in `mcp_secrets.py` already names this class of problem ("the untrusted ACP harness that hosts this tool is its PARENT by construction").

What the agent's shell **cannot** do: read the token. The token is only in the gateway and the vault, and `.vault` is on the sensitive-path floor and masked in the sandbox (module docstring of `secrets/vault.py`).

What it **could** do without mitigation: open the socket, speak MCP, and call any vendor tool directly. That skips the harness's tool approval and Crew's PreToolUse gate, much as the L1 sweep design warns against (`docs/system-specs/modules/connections.md`, "The sweep never calls a tool").

Mitigations, all in C4:

1. **The proxy is a gate, not a pipe.** C4 applies the same governance ceiling and per-agent MCP grant the harness gate uses to every `tools/call` before forwarding, and writes one SEL event per call (tool name, slug, session, outcome; never arguments that could hold data). A call that would need user approval is refused at C4 unless the harness approval for that call id was seen. How C4 learns of the approval (Crew's gate already sees `session/request_permission`) is a design detail for review.
2. **Pin one bridge per (session, server).** The first attested connection for a pair registers the peer pid; C4 also checks its cmdline is `kiro_crew.kas_env_shim` with the expected reference. Later connections from another pid are refused and audited. The bridge connects at MCP start, before any agent turn. This is new and is defence in depth: a same-uid process can fake a cmdline, so it narrows the hole rather than closing it.
3. **Least privilege per Connection.** Request the smallest scopes; the registry already records per-provider scope guidance (e.g. read-only by default, *(earlier research)*).

Residual: an agent that runs arbitrary code in its own session can reach the vendor with that session's grant. This is no worse than today's position for the same agent: it can already call the vendor's tools through the harness. The new part is that the call may not be shown for approval. The security review must decide if mitigations 1-2 are enough to ship by default.

### 4.4 Token never reaching the model or transcript

- The bridge and harness never hold it (above).
- C4 never echoes request headers into errors; mirror the module docstring of `kas_http_bridge.py` ("No header value is ever written to stderr or into an error the engine sees") and that of `kas_secret_storage.py` (values not logged; failures log exception type only).
- Vendor responses are relayed as-is. A vendor that echoes the bearer in a body would leak it; none of the eight is known to, **unverified**. C4 can strip the exact current access token from relayed bodies as cheap insurance (~20 lines).
- The consent URL is not a secret (state + PKCE challenge) and already goes through the banner gate.

### 4.5 Other local processes, same user

The vault's own doc says: "The protection is enforced by Kiro Crew, not by the operating system: another program running as you can still open the files" (`src/kiro_crew/docs/secrets-vault.md`, "Where it lives, and why the agent cannot read it"; also the module docstring of `secrets/vault.py`). This RFC does not change that. Any malware running as the user can read the key file and decrypt the vault. Compared with today it is an improvement over v2's plaintext `token.json`, and equal to the KAS vault. OS keychain storage is out of scope; it was floated in `rfc-crew-agent-sdk-boundary.md`, "If the Agent SDK owned the MCP servers".

### 4.6 Multi-user hosts

- Other OS users: blocked by owner-only data home and peer-credential checks (§4.2).
- Several people on one gateway (Shared Crew): grants are gateway-wide today on both v2 and KAS (module docstring of `kas_secret_storage.py`). MVP keeps that model (one token per connection per gateway). Per-member tokens are possible later because Crew owns the store (`mcp-oauth-ownership.md` lists this as a current limitation), but they need a product decision (§7).
- Windows: the attested Unix-socket channel does not exist; `_call_tool_inner` in `mcp_secrets.py` refuses. The bridge→gateway leg uses loopback TCP verified on both ends (Amendment 2026-11-07). MVP: POSIX only, flag refuses on Windows until phase 1b.

### 4.7 Callback listener

- Binds `127.0.0.1:<registry port>` only while one sign-in is pending; closes after one callback or a timeout.
- Another local process could hold the port first. It would get the code but cannot redeem it: it lacks the PKCE verifier (memory only) and the client secret (vault). The sign-in then fails visibly; C1 reports "port busy".
- `state` is checked exactly; only one pending sign-in per provider.

### 4.8 Revocation and disconnect

Crew-owned custody adds upstream revocation, which today is impossible (`mcp-oauth-ownership.md`, "How this works today"; `revoke_local_grant` in `mcp_grant.py` only unlinks local files). Disconnect: revoke upstream (best effort, report failure), delete the vault entry; live proxy sessions are refused until reconnect (and cut if the cleanup fails). A vendor-side revoke (user removes the app at the vendor) shows up as a refresh failure and flips status to `needs_auth`.

### 4.9 Audit

SEL events (reusing `log_api_access`, as `_audit` in `mediated_secrets.py` does): sign-in start/finish, token swap outcome, each refresh (outcome, never values), each `tools/call` through C4, each refused connection to C4, disconnect and revoke outcome.

### 4.10 Honest comparison with today

| | Today (v2) | Today (KAS) | Proposed |
|---|---|---|---|
| Works for pre-registered providers | yes, breaks ~8 h on GitHub | no | yes |
| Token at rest | plaintext file, sandbox-masked | Crew vault | Crew vault |
| Client secret at rest | vault + **plaintext agent spec** | vault (dropped by engine) | vault only |
| Token in harness memory | yes | yes | **no** |
| Refresh race handled | no | yes (re-reads on `invalid_grant`, KAS `MCPAuthProvider.invalidateTokens`) | yes, single-flight + CAS |
| Upstream revoke | no | no | yes |
| Agent shell can call vendor without approval | no direct path known | no direct path known | possible unless C4 gates (§4.3) |
| Crew code that handles token values | none | opaque store | OAuth client, refresher, proxy |

The proposal moves risk from "every harness" into "one Crew component". That concentrates review effort, and it concentrates blast radius.

---

## 5. Size estimate

Lines are product code / tests, a realistic range. Based on the sizes of the pieces reused (§3.2, `wc -l` at `aff16f9886`, unchanged at `4055dcd61e`).

| Component | Code | Tests | Notes |
|---|---|---|---|
| C1 OAuthClient (authorize, callback listener, swap, refresh call, revoke, Google extras) | 350-500 | 400-600 | Discovery, banner gate, client resolution reused |
| C2 TokenStore + vault `set_if_revision` | 110-150 | 120-180 | Vault reused |
| C3 Refresher, single-flight, rotation handling | 150-250 | 250-350 | Lock + CAS + re-read; race tests |
| C4 ConnectionProxy (streaming, attestation reuse, policy gate, pid pin, SEL) | 300-450 | 400-600 | Admission gates reused from `mediated_secrets.py` |
| C5 Bridge retarget to socket | 60-120 | 100-150 | `kas_http_bridge.py` reused |
| C6 Per-harness writer (KAS, session array, copilot file) | 150-250 | 200-300 | `kas_env_launch.py` reused |
| C7 Status/UI, migration, retire mint path | 150-300 | 150-250 | Frontend changes not separately sized |
| **Total** | **~1,300-2,000** | **~1,600-2,400** | plus 200-400 lines of docs/spec |

Without reuse, add roughly: new encrypted store (~600), transport attestation and member scope (~550), socket peer checks (already in `socketsec.py`), bridge (~270). About 1,200-1,500 lines saved.

### Phasing

| Phase | Scope | Size (code / tests) | Flag |
|---|---|---|---|
| 0 | Docs: amend `mcp-oauth-ownership.md`, the invariant in `docs/system-specs/modules/connections.md`, this RFC accepted. Optional: send KAS the 5-line fix (alt. a) | docs only | — |
| 1 MVP | GitHub, KAS only, POSIX only. C1-C5 + KAS part of C6 + minimal C7 | ~900-1,300 / ~1,100-1,600 | `dashboard.feature_previews.crew_oauth` (Settings > Feature previews), off by default; per-provider allowlist |
| 1b | Windows: bridge→gateway over verified loopback TCP (Amendment 2026-11-07) | ~50-80 / ~150-250 | same flag |
| 2 | Asana + six Google (offline params, agentic-client runbook note), refresh background pass | +150-250 / +200-300 | same flag |
| 3 | All other harnesses except kiro-cli (session array, copilot file), disconnect revoke for all, retire v2 mint; scope and order in [Amendment 2026-11-09](#amendment-2026-11-09-per-session-custody-on-every-harness-except-kiro-cli-and-the-pod-https-callback) | +250-450 / +300-500 | flag on by default after security sign-off |
| 4 (optional) | DCR providers via Crew (decided and phased in [Amendment 2026-11-08 (DCR)](#amendment-2026-11-08-dcr-cards-move-to-crew-owned-oauth)); per-member tokens; Windows named-pipe hardening | not sized | separate decision |

Everything through phase 3 can ship default-off: with the flag off, C6 writes today's `oauth` block and nothing else changes.

---

## 6. Alternatives considered

### (a) Fix KAS only

KAS adds `clientSecret` to `OAuthConfigSchema`, passes `client_secret` in the `MCPAuthProvider` constructor, and adds an `oauth` slot to `ClientAgentMcpServerSchema`. About 10 lines + tests in KAS, 20-40 in Crew (capability flag `ACP_BACKENDS_REMOTE_MCP_OAUTH` in `backends_mcp.py`, keep the block in the projection) *(earlier research)*. A draft KAS pull request tried the secret half in May and is stale *(earlier research)*.

- For: smallest; fixes the default backend; KAS already persists tokens via Crew's vault and handles the rotation race.
- Against: does nothing for claude/codex/others; Google still needs `access_type` (a further KAS field, or Crew's URL rewrite); the secret still crosses the wire into KAS `session.json`; every future harness repeats the work; depends on another team's release.

### (b) Push each harness to fix

- For: no new custody in Crew.
- Against: N teams, N schedules, N token stores, N refresh bugs. Some harnesses are third-party (claude, codex, copilot) and may never add "pre-registered client with secret plus extra authorize params". Crew cannot test or fix them.

### (c) Crew writes short-lived tokens into config

Crew runs OAuth but hands the harness the access token as an `Authorization` header.

- Against: this is "Path B" from `mcp-oauth-ownership.md`. The token sits in agent-readable config or, on KAS, in `session.json` (module docstring of `kas_env_launch.py`). Refresh needs a session restart or a config hot reload that most harnesses lack (`ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD` in `backends_mcp.py` lists only kiro for MCP config hot reload). The existing header bridge (#28615) would hide it from the wire but keeps it in a launch snapshot the sandboxed shell can read (module docstring of `kas_env_launch.py`). A token readable by the agent can leave the host; a proxy capability cannot.
- For: least new code (C1-C3 only).

### (d) This proposal

- For: one implementation for all harnesses; fixes refresh by design; adds upstream revoke and "connected as"; token never in the harness; reuses vault, attestation and bridge.
- Against: Crew now owns credential handling and a streaming proxy; the in-session agent risk (§4.3) is new; Windows needs separate work; ~1,300-2,000 lines to maintain.

### Recommendation

Do (a) in parallel if the KAS team will take it: it unblocks the default backend quickly and costs Crew almost nothing. Build (d) as the durable path, starting with the phase 1 MVP behind a default-off flag. Reject (b) as a plan (it can still happen opportunistically) and reject (c).

---

## 7. Open questions and decisions for the team

1. **Retire or rewrite the custody rule.** the rule in `mcp-oauth-ownership.md` and its echoes (`docs/system-specs/modules/connections.md`, `l1_smoke.py`, `mcp_discovery_remote.py`) must change. L1 verdicts change too: with a held token, L1 could run real `tools/list` instead of grading a challenge as `GRANT_HELD`. Owner: Connections maintainers.
2. **Security review owner** for C1, C3 and C4, especially §4.3. Who signs off before the flag defaults on?
3. **DCR providers.** Move them to Crew custody too (one model), or leave them on harness OAuth (less change, but non-KAS harness DCR support is unverified)? Closed by [Amendment 2026-11-08 (DCR)](#amendment-2026-11-08-dcr-cards-move-to-crew-owned-oauth): move them to Crew.
4. **Approval at the proxy.** How C4 knows a `tools/call` was approved by the harness gate. Options: C4 refuses calls without a matching approval record Crew's gate saw; or C4 applies only the static ceiling and grant list. Needs a decision with the governance owners.
5. **Per-member tokens on Shared Crew.** Keep gateway-wide grants (today's model) or scope tokens per member?
6. **Migration.** Existing v2 grants cannot be read without opening kiro-cli's files, which the current rule forbids. Proposal: ask users to reconnect once; do not import.
7. **Windows.** Closed by Amendment 2026-11-07: verified loopback TCP, as phase 1b.
8. **Google 100-refresh-token limit per account per client** *(earlier research)*: one Crew-held token per product per gateway keeps the count low, but six Google slugs on one client may each hold one. Share one Google token across slugs (scopes merged) or keep one per slug? Closed by [Amendment 2026-11-08](#amendment-2026-11-08-one-grant-per-google-slug-revoke-on-the-last): one per slug.
9. **KAS team ask.** Do we still file the KAS bug (a draft exists in earlier research) if (d) is accepted? Recommended yes: other KAS users need it.

---

## 8. Unverified items

- kiro-cli v2 `token.json` file mode (plaintext is from the earlier research; mode not checked).
- Whether claude, codex, opencode, goose, pi, copilot, strands, deepseek can do pre-registered or DCR OAuth for a Crew-projected entry if given the data another way.
- Vendor revoke endpoints and behaviour (GitHub, Asana, Google) live.
- Whether any of the eight vendors echo the bearer in a response body.
- The remote-gateway paste-back code path in `connections/mint.py` for pre-registered providers.
- WakaTime OAuth: designed in a note; implementation not found on main.
- Nothing in this RFC was measured live; all claims are from code and docs at the heads above.

---

## Amendment 2026-11-07: Windows bridge over verified loopback TCP, ahead of phase 2

Read at `0795ebc479`. Phase 1 shipped behind the flag in [#29543](https://github.com/kirodotdev/KiroCrew/pull/29543) and [#31821](https://github.com/kirodotdev/KiroCrew/pull/31821); [#31149](https://github.com/kirodotdev/KiroCrew/issues/31149) tracks its follow-ups. This amendment changes §3.3, §4.2, §4.6, the phasing table and open question 7. Each of those points here. Everything else stands.

**Threat scope.** Kiro Crew is a single-owner personal tool. Other local OS users are out of scope as a threat. The same-user rules stay: the bridge token, the pid pin (§4.3) and per-call approval at the proxy.

**Windows transport.** The bridge→gateway leg uses loopback TCP on `127.0.0.1`. Both ends check the connection before any credential byte moves:

| End | Check | Existing code |
|---|---|---|
| Bridge | Far-end pid by exact 4-tuple from the kernel TCP table must be the recorded gateway pid, with its start token and our SID | `tcp_verified_urlopen` in `loopback_http.py`; `_connected_peer_verdict` in `cli_server_peer.py` |
| Gateway | Peer pid by 4-tuple, then same user (SID); then the bridge token and pid pin as on POSIX | `_request_peer_pid` in `member_memory_auth.py`; `GetExtendedTcpTable` in `platform_compat_ports.py` |

A failed or ambiguous table lookup gives no pid, so the call is refused. A remote peer has no row in the local table, so it is refused too. The TCP arm of `_request_peer_pid` does not check the owner today; the Windows code adds that check first.

**POSIX does not change.** The bridge stays Unix-socket-only with the peer-credential checks. There is no TCP fallback, even when the socket file is missing.

**Why not a named pipe first.** A pipe stops other users before they connect. With other users out of scope, that is depth, not the core control. It would also need a new HTTP-over-pipe client and a new local-IPC arm in the shared Unix-socket request check. The pipe stays optional later hardening (phase 4). CPython does not expose AF_UNIX on Windows ([cpython#77589](https://github.com/python/cpython/issues/77589)).

**Phasing.** Windows moves ahead of phase 2 as phase 1b. Open question 7 is closed.

**Later.** [MCP-over-ACP](https://agentclientprotocol.com/rfds/mcp-over-acp) (a draft) would let the harness reach the Connection over its existing ACP link. That removes the bridge on every OS. KAS 3.1.0 does not advertise it, so it is tracked, not planned.

## Amendment 2026-11-08: DCR cards move to Crew-owned OAuth

Read at `43ac0cd051`. Phase 1 shipped behind the flag in [#29543](https://github.com/kirodotdev/KiroCrew/pull/29543); [#33039](https://github.com/kirodotdev/KiroCrew/pull/33039) adds Asana and Google as phase 2. This amendment closes open question 3 and sets the scope and order of the DCR work that phase 4 left unsized. Everything else stands. Source: the author's research notes `research/crew-oauth-dcr.md` (not in this repository), sections 5.2 (row 0), 5.4 and 5.5, which hold the live probe of all 30 DCR cards (2026-11-08).

**Decision.** The 30 DCR Connection cards move to Crew-owned OAuth, and owner-added remote MCP servers follow later. Crew registers its own client at each vendor's `registration_endpoint` (RFC 7591), holds the client and the tokens in the vault, and serves the harness through the same proxy and bridge as GitHub. One generic engine covers every card; per-card facts are registry data, with no vendor-specific Python.

**Client name.** Crew registers as `client_name: "Kiro Crew"`. A card may set an optional `dcr_client_name` override for a vendor that admitted `kiro` (the name KAS registers), so an existing admission is not lost. Figma and Zapier key on this name: Figma admits by catalog, and Zapier shows one MCP server per client name.

**Issuer.** A registry card pins the committed `l0_expectations.authorization_server`, the same invariant as the L0 probe. If the advertised `authorization_servers[0]` differs, sign-in fails with `issuer_changed` and is audited. An owner-added server has no committed issuer, so it follows the advertised one through the vetted fetch in `mcp_oauth_discovery.py` (SSRF vet, pinned connector, HTTPS:443, no redirects). Crew never falls back to guessed `/authorize`, `/token` or `/register` paths.

**Migration.** No import of KAS or kiro-cli v2 grants. Each card needs one reconnect when custody flips (open question 6 already chose this for v2).

- KAS entries sit in Crew's vault, but `mcp_grant_kas.py` promises never to decrypt them, the key format is a KAS internal, and their refresh token is bound to KAS's `kiro` client.
- **Cleanup at the flip.** After Crew's own sign-in for a card succeeds, Crew deletes that URL's old harness grant: the KAS `kiro.mcp.<hash>.*` entries through the KAS store's own delete (Crew decrypts nothing), and the v2 files through the existing census in `mcp_grant.revoke_local_grant`. This stops two live grants fighting where the vendor keeps only the last one (Miro) or shows one server per client (Zapier). The old upstream grant stays at the vendor; the card says so once, with the `revoke_page_url` link.

**Callback.** One fixed loopback port for every DCR card, `http://127.0.0.1:<port>/oauth/callback`, registered as the `127.0.0.1` literal. The port sits beside the registry's per-provider block, and the registry validator reserves it. The callback's `state` routes it to the pending sign-in (constant-time compare, single use, 600 s TTL). A fixed URI also gives vendors with a redirect allowlist (Canva) one concrete value to admit.

- **Later, separately reviewed:** an HTTPS callback route on the gateway's own public URL for pods, `https://<gateway host>/api/connections/oauth/callback`. It ends paste-back for DCR cards on a pod. It is an unauthenticated route, so it gets its own security review before it lands. Until then, a pod uses the loopback URI, and paste-back hands the URL to the in-process sign-in.

**Spec target.** The [2025-11-25 MCP authorization spec](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization), which for a client is a superset of 2025-06-18; rmcp 3.0 and KAS follow it. That means: `resource_metadata` from the 401 challenge, path-aware AS metadata discovery, S256 only (refuse when not listed, ignore `plain`), `resource` (RFC 8707) on authorize, token and refresh, and the callback `iss` check (RFC 9207) when advertised. The token auth method comes from metadata: `none` when listed, else `client_secret_basic` or `client_secret_post`; five issuers do not list `none`. `invalid_client` triggers re-registration; `invalid_grant` never does.

- **DCR first.** All 30 issuers advertise `registration_endpoint` and S256.
- **CIMD deferred.** A Client ID Metadata Document needs an HTTPS URL Crew controls. Reusing Kiro's document would make Crew appear as Kiro. 13 of 30 issuers support CIMD today.

**Phasing.** This replaces the phase 4 "DCR providers via Crew" item.

| Step | Scope | Gate |
|---|---|---|
| 4a | Engine (discovery, DCR register and client entry, metadata-driven endpoints, RFC 7009 revoke, shared callback) + Atlassian and the other five visible DCR cards (notion, linear, stripe, vercel, gitlab) as data, in one code PR | same `crew_oauth` flag, default off; live check per card |
| 4b | HTTPS gateway callback route for pods (rules in Amendment 2026-11-09) | own security review |
| 4c | The 20 launch-gated DCR cards, each as its launch gate passes | data only |
| 4d | Owner-added remote MCP servers (URL-keyed vault entries, owner-record matcher, advertised issuer) | different trust input |
| 4e | figma, canva, dropbox, square | after each vendor admits Crew's client |

Atlassian is the first live card: its KAS sign-in is the worst today (the 60 s window plus pod paste-back), and its path issuer exercises the hardest discovery branch. Linear is the fallback if Atlassian refuses Crew's redirect. Step 4a rebases on #33039 once it merges, because both touch `_ENDPOINTS`, `_revoke` and `SUPPORTED_SLUGS`.

**Security-review delta** (against the merged GitHub MVP and #33039):

1. **Outbound registration POST** to a URL taken from vendor metadata. Mitigated by the committed-issuer pin for registry cards, plus the SSRF vet, pinned connector, HTTPS:443, no redirects and a bounded body for all.
2. **New secret kinds in the vault:** the DCR `client_secret` and `registration_access_token`, in a new `CONNECTIONS_<SLUG>_OAUTH_CLIENT` entry. Same vault, same never-log rule (`_audit` writes coarse codes only).
3. **Unauthenticated HTTPS callback route** (step 4b): admission by pending `state` only, single use, TTL, no reflection of input, `no-store` and `no-referrer`, redirect host from Crew config and never from the request `Host` header.
4. **Mix-up defence:** the `iss` check (RFC 9207) and `resource` binding (RFC 8707).
5. **Owner-added servers** follow an advertised issuer, bounded by the owner record in sealed `config.json` and the existing consent-URL banner gate.
6. **Cleanup at the flip** deletes KAS entries through the store's own delete; Crew decrypts nothing.

Unchanged: per-call approval at the proxy, the pid pin, the bridge transport (POSIX socket, Windows verified TCP) and the §4.3 residual risk.

**Unverified, needs live tests.** Whether each vendor accepts Crew's `127.0.0.1` redirect and `Kiro Crew` client name; refresh rotation per vendor; the public URL shape of a Crew pod for step 4b.

## Amendment 2026-11-08: one grant per Google slug, revoke on the last

Phase 2 adds Asana and the six Google slugs as data entries in `connections/crew_oauth.py` (endpoints, the revoke call's form, Google's `access_type=offline` + `prompt=consent`). This amendment closes open question 8. Everything else stands.

**Decision.** Each Google slug keeps its own grant and vault entry, as GitHub and Asana do. The six-product limit (at most six refresh tokens per gateway for one client) stays well under Google's 100.

**Revoke.** One Google client usually serves every Google card (the registration guides say so), and Google's revoke ends the user's whole grant for that client. So Disconnect on a Google card revokes upstream only when no other Google card on the same client id and the same Google account still holds a pair; otherwise it deletes only its own vault entry. The account is the `sub` of the id_token Google returns because the consent adds the `openid` scope; it is stored as `account` in the pair (C2) and never logged, beside the `client` id that issued the pair; both are compared as stored, not as configured now. Google revokes across every client of one Cloud project, so for Google the client is compared by its project number (the client id's leading digits). An unknown account or client revokes. A Google code swap and its write hold the same grant-group lock as Disconnect. The last Google card to disconnect revokes. Asana and GitHub revoke as before.

**Not chosen.** One shared Google grant (merged scopes, one Connect for all six) would change the card's shape; it stays open for a later RFC.

## Amendment 2026-11-09: per-session custody on every harness except kiro-cli, and the pod HTTPS callback

Read at `851ba86e41`. Phase 1 shipped in [#29543](https://github.com/kirodotdev/KiroCrew/pull/29543), phase 2 in [#33039](https://github.com/kirodotdev/KiroCrew/pull/33039), and step 4a (the six visible DCR cards) in [#33077](https://github.com/kirodotdev/KiroCrew/pull/33077), all on KAS behind the `crew_oauth` flag. This amendment sets the scope and order of phase 3 ("all other harnesses") and the rules of step 4b (the pod callback route). Everything else stands. Sources: the author's research notes `research/crew-oauth-all-harnesses.md` (sections 4.1-4.6, 5, 7, 8) and `research/crew-oauth-dcr.md` (sections 3 and 5.5), not in this repository.

**Custody is decided per session.** Today `manages(slug)` in `connections/crew_oauth.py` reads the global `agent.acp_backend` (`_chat_backend_is_kas`), and the proxy re-checks it on every call (`crew_oauth_proxy.py`). With mixed fleets (a Claude chat beside a kiro-cli crewmate) that is wrong for one of them. The gate splits in three:

- `custody_on(slug)`: flag on, POSIX, supported slug. Global.
- `serves_backend(backend)`: the session's own backend is in `ACP_BACKENDS_CREW_OAUTH_BRIDGE` (new, in `agent_sdk/backends_mcp.py`, beside the other `ACP_BACKENDS_*` sets).
- `manages(slug, backend)`: both. The projection passes the session's backend. The UI keeps calling it without one, so the card follows the chat backend as today.

The proxy re-check becomes `custody_on(bridge.slug)`: registration already proved the session was in custody, and a backend read there would refuse a codex bridge whenever chats default to KAS.

**kiro-cli never.** Custody never applies to a kiro-cli session; it keeps its native `oauth` block. A test pins `ACP_BACKEND_KIRO` out of the set.

**Allowlist, not `!= kiro`.** The set starts as `{kas}`. A harness joins only with a live round-trip capture (`test/fixtures/acp_frames/<harness>/crew-oauth-bridge-live.jsonl`): mount, `initialize` and `tools/list` through the proxy, `tools/call` raises `session/request_permission`, approval minted, call forwarded, result returned, "allow always" downgraded. A harness that joined before its approval channel was proved would have every call refused. The end state is every backend except kiro-cli.

**Spec secret.** The agent spec file is what kiro-cli reads, and today custody strips `oauth.clientSecret` from it (`default_spec_commit.py`). Per-session custody keeps the secret in the owner-only spec while any kiro-cli session can still start (chat, member or task backend), so kiro-cli's native sign-in keeps working. It is stripped only when none can. This is the same exposure as with the flag off; custody no longer removes the secret from disk in a mixed fleet.

**Allow always becomes allow once.** On every harness, an "allow always" answer for a bridged server stops later asks, and the proxy then refuses those calls (each call needs its own minted approval). Crew answers with the once id whenever the server is a Connection, generalising today's copilot rule in `client_permissions.py` and `session_handle_permissions.py`. Crew's own approval memory still saves the user from re-clicking.

**Warm-pool rekey.** Bridges are keyed by `session_key` at projection. A warm-pool claim rekeys the client (`rekey` in `acp/client_lifecycle.py` and `AcpSessionProvider.rekey`). The bridge follows: both rekeys call a new `crew_oauth_proxy.rekey_session(old, new)`. Without it, approvals are looked up under the new key, nothing is minted and every call fails closed.

**Shared projection.** One helper rewrites each Crew-owned Connection element in the `session/new` array into the shim stdio element, at the two array call sites (the AcpClient composition in `client_mcp_array.py` and codex's in `runtime_session_create.py`). Copilot and Strands pick the element up in their launch files unchanged. The array path removes its snapshots at close, as KAS does at provider shutdown; the boot sweep covers crashes.

**Rollout.** Each step is one code PR, flag still default-off.

| Step | Harness | Own work |
|---|---|---|
| 3a | claude, with the gate split, shared helper, downgrade and rekey | none Claude-only |
| 3b | opencode, deepseek, strands | capture only; opencode and deepseek also need a trusted MCP server identity (below) |
| 3c | copilot | scheduled (follow-up code PR): first check whether Copilot's own login already works for a client-supplied GitHub element; if so, custody swaps that grant for Crew's |
| 3d | codex | scheduled (follow-up code PR): unwrap `{server, tool, arguments}` in `_call_arguments`; strip `annotations` from `tools/list` so `readOnlyHint` tools still ask |
| 3e | goose | ruled 2026-11-09 (below): `rawInput` carries the exact forwarded arguments, so the args digest binds; no name-only binding |
| 3f | pi | approved 2026-11-09 (below): receives the Crew-owned Connection element through the sealed MCP handoff file |
| 3g | flag default-on | after security sign-off, as in §5 |

The annotation strip applies to every bridge: it is harmless on KAS, which already forces `ask`.

**Owner rulings and corrections, 2026-11-09 (steps 3b, 3e, 3f).**

- **3f pi: approved.** The owner accepts the product-shape change. With custody on, pi (broker-only) receives the Crew-owned Connection element from the spec, rewritten to the shim stdio element, through the sealed per-session MCP handoff file (`KIROCREW_PI_MCP_HANDOFF` in `acp/harness/pi.py`), the only path that carries Crew's servers into pi. Same-name rule: a user's own `github` (or any same-named) entry in pi's `mcp.json` wins. Crew's element is then not mounted, and the panel says why, using the existing `mcp_json` reason in `acp/pi_mcp_registration.py`. pi still joins the allowlist only with its live capture.
- **3e goose: no name-only binding.** The approval stays bound to session, server, tool and args digest, as on every other harness. Binding by tool name only is rejected. The row's earlier premise was wrong: goose's `rawInput` is the exact forwarded arguments (block/goose `0edf2bdc`, `conversion.rs` 92-157 and `tool_approval.rs` 135-149); `{"": "{}"}` was a call with no arguments. So goose can bind the digest and joins with its live capture like the others.
- **Real blocker for goose, copilot, codex, pi and strands.** The enforced-harness sandbox mask hides `kas-mcp-launch`, so the bridge never starts there. The fix is a narrow `crew-oauth-bridge` leaf that exposes bridge snapshots only, in its own code PR.
- **opencode and deepseek** stay out of the allowlist for now (left out of #33597): their permission frames name no trusted MCP server, so the approval cannot be bound to one. They join once a trusted server identity is found.

**Pod HTTPS callback (step 4b).** Pods get `https://<gateway host>/api/connections/oauth/callback` for DCR cards, which removes the paste-back step there. The route carries no dashboard token, because a vendor redirect carries no Crew credential. Its rules:

- admits only a `GET` whose `state` matches a pending sign-in (constant-time compare); single use; gone after 600 s;
- never reflects input; answers a fixed page with `Cache-Control: no-store` and `Referrer-Policy: no-referrer`;
- the redirect host comes from Crew config (the pod's front or tunnel URL), never from the request `Host` header;
- SEL-audited like the other sign-in events.

The code alone is useless without the in-memory PKCE verifier and, for a confidential client, the vault secret. Pre-registered cards (GitHub, Asana, Google) keep the loopback and paste-back: their redirect URI is fixed in the operator's app. When the pod URL changes, the stored `redirect_uri` no longer matches and Crew re-registers the DCR client.

**Security-review delta** (against the KAS MVP, #33039 and #33077):

1. **No new bypass class.** The proxy gate is unchanged: bridge token, pid pin, and a single-use approval bound to session, server, tool and args digest. A harness that does not ask cannot reach the vendor. Pre-approval in a harness's own config (Claude settings, goose `permission.yaml`, codex `config.toml`) only causes refusals.
2. **New trusted inputs per harness.** Approval binding now trusts each harness's MCP identity and args channel (Claude placed-server adoption, codex `_meta` marker and `rawInput`, copilot title match, gate envelopes on deepseek and strands). Each was reviewed for tool gating; binding vendor egress to them is new and is reviewed per harness, with its capture.
3. **Response rewriting.** The annotation strip makes the proxy parse `tools/list` answers. It is size-bounded and never forwards a partly parsed answer.
4. **Allow-always downgrade** tightens every harness.
5. **Same residual as §4.3.** Harnesses that start MCP children lazily widen the window before the first pid pin; each capture must show the bridge connecting before the first prompt.
6. **Spec secret** stays in the owner-only spec in a mixed fleet: no new exposure.
7. **Rekey** failure is an availability issue (fail closed), not a leak.
8. **Unauthenticated HTTPS callback route** on pods, bounded by the rules above. The mix-up defence (`iss`, RFC 9207; `resource`, RFC 8707) from the DCR amendment applies to it unchanged.
9. **Unchanged:** Windows bridge transport, `kirocrew chat` (no gateway port, native path), entries that send a credential header (native path).

**Unverified, needs live tests.** The `rawInput` shape on opencode, deepseek, copilot, pi and strands; a user-scope `github` server clashing with Crew's on Claude, codex and pi; codex keeping annotation-stripped tools listed; whether KAS has the allow-always and rekey gaps too; the public URL shape of a Crew pod; whether a remote harness host keeps the native path.

## Amendment 2026-11-09: bridge snapshot leaf for enforced harnesses, and pi's envelope owner

**Why.** codex, copilot, pi and strands are enforced harnesses: their child runs under a credential mask that withholds `kas-mcp-launch`, because that leaf holds other MCP servers' env values. The bridge child could not read its own snapshot there, so it never started.

**Decision.** Connection bridge snapshots move to their own leaf, `crew-oauth-bridge`. Each holds a slug, the gateway port and a per-bridge token; no vendor token, client secret, url or other server's env. It is sealed read-only and pre-created like `kas-mcp-launch`, fenced from the agent's file tools by the read-gate floor, and readable by an enforced harness's child. `kas-mcp-launch` stays withheld. The launcher accepts only a Connection snapshot from the new leaf.

**Transport under the mask.** The same mask hides `.local_secret` and `run/`, so such a bridge cannot send the internal secret or check the gateway record. It reaches the proxy on the agent socket instead ([rfc-sandbox-ipc-capability.md](rfc-sandbox-ipc-capability.md)): its element carries a one-time pickup bound to the session's stub token and runtime tree, the bridge redeems it for a capability bound to its own pid, and checks the listener is the gateway pid the element named. The pickup goes only to an element whose executable and whole argv are the launcher's, naming a registered bridge of that session and server, and its capability is admitted on `/api/connection-proxy` only. The proxy also requires the capability's session to be the bridge's.

**Not per session.** A view of only this session's snapshot is not possible on every harness: the sandbox is built when the process spawns, and one codex process serves many sessions whose bridges are written later. The shared leaf is the narrowest grant that works everywhere.

**Security delta.** An enforced child of one session can read another session's bridge token. That token alone does nothing: the proxy pins each bridge to the first process that used it, and forwards a `tools/call` only against an approval Crew's gate minted for that session, server, tool and arguments. The same-session residual (§4.3) is unchanged.

**pi identity.** pi's permission frames name no trusted MCP server. Crew's own pi gate extension now adds `mcp: {server, tool}` to its nonce-bound envelope, read from its registration table (the servers it mounted from the handoff, by pi's tool prefix), never from the title. Crew reads it only for the Connection approval and only for a server it handed that session. It never sets the identity other auto-approve gates trust.

**Members.** codex, copilot, pi and strands join `ACP_BACKENDS_CREW_OAUTH_BRIDGE`. The proxy strips `annotations` from `tools/list` answers, so codex asks for `readOnlyHint` tools too.

**goose and deepseek (rows 3b, 3e).** They join the same set and use the same leaf. goose names the server in `_meta.goose.toolCall`; its `rawInput` is the `arguments` it sends to the MCP server, captured live in ask mode (`acp_frames/goose/crew-oauth-bridge-args-live.jsonl`). dsh has no identity channel: it names an MCP tool by the public name it registers and dispatches by, the `tool_call` title `mcp__<server>__<tool>`, and its `rawInput` is the arguments its MCP client sends (`acp_frames/deepseek/crew-oauth-bridge-args-live.jsonl`). Crew splits that title only for a Crew-owned Connection server, only when exactly one placed server fits, and reads the pair only for the Connection approval, like pi's envelope owner. It never sets the identity other auto-approve gates trust.

**opencode stays out (row 3b).** Its live ask-mode capture (`acp_frames/opencode/crew-oauth-bridge-args-live.jsonl`, opencode 1.19.1) shows the `tool_call` and the `session/request_permission` frames both carry `rawInput` `{}`; the arguments arrive in the in-progress `tool_call_update` sent after the answer. An approval minted at the ask can therefore bind only `{}`, and every call that has arguments is refused. It joins once opencode states the arguments on the ask.

## Amendment 2026-11-11: owner-added remote MCP servers sign in through Crew, no new cards

**Why.** A user adds a remote MCP server by hand (Customize → Connections → MCP Servers) with a `url` and often an `oauth` block. Today the harness runs that sign-in. Two failures follow: KAS ignores a `callbackUrl` and listens on a random port the vendor app never registered (a Slack app needs one exact `http://localhost:<port>/callback`), and several Crew sessions race for the same callback port. The owner ruled (2026-11-11): support this user-defined shape with Crew-owned OAuth, do not add a card per vendor, leave kiro-cli as it is.

**Decision.** An owner-added server runs the same chain as a DCR card: the gateway runs the consent, swap and refresh; the pair is in the vault; sessions get the stdio bridge; the gateway adds the bearer. Only the gateway listens, once, for one sign-in.

- **Opt-in.** Signing in through Crew is the opt-in, so no new config field. With the preview flag on and a bridged chat backend, the MCP Servers page's **Sign in** (`POST /api/connections/mint {server}`) runs Crew's sign-in instead of the harness mint. A session bridges the entry only while Crew holds its grant. Before that, the entry keeps the harness's own OAuth, as today.
- **Which entries.** Only a server the owner recorded from the dashboard (`connections/owner_servers.py`, sealed `config.json`), at the exact recorded `url`, under a name that is not a registry slug. An entry that sends its own credential header keeps its own path, and so does a legacy `type: "sse"` entry, because the bridge speaks streamable HTTP only. A server an agent added or re-pointed has no matching record and is never bridged.
- **Live sessions.** A session started before the first grant still holds the harness's own entry. When Crew stores that grant, every live provider that applies an MCP change at its next turn (KAS re-injection, Strands relaunch) is marked, so it re-projects and picks up the bridge without a reset. Any other session picks it up at its next start.
- **Grant key.** `custom-<sha256(name, url)[:20]>`. The vault entries are `CONNECTIONS_CUSTOM_<KEY>_OAUTH_TOKENS` and `..._OAUTH_CLIENT`. A changed `url` is a new key, so a token issued for one url never reaches another.
- **Endpoints.** Always discovered from the entry's `url` with its query removed (RFC 9728, then RFC 8414 / OIDC), with the same vetted, pinned, HTTPS:443, no-redirect fetches as a DCR card. The query-free url is also the `resource` and the audited url, so a query-string credential never reaches the AS or the SEL. No endpoint is guessed or typed. There is no committed issuer: Crew follows the issuer the url advertises, as the harness does, because the url itself is the owner's sealed record. The callback `iss` check (RFC 9207) and `resource` (RFC 8707) apply.
- **Client.** With `oauth.clientId`, the owner's own client: a public PKCE client (Slack), or a confidential one when `oauth.clientSecret` is set. It is stored only together with the pair its code swap returned, in one vault write (`SecretVault.set_many_sync`), so a declined consent, a refused replacement or a failed write leaves the old pair with the client that issued it; `invalid_client` forgets only the stored client. The scopes asked for are the entry's own (`kiro_entry_scopes`, either spelling), ahead of a challenge's. With no `clientId`, Crew registers its own client by DCR, as for a card, but stages it the same way: it is stored only with its first pair, so a sign-in that fails after a re-registration keeps the working grant. An AS with neither fails closed (`dcr_unsupported`).
- **Port.** The owner's `oauth.redirectUri` when it is `http://localhost|127.0.0.1:<port>/<path>`: Crew binds that port on 127.0.0.1 for this one consent (600 s) and closes it. Any other `redirectUri` is refused (`redirect_unsupported`), because Crew could not receive the code. Without one, the shared DCR port 48100 with `state` routing, or the gateway's public HTTPS route on a pod.
- **Consent host.** No list is widened. The sign-in's own discovery, from the owner-recorded url, found the `authorization_endpoint`; only that exact host and path are recorded for the banner gate (`record_discovered_oauth_endpoint`, the same 24 h approval a harness-minted link for an owner server gets). The owner's click on their own recorded server is the confirmation.
- **Secret custody.** A `clientSecret` is copied at sign-in into the vault client entry, which the refresh reads. It never reaches a harness: KAS already drops an `oauth` block naming one, and the bridge entry carries no `oauth`. The agent spec keeps the owner's copy, because kiro-cli reads it (out of scope).
- **Registry providers.** Unchanged. The new branch runs only for a name outside `SUPPORTED_SLUGS`.
- **Cards first (owner ruling, 2026-11-12).** A Connections card is a preset over this path. An entry whose name is a registry slug, or whose url is a registry provider's `mcp_url` under any name, keeps that provider's path; the owner-server path serves only entries matching no provider, so a card added later (Slack) never shares its port or vault key with a hand-added entry.

**Out of scope, follow-ups.** A server Crew's discovery cannot vet (a private host, a port other than 443, no RFC 9728 document) fails closed with `discovery_failed` while the preview is on; it signs in through the harness with the preview off. A Crew-held grant does not yet show on the MCP Servers row, and removing the server does not delete its vault entries (they are keyed by name and url, so nothing uses them). The bridge's `needs_auth` answer names the MCP Servers page.

**Security delta.** (1) A new trusted input: an owner-recorded url's advertised issuer. It is bounded by the sealed record, the discovery match on the consent endpoint and `iss`. (2) The owner's loopback port is bound only during one consent. (3) No change to the proxy gate (bridge token, pid pin, single-use approval).
