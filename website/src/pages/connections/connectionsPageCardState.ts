/**
 * What a Connections card shows, decided without rendering anything.
 *
 * The card's state (`ConnectionCardState`) and the fold that picks it
 * (`connectionStateFor`), with the inputs that fold reads: the newest chat
 * `mcp_oauth` banner per server (`latestOAuthByServer`), fenced against the click
 * that started an attempt (`PendingConnect`, `effectiveOAuth`); the minted
 * approval URL (`withMintedUrl`) and what each mint state means for a pending
 * attempt (`mintOutcome`, `mintRowIsOurs`); and the confirmed-only grant verdict
 * (`confirmedGrantPresent`, `probeIndicatesConnected`). Also the feedback a card
 * renders (`Feedback`, `disconnectFeedback`), whether a cancelled attempt removes
 * the entry it created (`uninstallOnCancel`), and which error details count as
 * the provider rejecting the authorization (`errorIndicatesProviderRejection`).
 *
 * `ConnectionsPage.tsx` and `ConnectionsPageCard.tsx` read these. The page
 * re-exports the public names, which callers import from there; this module
 * imports neither.
 */
import type { ConnectionMintState, ConnectionStatus } from '../../api/client'
import type { ChatMessage, McpServer } from '../../types'
import type { ErrorReport } from '../../utils/errorReport'
import type { ConnectionProvider } from './registry'

export type ConnectionCardState =
  | 'not-connected'
  | 'waiting-for-approval'
  | 'connected'
  | 'not-verified'
  | 'needs-attention'
  /** A pre-registered provider whose operator has not entered an OAuth client
   *  yet (`needsClientConfig` on the status row). The card is an instruction
   *  with a link to Settings → OAuth Apps, not an offer to connect: a mint
   *  would fail at the vendor with a registration error no user can act on. */
  | 'needs-configuration'

type ConnectionAction = 'connect' | 'disconnect' | 'relay' | 'test'
export type Feedback = {
  // THREE kinds, because "the click did not do what you asked" splits in two. An
  // `error` is a failure; a `warning` is a deliberate refusal that leaves the user
  // a repair to make. Both must ANNOUNCE (role=alert) -- only `success` is a
  // passing status update.
  kind: 'success' | 'warning' | 'error'
  text: string
  /** Localized supplemental guidance appended after `text` (e.g. a provider's
   *  prerequisite steps on a zero-tools verdict). */
  detail?: string
  /**
   * Structured context for an `error` feedback, when the journal holds it.
   *
   * Enrichment ONLY -- it never decides which surface renders: every `error`
   * kind routes through `ErrorNotice` because `kind` alone says the request
   * failed, and `ErrorNotice` degrades to its own message-keyed lookup when no
   * report is found. Gating the routing on a report instead would let a lookup
   * miss (a mocked client, a redaction difference, journal eviction) silently
   * fall back to a hand-written error line, which is the defect the shared
   * surface exists to prevent -- and it would fail on the least-exercised path.
   *
   * Carried explicitly rather than derived from `text`, because `text` is a
   * LOCALIZED string while the journal is keyed on the message the API layer
   * produced, so a lookup by the rendered text misses in every locale.
   */
  report?: ErrorReport
  revoke?: { href: string; provider: string }
  help?: { href: string }
}
export type OAuthState = {
  completed: boolean
  failed: boolean
  oauthUrl: string
  error: string
  timestamp: number
  /** The URL was minted on demand, so no browser tab was ever opened for it. */
  minted?: boolean
}

export interface PendingConnect {
  kind: 'new' | 'reconnect'
  /** Timestamp of the newest `mcp_oauth` banner observed for this server at
   *  click time (0 when none). Banner timestamps are gateway-generated, so
   *  fencing against a *snapshot of them* stays within one clock domain —
   *  never compare them to the browser's own wall clock. */
  sinceTs: number
  /** The row token this tab's own POST returned, when it returned one. The mint
   *  table is keyed by slug, so a sibling tab connecting the same provider
   *  REPLACES the row -- without this, a tab reads the sibling's terminal state as
   *  the verdict on its own attempt and clears a wait it should still be holding. */
  token?: string
}

/** A banner no newer than the snapshot taken at click time belongs to a prior
 *  grant of the same server name — it must never mark a fresh attempt
 *  connected/failed. */
export function effectiveOAuth(
  oauth: OAuthState | undefined,
  pending: PendingConnect | undefined,
): OAuthState | undefined {
  if (oauth && pending && oauth.timestamp <= pending.sinceTs) return undefined
  return oauth
}

