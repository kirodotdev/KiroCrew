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

**Proposal.** Kiro Crew runs OAuth itself, in the gateway: consent URL, code swap with the secret, refresh, revoke. Tokens live in Crew's existing encrypted vault. Each harness gets a plain **stdio** MCP server (a tiny bridge) that forwards to the gateway over Crew's owner-only, kernel-attested Unix socket. The gateway adds `Authorization: Bearer` and forwards to the vendor. The token never enters the harness, the sandbox, the wire or the transcript. Any harness that can start a stdio MCP server works with no OAuth code of its own.

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
| C5 | **Bridge** (inside the harness/sandbox) | Stdio MCP server; forwards each message to C4 over the Unix socket. Holds no token and no vendor URL. | Adapt `kas_http_bridge.py` (269 lines, #28615): today it POSTs to an `https://` URL with headers from a launch snapshot; change the target to the gateway socket. Launched through `kas_env_shim.py` (234 lines) |
| C6 | **Per-harness config writer** | Replace each Connection entry's `url` + `oauth` with the bridge stdio entry. KAS: in the `kas_agents.py` projection. claude/codex/opencode/goose/deepseek/pi/strands: the `session/new` MCP array. copilot: its `--additional-mcp-config` file, since it rejects stdio on `session/new`. | Mostly reuse: `acp/kas_env_launch.py` (372 lines), `ACP_BACKENDS_SESSION_MCP_ARRAY` in `backends_mcp.py` (and the copilot/strands file routes beside it). Stop writing `oauth.clientSecret` into any spec |
| C7 | **Connections status/UI** | Read state from C2 (connected, account, expiry, `needs_auth`). Disconnect = revoke upstream + delete entry. | Edits to `connections/status.py`, `mint.py` retirement path, `mcp_grant.py` kept only for migration |

### 3.3 The closest precedent: mediated secrets

`kirocrew-secrets` already does gateway-side injection of a vault secret into an outbound HTTPS call, so the value never reaches the agent:

- The tool runs inside the agent sandbox, holds only the secret **name**, and forwards non-secret intent to `POST /api/mediated-secret-request` (module docstring of `mcp_secrets.py`).
- Transport is the owner-only dashboard Unix socket, **no TCP fallback**; the client checks the answering peer is the genuine gateway pid before sending credential bytes (`_call_tool_inner` in `mcp_secrets.py`). Windows is refused outright (same function).
- The gateway admits only internal authority + `X-Session-Key` + an **attested** member scope (`_admit` in `mediated_secrets.py`). Attestation is either Unix-socket kernel peer ancestry or a gateway-signed per-session token (`session_key_is_attested` in `member_memory_auth.py`).
- Each call needs a single-use capability minted by the ACP approval of that tool call (the `X-Mediated-Secret-Capability` check in `mediated_secrets.py`, `mediated_request_capability.py`, 423 lines).
- Dispatch SSRF-vets and DNS-pins the destination and resolves the secret only at send time (module docstring of `mcp_secrets.py`; `secrets_mediation/dispatch.py`, 440 lines). Everything is SEL-audited (`_audit` in `mediated_secrets.py`).

What this RFC **reuses** from it: the vault, the Unix-socket transport and peer check, `member_request_scope` / `session_key_is_attested`, the SEL audit call, the "value never leaves the gateway" discipline, and the Windows refusal stance.

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

**Disconnect.** C1 revokes upstream where the vendor has an endpoint (GitHub, Google, Asana: endpoint names per vendor docs, **unverified live**), C2 deletes the entry with `delete_if_revision`, C4 drops open sessions for that slug.

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
- Windows: the attested Unix-socket channel does not exist; `_call_tool_inner` in `mcp_secrets.py` refuses. The proxy can use `mcp_gateway`'s named-pipe transport with SID checks (`GATEWAY_SUPPORTED_PLATFORMS` in `mcp_gateway/__init__.py`, the platform table in `socketsec.py`), but that path is **not built for this use**. MVP: POSIX only, flag refuses on Windows.

### 4.7 Callback listener

- Binds `127.0.0.1:<registry port>` only while one sign-in is pending; closes after one callback or a timeout.
- Another local process could hold the port first. It would get the code but cannot redeem it: it lacks the PKCE verifier (memory only) and the client secret (vault). The sign-in then fails visibly; C1 reports "port busy".
- `state` is checked exactly; only one pending sign-in per provider.

### 4.8 Revocation and disconnect

Crew-owned custody adds upstream revocation, which today is impossible (`mcp-oauth-ownership.md`, "How this works today"; `revoke_local_grant` in `mcp_grant.py` only unlinks local files). Disconnect: revoke upstream (best effort, report failure), delete the vault entry, end live proxy sessions. A vendor-side revoke (user removes the app at the vendor) shows up as a refresh failure and flips status to `needs_auth`.

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
| 1 MVP | GitHub, KAS only, POSIX only. C1-C5 + KAS part of C6 + minimal C7 | ~900-1,300 / ~1,100-1,600 | `connections.crew_oauth: false` default; per-provider allowlist |
| 2 | Asana + six Google (offline params, agentic-client runbook note), refresh background pass | +150-250 / +200-300 | same flag |
| 3 | All other harnesses (session array, copilot file), disconnect revoke for all, retire v2 mint | +250-450 / +300-500 | flag on by default after security sign-off |
| 4 (optional) | DCR providers via Crew; per-member tokens; Windows named pipe | not sized | separate decision |

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
3. **DCR providers.** Move them to Crew custody too (one model), or leave them on harness OAuth (less change, but non-KAS harness DCR support is unverified)?
4. **Approval at the proxy.** How C4 knows a `tools/call` was approved by the harness gate. Options: C4 refuses calls without a matching approval record Crew's gate saw; or C4 applies only the static ceiling and grant list. Needs a decision with the governance owners.
5. **Per-member tokens on Shared Crew.** Keep gateway-wide grants (today's model) or scope tokens per member?
6. **Migration.** Existing v2 grants cannot be read without opening kiro-cli's files, which the current rule forbids. Proposal: ask users to reconnect once; do not import.
7. **Windows.** Ship POSIX only, or build the named-pipe path in phase 3?
8. **Google 100-refresh-token limit per account per client** *(earlier research)*: one Crew-held token per product per gateway keeps the count low, but six Google slugs on one client may each hold one. Share one Google token across slugs (scopes merged) or keep one per slug?
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
