---
title: Settings MCP sign-in runs through KAS into the owner's grant vault
status: draft
author: Bolin Chen
created: 2026-11-02
last-audited: 2026-11-02
audited-at: e144d032c0
doc-pr: null
implementation-prs: []
tracking-issues: [24693]
supersedes: []
superseded-by: []
---

# RFC: Settings MCP sign-in runs through KAS into the owner's grant vault

- Status: draft. Needs a design sign-off from a maintainer before it leaves draft.
- Builds on the per-user grant vault (#24677). Measured on that branch at
  `e144d032c0`; nothing here is on main yet.
- Changed default: none. `agent.kas_persist_mcp_sign_in` stays off; this path
  runs only when the owner has turned it on.

## Summary

On the KAS backend, Settings > MCP > Sign in starts the sign-in through a KAS
engine process instead of through kiro-cli. That process is bound to the owner,
so the token the engine stores lands in the owner's per-user grant vault, which
every later owner KAS chat reads. Nothing is copied between stores.

## Motivation

Today the Settings button (`McpRowSignIn.tsx` -> `POST /api/connections/mint`)
always spawns a dedicated kiro-cli process (`connections/mint.py`,
`start_oauth_mint`). Its grant lands in kiro-cli's own OAuth cache
(`mcp_grant.py`). A KAS chat never reads that cache: its engine keeps grants in
process memory, or, with `agent.kas_persist_mcp_sign_in` on, in
`<data_home>/kas/mcp-grants/` through `_kiro/secret/*`
(`acp/kas_secret_storage.py`). No code in `src/kiro_crew/connections/` reads
`acp_backend`, so the mint is backend-blind.

An install moved to KAS by the 0.11 default switch therefore loses every remote
OAuth server, and signing in again from Settings cannot bring it back.

## Goals

1. On KAS, a Settings sign-in completes a grant that a later owner KAS chat uses
   without asking again.
2. The grant lives only in the owner's per-user vault from #24677.
3. Every guard the kiro-cli mint applies applies here too.

## Non-goals

- Changing the default of `agent.kas_persist_mcp_sign_in` (a separate change).
- Copying or translating a kiro-cli grant into the KAS vault, in either
  direction.
- Signing in for a non-owner user from Settings. The route is owner-only.
- Connections cards. Their status, premint and Disconnect read only kiro-cli's
  cache, so a card's Connect keeps the kiro-cli mint on every backend. The mint
  route takes the KAS path for a registry provider only when the body carries
  `surface: "settings"`, which the Settings MCP table sends; an owner-added
  server (`{server}`) is signed in only from that table, so it always does.
- A grant badge that reads the vault. Crew cannot compute the engine's key (see
  Design, "Badge"), so the row reports the mint's own outcome instead.

## Design

### Token flow

```
McpRowSignIn (owner)
  -> POST /api/connections/mint {server} | {slug, surface: "settings"}
                                                   owner-only, URL from the owner's spec or registry
  -> KAS and persistence on?  no  -> start_oauth_mint   (kiro-cli, unchanged)
                              yes -> start_kas_mint     (connections/mint_kas.py)
       persistence turned off since that check -> row failed, kas_sign_in_not_kept
       write the same single-server spec kirocrew-mint-<alias>
       bind key connections-mint-kas-<slug> -> "owner"   (mcp_grant_principals.bind)
       spawn one KAS AcpRuntime(agent=that spec, mcp_grant_key=that key)
         handshake: principal_for(key) == "owner" -> secretStorage declared,
                    SecretStorageServer("owner", key)
         no storage declared (key conflicted, config flipped) -> row failed,
                    reason kas_vault_unavailable; process disposed
       create one promptless session; init drain reads _kiro/mcp/status
         server connected      -> granted (the vault already held a working grant)
         server not reported   -> failed, mint_server_absent
         failedAuthorization   -> AcpRuntime.begin_mcp_sign_in(session, server)
       _kiro/openExternalUrl -> session queue -> pop_pending_oauth_requests
       credential guard (oauth_url_contains_credential) -> row waiting + oauth_url
  -> GET /api/connections/mint?server=… (unchanged feed)
  user consents; the engine's redirect lands on its own loopback listener
  engine: _kiro/secret/store kiro.mcp.<h>.tokens -> owner vault
  engine: _kiro/mcp/status connected -> still_bound()? -> granted, dispose process
                                                    no -> failed kas_vault_unavailable
later owner KAS chat
  -> its key is bound to "owner" by #24677 -> same vault directory
  -> engine computes the same kiro.mcp.<h> key (same url/headers/oauth entry)
  -> _kiro/secret/get returns the tokens; server connects with no prompt
```

### Which store holds what

| Data | kiro-cli backend | KAS backend, persistence on |
|---|---|---|
| `client` + `tokens` | kiro-cli OAuth cache (unchanged) | `kas/mcp-grants/principals/<sha256("owner")>/.vault/` |
| `verifier` + `discovery` | kiro-cli process memory | the mint process's `SecretStorageServer` memory, gone at dispose |
| PKCE verifier, loopback listener | the held kiro-cli mint process | the held KAS mint process |
| Mint row (state, URL, token) | `connections.mint._mints` | the same table, same row shape |