/** Drop a chat banner the user already dismissed with Cancel.
 *
 *  Cancel clears this tab's wait and disposes the backend mint, but an older
 *  chat Authorize card for the same server still carries an `oauthUrl`, and
 *  that alone holds the card in waiting-for-approval -- a Cancel that appears
 *  to do nothing. `dismissedTs` is a snapshot of the newest banner timestamp at
 *  the click (same gateway clock domain as `effectiveOAuth`), so only a banner
 *  raised AFTER the cancel can bring the waiting card back.
 *
 *  Only a still-open banner is fenced. The chat Authorize link stays clickable
 *  after Cancel, and completing consent there patches that SAME banner in place
 *  (same timestamp) to `completed` or `failed`. That terminal verdict must reach
 *  the card, or it shows Authorize after the grant succeeded. */
export function undismissedOAuth(
  oauth: OAuthState | undefined,
  dismissedTs: number | undefined,
): OAuthState | undefined {
  if (
    oauth && dismissedTs !== undefined
    && !oauth.completed && !oauth.failed
    && oauth.timestamp <= dismissedTs
  ) return undefined
  return oauth
}

/** Fold a minted approval URL into the card's OAuth view.
 *
 * Applied AFTER `effectiveOAuth`, so a minted URL never passes through the
 * banner staleness fence: a mint is started by the click being served, so it is
 * current by construction and carries no gateway banner timestamp to compare
 * against. A URL is taken only from a `waiting` mint — every other state either
 * has no URL or holds one that can no longer be redeemed.
 *
 * A chat banner that already carries a URL wins: it is the same consent request,
 * and preferring one source keeps the rendered link stable across polls.
 */
/** What a mint state means for the card, given how the entry got there.
 *
 *  The full table — every mint state against both entry situations — so the card
 *  implements a decision rather than accumulating one branch per review round:
 *
 *  | mint state | entry           | wait  | probe | error | uninstall |
 *  |------------|-----------------|-------|-------|-------|-----------|
 *  | absent     | either          | keep  |  no   |  no   |    no     |
 *  | minting    | either          | keep  |  no   |  no   |    no     |
 *  | waiting    | either          | keep  |  no   |  no   |    no     |
 *  | granted    | either          | clear | YES   |  no   |    no     |
 *  | failed     | new-this-flow   | clear |  no   | YES   |    no     |
 *  | failed     | pre-existing    | clear |  no   | YES   |    no     |
 *  | expired    | any             | clear |  no   |  no   |
 *
 *  No terminal state deletes configuration. An expired mint clears this tab's
 *  wait and leaves the entry in place, so the card shows needs-attention and the
 *  user retries with Connect or removes it with Disconnect. Deleting an entry on
 *  a timeout meant racing a sibling tab for the same slug-keyed row, and no
 *  amount of token fencing makes an automatic delete worth that: config removal
 *  is a decision the user makes explicitly.
 *
 *  Two rows carry the reasoning:
 *  - `granted` must PROBE. The card's cached status predates consent, so without
 *    a fresh read it keeps showing the pre-consent error after authorization
 *    succeeded.
 *  - `failed` keeps the entry on purpose. Something went wrong rather than timed
 *    out, so the error surface plus a retryable entry beats silently undoing the
 *    install.
 */
export type MintOutcome = {
  clearWait: boolean
  probe: boolean
  error: boolean
}

const MINT_WAIT_HELD: MintOutcome = {
  clearWait: false, probe: false, error: false,
}


/** Whether a row is the one THIS tab's POST started. Unknown on either side reads
 *  as ours: a row with no token predates the fence, and a pending wait with no
 *  token means the POST answered without one -- neither is a sibling's. */
function mintRowIsOurs(
  mint: ConnectionMintState | undefined,
  pending: PendingConnect | undefined,
): boolean {
  if (!mint?.token || !pending?.token) return true
  return mint.token === pending.token
}

export function mintOutcome(
  mint: ConnectionMintState | undefined,
  pending?: PendingConnect,
): MintOutcome {
  // A row carrying a DIFFERENT token is a sibling tab's, not this tab's. Clear the
  // wait -- the mint table is keyed by slug, so this tab's row was REPLACED and no
  // verdict for its own attempt is ever coming, and holding would spin forever --
  // but claim nothing from the sibling's outcome: no probe, no error. This is the
  // client half of the fence the backend applies; neither is sufficient alone,
  // because the client cannot see a supersede that lands after it reads, and the
  // server cannot see which tab is asking.
  if (!mintRowIsOurs(mint, pending)) return { clearWait: true, probe: false, error: false }
  switch (mint?.state) {
    case 'granted':
      return { clearWait: true, probe: true, error: false }
    case 'failed':
      return { clearWait: true, probe: false, error: true }
    case 'expired':
      return { clearWait: true, probe: false, error: false }
    default:
      return MINT_WAIT_HELD
  }
}


