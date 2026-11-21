/**
 * The list of remote Crews reachable through the Kiro relay, for every owner's
 * dashboard, with no setting.
 *
 * Shown once a read answers. A route that 404s (a build that keeps the viewer
 * off) and a refusal (signed out, remote access off, not granted) hide it
 * quietly. A network or server failure, or a throttle, shows the list it had
 * (none on a first read) and says the read failed (`failed`). The desktop app opens a Crew as a tab in its main window
 * through the `relayCrewAPI` bridge; a browser opens it in a new tab, which the
 * gateway serves on an origin of its own (`relay_viewer/browser_origin.py`).
 */
import { useCallback } from 'react'
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { api, ApiError, type RelayCrewEntry, type RelayViewerAccount } from '../api/client'
import { parseErrorCode } from '../utils/errorReport'
import { i18nT } from '../i18n/t'
import { compareText } from '../i18n/format'
import { timeAgo } from '../utils/timeAgo'
import type { AppDispatch, RootState } from '../store'
import { relayOpenStarted } from '../store/instancesSlice'
import { openRelayPane, relayPaneBridge } from '../lib/relayCrewPanes'

export type RelayCrewOpenResult = { ok: boolean; error?: string }
type RelayCrewBridge = { open: (endpointId: string) => Promise<RelayCrewOpenResult> }

/** Read lazily, not at module load, so a test can stub `window.relayCrewAPI` per case. */
function relayCrewBridge(): RelayCrewBridge | null {
  const b = (window as { relayCrewAPI?: Partial<RelayCrewBridge> }).relayCrewAPI
  return typeof b?.open === 'function' ? (b as RelayCrewBridge) : null
}

/** Hand ONLY the endpoint ID to the desktop shell. Main mints the access token
 *  and loads the Crew in its own window; nothing else crosses the bridge. */
export async function openRelayCrew(endpointId: string): Promise<RelayCrewOpenResult> {
  const bridge = relayCrewBridge()
  if (!bridge) return { ok: false, error: 'UNAVAILABLE' }
  try {
    return await bridge.open(endpointId)
  } catch {
    return { ok: false, error: 'UNAVAILABLE' }
  }
}

/** Open a relay Crew as a tab in the main window; a shell without the pane calls
 *  opens it in its own window through `openRelayCrew`, and a browser in a new tab. */
export function openRelayCrewTab(endpointId: string, name: string, dispatch: AppDispatch): Promise<RelayCrewOpenResult> {
  const bridge = relayPaneBridge()
  if (bridge) return openRelayPane(bridge, { id: endpointId, name }, dispatch)
  return relayCrewBridge() ? openRelayCrew(endpointId) : openRelayCrewInBrowser(endpointId, dispatch)
}

/** endpointId -> its open still waiting for the gateway: a second click joins it
 *  rather than minting again, which would end the first open's access. */
const browserOpening = new Map<string, Promise<RelayCrewOpenResult>>()

/** Open a relay Crew in a new browser tab. The tab is opened inside the click (a
 *  later `window.open` is a blocked pop-up) on a blank page, cut from this page,
 *  and sent to the Crew only once the gateway answered and no newer choice was made
 *  meanwhile; otherwise it closes. Never throws. */
export function openRelayCrewInBrowser(endpointId: string, dispatch: AppDispatch): Promise<RelayCrewOpenResult> {
  const pending = browserOpening.get(endpointId)
  if (pending) return pending
  dispatch(relayOpenStarted())
  const seq = () => dispatch((_d, getState: () => RootState) => getState().instances.pickSeq ?? 0)
  const gen = seq()
  const tab = window.open('about:blank', '_blank')
  if (!tab) return Promise.resolve({ ok: false, error: 'POPUP_BLOCKED' })
  try {
    tab.opener = null
  } catch { /* already cut */ }
  const opening = api.relayViewerBrowserOpen(endpointId).then(
    ({ url }) => {
      if (seq() !== gen) {
        tab.close()
        return { ok: true }
      }
      tab.location.replace(url)
      return { ok: true }
    },
    (err: unknown) => {
      tab.close()
      return { ok: false, error: parseErrorCode(err instanceof ApiError ? err.body : undefined) || 'UNAVAILABLE' }
    },
  ).finally(() => browserOpening.delete(endpointId))
  browserOpening.set(endpointId, opening)
  return opening
}

/** Ask the desktop shell to close every relay Crew window now, after this
 *  dashboard's Sign out or Switch account lands. Does nothing without the bridge
 *  and never throws: a sign-out made elsewhere is caught at the next renewal. A
 *  browser tab is not closed from here (this page cut its `opener`, which also
 *  ends this page's hold on it): the gateway tells the tab itself. */
export async function closeRelayCrews(): Promise<void> {
  const b = (window as { relayCrewAPI?: { closeAll?: () => Promise<unknown> } }).relayCrewAPI
  if (typeof b?.closeAll !== 'function') return
  try {
    await b.closeAll()
  } catch {
    /* the renewal backstop still closes them */
  }
}

