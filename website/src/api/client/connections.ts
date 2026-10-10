/**
 * Third-party account connections: the approval-URL mint, premint and mint
 * state, authorization status, provider test, cancel/disconnect, and
 * operator-registered OAuth clients.
 */

import type { ClientTransport } from './transport'

/**
 * A Connections provider's approval-URL mint, as the card reads it.
 *
 * `idle` means no mint exists — distinct from `failed`, which is a mint that ran
 * and produced nothing. `oauth_url` is present only while `waiting`, and only
 * while the process holding the URL is alive: the backend reports `expired`
 * rather than serving a URL no redirect can be redeemed against.
 */
export interface ConnectionMintState {
  slug: string
  state: 'idle' | 'minting' | 'waiting' | 'granted' | 'failed' | 'expired'
  oauth_url?: string
  reason?: string
  /** Copy-ready "host/path" of the authorization URL the credential gate
   *  refused. Present only beside reason === 'mint_url_rejected', and only when
   *  the backend could reduce the URL to a string the oauth_endpoints.json
   *  loader would accept (query, fragment, port and userinfo are never sent). */
  rejected_endpoint?: string
  /** Opaque id of the backend row, unique across gateway restarts as well as
   *  within one process. Reported so a row can be told apart from its
   *  successor for the same provider. */
  token?: string
}

/**
 * A provider's authorization verdict from GET /api/connections/status.
 *
 * This is the AUTHORIZATION axis only: `grantPresent` says whether kiro-cli
 * holds an OAuth grant. Endpoint reachability is a separate axis carried by the
 * `/api/mcp` server status — the two together are what let the card tell a
 * provider authorized outside the dashboard (grant present, probe answers 401)
 * from one never authorized (no grant, same 401). `connectedSince` is a
 * persisted first-authorization timestamp, present only while a grant exists.
 */
export interface ConnectionStatus {
  slug: string
  status: 'connected' | 'awaiting_consent' | 'not_connected'
  reason?: string
  grantPresent: boolean
  /** True when the grant lookup itself failed, so `grantPresent: false` means
   *  "could not look" rather than "absent". */
  grantIndeterminate?: boolean
  connectedSince?: string
  /** True for a pre-registered provider whose operator has not entered a usable
   *  OAuth client; present only when true and never alongside a held grant. */
  needsClientConfig?: boolean
}

/** Where a client-record half came from; `null` means not set anywhere. */
export type ConnectionOAuthClientSource = 'env' | 'config' | 'vault' | 'registry'

/** One pre-registered provider's operator OAuth client, as GET /api/connections/oauth-clients
 *  reports it. Carries the PUBLIC client id and only a boolean for the secret. */
export interface ConnectionOAuthClient {
  slug: string
  /** The vendor requires a client secret at the token endpoint. */
  confidential: boolean
  /** The exact redirect URI to register in the vendor console. */
  redirect_uri: string
  /** Path under docs/guides/ of the registration runbook. */
  registration_guide: string
  client_id: string | null
  client_id_source: ConnectionOAuthClientSource | null
  client_secret_set: boolean
  client_secret_source: ConnectionOAuthClientSource | null
  /** A client id is present, plus a secret when `confidential`. */
  configured: boolean
}

export interface ConnectionOAuthClientSave {
  client_id?: string
  client_secret?: string
  client_secret_clear?: boolean
}

/** Authenticated provider-tool verdict returned by POST /api/connections/test. */
export interface ConnectionTestResult {
  schema_version: number
  slug: string
  verdict: 'usable' | 'no_tools' | 'failed'
  code: string
  toolCount: number
}

