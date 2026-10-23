import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { uiLocation } from '../../uiLocations/uiLocation'
import { GuideRevealScope } from '../../guide/GuideRevealScope'
import { Link2, Server } from 'lucide-react'
import { useConnectionsSubTab } from './useConnectionsSubTab'
import { api, ApiError, type ConnectionMintState, type ConnectionStatus } from '../../api/client'
import { useAppSelector } from '../../store'
import type { McpApplyChange, McpServer } from '../../types'
import { Badge, ContentSkeleton, SearchInput } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import RestartButton from '../../components/RestartButton'
import { findReport } from '../../utils/errorReport'
import McpTab from '../overview/McpTab'
import {
  CONNECTION_PROVIDERS,
  serverForConnection,
  type ConnectionProvider,
} from './registry'
import { guideAnchor } from '../../uiLocations/targetRegistry'
import { ConnectionCard } from './ConnectionsPageCard'
import {
  confirmedGrantPresent, connectionStateFor, disconnectFeedback, effectiveOAuth, latestOAuthByServer, mintOutcome,
  mintRowIsOurs, undismissedOAuth, uninstallOnCancel, withMintedUrl, type ConnectionAction, type Feedback, type PendingConnect,
} from './connectionsPageCardState'

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

/** Authorization-status poll cadence. A grant changes rarely and the read is a
 *  local stat, so this is slow relative to the mint poll — it only has to notice
 *  a grant completed outside the dashboard and keep connected-since fresh. */
const CONNECTION_STATUS_POLL_MS = 30_000

/** The guide section that shows the oauth_endpoints.json entry a refused
 *  approval address needs. Linked from the mint_url_rejected feedback via
 *  `Feedback.help`, so the card's remedy ends in a link rather than a bare path. */
const OAUTH_ENDPOINT_ALLOWLIST_GUIDE_URL =
  'https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/connecting-remote-oauth-mcp-server.md#if-the-host-is-not-recognized-the-oauth-endpoint-allowlist'

/**
 * `servicesEnabled` gates the provider gallery. The gallery ships ON, so the
 * dashboard normally passes `true`; the PARAMETER default stays closed so a
 * caller that forgets to pass it cannot open a gallery by omission. False means
 * the instance pulled the `connections_ui` escape hatch: the Services panel
 * offers no providers, so no card, Connect button or OAuth flow is reachable.
 *
 * A closed panel still RENDERS rather than being removed, which is deliberate.
 * Hiding the sub-tab and defaulting to the MCP Servers table was tried and
 * reverted: it makes that table the default-rendered surface and so exposes its
 * pre-existing i18n debt to the render-time gate, which measured
 * `capabilities-mcp` going 44 -> 102 findings. Emptying the list keeps the
 * measured surface comparable (568 -> 558 overall, gate PASS) while still
 * removing every way to actually connect a provider.
 */