export function withMintedUrl(
  oauth: OAuthState | undefined,
  mint: ConnectionMintState | undefined,
): OAuthState | undefined {
  const minted = mint?.state === 'waiting' ? (mint.oauth_url || '') : ''
  if (!minted || oauth?.oauthUrl) return oauth
  return {
    completed: false,
    failed: false,
    error: '',
    timestamp: 0,
    ...(oauth ?? {}),
    oauthUrl: minted,
    minted: true,
  }
}

/** Only a cancelled *new* connect uninstalls the entry it just created;
 *  cancelling a reconnect (or a stateless wait) must not destroy config. */
export function uninstallOnCancel(pending: PendingConnect | undefined): boolean {
  return pending?.kind === 'new'
}

export function disconnectFeedback(
  provider: Pick<ConnectionProvider, 'name' | 'revoke_page_url'>,
  text: string,
  kind: Feedback['kind'] = 'success',
): Feedback {
  return {
    kind,
    text,
    revoke: { href: provider.revoke_page_url, provider: provider.name },
  }
}

/**
 * The ONE reading of a probe status against the authorization axis. Both the
 * card's badge and the Test button's verdict fold through this, because they
 * judge the same probe and a second reading is how they came to disagree:
 * a connected Linear card rendered Connected while its Test click reported a
 * failure, from `status !== 'ok'` on the exact answer the badge folds as healthy.
 *
 * Exported for test.
 */
export function probeIndicatesConnected(status: string, grantPresent?: boolean): boolean {
  // `ok` is REACHABILITY and it is cached, so it outlives a revoked grant. Only
  // a CONFIRMED absent grant (never the indeterminate or not-yet-loaded
  // undefined) is a fresher fact than it.
  if (status === 'ok') return grantPresent !== false
  // A tokenless probe of a remote OAuth server answers 401, which the gateway
  // reports as `needs_auth` — kiro-cli owns token custody, so needs_auth beside
  // a grant IS the healthy shape. The grant axis is the only thing separating
  // "authorized outside this app" from "nobody authorized this", so an absent
  // OR indeterminate verdict is not a grant.
  if (status === 'needs_auth') return grantPresent === true
  return false
}

/**
 * The confirmed-only grant verdict, read once and shared. An indeterminate
 * lookup reports `grantPresent: false` without knowing anything, so it must
 * collapse to `undefined` (the honest hedge) rather than to a confirmed absence.
 *
 * Exported for test.
 */
export function confirmedGrantPresent(status: ConnectionStatus | undefined): boolean | undefined {
  return status && !status.grantIndeterminate ? status.grantPresent : undefined
}

/**
 * Whether an error detail is EVIDENCE of the provider rejecting the authorization.
 *
 * The needs-attention banner's strongest copy asserts a provider VERDICT —
 * "{{provider}} says this connection is no longer valid" — so it may only render
 * over auth-shaped evidence (an OAuth error code, a 401/403, a revocation, a
 * refused consent). Everything else a probe can surface (timeouts, DNS failures,
 * connection resets) is transport noise the provider never spoke through, and
 * claiming a verdict over it sends the user to revoke/reauthorize flows for a
 * network blip. Default false: asserting a verdict needs positive evidence, an
 * unknown error does not earn it.
 *
 * Exported for test.
 */
export function errorIndicatesProviderRejection(detail: string | undefined): boolean {
  if (!detail) return false
  const normalized = detail.toLowerCase()
  // OAuth error codes and rejection words are matched as whole tokens: a bare
  // substring test read "certificate has expired" (a TLS transport failure) as
  // a provider rejection, re-creating the exact misattribution this classifier
  // exists to prevent. "expired" only counts beside a credential noun, and the
  // bare status digits only as standalone tokens (not inside a port or an id).
  if (/\b(invalid_grant|invalid_token|invalid_client|unauthorized|forbidden|revoked)\b/.test(normalized)) {
    return true
  }
  if (/\b(?:token|grant|authorization|credential|session)\b[^.]*\bexpired\b|\bexpired\b[^.]*\b(?:token|grant|authorization|credential|session)\b/.test(normalized)) {
    return true
  }
  if (/\b(denied|consent)\b/.test(normalized)) return true
  return /(?:^|[^\d.])(401|403)(?:[^\d.]|$)/.test(normalized)
}