export function createConnectionsEndpoints({ post, put, del, j, jfetch: fetch }: ClientTransport) {
  const accounts = {
    // Connections approval-URL mint. POST starts one; GET is the card's feed for it.
    connectionsMint: (slug: string) =>
      post('/api/connections/mint', { slug }).then(j) as Promise<{ ok: boolean; slug: string; state: string; token: string }>,
    connectionsMintState: (slug: string) =>
      fetch(`/api/connections/mint?slug=${encodeURIComponent(slug)}`).then(j) as Promise<ConnectionMintState>,
    // The same mint for a remote server the owner added outside the registry,
    // named by its exact mcp.json key. The gateway reads its URL from the owner's
    // own config, so the body carries only the name.
    connectionsMintServer: (server: string) =>
      post('/api/connections/mint', { server }).then(j) as Promise<{ ok: boolean; slug: string; state: string; token: string }>,
    connectionsMintServerState: (server: string) =>
      fetch(`/api/connections/mint?server=${encodeURIComponent(server)}`).then(j) as Promise<ConnectionMintState>,
    // Warm every mintable provider's URL in one activation, so a later Connect
    // serves a URL the warm table already holds instead of paying a cold spawn.
    // Deliberately BODYLESS: what is mintable is a fact about the user's registry
    // and grant state, never a caller's choice, and the bound on what may be
    // spawned stays on the server's side of the wire. `preminting` names the
    // providers warming was STARTED for, which is why it can answer before any of
    // them holds a URL — a card's verdict remains its mint state, never this.
    // Owner-gated, so a non-owner session rejects (403); the caller is expected to
    // treat that as "no warm table", not as an error worth showing.
    connectionsPremint: () =>
      post('/api/connections/premint').then(j) as Promise<{ ok: boolean; preminting: string[] }>,
    // Authorization verdict + first-connect time per visible provider. Additive to
    // the mint feed above; never mints.
    connectionsStatus: () =>
      fetch('/api/connections/status').then(j) as Promise<{ schema_version: number; connections: ConnectionStatus[] }>,
    // Promptless authenticated enumeration through kiro-cli. The runtime owns
    // bearer injection and provider tools/list; this receives only a verdict and count.
    connectionsTest: (slug: string) =>
      post('/api/connections/test', { slug }).then(j) as Promise<ConnectionTestResult>,
    // Dispose an in-flight mint (process, listener, spec). Does NOT touch the MCP
    // config entry — the card owns that. `token` fences a sibling tab's row.
    connectionsCancel: (slug: string, token?: string) =>
      post('/api/connections/cancel', token ? { slug, token } : { slug }).then(j) as Promise<{ ok: boolean; slug: string; dropped: boolean }>,
    // Undo a connection on THIS machine: disposes any in-flight mint, deletes the
    // runtime's stored grant artifacts when they are ours alone, and removes the MCP
    // entry. `grantRemoved` and `grantSurviving` are separate answers because the
    // artifacts are a pair and either half can fail alone; `entryRemoved` is false
    // when the entry configured under this slug points at a different endpoint (so it
    // is not ours to delete); `grantSharedWith` names the other entries using the same
    // endpoint, which is why the grant was deliberately kept. `grantCensusUnreadable`
    // names the sources the census could not read, so the card can say WHICH file to
    // repair -- optional because the other half of `grantCensusIncomplete` (an entry
    // whose URL could not be compared) names no file, and a gateway predating the
    // field sends none.
    connectionsDisconnect: (slug: string) =>
      post('/api/connections/disconnect', { slug }).then(j) as Promise<{ ok: boolean; grantRemoved: boolean; grantSurviving: string[]; entryRemoved: boolean; grantSharedWith: string[]; grantCensusIncomplete: boolean; grantCensusUnreadable?: string[] }>,
    // Operator-registered OAuth clients for providers that refuse dynamic client
    // registration (Settings → OAuth Apps). The list is readable by any dashboard
    // user (it is what the gallery's "needs configuration" card is built from and
    // carries no secret); save and delete are owner-only.
    connectionsOAuthClients: () =>
      fetch('/api/connections/oauth-clients').then(j) as Promise<{ schema_version: number; clients: ConnectionOAuthClient[] }>,
    // Omitted fields are left as they are, so the id can be saved without re-entering
    // a secret the panel never displays; `client_secret_clear` removes the stored one.
    connectionsOAuthClientSave: (slug: string, body: ConnectionOAuthClientSave) =>
      put(`/api/connections/oauth-clients/${encodeURIComponent(slug)}`, body).then(j) as Promise<{ ok: boolean; client: ConnectionOAuthClient | null }>,
    connectionsOAuthClientDelete: (slug: string) =>
      del(`/api/connections/oauth-clients/${encodeURIComponent(slug)}`).then(j) as Promise<{ ok: boolean; client: ConnectionOAuthClient | null }>,
  }

  return { accounts }
}
