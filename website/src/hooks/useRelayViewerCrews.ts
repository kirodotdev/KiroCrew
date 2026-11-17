/**
 * The desktop app's list of remote Crews reachable through the Kiro relay.
 *
 * Two gates, both needed: the desktop shell's `relayCrewAPI` preload bridge (a
 * plain browser tab and the PWA have none), and the gateway's crews route. The
 * route is always on; only an older gateway answers 404. Either one missing
 * hides the whole list, so a browser tab's dashboard is unchanged.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { api, ApiError, type RelayCrewEntry, type RelayViewerAccount } from '../api/client'
import { parseErrorCode, parseErrorField } from '../utils/errorReport'
import { i18nT } from '../i18n/t'
import { timeAgo } from '../utils/timeAgo'
import type { AppDispatch } from '../store'
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

/** Open a relay Crew as a tab in this window while the hidden pane key is on,
 *  else in its own window through `openRelayCrew`, as before. */
export function openRelayCrewTab(endpointId: string, name: string, dispatch: AppDispatch): Promise<RelayCrewOpenResult> {
  const bridge = relayPaneBridge()
  return bridge ? openRelayPane(bridge, { id: endpointId, name }, dispatch) : openRelayCrew(endpointId)
}

/** Ask the desktop shell to close every relay Crew window now, after this
 *  dashboard's Sign out or Switch account lands. Does nothing without the bridge
 *  and never throws: a sign-out made elsewhere is caught at the next renewal. */
export async function closeRelayCrews(): Promise<void> {
  const b = (window as { relayCrewAPI?: { closeAll?: () => Promise<unknown> } }).relayCrewAPI
  if (typeof b?.closeAll !== 'function') return
  try {
    await b.closeAll()
  } catch {
    /* the renewal backstop still closes them */
  }
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
    default: return generic ?? i18nT('components.relayCrews.refusal_generic')
  }
}

/** Codes where sending the same request again can work: a CPS server failure, a
 *  throttle, a network failure, a cut answer, a Crew that may come back, a busy
 *  bridge. A refusal about access, sign-in or setup fails the same way again. */
const RETRYABLE = new Set(['SERVICE_ERROR', 'THROTTLED', 'UNREACHABLE', 'BAD_ANSWER', 'CREW_OFFLINE', 'UNAVAILABLE'])

export function relayRetryable(code: string | undefined): boolean {
  return RETRYABLE.has((code || '').toUpperCase())
}

/** The list refusals still shown: about a listed Crew, a throttle, or this
 *  install's own setup. Signed out, remote access off, access denied and every
 *  load failure with generic text hide the list instead, so a person with no
 *  relay access sees no Team Crews and no notice. The gateway log keeps the code
 *  (`relay viewer: crews -> CODE`), and the list is read again after a sign-in
 *  or on its back-off poll. */
const SHOWN_LIST_REFUSALS = new Set(['CREW_ACCESS_NOT_GRANTED', 'CREW_OFFLINE', 'THROTTLED', 'NOT_SET_UP'])
/** The quiet refusals that hide even a list already loaded; any other one keeps it. */
const HIDING_REFUSALS = new Set(['SIGNED_OUT', 'REMOTE_ACCESS_DISABLED', 'ACCESS_DENIED'])

/** A list read the gateway refused. `requestId` is CPS's request ID (not a secret),
 *  present only when CPS sent a usable one. `retryAt` is when Try again may send
 *  again (`Date.now()` ms) after a throttle with a Retry-After, else null. */