export default function ConnectionsPage({ servicesEnabled = false }: { servicesEnabled?: boolean } = {}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [activeTab, setActiveTab] = useConnectionsSubTab()
  const [search, setSearch] = useState('')
  /** Pending connect attempts. `kind` decides Cancel semantics (only a
   *  cancelled *new* connect uninstalls the entry it just created); `sinceTs`
   *  fences off stale `mcp_oauth` banners left over from an earlier grant of
   *  the same server name (they must not mark a fresh attempt connected). */
  const [locallyWaiting, setLocallyWaiting] = useState<Record<string, PendingConnect>>({})
  /** Newest chat banner timestamp seen when Cancel was pressed, per slug. A
   *  later Connect supersedes it (its own `sinceTs` fence takes over). */
  const [dismissedOAuth, setDismissedOAuth] = useState<Record<string, number>>({})
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

  // Authorization verdict + first-connect time per visible provider. Polled while
  // the gallery is mounted so a grant completed outside the dashboard, and the
  // connected-since clock, surface without a manual refresh. Additive to the mint
  // feed below; this never mints and never owns reachability (that stays with
  // /api/mcp). Declared before the mint feed because `waitingSlugs` reads the
  // awaiting_consent verdicts.
  const { data: statusBySlug = {} } = useQuery<Record<string, ConnectionStatus>>({
    queryKey: ['connections-status'],
    queryFn: async () => {
      const { connections } = await api.connectionsStatus()
      const next: Record<string, ConnectionStatus> = {}
      for (const entry of connections) next[entry.slug] = entry
      return next
    },
    enabled: servicesEnabled,
    // Only while the gallery is the visible surface. On the MCP Servers tab no
    // card is rendered, so a background poll would stat every provider's grant
    // artifacts every 30s for a surface nobody is looking at.
    refetchInterval: activeTab === 'services' ? CONNECTION_STATUS_POLL_MS : false,
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
    const mintFailures: Array<{ slug: string; reason?: string; endpoint?: string }> = []
    const grantedMints: string[] = []
    for (const provider of CONNECTION_PROVIDERS) {
      const pending = locallyWaiting[provider.slug]
      if (!pending) continue
      const server = serverForConnection(provider, servers)
      const fresh = effectiveOAuth(oauthByServer[provider.slug], pending)
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
          && mint.reason
        )
      ) {
        mintFailures.push({ slug: provider.slug, reason: mint.reason, endpoint: mint.rejected_endpoint })
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
      // Same staleness on the authorization axis: the status feed polls every
      // 30s, so its cached pre-consent verdict (grantPresent=false /
      // awaiting_consent) would outrank the grant that just landed and downgrade
      // the card for up to a full poll interval. Invalidate rather than
      // setQueryData: the fresh verdict is the backend's to compute.
      void queryClient.invalidateQueries({ queryKey: ['connections-status'] })
    }
    if (mintFailures.length) {
      setFeedback(current => {
        const next = { ...current }
        for (const { slug, reason, endpoint } of mintFailures) {
          let error: string
          let detail: string | undefined
          let help: Feedback['help']
          switch (reason) {
            case 'mint_timeouterror':
              error = t('pages.connectionsPage.mint_failure_timed_out')
              break
            case 'mint_process_gone':
              error = t('pages.connectionsPage.mint_failure_process_gone')
              break
            case 'mint_server_absent':
              error = t('pages.connectionsPage.mint_failure_server_absent')
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
      await api.mcpCustomAdd({ [provider.slug]: { url: provider.mcp_url } }, true)
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
              names: result.grantSharedWith.join(', '),
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
    setActiveTab(activeTab === 'services' ? 'mcp-servers' : 'services')
  }
  const openProvider = (slug: string) => {
    setSearch('')
    setHighlightedSlug(slug)
    setActiveTab('services')
  }

  return (
    <section className="min-w-0" aria-label={t('pages.connectionsPage.connections')}>
      {/* Apply & Restart lives here because this is where MCP server changes
          are made: a newly enabled server reaches a chat that is already
          running only after its session is relaunched. Outside the tablist so
          the tab roles stay a clean pair. The muted line beside it says what
          restarts and what is kept BEFORE the click — "Restart" alone reads as
          "interrupts whatever is running", and the confirm is too late for the
          reader deciding whether to press at all. */}
      {/* Narrow-first: at 320px the tablist, the hint and the button cannot
          share one row, so they stack (tabs, then the restart controls beneath
          them) and only join into one header band from `sm` up. */}
      <div className="mb-4 flex flex-col gap-2 border-b border-border sm:flex-row sm:items-center sm:justify-between sm:gap-3">
        <div className="flex" role="tablist" aria-label={t('pages.connectionsPage.connection_views')}>
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
            // One spread: the anchor's registering ref rides in as the location's own.
            {...uiLocation('connections.mcp-servers-tab', guideAnchor('mcp.servers-tab').ref)}
            data-guide-anchor="mcp.servers-tab"
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
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5 pb-2 sm:justify-end sm:pb-1.5">
          <span className="min-w-0 max-w-[34rem] text-[11.5px] leading-snug text-muted">{t('components.restartButton.beside_hint')}</span>
          <RestartButton />
        </div>
      </div>

      {/* The MCP servers panel's scope owner: a guide walking to a control
          on that panel points at its tab until this reads open. Outside the
          panel conditional, so it says "closed" while the panel is unmounted. */}
      <GuideRevealScope id="open:connections.mcp-servers-tab" open={activeTab === 'mcp-servers'}>
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

          {/* No hand-off: a card waiting for approval can hold an unsaved return address while this status re-polls. */}
          {isError && (
            <ErrorNotice message={t('pages.connectionsPage.could_not_load_status')} className="mb-3" />
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
                // A new Connect supersedes an earlier Cancel. Only the chat
                // banner is fenced: Cancel disposes its own mint, and a mint a
                // sibling tab starts later is live and must keep its URL.
                const dismissedTs = pending ? undefined : dismissedOAuth[provider.slug]
                const oauth = withMintedUrl(
                  undismissedOAuth(effectiveOAuth(oauthByServer[provider.slug], pending), dismissedTs),
                  mintByServer[provider.slug],
                )
                const status = statusBySlug[provider.slug]
                const state = connectionStateFor(
                  server,
                  oauth,
                  !!pending,
                  // Only a CONFIRMED verdict may steer the card: an indeterminate
                  // lookup reports grantPresent=false without knowing anything.
                  confirmedGrantPresent(status),
                  // The backend's mint table outlives this tab's local state, so
                  // a refresh mid-consent still renders the waiting card.
                  status?.status === 'awaiting_consent',
                  status?.needsClientConfig === true,
                  dismissedTs !== undefined,
                )
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
                    provider={provider}
                    server={server}
                    state={state}
                    oauth={oauth}
                    connectedSince={status?.connectedSince}
                    // The same confirmed-only verdict the state fold received:
                    // indeterminate stays undefined so the card keeps the hedge.
                    grantPresent={confirmedGrantPresent(status)}
                    grantSharedWith={status?.grantSharedWith}
                    grantSharedUnconfirmed={status?.grantSharedUnconfirmed}
                    busy={cardBusy}
                    testingProvider={testingProvider}
                    feedbackSlots={feedbackSlots}
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
      </GuideRevealScope>
    </section>
  )
}
