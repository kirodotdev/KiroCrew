import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { ChevronDown, Settings2 } from 'lucide-react'
import { motion, useReducedMotion } from 'framer-motion'
import { CONNECTION_PROVIDER_PARAM, GOOGLE_ENTRY_SLUG, isGoogleSlug } from './connectionsLinks'
import { OAuthAppPanel, OAuthClientWrittenContext, OAuthDraftsContext, OAuthDraftValuesContext, type OAuthDraftValues, type OAuthDraftValueStore } from './OAuthAppPanel'
import ProviderLogo from './ProviderLogo'
import { api, ApiError, type ConnectionMintState, type ConnectionStatus } from '../../api/client'
import { useAppSelector } from '../../store'
import type { McpApplyChange, McpServer } from '../../types'
import { Badge, Btn, ContentSkeleton, SearchInput } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import RestartButton from '../../components/RestartButton'
import { fmtList } from '../../i18n/format'
import { findReport, parseErrorCode } from '../../utils/errorReport'
import { OAUTH_ENDPOINT_ALLOWLIST_GUIDE_URL, docSectionHref } from '../../utils/docsLink'
import { safeGetSessionItem, safeSetSessionItem } from '../../utils/safeStorage'
import {
  CONNECTION_PROVIDERS,
  isPreregisteredProvider,
  serverForConnection,
  type ConnectionProvider,
} from './registry'
import { CARD_LAYOUT_TRANSITION, ConnectionCard, ConnectionStateBadge, PrerequisiteTip } from './ConnectionsPageCard'
import type { ConnectionCardState } from './connectionsPageCardState'
import {
  confirmedGrantPresent, connectionStateFor, disconnectFeedback, effectiveOAuth, latestOAuthByServer, mintOutcome,
  mintRowIsOurs, undismissedOAuth, uninstallOnCancel, withMintedUrl, type ConnectionAction, type Feedback, type OAuthState,
  type PendingConnect,
} from './connectionsPageCardState'

/** How a settings search hit or an old OAuth Apps link names one OAuth app. */
const OAUTH_HIGHLIGHT_PREFIX = 'connections.oauth-client-'

/** The card (or the Google entry) a provider slug lives in on this page. */
/** Every Google product the one Google OAuth app part saves and removes. */
const GOOGLE_PANEL_SLUGS = CONNECTION_PROVIDERS.filter(provider => isGoogleSlug(provider.slug)).map(provider => provider.slug)
/** The other pre-registered providers, each with its own OAuth app part. */
const OAUTH_ONLY_SLUGS = CONNECTION_PROVIDERS
  .filter(provider => isPreregisteredProvider(provider) && !isGoogleSlug(provider.slug))
  .map(provider => provider.slug)

function entryOf(slug: string): string {
  return isGoogleSlug(slug) ? GOOGLE_ENTRY_SLUG : slug
}

// A provider card renders from `ConnectionsPageCard.tsx`, and what it shows is
// decided in `connectionsPageCardState.ts`; neither imports this module. Callers
// keep importing every name exported below from here.
export {
  confirmedGrantPresent, connectionStateFor, disconnectFeedback, effectiveOAuth, errorIndicatesProviderRejection,
  latestOAuthByServer, mintOutcome, probeIndicatesConnected, uninstallOnCancel, withMintedUrl,
} from './connectionsPageCardState'
export type { ConnectionCardState, Feedback, MintOutcome, OAuthState, PendingConnect } from './connectionsPageCardState'

/** Mint poll cadence. A cold mint takes seconds, so this is tuned to surface the
 *  URL promptly without spinning on a request that mostly answers `minting`. */
const MINT_POLL_MS = 2_000

/** Whether a chat banner records a consent completed after a failed check: a
 *  newer banner, or the very banner the attempt saw open (same timestamp) that
 *  has completed since. An older banner is never one. */
function completedAfterFailedCheck(
  banner: OAuthState | undefined,
  fence: { sinceTs: number; completedBefore: boolean },
): boolean {
  if (!banner?.completed) return false
  return banner.timestamp > fence.sinceTs || (banner.timestamp === fence.sinceTs && !fence.completedBefore)
}

/** The Services search text, kept in `sessionStorage` for the browser tab. */
const SERVICES_SEARCH_KEY = 'kirocrew:connections-services-search'

/** Authorization-status poll cadence. A grant changes rarely and the read is a
 *  local stat, so this is slow relative to the mint poll — it only has to notice
 *  a grant completed outside the dashboard and keep connected-since fresh. */
const CONNECTION_STATUS_POLL_MS = 30_000

/**
 * `servicesEnabled` gates the provider gallery. The gallery ships ON, so the
 * dashboard normally passes `true`; the PARAMETER default stays closed so a
 * caller that forgets to pass it cannot open a gallery by omission. False means
 * the instance pulled the `connections_ui` escape hatch: the Services panel
 * offers no providers, so no card, Connect button or OAuth flow is reachable.
 *
 * A closed panel still RENDERS rather than being removed, so the page keeps
 * one shape with the gallery empty.
 *
 * A pre-registered provider's card opens its OAuth app part in place; the six
 * Google products are one entry with one OAuth app part for all of them.
 */