/** A refusal that says the person is signed out: an ordinary state with a way
 *  forward (sign in), so it is told as a status, not as an error. */
export function isSignedOutRefusal(code: string | undefined): boolean {
  return (code || '').toUpperCase() === 'SIGNED_OUT'
}

/** Plain text for a typed refusal code (the control plane client's codes, design
 *  section 5). Matched case-insensitively; every other code, the bridge's own
 *  INVALID_ENDPOINT and UNAVAILABLE included, gets the generic text. */
export function relayRefusalText(code: string | undefined, generic?: string): string {
  switch ((code || '').toUpperCase()) {
    case 'CREW_ACCESS_NOT_GRANTED': return i18nT('components.relayCrews.refusal_not_granted')
    case 'REMOTE_ACCESS_DISABLED': return i18nT('components.relayCrews.refusal_remote_access_off')
    case 'CREW_OFFLINE': return i18nT('components.relayCrews.refusal_offline')
    case 'THROTTLED': return i18nT('components.relayCrews.refusal_throttled')
    case 'SIGNED_OUT': return i18nT('components.relayCrews.refusal_signed_out')
    case 'NOT_SET_UP': return i18nT('components.relayCrews.refusal_not_configured')
    case 'BROWSER_NOT_LOCAL': return i18nT('components.relayCrews.refusal_browser_not_local')
    case 'POPUP_BLOCKED': return i18nT('components.relayCrews.refusal_popup_blocked')
    default: return generic ?? i18nT('components.relayCrews.refusal_generic')
  }
}

/** The throttle's `retryAfter` seconds from a gateway error body, when it is a positive number. */
function retryAfterSeconds(body: string | undefined): number | null {
  try {
    const value = (JSON.parse(body || '') as { retryAfter?: unknown } | null)?.retryAfter
    return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null
  } catch {
    return null
  }
}

/** A Crew can be opened only while the relay reports its tunnel connected. Presence
 *  follows lease renewals alone, so a Crew whose tunnel is down can still read as present. */
export function isRelayCrewOnline(entry: RelayCrewEntry): boolean {
  return (entry.relayConnectionStatus || '').toUpperCase() === 'CONNECTED'
}

/** Presence that means the host still renews its lease, so its tunnel is probably coming back. */
const RECONNECTING_PRESENCE = new Set(['CONNECTED', 'HEARTBEAT_LATE'])

/** How long after its last lease renewal a Crew with no tunnel still reads as
 *  reconnecting. A host renews about once a minute, so two missed renewals mean it
 *  stopped (it was shut down, or its owner turned Remote Control off) even while
 *  presence has not caught up. */
export const RELAY_CREW_RECONNECTING_FOR_MS = 2 * 60_000

/** A row's status words. A row that can't be opened takes its words from presence, and
 *  adds when the Crew was last seen when the control plane gave a time (as Kiro Mobile does). */
export function relayCrewStatusText(entry: RelayCrewEntry, now: number = Date.now()): string {
  if (isRelayCrewOnline(entry)) return i18nT('components.relayCrews.online')
  const stale = !!entry.lastSeenAt && now - entry.lastSeenAt * 1000 > RELAY_CREW_RECONNECTING_FOR_MS
  const words = RECONNECTING_PRESENCE.has((entry.presence || '').toUpperCase()) && !stale
    ? i18nT('components.relayCrews.reconnecting')
    : i18nT('components.relayCrews.offline')
  if (!entry.lastSeenAt) return words
  return `${words} · ${i18nT('components.relayCrews.last_seen', { when: timeAgo(entry.lastSeenAt) })}`
}

export const RELAY_VIEWER_CREWS_KEY = ['relay-viewer', 'crews'] as const

/** The Crews in one order whatever order the service answers in, which changes
 *  from one call to the next: by name, then by endpoint ID, so a Crew stays
 *  where the person last saw it and a click lands on the row they aimed at. */
export function sortRelayCrews(crews: readonly RelayCrewEntry[]): RelayCrewEntry[] {
  return [...crews].sort((a, b) => compareText(a.name, b.name)
    || (a.endpointId < b.endpointId ? -1 : a.endpointId > b.endpointId ? 1 : 0))
}

/** The most IDs one seen request carries; the route's own cap (`_MAX_SEEN_IDS`). */
const SEEN_BATCH = 200

/** Tell the gateway the person saw these Crews: the IDs it confirmed it saved, and
 *  whether any batch failed. A failed batch keeps its dots, which is true, and the
 *  next open sends it again; the caller says it wasn't saved. */
export async function recordRelayCrewsSeen(endpointIds: string[]): Promise<{ saved: string[]; failed: boolean }> {
  const saved: string[] = []
  let failed = false
  for (let i = 0; i < endpointIds.length; i += SEEN_BATCH) {
    const batch = endpointIds.slice(i, i + SEEN_BATCH)
    try {
      await api.relayViewerCrewsSeen(batch)
      saved.push(...batch)
    } catch {
      failed = true
    }
  }
  return { saved, failed }
}