export function connectionStateFor(
  server: McpServer | undefined,
  oauth: OAuthState | undefined,
  locallyWaiting = false,
  grantPresent?: boolean,
  awaitingConsent = false,
  needsClientConfig = false,
  dismissed = false,
): ConnectionCardState {
  // The backend sets `needsClientConfig` only while no grant exists, so this
  // cannot hide a connected card; it outranks the not-connected fold below
  // because Connect would only fail at the vendor. A consent already in flight
  // (an operator configured, clicked, then cleared the record) still renders as
  // waiting -- the URL is live and the poll will settle it.
  if (needsClientConfig && !locallyWaiting && !awaitingConsent && !oauth?.oauthUrl) {
    return 'needs-configuration'
  }
  if (!server) {
    // `awaitingConsent` is the backend's mint table saying a flow for this
    // provider is in flight RIGHT NOW. It is what survives a refresh: the
    // locally-pending map and the chat's oauth message are both per-tab state,
    // so without it a reload mid-consent silently drops back to Connect while
    // the approval URL is still live.
    return locallyWaiting || awaitingConsent ? 'waiting-for-approval' : 'not-connected'
  }
  if (oauth?.failed) return 'needs-attention'
  // A completed OAuth flow in THIS session outranks a possibly-lagging status
  // poll: the grant was just written, the feed may not have re-read yet.
  if (oauth?.completed) return 'connected'
  // A pending attempt THIS TAB is holding, or the backend's own mint table
  // saying a flow is in flight right now, outranks the cached probe verdicts
  // below. The mint side of this fix validates an existing grant before ever
  // reporting a mint `granted`, so a live `awaitingConsent`/`locallyWaiting`
  // here means either a genuinely fresh consent flow (the old grant did not
  // hold up) or a not-yet-decided reconnect -- never a flow the backend itself
  // already knows is stale. Reading `server.status === 'ok'` first, as this
  // branch used to, is exactly what let Connect flip Stripe and Vercel to
  // Connected on a cached probe the instant the click landed, well before the
  // mint had validated anything: the card claimed an authorization no fresher
  // fact yet backed. `oauth?.oauthUrl` is kept alongside the mint signal for
  // the chat-message delivery path, which never sets `awaitingConsent`.
  if (locallyWaiting || awaitingConsent || oauth?.oauthUrl) return 'waiting-for-approval'
  if (server.status === 'ok') {
    // The reachability probe is cached, so `ok` outlives a revoked grant. A
    // CONFIRMED absent grant (grantPresent === false, never the indeterminate
    // or not-yet-loaded undefined) is the fresher authorization fact and wins:
    // render the honest not-verified card instead of a Connected badge for an
    // authorization that no longer exists.
    return probeIndicatesConnected(server.status, grantPresent) ? 'connected' : 'not-verified'
  }
  // The status probe carries no OAuth token — kiro-cli owns token custody and
  // Kiro Crew stores no credential — so a remote OAuth server answers it with 401
  // and the gateway reports `needs_auth`. Two very different situations produce
  // that identical answer: a server nobody has authorized, and a server
  // authorized OUTSIDE the dashboard, which the runtime calls fine and which
  // raised no `mcp_oauth` banner here. The authorization axis from
  // /api/connections/status (`grantPresent`) is what tells them apart: a grant on
  // disk means the runtime IS authorized and the card is connected; no grant
  // leaves the honest `not-verified` (needs authorization to see this server).
  // Absent `grantPresent` (status feed not yet loaded) keeps the prior behaviour.
  // It must reach neither the error card (#1853) nor the spinner below, which
  // would imply a grant is in flight.
  if (server.status === 'needs_auth') {
    return probeIndicatesConnected(server.status, grantPresent) ? 'connected' : 'not-verified'
  }
  if (server.status === 'error' || server.status === 'disabled') return 'needs-attention'
  // No verdict yet. Before a Cancel this reads as a flow still settling; after
  // one there is no flow left, so the waiting card (whose only action is the
  // Cancel just pressed) would strand the user. Offer Authorize instead.
  return dismissed ? 'not-verified' : 'waiting-for-approval'
}

/**
 * The card's approval-URL feed: the newest mcp_oauth chat message per server.
 *
 * Exported for test. `card_owned` is deliberately NOT consulted — that flag is a
 * hint to the CHAT renderer that this card already shows the same prompt, and the
 * card is the surface it points at. Filtering on it here would leave the card
 * with no URL at all.
 */
export function latestOAuthByServer(
  activeMessages: readonly ChatMessage[],
  slotMessages: Record<string, ChatMessage[]>,
): Record<string, OAuthState> {
  const result: Record<string, OAuthState> = {}
  const messages = [...Object.values(slotMessages).flat(), ...activeMessages]
  messages.forEach((message, index) => {
    if (message.role !== 'mcp_oauth') return
    const serverName = String(message.meta?.server_name || '').trim().toLowerCase()
    if (!serverName) return
    const parsed = Date.parse(message.ts || '')
    const timestamp = Number.isFinite(parsed) ? parsed : index
    const current = result[serverName]
    if (current && current.timestamp > timestamp) return
    result[serverName] = {
      completed: !!message.meta?.completed,
      failed: !!message.meta?.failed,
      oauthUrl: String(message.meta?.oauth_url || ''),
      error: String(message.meta?.error || ''),
      timestamp,
    }
  })
  return result
}

// `ConnectionsPage.tsx` reads these too.
export { mintRowIsOurs }
export type { ConnectionAction }