export default function ConnectionsPage({ servicesEnabled = false }: { servicesEnabled?: boolean } = {}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const reduceMotion = useReducedMotion()
  /** OAuth app forms holding unsaved input, by setting slug. */
  const [draftSlugs, setDraftSlugs] = useState<ReadonlySet<string>>(() => new Set())
  const reportDraft = useCallback((settingSlug: string, dirty: boolean) => {
    setDraftSlugs(prev => {
      if (prev.has(settingSlug) === dirty) return prev
      const next = new Set(prev)
      if (dirty) next.add(settingSlug)
      else next.delete(settingSlug)
      return next
    })
  }, [])
  // The values behind those drafts, kept by the page because it outlives the
  // forms: a config read landing or the gallery flag changing swaps the
  // OAuth-apps-only forms for the gallery's, and the new form reads its draft
  // back from here. Dropped with the page.
  const draftValues = useRef(new Map<string, OAuthDraftValues>())
  const draftValueStore = useMemo<OAuthDraftValueStore>(() => ({
    get: key => draftValues.current.get(key),
    set: (key, values) => { draftValues.current.set(key, values) },
  }), [])
  const [params, setParams] = useSearchParams()
  const providerParam = params.get(CONNECTION_PROVIDER_PARAM)
  const highlightParam = params.get('highlight')
  /** Entries (a provider slug, or the Google entry) whose OAuth app part is open. */
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set())
  const toggleExpanded = (entry: string) => setExpanded(prev => {
    const next = new Set(prev)
    if (next.has(entry)) next.delete(entry)
    else next.add(entry)
    return next
  })
  // Kept for the browser tab: opening a chat unmounts this page, and the
  // search must still be there on the way back (#25962).
  const [search, setSearch] = useState(() => safeGetSessionItem(SERVICES_SEARCH_KEY) ?? '')
  useEffect(() => {
    safeSetSessionItem(SERVICES_SEARCH_KEY, search)
  }, [search])
  /** Pending connect attempts. `kind` decides Cancel semantics (only a
   *  cancelled *new* connect uninstalls the entry it just created); `sinceTs`
   *  fences off stale `mcp_oauth` banners left over from an earlier grant of
   *  the same server name (they must not mark a fresh attempt connected). */
  const [locallyWaiting, setLocallyWaiting] = useState<Record<string, PendingConnect>>({})
  // A client save or delete cancels that product's sign-in on the backend, and
  // the poll then reads `idle`, which never settles a wait: drop it here so the
  // card offers Connect again instead of waiting on a sign-in that is gone.
  // The cached `awaiting_consent` verdict would put the card straight back into
  // waiting, so it goes too, with the same fence Cancel uses: cancel the poll
  // in flight, drop the verdict, then re-fetch. This runs per committed write,
  // so a partial fan-out that ends in an error still clears what it wrote.
  const clientWritten = useCallback((slugs: string[]) => {
    setLocallyWaiting(current => {
      if (!slugs.some(slug => current[slug])) return current
      const next = { ...current }
      for (const slug of slugs) delete next[slug]
      return next
    })
    void queryClient.cancelQueries({ queryKey: ['connections-status'] }).then(() => {
      queryClient.setQueryData<Record<string, ConnectionStatus>>(['connections-status'], current => {
        if (!current || !slugs.some(slug => slug in current)) return current
        const next = { ...current }
        for (const slug of slugs) delete next[slug]
        return next
      })
      void queryClient.invalidateQueries({ queryKey: ['connections-status'] })
    })
  }, [queryClient])
  /** Newest chat banner timestamp seen when Cancel was pressed, per slug. A
   *  later Connect supersedes it (its own `sinceTs` fence takes over). */
  const [dismissedOAuth, setDismissedOAuth] = useState<Record<string, number>>({})
  /** Slugs whose Connect mint failed with `mint_grant_unproven` or
   *  `mint_sign_in_rejected`: any stored grant files are still there, but they
   *  did not pass the check, so the card does not count them as a grant until a
   *  later mint reports `granted`. The
   *  attempt's banner fence stays too: a chat banner from before it (`sinceTs`)
   *  is no proof, unless it completes only AFTER the failure
   *  (`completedBefore` false), which is a new consent. */
  const [unprovenGrants, setUnprovenGrants] = useState<Record<string, { sinceTs: number; completedBefore: boolean }>>({})
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
    refetchInterval: Object.values(locallyWaiting).some(Boolean) ? 5_000 : false,
  })

  // Authorization verdict + first-connect time per visible provider. Polled while
  // the gallery is mounted so a grant completed outside the dashboard, and the
  // connected-since clock, surface without a manual refresh. Additive to the mint
  // feed below; this never mints and never owns reachability (that stays with
  // /api/mcp). Declared before the mint feed because `waitingSlugs` reads the
  // awaiting_consent verdicts.
  const { data: statusBySlug = {}, isError: statusIsError } = useQuery<Record<string, ConnectionStatus>>({
    queryKey: ['connections-status'],
    queryFn: async () => {
      const { connections } = await api.connectionsStatus()
      const next: Record<string, ConnectionStatus> = {}
      for (const entry of connections) next[entry.slug] = entry
      return next
    },
    enabled: servicesEnabled,
    // Only while this page is mounted, which is while the gallery is on screen.
    refetchInterval: CONNECTION_STATUS_POLL_MS,
    // A cached verdict from before a Connect would show Connect again on
    // navigating back, and a click would replace the live sign-in.
    refetchOnMount: 'always',
  })

  // Minted approval URLs, keyed by slug. Fetched only while a connect is pending:
  // outside that window nothing is minting and the endpoint would answer `idle`.
  // "Pending" has two sources of truth, and both must feed the poll: this tab's
  // own clicks (locallyWaiting) AND the backend's awaiting_consent verdict --
  // per-tab state dies on a reload, so without the status-fed half the
  // refresh-survival waiting card would render with no approval URL and copy
  // telling the user to start a flow that is already running.
  const waitingSlugs = useMemo(() => {
    const slugs = new Set(Object.keys(locallyWaiting))
    for (const [slug, entry] of Object.entries(statusBySlug)) {
      if (entry.status === 'awaiting_consent') slugs.add(slug)
    }
    return [...slugs].sort()
  }, [locallyWaiting, statusBySlug])
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

  /** Latched by the premint effect below, so one page visit warms once. */
  const premintFiredRef = useRef(false)
  // Warm every mintable provider's approval URL once, ahead of any click, so a
  // Connect serves a URL the warm table already holds instead of paying a cold
  // spawn. This is the documented caller for POST /api/connections/premint.
  //
  // Keyed on `servicesEnabled` rather than mount, because the flag arrives with
  // the config query: the page's FIRST render is always gated-off, so a `[]`
  // effect would warm on every install that never opted in. Gated for the same
  // reason as the status feed above — behind a closed flag the gallery offers no
  // card to connect, and warming would spawn a process for a surface that has no
  // Connect button.
  //
  // The ref guards StrictMode's development double-invoke (an in-flight request
  // is not cancellable, so a teardown flag cannot un-spawn the first activation).
  // It is per-mount by design: a later remount from navigation may warm again,
  // which the engine's warm reuse makes cheap, and suppressing it across visits
  // would pin the first visit's grant state for the whole session.
  useEffect(() => {
    if (!servicesEnabled || premintFiredRef.current) return
    premintFiredRef.current = true
    // Strictly fire-and-forget: nothing here reaches render, and the response is
    // never a verdict (a card's state stays its own mint feed). Every failure is
    // swallowed on purpose — a non-owner dashboard session is denied by design,
    // and a cold mint on Connect is the intended fallback either way.
    void api.connectionsPremint().catch(() => undefined)
  }, [servicesEnabled])

  useEffect(() => {
    // Decided BEFORE any setState: a state updater runs on a later render, so
    // collecting side-effect targets inside one leaves them empty at read time.
    const cleared: string[] = []
    const mintFailures: Array<{
      slug: string; name: string; guide?: string; reason?: string; endpoint?: string
      fence: { sinceTs: number; completedBefore: boolean }
    }> = []
    const grantedMints: string[] = []
    for (const provider of CONNECTION_PROVIDERS) {
      const pending = locallyWaiting[provider.slug]
      if (!pending) continue
      const server = serverForConnection(provider, servers)
      const banner = oauthByServer[provider.slug]
      const fresh = effectiveOAuth(banner, pending)
      const mint = mintByServer[provider.slug]
      const outcome = mintOutcome(mint, pending)
      if (!(server?.status === 'ok' || fresh?.completed || fresh?.failed || outcome.clearWait)) {
        continue
      }
      cleared.push(provider.slug)
      if (
        outcome.error
        || (
          mintRowIsOurs(mint, pending)
          && mint?.state === 'expired'
        )
      ) {
        mintFailures.push({
          slug: provider.slug,
          name: provider.name,
          guide: provider.org_admin_guide,
          // A reasonless expiry is the sign-in window running out.
          reason: mint.reason ?? (mint.state === 'expired' ? 'expired' : undefined),
          endpoint: mint.rejected_endpoint,
          // Only the banner the attempt was fenced on (`sinceTs`) can say it was
          // still open; any other newest banner leaves the fence shut.
          fence: {
            sinceTs: pending.sinceTs,
            completedBefore: banner?.timestamp === pending.sinceTs ? !!banner.completed : true,
          },
        })
      }
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
    if (grantedMints.length || mintFailures.length) {
      // Same staleness on the authorization axis: the status feed polls every
      // 30s, so its cached pre-consent verdict (grantPresent=false /
      // awaiting_consent) would outrank the grant that just landed and downgrade
      // the card, or hold a failed attempt's card on waiting, for up to a full
      // poll interval. Invalidate rather than setQueryData: the fresh verdict is
      // the backend's to compute.
      void queryClient.invalidateQueries({ queryKey: ['connections-status'] })
    }
    // A refused sign-in fails only while no grant is observed or proven, so grant
    // files still on disk then are the ones its check disproved: fence them too.
    const unproven = mintFailures.filter(
      failure => failure.reason === 'mint_grant_unproven' || failure.reason === 'mint_sign_in_rejected',
    )
    if (unproven.length || grantedMints.length) {
      setUnprovenGrants(current => {
        const next = { ...current }
        for (const slug of grantedMints) delete next[slug]
        for (const { slug, fence } of unproven) next[slug] = fence
        return next
      })
    }
    if (mintFailures.length) {
      setFeedback(current => {
        const next = { ...current }
        for (const { slug, name, guide, reason, endpoint } of mintFailures) {
          let error: string
          let detail: string | undefined
          let help: Feedback['help']
          // A refused sign-in an org admin's policy can cause links the provider's
          // admin checklist, when the registry names one.
          const adminGuide = docSectionHref(guide)
          switch (reason) {
            case 'crew_oauth_callback_access_denied':
              error = t('pages.connectionsPage.mint_failure_callback_access_denied', { provider: name })
              if (adminGuide) help = { href: adminGuide }
              break
            case 'crew_oauth_callback_invalid_scope':
              error = t('pages.connectionsPage.mint_failure_callback_invalid_scope', { provider: name })
              if (adminGuide) help = { href: adminGuide }
              break
            case 'crew_oauth_callback_unauthorized_client':
              error = t('pages.connectionsPage.mint_failure_callback_unauthorized_client', { provider: name })
              if (adminGuide) help = { href: adminGuide }
              break
            case 'crew_oauth_callback_invalid_request':
              error = t('pages.connectionsPage.mint_failure_callback_invalid_request', { provider: name })
              break
            case 'crew_oauth_callback_unsupported_response_type':
              error = t('pages.connectionsPage.mint_failure_callback_unsupported_response_type', { provider: name })
              break
            case 'crew_oauth_callback_server_error':
              error = t('pages.connectionsPage.mint_failure_callback_server_error', { provider: name })
              break
            case 'crew_oauth_callback_temporarily_unavailable':
              error = t('pages.connectionsPage.mint_failure_callback_temporarily_unavailable', { provider: name })
              break
            case 'crew_oauth_callback_refused':
              error = t('pages.connectionsPage.mint_failure_callback_refused', { provider: name })
              break
            case 'mint_timeouterror':
              error = t('pages.connectionsPage.mint_failure_timed_out')
              break
            case 'mint_process_gone':
              error = t('pages.connectionsPage.mint_failure_process_gone')
              break
            case 'mint_kiro_cli_missing':
              error = t('pages.connectionsPage.mint_failure_kiro_cli_missing')
              break
            case 'expired':
            case 'crew_oauth_consent_expired':
              error = t('pages.connectionsPage.mint_failure_sign_in_timed_out')
              break
            case 'mint_server_absent':
              error = t('pages.connectionsPage.mint_failure_server_absent')
              break
            case 'mint_grant_unproven':
              error = t('pages.connectionsPage.mint_failure_grant_unproven')
              break
            case 'mint_sign_in_rejected':
              error = t('pages.connectionsPage.mint_failure_sign_in_rejected')
              break
            case 'mint_url_rejected':
              // Name WHICH endpoint was refused when the backend could reduce it
              // to a copy-ready host/path. The error line stays one sentence;
              // the oauth_endpoints.json remedy rides `detail` (its own line)
              // and the guide rides `help` (a link), so the alarm text does not
              // swallow the instructions. Without an endpoint the card keeps
              // its unnamed message rather than show a remedy that cannot work.
              if (endpoint) {
                error = t('pages.connectionsPage.mint_failure_url_rejected_endpoint', { endpoint })
                detail = t('pages.connectionsPage.mint_failure_url_rejected_endpoint_detail', { endpoint })
                help = { href: OAUTH_ENDPOINT_ALLOWLIST_GUIDE_URL }
              } else {
                error = t('pages.connectionsPage.mint_failure_url_rejected')
              }
              break
            default:
              error = t('pages.connectionsPage.mint_failure_unknown')
          }
          next[slug] = {
            kind: 'error',
            text: t('pages.connectionsPage.action_failed', { error }),
            ...(detail ? { detail } : {}),
            ...(help ? { help } : {}),
          }
        }
        return next
      })
    }
  }, [servers, oauthByServer, mintByServer, locallyWaiting, queryClient, t])

  // A chat consent that completes after a failed check proves the grant, so the
  // failed-check record goes: the card must not depend on that banner staying
  // in the transcript window to keep its Connected verdict.
  useEffect(() => {
    const proven = Object.entries(unprovenGrants)
      .filter(([slug, fence]) => completedAfterFailedCheck(oauthByServer[slug], fence))
      .map(([slug]) => slug)
    if (!proven.length) return
    setUnprovenGrants(current => {
      const next = { ...current }
      for (const slug of proven) delete next[slug]
      return next
    })
  }, [oauthByServer, unprovenGrants])

  const filteredProviders = useMemo(() => {
    // Opted out (`connections_ui: false`): offer nothing. No card renders, so no
    // Connect button and no OAuth flow is reachable, while the panel itself still
    // renders exactly the markup a launched gallery renders -- which is what keeps
    // the render-time i18n gate measuring a comparable surface.
    if (!servicesEnabled) return []
    const needle = search.trim().toLowerCase()
    if (!needle) return CONNECTION_PROVIDERS
    return CONNECTION_PROVIDERS.filter(provider =>
      `${provider.name} ${provider.slug} ${provider.mcp_url}`.toLowerCase().includes(needle),
    )
  }, [search, servicesEnabled])
  const feedbackSlots = filteredProviders.flatMap(provider => {
    const value = feedback[provider.slug]
    return value ? [{ slug: provider.slug, value }] : []
  })

  // `?provider=<slug>` (a managed row on the MCP Servers page links with it)
  // rings that card, clears a search that would hide it and opens its OAuth app
  // part. The param is then dropped with a replace, so a reload does not repeat it.
  useEffect(() => {
    if (!providerParam) return
    setSearch('')
    setHighlightedSlug(providerParam)
    setExpanded(prev => new Set(prev).add(entryOf(providerParam)))
    setParams(prev => {
      const next = new URLSearchParams(prev)
      next.delete(CONNECTION_PROVIDER_PARAM)
      return next
    }, { replace: true })
  }, [providerParam, setParams])

  // A settings search hit or an old OAuth Apps link (`?highlight=
  // connections.oauth-client-<slug>`) names an OAuth app part: open it so the
  // settings highlight finds the anchor. SettingsPage's highlight hook owns the
  // ring and strips the param.
  useEffect(() => {
    if (!highlightParam?.startsWith(OAUTH_HIGHLIGHT_PREFIX)) return
    const slug = highlightParam.slice(OAUTH_HIGHLIGHT_PREFIX.length)
    setSearch('')
    setExpanded(prev => new Set(prev).add(entryOf(slug)))
  }, [highlightParam])

  // Retried when the list lands and when the gallery is turned on: on a cold
  // load the card is not rendered on the first frame. Scrolls once per highlight.
  const scrolledFor = useRef('')
  useEffect(() => {
    if (!highlightedSlug || scrolledFor.current === highlightedSlug || isLoading || !servicesEnabled) return
    const frame = requestAnimationFrame(() => {
      const el = document.getElementById(`connection-${highlightedSlug}`)
      if (!el) return
      scrolledFor.current = highlightedSlug
      el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
    return () => cancelAnimationFrame(frame)
  }, [highlightedSlug, isLoading, servicesEnabled])

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
        [provider.slug]: {
          kind: 'error',
          text: t('pages.connectionsPage.action_failed', { error: message }),
          // Keyed on the message the API layer journaled, not the localized text
          // above, so the endpoint/status/`code` context survives into the
          // shared error surface in every locale.
          report: findReport(message),
        },
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
      // Request the registry's scopes, as the warm path does: an entry written
      // without them authorizes with the provider's default grant (wider than
      // the read-only set for Google) and no longer matches the warm plan.
      const scopes = provider.recommended_scopes ?? []
      await api.mcpCustomAdd(
        { [provider.slug]: scopes.length ? { url: provider.mcp_url, scopes: [...scopes] } : { url: provider.mcp_url } },
        true,
      )
    }
    // Ask for the approval URL rather than waiting for one, and await it: a
    // rejected POST must reach `run`'s error path instead of leaving the card in
    // a waiting state no mint will ever answer. Ordered after the entry write
    // because the mint activates a one-server spec derived from it. The response
    // names the row THIS tab started, so a sibling tab's terminal state cannot be
    // mistaken for ours.
    const started = await api.connectionsMint(provider.slug)
    setDismissedOAuth(current => {
      if (!(provider.slug in current)) return current
      const next = { ...current }
      delete next[provider.slug]
      return next
    })
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
    // Cancel must NOT revoke, and this branch is load-bearing. A grant is keyed by
    // ENDPOINT, not by entry, so a cancelled *new* connect routed through the
    // revoking endpoint would delete a grant that a user's own separately-named
    // server at the same URL is still using — silently, because `cancelled`
    // suppresses the note below. Cancel therefore keeps the entry-only removal it
    // always had; only a deliberate Disconnect revokes.
    if (cancelled) {
      await api.mcpApply([{ name: server.name, uninstall: true }])
    }
    // One call does all three local things: dispose any in-flight mint, delete the
    // stored grant artifacts when they are ours alone, and remove the MCP entry.
    // This was an mcpApply uninstall, which took the entry out and left a usable
    // refresh token on disk — so a later reconnect silently resumed a grant this
    // card had already told the user was gone.
    const result = cancelled ? undefined : await api.connectionsDisconnect(provider.slug)
    setLocallyWaiting(current => {
      const next = { ...current }
      delete next[provider.slug]
      return next
    })
    await queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
    // The grant feed too, mirroring the connect-completed path: a Disconnect that
    // deletes the grant but keeps the entry would otherwise leave the cached
    // grantPresent=true rendering "Connected" beside a note saying the grant is
    // gone, until the next poll.
    void queryClient.invalidateQueries({ queryKey: ['connections-status'] })
    if (result) {
      // Facts are reported INDEPENDENTLY, never as an exclusive chain — two review
      // rounds landed findings in this span because each single message asserted a
      // second fact it never tested ("Entry removed." while the entry stayed;
      // "Disconnected, but…" while the backend declined). The GRANT clause states
      // only what happened to the grant; the ENTRY clause is appended whenever the
      // backend left the entry alone. Outcomes that announce: a survivor is an
      // `error` (a grant outliving the click is the state this endpoint exists to
      // prevent), a census gap is a `warning` (nothing failed, but the grant is
      // still there and the configuration needs checking), and a not-ours entry is
      // a `warning` too (the card still shows Connected with a live Disconnect
      // button, so a green success would misreport a click that changed nothing).
      // A grant deliberately kept for a NAMED sharer needs nothing from the user,
      // so it stays a success status.
      // `grantSurviving` now reports FAILED unlinks only: the backend re-stats
      // just the pairs it actually tried to remove, so a deliberate keep (a
      // sharer, or a census gap) never appears here. That is what collapses the
      // precedence ladder these branches used to need — a survivor no longer has
      // to be disambiguated against `shared`/`censusGap` before it can alert.
      const survived = result.grantSurviving.length > 0
      const shared = result.grantSharedWith.length > 0
      const censusGap = !shared && result.grantCensusIncomplete
      const entryKept = !result.entryRemoved
      // The not-ours outcome: nothing here was this provider's to remove — no
      // grant artifacts existed and no purge-eligible entry matched, so the
      // click changed nothing. The entry clause is the whole message there, and
      // it must hand the user a next move: without the recourse their only
      // move is to click Disconnect again. The message states only what the
      // response proves (nothing changed) — `entryRemoved=false` cannot say WHY
      // the entry was kept, so the copy never asserts a cause.
      const entryNotOurs = entryKept && !survived && !shared && !censusGap && !result.grantRemoved
      // The census knows which source it could not read, so the repair instruction
      // names it. Empty is the honest case, not a missing field: `censusIncomplete`
      // is also set by an entry whose URL could not be compared, which names no
      // file -- so that outcome keeps the source-less wording instead of
      // interpolating a blank into "fix that file".
      const unreadable = result.grantCensusUnreadable ?? []
      const grantClause = survived
        ? t('pages.connectionsPage.disconnect_grant_survived')
        : shared
          ? t('pages.connectionsPage.disconnect_grant_shared', {
              names: fmtList(result.grantSharedWith),
            })
          : censusGap
            ? unreadable.length > 0
              ? t('pages.connectionsPage.disconnect_census_incomplete_source', {
                  source: unreadable[0],
                })
              : t('pages.connectionsPage.disconnect_census_incomplete')
            : result.grantRemoved && entryKept
            ? t('pages.connectionsPage.disconnect_entry_not_ours')
            : entryKept
              ? '' // no grant existed and the entry stayed: the entry clause is the whole story
              : t('pages.connectionsPage.disconnected_locally')
      const entryClause =
        entryKept && (survived || shared || !result.grantRemoved)
          ? t('pages.connectionsPage.disconnect_entry_left_alone')
          : ''
      // The rebuild after the purge did not confirm a write, so new sessions may
      // still load the entry. That is a failed step, reported like a survivor.
      // Only when the entry actually left the config: a click that removed
      // nothing changed nothing the spec should follow.
      const specNotRewritten = result.agentSpecRewritten === false && result.entryRemoved
      const specClause = specNotRewritten ? t('pages.connectionsPage.disconnect_spec_not_rewritten') : ''
      setFeedback(current => ({
        ...current,
        [provider.slug]: disconnectFeedback(
          provider,
          [grantClause, entryClause, specClause].filter(Boolean).join(' '),
          // A census gap tells the user their access was NOT withdrawn and hands
          // them a repair to make; a not-ours entry leaves the card showing
          // Connected with a live Disconnect button, so a green success would
          // misreport a click that changed nothing. Neither is an `error`,
          // because nothing failed — a safety rule declined to act, or there was
          // nothing here to act on.
          survived || specNotRewritten ? 'error' : censusGap || entryNotOurs ? 'warning' : 'success',
        ),
      }))
    }
  })

  const cancelConnection = async (provider: ConnectionProvider, server?: McpServer): Promise<boolean> => {
    const pending = locallyWaiting[provider.slug]
    // Dispose the in-flight backend mint (its kiro-cli process, loopback listener
    // and ephemeral spec) whether or not we also uninstall the config below. This
    // is what main lacked: a cancelled reconnect or stateless wait dropped only
    // the local wait and left the mint held to its TTL.
    //
    // Deliberately NOT awaited. Disposal waits on a child process shutdown, which
    // is bounded only by the gateway's shutdown timeout (~10s), and awaiting it
    // would leave Cancel un-actioned and re-clickable for that whole window. The
    // withdrawal the user asked for is local; the dispose is bookkeeping that
    // follows. Token-fenced so a stale tab cannot dispose a sibling's row, and
    // the rejection is swallowed so a gateway failure never surfaces as a Cancel
    // that did not work.
    void api.connectionsCancel(provider.slug, pending?.token).catch(() => undefined).finally(() => {
      // The dispose just changed the backend verdict, so re-fetch it rather
      // than waiting out the 30s poll.
      void queryClient.invalidateQueries({ queryKey: ['connections-status'] })
    })
    // Standard optimistic-update fence: a 30s poll already in flight was
    // fetched BEFORE the cancel, so letting it resolve after the drop below
    // would repopulate the stale awaiting_consent verdict until the
    // settlement invalidation lands. Cancel the in-flight fetch first.
    await queryClient.cancelQueries({ queryKey: ['connections-status'] })
    // Drop this provider's cached verdict NOW: the poll cached `awaiting_consent`
    // for up to 30s, and with the flow just disposed that stale entry would put
    // the card straight back into waiting-for-approval -- a Cancel that appears
    // to not work. Dropping (not fabricating a verdict) returns the card to the
    // status-not-yet-loaded behaviour until the invalidated query answers.
    queryClient.setQueryData<Record<string, ConnectionStatus>>(['connections-status'], current => {
      if (!current || !(provider.slug in current)) return current
      const next = { ...current }
      delete next[provider.slug]
      return next
    })
    // The wait dies with the click, unconditionally and BEFORE the uninstall:
    // the mint was just disposed, so if the uninstall below fails there is no
    // outcome left that could ever clear this flag -- leaving it set would
    // strand the card on a waiting state with no live flow behind it.
    setLocallyWaiting(current => {
      const next = { ...current }
      delete next[provider.slug]
      return next
    })
    // Fence off the chat banner holding the card open (often an older
    // Authorize card for the same server), or Cancel renders the same card.
    setDismissedOAuth(current => ({
      ...current,
      [provider.slug]: oauthByServer[provider.slug]?.timestamp ?? 0,
    }))
    if (uninstallOnCancel(pending)) {
      // The entry may not be in the cached list yet (probe still pending) —
      // fall back to the slug the connect just wrote so Cancel always undoes it.
      const target = server ?? ({ name: provider.slug } as McpServer)
      return disconnect(provider, target, true)
    }
    return true
  }

  const testConnection = async (provider: ConnectionProvider) => run(provider, 'test', async () => {
    let result
    try {
      result = await api.connectionsTest(provider.slug)
    } catch (error) {
      // A single-flight refusal is a REJECTED REQUEST, so it belongs to the
      // shared error surface (`ErrorNotice`, via `Feedback.report`) rather than
      // this page's plain feedback line -- but it is named rather than left to
      // the ambiguous "action_failed" catch in `run` below, because the one
      // thing the user needs is WHICH provider is holding the slot.
      if (error instanceof ApiError && error.status === 409) {
        let runningSlug = ''
        try {
          const parsed: unknown = JSON.parse(error.body)
          if (parsed && typeof parsed === 'object' && typeof (parsed as { slug?: unknown }).slug === 'string') {
            runningSlug = (parsed as { slug: string }).slug
          }
        } catch { /* malformed body — fall back to the generic provider name below */ }
        const runningProvider = CONNECTION_PROVIDERS.find(candidate => candidate.slug === runningSlug)?.name
          ?? provider.name
        setFeedback(current => ({
          ...current,
          [provider.slug]: {
            kind: 'error',
            text: t('pages.connectionsPage.test_in_flight', { provider: runningProvider }),
            // Keyed on the message the API layer journaled, not the localized
            // text rendered above, so the endpoint/status/`code` context is
            // recovered in every locale. A miss is tolerated -- `kind: 'error'`
            // is what routes this to the shared surface, not the report.
            report: findReport(error.message),
          },
        }))
        return
      }
      throw error
    }
    if (result.verdict === 'usable') {
      setFeedback(current => ({
        ...current,
        [provider.slug]: { kind: 'success', text: t('pages.connectionsPage.connection_healthy') },
      }))
      return
    }
    if (result.verdict === 'no_tools') {
      // A connected-but-toolless GitLab is exactly who needs the provider-side
      // steps, and its prerequisite copy describes this very state ("connecting
      // succeeds but exposes no tools"), so it rides along localized. Atlassian's
      // copy describes a pre-connect consent gate the user already passed, so it
      // deliberately does not.
      setFeedback(current => ({
        ...current,
        [provider.slug]: {
          kind: 'warning',
          text: t('pages.mcpManagement.assessment.reason_no_tools_listed'),
          detail:
            provider.slug === 'gitlab'
              ? t('pages.connectionsPage.prerequisite_gitlab_steps')
              : undefined,
          help: provider.slug === 'gitlab' ? { href: provider.docs_url } : undefined,
        },
      }))
      return
    }
    throw new Error(t('pages.connectionsPage.test_failed'))
  })

  const relayReturnAddress = async (provider: ConnectionProvider, returnAddress: string, minted: boolean) => run(provider, 'relay', async () => {
    try {
      await api.mcpOAuthRelay(provider.slug, returnAddress)
    } catch (error) {
      // A spent approval can never take this address. Resolve rather than
      // throw: the card clears its field only on a resolved relay.
      if (error instanceof ApiError && parseErrorCode(error.body) === 'approval_superseded') {
        setFeedback(current => ({
          ...current,
          [provider.slug]: {
            kind: 'error',
            text: t('pages.connectionsPage.approval_superseded'),
            report: findReport(error.message),
          },
        }))
        return
      }
      throw error
    }
    setFeedback(current => ({
      ...current,
      [provider.slug]: { kind: 'success', text: t('pages.connectionsPage.return_address_delivered') },
    }))
    // The line above promises a verdict. A mint's verdict reaches the card only through
    // this tab's wait, which a reload loses, so hold one when the card's URL was the
    // mint's. A chat banner's sign-in needs none: the banner is patched in place with
    // its own verdict, which a wait would fence off.
    const mint = mintByServer[provider.slug]
    if (minted && mint) {
      setLocallyWaiting(current => current[provider.slug] ? current : {
        ...current,
        [provider.slug]: {
          kind: 'reconnect',
          sinceTs: oauthByServer[provider.slug]?.timestamp ?? 0,
          token: mint.token,
        },
      })
    }
    await queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
  })

  // The six Google products render together as one entry, where the first one
  // the filter keeps would have stood; every other provider is its own card.
  // Every card stays mounted and a search only hides the ones it does not
  // match, so an unsaved OAuth app draft survives a search.
  const matching = new Set(filteredProviders.map(provider => provider.slug))
  const allProviders = servicesEnabled ? CONNECTION_PROVIDERS : []
  const googleProviders = allProviders.filter(provider => isGoogleSlug(provider.slug))
  const googleMatching = googleProviders.filter(provider => matching.has(provider.slug))
  const entries = allProviders.filter(
    provider => !isGoogleSlug(provider.slug) || provider === googleProviders[0],
  )
  const matchingEntries = entries.filter(provider =>
    isGoogleSlug(provider.slug) ? googleMatching.length > 0 : matching.has(provider.slug))
  const oauthPanelId = (entry: string) => `connection-oauth-app-${entry}`

  return (
    <OAuthDraftsContext.Provider value={reportDraft}>
    <OAuthClientWrittenContext.Provider value={clientWritten}>
    <OAuthDraftValuesContext.Provider value={draftValueStore}>
    <section className="min-w-0" aria-label={t('pages.connectionsPage.connections')}>
      {/* Apply & Restart lives here because a Connect adds an MCP server: a
          newly enabled server reaches a chat that is already running only after
          its session is relaunched. The muted line beside it says what restarts
          and what is kept BEFORE the click. */}
      <div className="mb-4 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-border pb-2 sm:justify-end sm:pb-1.5">
        <span className="min-w-0 max-w-[34rem] text-[11.5px] leading-snug text-muted">{t('components.restartButton.beside_hint')}</span>
        <RestartButton />
      </div>

      <div id="connections-services-panel">
          {servicesEnabled && <div className="mb-4 flex items-center gap-3">
            <SearchInput
              value={search}
              onChange={event => setSearch(event.target.value)}
              placeholder={t('pages.connectionsPage.search_services')}
              aria-label={t('pages.connectionsPage.search_services')}
              className="max-w-[520px] flex-1"
            />
            <Badge variant="muted">{t('pages.connectionsPage.services_available', { value: matchingEntries.length })}</Badge>
          </div>}

          {/* The gallery turned off (`connections_ui: false`) hides the cards,
              not the OAuth apps: a stored client must stay viewable, rotatable
              and removable, and this page is the only place that can. */}
          {!servicesEnabled && (
            <div className="space-y-3" data-testid="connections-oauth-apps-only">
              {OAUTH_ONLY_SLUGS.map(slug => (
                <div key={slug} className="rounded-lg border border-border bg-card p-3.5">
                  <OAuthAppPanel slugs={[slug]} settingSlug={slug} />
                </div>
              ))}
              <div className="rounded-lg border border-border bg-card p-3.5">
                <OAuthAppPanel slugs={GOOGLE_PANEL_SLUGS} settingSlug={GOOGLE_ENTRY_SLUG} />
              </div>
            </div>
          )}

          {/* No hand-off: a card waiting for approval can hold an unsaved return address while this status re-polls. */}
          {(isError || statusIsError) && (
            <ErrorNotice message={t('pages.connectionsPage.could_not_load_status')} className="mb-3" />
          )}

          {isLoading ? (
            <ContentSkeleton rows={6} />
          ) : (
            <>
            {servicesEnabled && filteredProviders.length === 0 && (
              <div className="rounded-lg border border-dashed border-border px-4 py-10 text-center text-sm text-muted">
                {t('pages.connectionsPage.no_matching_services')}
              </div>
            )}
            <div className="grid grid-cols-1 gap-3 xl:grid-cols-2 2xl:grid-cols-3" hidden={filteredProviders.length === 0}>
              {(() => {
                const cardModel = (provider: ConnectionProvider) => {
                const server = serverForConnection(provider, servers)
                const pending = locallyWaiting[provider.slug]
                // A new Connect supersedes an earlier Cancel. Only the chat
                // banner is fenced: Cancel disposes its own mint, and a mint a
                // sibling tab starts later is live and must keep its URL.
                const dismissedTs = pending ? undefined : dismissedOAuth[provider.slug]
                // After a failed check its attempt's banner fence stays, so a chat
                // banner from before it cannot say Connected or Waiting -- unless
                // it completed only after the failure: that is a new consent.
                const unproven = unprovenGrants[provider.slug]
                const banner = oauthByServer[provider.slug]
                const fenced = !pending && unproven && banner && banner.timestamp <= unproven.sinceTs
                  && !completedAfterFailedCheck(banner, unproven)
                const oauth = withMintedUrl(
                  undismissedOAuth(effectiveOAuth(fenced ? undefined : banner, pending), dismissedTs),
                  mintByServer[provider.slug],
                )
                const status = statusBySlug[provider.slug]
                // Only a CONFIRMED verdict may steer the card: an indeterminate
                // lookup reports grantPresent=false without knowing anything, and
                // grant files this page's Connect just failed to prove are no
                // confirmed grant either.
                const confirmedGrant = confirmedGrantPresent(status)
                const grantPresent = confirmedGrant === true && unproven !== undefined ? undefined : confirmedGrant
                const state = connectionStateFor(
                  server,
                  oauth,
                  !!pending,
                  grantPresent,
                  // The backend's mint table outlives this tab's local state, so
                  // a refresh mid-consent still renders the waiting card.
                  status?.status === 'awaiting_consent',
                  status?.needsClientConfig === true,
                  dismissedTs !== undefined,
                )
                return { server, oauth, status, grantPresent, unproven, state }
                }
                const renderCard = (provider: ConnectionProvider): ReactNode => {
                const { server, oauth, status, grantPresent, unproven, state } = cardModel(provider)
                const cardBusy = busy?.slug === provider.slug ? busy.action : undefined
                // Named only when a DIFFERENT card owns the running test: this
                // card's own in-flight test is already covered by `cardBusy`,
                // and naming a card against itself would read as nonsense
                // ("Vercel is testing" on Vercel's own disabled button).
                const testingProvider = busy?.action === 'test' && busy.slug !== provider.slug
                  ? CONNECTION_PROVIDERS.find(candidate => candidate.slug === busy.slug)?.name
                  : undefined
                return (
                  <ConnectionCard
                    key={provider.slug}
                    hidden={!matching.has(provider.slug)}
                    handOffAllowed={draftSlugs.size === 0}
                    provider={provider}
                    server={server}
                    state={state}
                    oauth={oauth}
                    connectedSince={status?.connectedSince}
                    // The same confirmed-only verdict the state fold received:
                    // indeterminate stays undefined so the card keeps the hedge.
                    grantPresent={grantPresent}
                    grantUnproven={unproven !== undefined}
                    grantSharedWith={status?.grantSharedWith}
                    grantSharedUnconfirmed={status?.grantSharedUnconfirmed}
                    backendLacksOauth={status?.backendLacksOauth === true}
                    signInInChat={status?.signInInChat === true}
                    crewHeld={status?.crewHeld === true}
                    signInRedirect={status?.signInRedirect}
                    busy={cardBusy}
                    testingProvider={testingProvider}
                    feedbackSlots={feedbackSlots}
                    highlighted={highlightedSlug === provider.slug}
                    // Clicking a card opens it in place. A Google product is a
                    // row of the open Google entry, so it has no toggle of its own.
                    expand={isGoogleSlug(provider.slug) ? undefined : {
                      expanded: expanded.has(provider.slug),
                      onToggle: () => toggleExpanded(provider.slug),
                      panelId: oauthPanelId(provider.slug),
                    }}
                    oauthPanel={isPreregisteredProvider(provider) && !isGoogleSlug(provider.slug)
                      ? <OAuthAppPanel slugs={[provider.slug]} settingSlug={provider.slug} />
                      : undefined}
                    onConnect={() => connect(provider)}
                    onCancel={() => cancelConnection(provider, server)}
                    onDisconnect={() => server ? disconnect(provider, server) : Promise.resolve()}
                    onReconnect={() => connect(provider, server)}
                    onTest={() => testConnection(provider)}
                    onRelay={returnAddress => relayReturnAddress(provider, returnAddress, oauth?.minted === true)}
                  />
                )
                }
                return entries.map(provider => {
                  if (!isGoogleSlug(provider.slug)) return renderCard(provider)
                  // A Google consent in flight or a Google product to reconnect
                  // keeps the entry open, as such a card stays open on its own.
                  const googleStates = googleProviders.map(provider => cardModel(provider).state)
                  const open = expanded.has(GOOGLE_ENTRY_SLUG) || googleStates.some(state =>
                    state === 'waiting-for-approval' || state === 'needs-attention' || state === 'not-verified')
                  // One status for the closed entry: the one that needs the user
                  // first, else Connected when any product is, else the shared
                  // configuration state, else Not connected.
                  const googleState: ConnectionCardState =
                    (['needs-attention', 'waiting-for-approval', 'not-verified'] as const).find(state => googleStates.includes(state))
                    ?? (googleStates.includes('connected') ? 'connected'
                      : googleStates.length > 0 && googleStates.every(state => state === 'needs-configuration') ? 'needs-configuration'
                        : 'not-connected')
                  // The step the six products share, said once as ONE
                  // tip beside the entry's own action (connection-card-caveat-tip):
                  // closed, that is Set up / Choose Google products; open, the
                  // name toggle. No product row carries its own triangle for it.
                  const googleStepTip = googleStates.some(state => state === 'needs-configuration' || state === 'not-connected') ? (
                    <PrerequisiteTip
                      label={t('pages.connectionsPage.prerequisites_for_provider', { provider: t('pages.connectionsPage.google_entry_name') })}
                      heading={t('pages.connectionsPage.before_you_connect')}
                      text={googleState === 'needs-configuration'
                        ? t('pages.connectionsPage.google_entry_step_set_up')
                        : t('pages.connectionsPage.google_entry_step_connect')}
                    />
                  ) : null
                  return (
                    // One entry for the six Google products. Open, it holds one
                    // row per product with its own status, Connect and
                    // Disconnect, then the one OAuth app part they share.
                    <motion.section
                      layout={reduceMotion ? false : true}
                      transition={CARD_LAYOUT_TRANSITION}
                      key={GOOGLE_ENTRY_SLUG}
                      id={`connection-${GOOGLE_ENTRY_SLUG}`}
                      hidden={googleMatching.length === 0}
                      aria-labelledby="connection-google-heading"
                      className={`min-w-0 rounded-lg border border-border bg-card p-3.5 shadow-sm ${open ? 'xl:col-span-2 2xl:col-span-3' : ''}`}
                    >
                      <header className="flex items-center gap-2.5">
                        <ProviderLogo slug="gmail" />
                        <h3 id="connection-google-heading" className="m-0 min-w-0 flex-1 truncate text-[14px] font-semibold text-text-strong">
                          <button
                            type="button"
                            onClick={() => toggleExpanded(GOOGLE_ENTRY_SLUG)}
                            aria-expanded={open}
                            aria-controls={oauthPanelId(GOOGLE_ENTRY_SLUG)}
                            className="inline-flex max-w-full items-center gap-1 border-none bg-transparent p-0 text-left font-semibold text-text-strong hover:text-accent"
                          >
                            <span className="min-w-0 truncate">{t('pages.connectionsPage.google_entry_name')}</span>
                            <ChevronDown className={`h-3.5 w-3.5 shrink-0 transition-transform ${open ? 'rotate-180' : ''}`} aria-hidden="true" />
                          </button>
                        </h3>
                        {open && googleStepTip}
                        <ConnectionStateBadge state={googleState} />
                      </header>
                      {/* Closed, the entry names the products it holds. Brand
                          names, not prose: kept out of the i18n scan. */}
                      {!open && (
                        <p className="mb-0 mt-1.5 text-[12.5px] leading-[17px] text-muted" data-i18n-opaque="">
                          {googleMatching.map(p => p.name).join(' · ')}
                        </p>
                      )}
                      {/* The next step, as a sibling card shows it: Configure while
                          no Google client is set, else Connect. Either opens the
                          entry, where the shared form and the product rows are. */}
                      {!open && (googleState === 'needs-configuration' || googleState === 'not-connected') && (
                        <div className="mt-2.5 flex items-center justify-end gap-2">
                          {googleStepTip}
                          <Btn
                            primary
                            onClick={() => toggleExpanded(GOOGLE_ENTRY_SLUG)}
                            aria-expanded={false}
                            aria-controls={oauthPanelId(GOOGLE_ENTRY_SLUG)}
                          >
                            {googleState === 'needs-configuration' && <Settings2 className="w-3.5 h-3.5" aria-hidden="true" />}
                            {googleState === 'needs-configuration'
                              ? t('pages.connectionsPage.set_up_provider', { provider: t('pages.connectionsPage.google_entry_name') })
                              : t('pages.connectionsPage.choose_google_products')}
                          </Btn>
                        </div>
                      )}
                      {/* Hidden, not unmounted, while closed: closing the entry
                          keeps a typed but unsaved Client ID and secret. The OAuth
                          app part always covers all six products, whatever the
                          search shows, so one Save and one Remove reach them all. */}
                      <div id={oauthPanelId(GOOGLE_ENTRY_SLUG)} className="mt-3" hidden={!open}>
                        {/* The products, with their Connect, come first: that is
                            the next step once the shared OAuth app below is set. */}
                        <div className="mb-3 grid grid-cols-1 gap-3 xl:grid-cols-2 2xl:grid-cols-3">
                          {googleProviders.map(renderCard)}
                        </div>
                        <div className="rounded-lg border border-border bg-card p-3.5">
                          <OAuthAppPanel slugs={GOOGLE_PANEL_SLUGS} settingSlug={GOOGLE_ENTRY_SLUG} />
                        </div>
                      </div>
                    </motion.section>
                  )
                })
              })()}
            </div>
            </>
          )}
      </div>
    </section>
    </OAuthDraftValuesContext.Provider>
    </OAuthClientWrittenContext.Provider>
    </OAuthDraftsContext.Provider>
  )
}