/** Clear the dot of these Crews in the cached list now, without waiting for the
 *  next read. A read already in flight is cancelled first: its answer predates the
 *  save, so it would bring the dots back. */
export async function clearRelayCrewsUnseen(queryClient: QueryClient, endpointIds: string[]): Promise<void> {
  if (endpointIds.length === 0) return
  await queryClient.cancelQueries({ queryKey: RELAY_VIEWER_CREWS_KEY })
  const ids = new Set(endpointIds)
  queryClient.setQueryData<{ crews: RelayCrewEntry[] }>(RELAY_VIEWER_CREWS_KEY, old =>
    old ? { ...old, crews: old.crews.map(c => (ids.has(c.endpointId) ? { ...c, unseen: false } : c)) } : old)
}

/** The answers that mean this page cannot use the viewer at all, so reading stops:
 *  the viewer off (404), and a page reached through the relay or by someone who is
 *  not the owner (403 with those codes). */
const HIDING_403 = new Set(['relay_local_credential', 'owner_only'])
const isOffError = (err: unknown) =>
  err instanceof ApiError && (err.status === 404 || (err.status === 403 && HIDING_403.has(parseErrorCode(err.body) || '')))

/** A refusal that a read soon would answer the same way: signed out, remote access
 *  off, not granted (a 4xx other than a throttle). It hides even a loaded list, which
 *  was read for a sign-in or access that no longer holds. A network or server failure,
 *  a throttle, and `NOT_SET_UP` (a dev viewer setting that is wrong) are not one: they
 *  show the failure notice. */
const isRefusal = (err: unknown) =>
  err instanceof ApiError && err.status >= 400 && err.status < 500 && err.status !== 429
  && parseErrorCode(err.body) !== 'NOT_SET_UP'

/** How often the list is re-read: sooner while a listed Crew is not online, so a host
 *  that just connected reads as online without a reload, and once a minute otherwise.
 *  After a refusal, every 15 minutes. A longer Retry-After wins over each. */
export const RELAY_CREWS_POLL_OFFLINE_MS = 15_000
export const RELAY_CREWS_POLL_MS = 60_000
export const RELAY_CREWS_POLL_REFUSED_MS = 15 * 60_000

/** The next read's delay for a query in *state*, or false to stop reading. */
function relayCrewsPollMs(state: { error: unknown; data?: { crews: RelayCrewEntry[] } }): number | false {
  const { error, data } = state
  if (isOffError(error)) return false
  let ms = isRefusal(error)
    ? RELAY_CREWS_POLL_REFUSED_MS
    : data?.crews.some(c => !isRelayCrewOnline(c)) ? RELAY_CREWS_POLL_OFFLINE_MS : RELAY_CREWS_POLL_MS
  const wait = error instanceof ApiError ? retryAfterSeconds(error.body) : null
  if (wait !== null) ms = Math.max(ms, wait * 1000)
  return ms
}
/** Opening the switcher re-reads the list unless a successful read is this recent. */
const OPEN_FRESH_MS = 5_000

export function useRelayViewerCrews(): {
  hidden: boolean
  crews: RelayCrewEntry[]
  /** The account the loaded list was read for; null when the gateway named none. */
  account: RelayViewerAccount | null
  /** The latest read failed (a network or server failure, or a throttle); the list shown is the last one loaded, if any. */
  failed: boolean
  /** Read a loaded list again unless the last read is under OPEN_FRESH_MS old (the menu opened). */
  refreshOnOpen: () => void
} {
  const queryClient = useQueryClient()
  const q = useQuery({
    queryKey: RELAY_VIEWER_CREWS_KEY,
    // Every dashboard reads this now, so an answer without a list (an older or
    // stand-in gateway) is read as an empty one rather than breaking the shell.
    queryFn: async () => {
      const answer = await api.relayViewerCrews()
      return { ...answer, crews: Array.isArray(answer?.crews) ? sortRelayCrews(answer.crews) : [] }
    },
    // The poll below is the retry, at its own pace.
    retry: false,
    refetchInterval: query => relayCrewsPollMs(query.state),
  })
  // Stable, so the menu can call it once when it opens. Only a loaded list is read again.
  const refreshOnOpen = useCallback(() => {
    const s = queryClient.getQueryState(RELAY_VIEWER_CREWS_KEY)
    if (!s?.data || s.fetchStatus === 'fetching' || Date.now() - s.dataUpdatedAt < OPEN_FRESH_MS) return
    void queryClient.refetchQueries({ queryKey: RELAY_VIEWER_CREWS_KEY, exact: true })
  }, [queryClient])
  // A failed read keeps the list it had, unless it was a refusal or the route is off.
  if (isOffError(q.error) || isRefusal(q.error) || (!q.data && !q.error)) {
    return { hidden: true, crews: [], account: null, failed: false, refreshOnOpen }
  }
  return { hidden: false, crews: q.data?.crews ?? [], account: q.data?.account ?? null, failed: !!q.error, refreshOnOpen }
}