export type RelayListRefusal = {
  text: string
  code?: string
  requestId?: string
  retryAt: number | null
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

const isOffError = (err: unknown) => err instanceof ApiError && err.status === 404

/** The typed code of a refused list read, upper-cased; '' for no error or no code. */
const refusalCode = (err: unknown) =>
  (err instanceof ApiError ? parseErrorCode(err.body) || '' : '').toUpperCase()

/** How often the list is re-read: sooner while a listed Crew is not online, so a host
 *  that just connected reads as online without a reload, and once a minute otherwise. */
export const RELAY_CREWS_POLL_OFFLINE_MS = 15_000
export const RELAY_CREWS_POLL_MS = 60_000
/** After a refusal about access (the org has remote access off, or access is denied),
 *  which the next minute will not change, the list is re-read this rarely. */
export const RELAY_CREWS_POLL_REFUSED_MS = 15 * 60_000
const SLOW_REFUSALS = new Set(['REMOTE_ACCESS_DISABLED', 'ACCESS_DENIED'])
/** The app's Kiro sign-in status (`KasLoginGate`, `KiroSignInNotice`). Observed only:
 *  after SIGNED_OUT the list slows to the 15-minute read, and a sign-in landing here
 *  reads it again at once. */
const SIGN_IN_STATUS_KEY = ['kas-login'] as const

/** When to read the list again after a refusal: 15 minutes after SIGNED_OUT (a
 *  sign-in in this app reads it at once; one made elsewhere, such as the terminal,
 *  is picked up by this slow read) or an access refusal, else a minute; and never
 *  sooner than the refusal's Retry-After. */
function refusedPollMs(err: unknown): number | false {
  const code = refusalCode(err)
  if (code === 'SIGNED_OUT') return RELAY_CREWS_POLL_REFUSED_MS
  const base = SLOW_REFUSALS.has(code) ? RELAY_CREWS_POLL_REFUSED_MS : RELAY_CREWS_POLL_MS
  const wait = retryAfterSeconds(err instanceof ApiError ? err.body : undefined)
  return wait === null ? base : Math.max(base, wait * 1000)
}
/** Opening the switcher re-reads the list unless a successful read is this recent. */
const OPEN_FRESH_MS = 5_000

export function useRelayViewerCrews(): {
  hidden: boolean
  crews: RelayCrewEntry[]
  /** The account the loaded list was read for; null on a refusal or when the gateway named none. */
  account: RelayViewerAccount | null
  refusal: RelayListRefusal | null
  /** Read the list again now (the notice's Try again). */
  retry: () => void
  /** A read is in flight, so Try again waits for it. */
  retrying: boolean
  /** Read a loaded list again unless the last read is under OPEN_FRESH_MS old (the menu opened). */
  refreshOnOpen: () => void
} {
  const hasBridge = relayCrewBridge() !== null
  const queryClient = useQueryClient()
  const q = useQuery({
    queryKey: RELAY_VIEWER_CREWS_KEY,
    queryFn: () => api.relayViewerCrews(),
    enabled: hasBridge,
    // A 404 is the feature-off answer, not a blip to retry.
    retry: false,
    // Presence changes on the host's side, so re-read; stop once the route says the feature is off.
    refetchInterval: query => {
      if (isOffError(query.state.error)) return false
      if (query.state.error) return refusedPollMs(query.state.error)
      return query.state.data?.crews.some(c => !isRelayCrewOnline(c)) ? RELAY_CREWS_POLL_OFFLINE_MS : RELAY_CREWS_POLL_MS
    },
  })
  // Stopped on SIGNED_OUT: read again once the sign-in status turns signed in. Only a
  // real transition counts (a status answer that was not signed in, then one that is,
  // newer than the refusal): a sign-in whose refresh was refused still reads
  // `authenticated: true`, and each unchanged poll of it must not re-read the list.
  const signIn = useQuery({ queryKey: SIGN_IN_STATUS_KEY, queryFn: api.kasLoginStatus, enabled: false })
  const signedOutAt = hasBridge && refusalCode(q.error) === 'SIGNED_OUT' ? q.errorUpdatedAt : 0
  const hasStatus = signIn.data !== undefined
  const authed = signIn.data?.authenticated === true
  const statusAt = signIn.dataUpdatedAt
  const lastAuthed = useRef<boolean | null>(null)
  const refetchList = q.refetch
  useEffect(() => {
    if (!hasStatus) return
    const was = lastAuthed.current
    lastAuthed.current = authed
    if (signedOutAt && authed && was === false && statusAt > signedOutAt) void refetchList()
  }, [signedOutAt, hasStatus, authed, statusAt, refetchList])
  // Stable, so the menu can call it once when it opens. Only a loaded list is read
  // again: with none, a refetch puts a refusal back to pending, which is Try again's job.
  const refreshOnOpen = useCallback(() => {
    const s = queryClient.getQueryState(RELAY_VIEWER_CREWS_KEY)
    if (!s?.data || s.fetchStatus === 'fetching' || Date.now() - s.dataUpdatedAt < OPEN_FRESH_MS) return
    void queryClient.refetchQueries({ queryKey: RELAY_VIEWER_CREWS_KEY, exact: true })
  }, [queryClient])
  // The refusal a Try again was clicked from. With no list yet, react-query puts
  // a refetched error back to pending, which would hide the notice mid-read.
  // `at` is the error's time when Try again was clicked. A newer error moves
  // q.errorUpdatedAt on and retires it; a loaded list ends the pending state.
  const [held, setHeld] = useState<{ refusal: RelayListRefusal; at: number } | null>(null)
  const off = isOffError(q.error)
  let refusal: RelayListRefusal | null = null
  if (q.error) {
    const body = q.error instanceof ApiError ? q.error.body : undefined
    const code = parseErrorCode(body)
    const wait = retryAfterSeconds(body)
    refusal = {
      // A load failure must not read as an Open failure, so the list has its own fallback.
      text: relayRefusalText(code, i18nT('components.relayCrews.refusal_list_generic')),
      code,
      requestId: parseErrorField(body, 'requestId'),
      retryAt: wait === null ? null : q.errorUpdatedAt + wait * 1000,
    }
  }
  const retry = () => {
    setHeld(refusal ? { refusal, at: q.errorUpdatedAt } : null)
    void q.refetch()
  }
  if (hasBridge && !off && q.isPending && q.isFetching && held && held.at === q.errorUpdatedAt) {
    // Try again is reading: keep its refusal up, with Try again off, until the answer.
    return { hidden: false, crews: [], account: null, refusal: held.refusal, retry, retrying: true, refreshOnOpen }
  }
  const code = (refusal?.code || '').toUpperCase()
  const quiet = refusal !== null && !SHOWN_LIST_REFUSALS.has(code)
  if (quiet && !off && q.data && !HIDING_REFUSALS.has(code)) {
    // A load blip after a good read keeps the last good list until the next one.
    return { hidden: false, crews: q.data.crews, account: q.data.account ?? null, refusal: null, retry, retrying: q.isFetching, refreshOnOpen }
  }
  if (!hasBridge || off || quiet || q.isPending) return { hidden: true, crews: [], account: null, refusal: null, retry, retrying: false, refreshOnOpen }
  if (refusal) return { hidden: false, crews: [], account: null, refusal, retry, retrying: q.isFetching, refreshOnOpen }
  return { hidden: false, crews: q.data?.crews ?? [], account: q.data?.account ?? null, refusal: null, retry, retrying: q.isFetching, refreshOnOpen }
}
