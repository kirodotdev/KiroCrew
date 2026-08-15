import { useEffect, useMemo, useState, type KeyboardEvent, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  ExternalLink,
  KeyRound,
  Link2,
  Loader2,
  RotateCw,
  Server,
  Unplug,
  X,
} from 'lucide-react'
import { api, type ConnectionMintState } from '../../api/client'
import { useAppSelector } from '../../store'
import type { ChatMessage, McpApplyChange, McpServer } from '../../types'
import { fmtDate } from '../../i18n/format'
import { Badge, Btn, ContentSkeleton, SearchInput } from '../../components/ui'
import McpTab from '../overview/McpTab'
import ProviderLogo from './ProviderLogo'
import {
  CONNECTION_PROVIDERS,
  serverForConnection,
  type ConnectionProvider,
} from './registry'

/** Mint poll cadence. A cold mint takes seconds, so this is tuned to surface the
 *  URL promptly without spinning on a request that mostly answers `minting`. */
const MINT_POLL_MS = 2_000

export type ConnectionCardState =
  | 'not-connected'
  | 'waiting-for-approval'
  | 'connected'
  | 'not-verified'
  | 'needs-attention'

type ConnectionAction = 'connect' | 'disconnect' | 'relay' | 'test'
export type Feedback = {
  kind: 'success' | 'error'
  text: string
  revoke?: { href: string; provider: string }
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

const PROVIDER_TONES: Record<string, string> = {
  notion: 'bg-text-strong text-bg',
  github: 'bg-[#24292f] text-white',
  linear: 'bg-[#5e6ad2] text-white',
  atlassian: 'bg-[#1868db] text-white',
  stripe: 'bg-[#635bff] text-white',
  vercel: 'bg-text-strong text-bg',
}

function safeApprovalUrl(value: string): string {
  try {
    const url = new URL(value)
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.toString() : ''
  } catch {
    return ''
  }
}

/** Accept only the loopback redirect shape produced by the runtime callback. */
export function isValidLoopbackReturnAddress(value: string): boolean {
  try {
    const url = new URL(value.trim())
    const loopback = url.hostname === '127.0.0.1' || url.hostname === '[::1]' || url.hostname === '::1'
    const codes = url.searchParams.getAll('code')
    return url.protocol === 'http:'
      && loopback
      && url.port !== ''
      && url.username === ''
      && url.password === ''
      && url.hash === ''
      && codes.length === 1
      && codes[0] !== ''
  } catch {
    return false
  }
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
): Feedback {
  return {
    kind: 'success',
    text,
    revoke: { href: provider.revoke_page_url, provider: provider.name },
  }
}

export function connectionStateFor(
  server: McpServer | undefined,
  oauth: OAuthState | undefined,
  locallyWaiting = false,
): ConnectionCardState {
  if (!server) return locallyWaiting ? 'waiting-for-approval' : 'not-connected'
  if (oauth?.failed) return 'needs-attention'
  if (oauth?.completed || server.status === 'ok') return 'connected'
  if (locallyWaiting || oauth?.oauthUrl) return 'waiting-for-approval'
  // The status probe carries no OAuth token — kiro-cli owns token custody and
  // Kiro Crew stores no credential — so a remote OAuth server answers it with 401
  // and the gateway reports `needs_auth`. Two very different situations produce
  // that identical answer: a server nobody has authorized, and a server
  // authorized OUTSIDE the dashboard, which the runtime calls fine and which
  // raised no `mcp_oauth` banner here. `needs_auth` is therefore honest about
  // the PROBE (it needs authorization to see this server) and would be a claim
  // we cannot support if restated as a fact about the server — which is why the
  // state is named for what we know rather than for what the user must do. It
  // must reach neither the error card (#1853) nor the spinner below, which would
  // imply a grant is in flight.
  if (server.status === 'needs_auth') return 'not-verified'
  if (server.status === 'error' || server.status === 'disabled') return 'needs-attention'
  return 'waiting-for-approval'
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

interface ConnectionCardProps {
  provider: ConnectionProvider
  server?: McpServer
  state: ConnectionCardState
  oauth?: OAuthState
  busy?: ConnectionAction
  feedback?: Feedback
  highlighted: boolean
  onConnect: () => Promise<unknown>
  onCancel: () => Promise<unknown>
  onDisconnect: () => Promise<unknown>
  onReconnect: () => Promise<unknown>
  onTest: () => Promise<unknown>
  onRelay: (returnAddress: string) => Promise<boolean>
}

function ConnectionCard({
  provider,
  server,
  state,
  oauth,
  busy,
  feedback,
  highlighted,
  onConnect,
  onCancel,
  onDisconnect,
  onReconnect,
  onTest,
  onRelay,
}: ConnectionCardProps) {
  const { t } = useTranslation()
  const [returnAddress, setReturnAddress] = useState('')
  const [invalidReturnAddress, setInvalidReturnAddress] = useState(false)
  const approvalUrl = safeApprovalUrl(oauth?.oauthUrl || '')
  const accountLabel = server?.accountLabel || t('pages.connectionsPage.authorized_account')
  const logo = <ProviderLogo slug={provider.slug} />
  // `official_mcp_server` used to be a subtitle line under the name; the brand
  // mark now carries provenance visually, so keep the assurance as the card's
  // accessible/hover description instead of a third row of chrome.
  const provenance = t('pages.connectionsPage.official_mcp_server')
  const scopes = provider.recommended_scopes
  const stateMeta: Record<ConnectionCardState, { label: string; icon: ReactNode; tone: string }> = {
    'not-connected': {
      label: t('pages.connectionsPage.not_connected'),
      icon: <Link2 className="w-3.5 h-3.5" aria-hidden="true" />,
      tone: 'bg-bg-hover text-muted',
    },
    'waiting-for-approval': {
      label: t('pages.connectionsPage.waiting_for_approval'),
      icon: <CircleDashed className="w-3.5 h-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" />,
      tone: 'bg-warn-subtle text-warn',
    },
    connected: {
      label: t('pages.connectionsPage.connected'),
      icon: <CheckCircle2 className="w-3.5 h-3.5" aria-hidden="true" />,
      tone: 'bg-ok-subtle text-ok',
    },
    // Warn tone, not the error tone: an unverifiable state is not a failure. The
    // icon is static on purpose — a spinner would claim a grant is in flight
    // when nothing is pending.
    'not-verified': {
      label: t('pages.connectionsPage.not_verified'),
      icon: <KeyRound className="w-3.5 h-3.5" aria-hidden="true" />,
      tone: 'bg-warn-subtle text-warn',
    },
    'needs-attention': {
      label: t('pages.connectionsPage.needs_attention'),
      icon: <AlertTriangle className="w-3.5 h-3.5" aria-hidden="true" />,
      tone: 'bg-danger-subtle text-danger',
    },
  }
  const meta = stateMeta[state]
  const runRelay = async () => {
    if (!isValidLoopbackReturnAddress(returnAddress)) {
      setInvalidReturnAddress(true)
      return
    }
    setInvalidReturnAddress(false)
    const delivered = await onRelay(returnAddress.trim())
    if (delivered) setReturnAddress('')
  }

  return (
    <article
      id={`connection-${provider.slug}`}
      data-state={state}
      className={`relative flex flex-col rounded-lg border bg-card p-3.5 shadow-sm transition-colors ${
        highlighted ? 'border-accent ring-1 ring-accent/40' : state === 'needs-attention' ? 'border-danger/40' : 'border-border'
      }`}
    >
      <header className="flex items-center gap-2.5">
        <span className="flex shrink-0 items-center" title={provenance} aria-label={provenance} role="img">
          {logo ?? (
            <span
              className={`flex h-5 w-5 items-center justify-center rounded text-[11px] font-bold ${PROVIDER_TONES[provider.slug] || 'bg-accent text-accent-fg'}`}
              aria-hidden="true"
            >
              {provider.name.slice(0, 1)}
            </span>
          )}
        </span>
        <h3 className="m-0 min-w-0 flex-1 truncate text-[14px] font-semibold text-text-strong">{provider.name}</h3>
        <span className={`inline-flex shrink-0 items-center gap-1 text-[11px] font-medium ${meta.tone}`}>
          {meta.icon}
          {meta.label}
        </span>
      </header>

      <p
        className="mb-2.5 mt-1.5 min-w-0 truncate text-[12.5px] text-muted"
        title={t('pages.connectionsPage.service_value_prop', { provider: provider.name })}
      >
        {t('pages.connectionsPage.service_value_prop', { provider: provider.name })}
      </p>

      <div className="mt-auto">
        {state === 'not-connected' && (
          <div className="flex items-center justify-between gap-3">
            <a href={provider.docs_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-[12px] text-muted hover:text-text">
              {t('pages.connectionsPage.documentation')} <ExternalLink className="w-3 h-3" aria-hidden="true" />
            </a>
            <Btn primary onClick={() => void onConnect()} disabled={!!busy}>
              {busy === 'connect' && <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden="true" />}
              {busy === 'connect' ? t('pages.connectionsPage.connecting') : t('pages.connectionsPage.connect')}
            </Btn>
          </div>
        )}

        {state === 'waiting-for-approval' && (
          <div className="space-y-3">
            <div className="text-[13px] font-medium text-text-strong">
              {/* A minted URL opened no tab, so "finish approving in your browser"
                  would point the user at a window that does not exist. Existing
                  keys only -- the fuller copy rewrite needs a 14-locale pass and
                  rides with the connections-copy slice. */}
              {t(oauth?.minted
                ? 'pages.connectionsPage.waiting_for_approval'
                : 'pages.connectionsPage.finish_approving_in_browser')}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {approvalUrl ? (
                <a href={approvalUrl} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-[12px] font-medium text-accent hover:text-accent-hover">
                  {t('pages.connectionsPage.reopen_approval')} <ExternalLink className="w-3 h-3" aria-hidden="true" />
                </a>
              ) : (
                <span className="inline-flex items-center gap-1 text-[12px] text-muted" aria-live="polite">
                  <Loader2 className="w-3 h-3 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                  {t('pages.connectionsPage.waiting_for_approval_address')}
                </span>
              )}
              <Btn className="ml-auto" onClick={() => void onCancel()} disabled={!!busy}>
                <X className="w-3.5 h-3.5" aria-hidden="true" /> {t('pages.connectionsPage.cancel')}
              </Btn>
            </div>
            <div className="rounded-md border border-warn/30 bg-warn-subtle p-2.5">
              <p className="m-0 text-[11px] leading-relaxed text-text">
                {t('pages.connectionsPage.remote_gateway_help')}
              </p>
              <div className="mt-2 block text-[11px] font-medium text-text">
                {t('pages.connectionsPage.return_address')}
              </div>
              <div className="mt-1 flex gap-1.5">
                <input
                  id={`return-address-${provider.slug}`}
                  type="url"
                  aria-label={t('pages.connectionsPage.return_address')}
                  value={returnAddress}
                  onChange={event => {
                    setReturnAddress(event.target.value)
                    if (invalidReturnAddress) setInvalidReturnAddress(false)
                  }}
                  onKeyDown={event => {
                    if (event.key === 'Enter') {
                      event.preventDefault()
                      void runRelay()
                    }
                  }}
                  placeholder={t('pages.connectionsPage.return_address_placeholder')}
                  autoComplete="off"
                  spellCheck={false}
                  disabled={busy === 'relay'}
                  aria-invalid={invalidReturnAddress}
                  aria-describedby={invalidReturnAddress ? `return-address-error-${provider.slug}` : undefined}
                  className="min-w-0 flex-1 rounded-md border border-border bg-bg px-2.5 py-1.5 font-mono text-[11px] text-text outline-none focus:ring-1 focus:ring-accent"
                />
                <Btn primary onClick={() => void runRelay()} disabled={!returnAddress.trim() || busy === 'relay'}>
                  {busy === 'relay' && <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden="true" />}
                  {busy === 'relay' ? t('pages.connectionsPage.relaying') : t('pages.connectionsPage.complete_connection')}
                </Btn>
              </div>
              {invalidReturnAddress && (
                <p id={`return-address-error-${provider.slug}`} role="alert" className="mb-0 mt-1.5 text-[11px] text-danger">
                  {t('pages.connectionsPage.invalid_return_address')}
                </p>
              )}
            </div>
          </div>
        )}

        {state === 'not-verified' && (
          <div className="space-y-3">
            <div className="flex items-start gap-2 rounded-md border border-warn/30 bg-warn-subtle p-2.5 text-[12px] text-text">
              <KeyRound className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warn" aria-hidden="true" />
              <span>{t('pages.connectionsPage.not_verified_help', { provider: provider.name })}</span>
            </div>
            <div className="flex justify-end">
              <Btn primary onClick={() => void onReconnect()} disabled={!!busy}>
                {busy === 'connect' ? <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden="true" /> : <KeyRound className="w-3.5 h-3.5" aria-hidden="true" />}
                {busy === 'connect' ? t('pages.connectionsPage.connecting') : t('pages.connectionsPage.authorize')}
              </Btn>
            </div>
          </div>
        )}

        {state === 'connected' && (
          <div className="space-y-3">
            <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-[12px]">
              <dt className="text-muted">{t('pages.connectionsPage.account')}</dt>
              <dd className="m-0 truncate font-medium text-text" title={accountLabel}>{accountLabel}</dd>
              <dt className="text-muted">{t('pages.connectionsPage.access')}</dt>
              <dd className="m-0 break-words text-text">
                {scopes.length > 0 ? t('pages.connectionsPage.recommended_access', { scopes: scopes.join(', ') }) : t('pages.connectionsPage.tool_controlled_access')}
              </dd>
              {server?.connectedSince && (
                <>
                  <dt className="text-muted">{t('pages.connectionsPage.connected_since')}</dt>
                  <dd className="m-0 text-text">{fmtDate(server.connectedSince)}</dd>
                </>
              )}
            </dl>
            <p className="m-0 text-[11px] leading-relaxed text-muted">
              {t('pages.connectionsPage.disconnect_help')}{' '}
              <a href={provider.revoke_page_url} target="_blank" rel="noopener noreferrer" className="text-accent hover:text-accent-hover">
                {t('pages.connectionsPage.revoke_at_provider', { provider: provider.name })} <ExternalLink className="lucide-inline" aria-hidden="true" />
              </a>
            </p>
            <div className="flex justify-end gap-2">
              <Btn onClick={() => void onTest()} disabled={!!busy}>
                {busy === 'test' ? <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden="true" /> : <RotateCw className="w-3.5 h-3.5" aria-hidden="true" />}
                {busy === 'test' ? t('pages.connectionsPage.testing') : t('pages.connectionsPage.test_connection')}
              </Btn>
              <Btn danger onClick={() => void onDisconnect()} disabled={!!busy}>
                <Unplug className="w-3.5 h-3.5" aria-hidden="true" /> {t('pages.connectionsPage.disconnect')}
              </Btn>
            </div>
          </div>
        )}

        {state === 'needs-attention' && (
          <div className="space-y-3">
            <div className="flex items-start gap-2 rounded-md bg-danger-subtle p-2.5 text-[12px] text-danger">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <span>
                {t('pages.connectionsPage.connection_invalid', { provider: provider.name })}
                {(oauth?.error || server?.error) && <span className="mt-1 block text-[11px] text-muted">{oauth?.error || server?.error}</span>}
              </span>
            </div>
            <div className="flex justify-end">
              <Btn primary onClick={() => void onReconnect()} disabled={!!busy}>
                {busy === 'connect' ? <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden="true" /> : <RotateCw className="w-3.5 h-3.5" aria-hidden="true" />}
                {busy === 'connect' ? t('pages.connectionsPage.reconnecting') : t('pages.connectionsPage.reconnect')}
              </Btn>
            </div>
          </div>
        )}
      </div>

      {feedback && (
        <div role={feedback.kind === 'error' ? 'alert' : 'status'} className={`mt-3 text-[11px] ${feedback.kind === 'error' ? 'text-danger' : 'text-ok'}`}>
          {feedback.text}
          {feedback.revoke && (
            <>
              {' '}
              <a href={feedback.revoke.href} target="_blank" rel="noopener noreferrer" className="font-medium text-accent hover:text-accent-hover">
                {t('pages.connectionsPage.revoke_at_provider', { provider: feedback.revoke.provider })} <ExternalLink className="lucide-inline" aria-hidden="true" />
              </a>
            </>
          )}
        </div>
      )}
    </article>
  )
}

/**
 * `servicesEnabled` gates the provider gallery. The Connections work is merged
 * on main but held for a later release, so the default is CLOSED: the Services
 * panel offers no providers, so no card, Connect button or OAuth flow is
 * reachable.
 *
 * The panel still RENDERS rather than being removed, which is deliberate.
 * Hiding the sub-tab and defaulting to the MCP Servers table was tried and
 * reverted: it makes that table the default-rendered surface and so exposes its
 * pre-existing i18n debt to the render-time gate, which measured
 * `capabilities-mcp` going 44 -> 102 findings. Emptying the list keeps the
 * measured surface comparable to main (568 -> 558 overall, gate PASS) while
 * still removing every way to actually connect a provider.
 */
export default function ConnectionsPage({ servicesEnabled = false }: { servicesEnabled?: boolean } = {}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [activeTab, setActiveTab] = useState<'services' | 'mcp-servers'>('services')
  const [search, setSearch] = useState('')
  /** Pending connect attempts. `kind` decides Cancel semantics (only a
   *  cancelled *new* connect uninstalls the entry it just created); `sinceTs`
   *  fences off stale `mcp_oauth` banners left over from an earlier grant of
   *  the same server name (they must not mark a fresh attempt connected). */
  const [locallyWaiting, setLocallyWaiting] = useState<Record<string, PendingConnect>>({})
  const [busy, setBusy] = useState<{ slug: string; action: ConnectionAction } | null>(null)
  const [feedback, setFeedback] = useState<Record<string, Feedback>>({})
  const [highlightedSlug, setHighlightedSlug] = useState('')
  const activeMessages = useAppSelector(state => state.chat.messages)
  const slotMessages = useAppSelector(state => state.chat.slotMessages)
  const oauthByServer = useMemo(
    () => latestOAuthByServer(activeMessages, slotMessages),
    [activeMessages, slotMessages],
  )
  const { data: servers = [], isLoading, isError } = useQuery<McpServer[]>({
    queryKey: ['mcp-servers'],
    queryFn: () => api.mcpServers(),
    refetchInterval: activeTab === 'services' && Object.values(locallyWaiting).some(Boolean) ? 5_000 : false,
  })

  // Minted approval URLs, keyed by slug. Fetched only while a connect is pending:
  // outside that window nothing is minting and the endpoint would answer `idle`.
  const waitingSlugs = useMemo(
    () => Object.keys(locallyWaiting).sort(),
    [locallyWaiting],
  )
  const { data: mintByServer = {} } = useQuery<Record<string, ConnectionMintState>>({
    queryKey: ['connections-mint', waitingSlugs],
    queryFn: async () => {
      const states = await Promise.all(
        waitingSlugs.map(slug => api.connectionsMintState(slug).catch(() => undefined)),
      )
      const next: Record<string, ConnectionMintState> = {}
      for (const state of states) if (state) next[state.slug] = state
      return next
    },
    enabled: waitingSlugs.length > 0,
    refetchInterval: MINT_POLL_MS,
    // A mint row is only valid for the attempt that produced it. Cached across an
    // inactive window it would be replayed on the next Connect for the same
    // provider, flashing a previous attempt's URL that no listener can redeem.
    gcTime: 0,
  })

  useEffect(() => {
    // Decided BEFORE any setState: a state updater runs on a later render, so
    // collecting side-effect targets inside one leaves them empty at read time.
    const cleared: string[] = []
    const failedMints: string[] = []
    const grantedMints: string[] = []
    for (const provider of CONNECTION_PROVIDERS) {
      const pending = locallyWaiting[provider.slug]
      if (!pending) continue
      const server = serverForConnection(provider, servers)
      const fresh = effectiveOAuth(oauthByServer[provider.slug], pending)
      const outcome = mintOutcome(mintByServer[provider.slug], pending)
      if (!(server?.status === 'ok' || fresh?.completed || fresh?.failed || outcome.clearWait)) {
        continue
      }
      cleared.push(provider.slug)
      if (outcome.error) failedMints.push(provider.slug)
      if (outcome.probe) grantedMints.push(provider.slug)
    }
    if (!cleared.length) return

    setLocallyWaiting(current => {
      const next = { ...current }
      for (const slug of cleared) delete next[slug]
      return next
    })
    if (grantedMints.length) {
      // The cached status predates consent, so without a fresh read the card
      // keeps showing its pre-consent error after authorization succeeded.
      void api.mcpProbe().then(probed => {
        queryClient.setQueryData<McpServer[]>(['mcp-servers'], probed as McpServer[])
      }).catch(() => undefined)
    }
    if (failedMints.length) {
      setFeedback(current => {
        const next = { ...current }
        for (const slug of failedMints) {
          // Existing strings only. The mint's reason is a coarse machine code and
          // is deliberately not shown; the dedicated copy lands with the
          // connections-copy slice, which carries the 14-locale pass.
          next[slug] = {
            kind: 'error',
            text: t('pages.connectionsPage.action_failed', {
              error: t('pages.connectionsPage.unknown_error'),
            }),
          }
        }
        return next
      })
    }
  }, [servers, oauthByServer, mintByServer, locallyWaiting, queryClient, t])

  const filteredProviders = useMemo(() => {
    // Held feature: offer nothing. No card renders, so no Connect button and no
    // OAuth flow is reachable, while the panel itself still renders exactly the
    // markup it renders on main -- which is what keeps the render-time i18n gate
    // measuring a comparable surface.
    if (!servicesEnabled) return []
    const needle = search.trim().toLowerCase()
    if (!needle) return CONNECTION_PROVIDERS
    return CONNECTION_PROVIDERS.filter(provider =>
      `${provider.name} ${provider.slug} ${provider.mcp_url}`.toLowerCase().includes(needle),
    )
  }, [search, servicesEnabled])

  useEffect(() => {
    if (activeTab !== 'services' || !highlightedSlug) return
    requestAnimationFrame(() => {
      document.getElementById(`connection-${highlightedSlug}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
  }, [activeTab, highlightedSlug])

  const run = async (
    provider: ConnectionProvider,
    action: ConnectionAction,
    operation: () => Promise<void>,
  ): Promise<boolean> => {
    setBusy({ slug: provider.slug, action })
    setFeedback(current => {
      const next = { ...current }
      delete next[provider.slug]
      return next
    })
    try {
      await operation()
      return true
    } catch (error) {
      const message = error instanceof Error ? error.message : t('pages.connectionsPage.unknown_error')
      setFeedback(current => ({
        ...current,
        [provider.slug]: { kind: 'error', text: t('pages.connectionsPage.action_failed', { error: message }) },
      }))
      return false
    } finally {
      setBusy(current => current?.slug === provider.slug ? null : current)
    }
  }

  const connect = async (provider: ConnectionProvider, existing?: McpServer) => run(provider, 'connect', async () => {
    // Snapshot the newest banner already observed for this server: anything
    // at or below this timestamp predates the attempt (same clock domain as
    // the banners themselves — see PendingConnect.sinceTs).
    const sinceTs = oauthByServer[provider.slug]?.timestamp ?? 0
    if (existing) {
      // Round-trip the stored spec and only overlay the url: a `{ url }`-only
      // PUT is authoritative for the OAuth hints, so it would clear configured
      // `scopes`/`clientId` (and any other stated field) on every reconnect.
      const stored = await api.mcpCustomGet(existing.name)
      await api.mcpCustomUpdate(existing.name, { ...stored.spec, url: provider.mcp_url })
      // Editing a spec deliberately preserves the disabled flag ("editing is
      // not consent to run") — but Reconnect IS consent, so re-enable the
      // KiroCrew-managed scope. mcpToggle would write the GLOBAL mcp.json
      // (creating an empty stub for kirocrew-scoped names), so use the
      // scope-preserving apply instead: kirocrew on, every observed global
      // scope passed through unchanged (the backend defaults kiroGlobal to
      // false when omitted).
      if (!existing.enabled) {
        const reenable: McpApplyChange = { name: existing.name, kirocrew: true }
        for (const [scope, present] of Object.entries(existing.presence ?? {})) {
          if (scope !== 'kirocrew' && scope.endsWith('Global')) reenable[scope as `${string}Global`] = !!present
        }
        await api.mcpApply([reenable])
      }
    } else {
      await api.mcpCustomAdd({ [provider.slug]: { url: provider.mcp_url } }, true)
    }
    // Ask for the approval URL rather than waiting for one, and await it: a
    // rejected POST must reach `run`'s error path instead of leaving the card in
    // a waiting state no mint will ever answer. Ordered after the entry write
    // because the mint activates a one-server spec derived from it. The response
    // names the row THIS tab started, so a sibling tab's terminal state cannot be
    // mistaken for ours.
    const started = await api.connectionsMint(provider.slug)
    setLocallyWaiting(current => ({
      ...current,
      [provider.slug]: {
        kind: existing ? 'reconnect' : 'new',
        sinceTs,
        token: started?.token,
      },
    }))
    // Kick a real status probe so the card reflects the new entry instead of
    // dead-ending on the cached /api/mcp read.
    void api.mcpProbe().then(probed => {
      queryClient.setQueryData<McpServer[]>(['mcp-servers'], probed as McpServer[])
    }).catch(() => undefined)
    await queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
  })

  const disconnect = async (provider: ConnectionProvider, server: McpServer, cancelled = false) => run(provider, 'disconnect', async () => {
    await api.mcpApply([{ name: server.name, uninstall: true }])
    setLocallyWaiting(current => {
      const next = { ...current }
      delete next[provider.slug]
      return next
    })
    await queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
    if (!cancelled) {
      setFeedback(current => ({
        ...current,
        [provider.slug]: disconnectFeedback(provider, t('pages.connectionsPage.disconnected_locally')),
      }))
    }
  })

  const cancelConnection = async (provider: ConnectionProvider, server?: McpServer): Promise<boolean> => {
    if (uninstallOnCancel(locallyWaiting[provider.slug])) {
      // The entry may not be in the cached list yet (probe still pending) —
      // fall back to the slug the connect just wrote so Cancel always undoes it.
      const target = server ?? ({ name: provider.slug } as McpServer)
      return disconnect(provider, target, true)
    }
    setLocallyWaiting(current => {
      const next = { ...current }
      delete next[provider.slug]
      return next
    })
    return true
  }

  const testConnection = async (provider: ConnectionProvider) => run(provider, 'test', async () => {
    const probed = await api.mcpProbe() as McpServer[]
    queryClient.setQueryData<McpServer[]>(['mcp-servers'], probed)
    const tested = serverForConnection(provider, probed)
    if (!tested || tested.status !== 'ok') throw new Error(t('pages.connectionsPage.test_failed'))
    setFeedback(current => ({
      ...current,
      [provider.slug]: { kind: 'success', text: t('pages.connectionsPage.connection_healthy') },
    }))
  })

  const relayReturnAddress = async (provider: ConnectionProvider, returnAddress: string) => run(provider, 'relay', async () => {
    await api.mcpOAuthRelay(provider.slug, returnAddress)
    setFeedback(current => ({
      ...current,
      [provider.slug]: { kind: 'success', text: t('pages.connectionsPage.return_address_delivered') },
    }))
    await queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
  })

  const selectTab = (tab: 'services' | 'mcp-servers') => setActiveTab(tab)
  const onTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
    event.preventDefault()
    setActiveTab(current => current === 'services' ? 'mcp-servers' : 'services')
  }
  const openProvider = (slug: string) => {
    setSearch('')
    setHighlightedSlug(slug)
    setActiveTab('services')
  }

  return (
    <section className="min-w-0" aria-label={t('pages.connectionsPage.connections')}>
      <div className="mb-4 flex border-b border-border" role="tablist" aria-label={t('pages.connectionsPage.connection_views')}>
        <button
          id="connections-services-tab"
          type="button"
          role="tab"
          aria-selected={activeTab === 'services'}
          aria-controls="connections-services-panel"
          tabIndex={activeTab === 'services' ? 0 : -1}
          onClick={() => selectTab('services')}
          onKeyDown={onTabKeyDown}
          className={`flex items-center gap-1.5 border-b-2 px-3 py-2 text-[13px] font-medium transition-colors ${activeTab === 'services' ? 'border-accent text-accent' : 'border-transparent text-muted hover:text-text'}`}
        >
          <Link2 className="h-4 w-4" aria-hidden="true" /> {t('pages.connectionsPage.services')}
        </button>
        <button
          id="connections-mcp-tab"
          type="button"
          role="tab"
          aria-selected={activeTab === 'mcp-servers'}
          aria-controls="connections-mcp-panel"
          tabIndex={activeTab === 'mcp-servers' ? 0 : -1}
          onClick={() => selectTab('mcp-servers')}
          onKeyDown={onTabKeyDown}
          className={`flex items-center gap-1.5 border-b-2 px-3 py-2 text-[13px] font-medium transition-colors ${activeTab === 'mcp-servers' ? 'border-accent text-accent' : 'border-transparent text-muted hover:text-text'}`}
        >
          <Server className="h-4 w-4" aria-hidden="true" /> {t('pages.connectionsPage.mcp_servers')}
        </button>
      </div>

      {activeTab === 'services' ? (
        <div id="connections-services-panel" role="tabpanel" aria-labelledby="connections-services-tab">
          {servicesEnabled && <div className="mb-4 flex items-center gap-3">
            <SearchInput
              value={search}
              onChange={event => setSearch(event.target.value)}
              placeholder={t('pages.connectionsPage.search_services')}
              aria-label={t('pages.connectionsPage.search_services')}
              className="max-w-[520px] flex-1"
            />
            <Badge variant="muted">{t('pages.connectionsPage.services_available', { value: filteredProviders.length })}</Badge>
          </div>}

          {isError && (
            <div role="alert" className="mb-3 rounded-md border border-danger/30 bg-danger-subtle px-3 py-2 text-[12px] text-danger">
              {t('pages.connectionsPage.could_not_load_status')}
            </div>
          )}

          {isLoading ? (
            <ContentSkeleton rows={6} />
          ) : filteredProviders.length === 0 ? (
            <div className="rounded-lg border border-dashed border-border px-4 py-10 text-center text-sm text-muted">
              {t('pages.connectionsPage.no_matching_services')}
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-3 xl:grid-cols-2 2xl:grid-cols-3">
              {filteredProviders.map(provider => {
                const server = serverForConnection(provider, servers)
                const pending = locallyWaiting[provider.slug]
                const oauth = withMintedUrl(
                  effectiveOAuth(oauthByServer[provider.slug], pending),
                  mintByServer[provider.slug],
                )
                const state = connectionStateFor(server, oauth, !!pending)
                const cardBusy = busy?.slug === provider.slug ? busy.action : undefined
                return (
                  <ConnectionCard
                    key={provider.slug}
                    provider={provider}
                    server={server}
                    state={state}
                    oauth={oauth}
                    busy={cardBusy}
                    feedback={feedback[provider.slug]}
                    highlighted={highlightedSlug === provider.slug}
                    onConnect={() => connect(provider)}
                    onCancel={() => cancelConnection(provider, server)}
                    onDisconnect={() => server ? disconnect(provider, server) : Promise.resolve()}
                    onReconnect={() => connect(provider, server)}
                    onTest={() => testConnection(provider)}
                    onRelay={returnAddress => relayReturnAddress(provider, returnAddress)}
                  />
                )
              })}
            </div>
          )}
        </div>
      ) : (
        <div id="connections-mcp-panel" role="tabpanel" aria-labelledby="connections-mcp-tab">
          <McpTab onManagedProviderClick={openProvider} />
        </div>
      )}
    </section>
  )
}
