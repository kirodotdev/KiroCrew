import { useEffect, useMemo, useState, type KeyboardEvent, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  ExternalLink,
  Link2,
  Loader2,
  RotateCw,
  Server,
  Unplug,
  X,
} from 'lucide-react'
import { api } from '../../api/client'
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

export type ConnectionCardState =
  | 'not-connected'
  | 'waiting-for-approval'
  | 'connected'
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
  if (server.status === 'error' || server.status === 'disabled') return 'needs-attention'
  return 'waiting-for-approval'
}

function latestOAuthByServer(
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
              {t('pages.connectionsPage.finish_approving_in_browser')}
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

  useEffect(() => {
    setLocallyWaiting(current => {
      let changed = false
      const next = { ...current }
      for (const provider of CONNECTION_PROVIDERS) {
        const pending = current[provider.slug]
        if (!pending) continue
        const server = serverForConnection(provider, servers)
        const oauth = oauthByServer[provider.slug]
        const fresh = effectiveOAuth(oauth, pending)
        if (server?.status === 'ok' || fresh?.completed || fresh?.failed) {
          delete next[provider.slug]
          changed = true
        }
      }
      return changed ? next : current
    })
  }, [servers, oauthByServer])

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
      await api.mcpCustomUpdate(existing.name, { url: provider.mcp_url })
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
    setLocallyWaiting(current => ({ ...current, [provider.slug]: { kind: existing ? 'reconnect' : 'new', sinceTs } }))
    // Kick a real status probe so the card reflects the new entry instead of
    // dead-ending on the cached /api/mcp read; the authorization itself (and
    // its approval URL) is produced by the runtime on the next agent turn.
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
                const oauth = effectiveOAuth(oauthByServer[provider.slug], pending)
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