The mint never reads or writes a vault entry itself. Only the engine does, over
the request path #24677 already guards (key regex, 64 KB cap, per-principal
directory, link check, latch-off on conflict).

### Per-backend Settings behavior

| Backend | `kas_persist_mcp_sign_in` | Sign in does |
|---|---|---|
| kiro-cli | any | the kiro-cli mint, unchanged |
| KAS | off | the kiro-cli mint, unchanged. A KAS sign-in would die with the mint process, so there is nothing better to do; a separate change says on the row that this sign-in does not reach KAS chats |
| KAS | on | the KAS mint above |
| any other | any | the kiro-cli mint, unchanged (no other backend reads a Crew grant) |

The backend is the one owner dashboard chats run on: `agent.acp_backend` after
config normalization. A member or a crewmate pinned to another backend does not
change what Settings does.

### Badge

The engine names a grant `kiro.mcp.<sha256>`, where the digest is its own hash of
URL, headers and OAuth config. Crew does not compute it, so Settings cannot stat
the vault for a row. The mint feed carries `store: "kas"` on a KAS row, and when
that row reaches `granted` while the probe still reads no kiro-cli grant, the
row says the sign-in was kept for KAS chats instead of offering Sign in again at
once. The probe's own badge is unchanged.

### Remote-gateway callback relay

The relay (`POST /api/mcp/oauth/relay`) delivers a pasted return address to a
loopback port on the gateway host and never mints anything. The KAS engine is a
child of the gateway on that host, so the relay reaches it exactly when the
engine's redirect URI is a loopback listener inside the held mint process. That
holds for the chat Authorize card today on the same relay, and the mint holds
its process for the same TTL the kiro-cli mint does. No relay code changes.
Not verified against a live engine in this change: see Open questions.

### Failure modes

| Failure | Row | What is released |
|---|---|---|
| persistence turned off between the route's check and the flow | `failed`, `kas_sign_in_not_kept` | nothing was taken |
| storage not declared at handshake | `failed`, `kas_vault_unavailable` | process, spec |
| server absent from the status snapshot | `failed`, `mint_server_absent` | process, spec |
| sign-in slot busy (`begin_mcp_sign_in` False) | `failed`, `mint_sign_in_busy` | process, spec |
| no consent URL within the ready timeout | `failed`, `mint_timeouterror` | process, spec |
| URL carries a credential pattern | `failed`, `mint_url_rejected` (+ `rejected_endpoint`) | process, spec |
| spawn or session error | `failed`, `mint_<exception type>` | process, spec |
| TTL (600 s) with no completion | `expired` | process, spec |
| Cancel, or a newer mint | row dropped or replaced | process, spec |
| process died while waiting | `expired`, `mint_process_gone` | spec |
| session key conflicted mid-flight | `failed`, `kas_vault_unavailable` (the #24677 latch moved the engine to memory, so nothing was kept) | process, spec |

`granted` is written only after re-reading the process's
`SecretStorageServer.still_bound()`, so the row never claims a grant the vault
does not hold. Conflicting the reserved key needs a non-owner caller to drive
it, which no route offers; the key prefix is not one a dashboard slot uses.

## Migration plan

One PR, stacked on #24677, behind the existing opt-in setting.

Exit criteria:

1. On KAS with persistence on, `POST /api/connections/mint {server}` spawns a
   KAS runtime whose grant key is bound to `owner`, and never a kiro-cli client.
2. A `tokens` value the mint process stores is returned to a new owner session's
   `SecretStorageServer`, and not to a `user:<x>` session's.
3. With persistence off, or on the kiro-cli backend, the request takes today's
   kiro-cli path.
4. A credential-bearing consent URL is never published.

## Backward compatibility

Nothing changes unless the owner is on KAS with `agent.kas_persist_mcp_sign_in`
on; every other install, the default included, keeps today's kiro-cli mint. The
mint feed gains one optional field, `store`.

## Security considerations

- No new store and no new reader: the vault is #24677's, reached only through the
  engine's `_kiro/secret/*` requests the mint process makes.
- The principal is fixed to `owner` because the route is owner-only
  (`require_owner_dashboard_request`). The mint key is bound before the spawn,
  so the handshake reads a settled binding.
- The consent URL takes the same credential gate and http(s) check as the
  kiro-cli path and the chat card.
- No token value, URL, or exception message is logged or audited; audit lines
  name the slug and a reason code only.
- The agent gains no power: the vault directory stays a keystone leaf hidden from
  the sandbox.

## Alternatives considered

- **Copy the kiro-cli grant into the vault.** Crew would have to recompute the
  engine's private key digest and translate token formats, and token bytes would
  move between stores. Rejected.
- **Let the next chat's Authorize card do it.** Already works per chat, but a
  Settings click is where a user goes when tools are missing, and the click
  leaves nothing behind.

## Open questions

1. Is the KAS engine's OAuth redirect always a loopback listener in its own
   process? If an engine build uses another redirect, the relay path refuses it
   and the user must finish in a browser on the gateway host. To be checked on a
   live engine before this leaves draft.
2. Does every owner KAS chat project a remote server entry byte-identical in
   url, headers and OAuth config to the mint's single-server spec? Both copy the
   main spec's entry, so they should; a live check closes it.
