/**
 * Milestones (RFC "Milestones for New-User Feature Discovery", Ship 1): the one
 * read of `GET /api/milestones`, the panel's open state, the marking actions,
 * and the starter-task runner the empty-state cards and the panel share.
 */
import { useCallback, useEffect, useSyncExternalStore } from 'react'
import { keepPreviousData, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { api } from '../../api/client'
import type { MilestoneEventAction, MilestoneFeatureItem, MilestoneSurface, MilestoneUsageItem, Milestones } from '../../api/client/featureDiscovery'
import { useAppDispatch, useAppSelector, store } from '../../store'
import { setPendingInput } from '../../store/chatSlice'
import { createChatForView, showCreatedChat } from '../../lib/createdChatView'
import { useAskingNavigate, useIsCurrentUrl, useMayLeaveForNavigation, useNavigationStakeNow } from '../NavigationLeaveGuard'
import { crewModeAgent, crewModeEnabled, gatewayRunsCrewMode } from '../../lib/crewMode'
import { ApiError } from '../../api/apiError'
import { appPathname } from '../../lib/basePath'
import { i18nT } from '../../i18n/t'
import { BADGE_TIERS, DIRECT_SEND_ROWS, FEATURE_COPY, GETTING_STARTED_IDS, type BadgeKind } from './copy'

export const MILESTONES_QUERY_KEY = ['milestones'] as const

/** Set once this gateway answered 404 for the route: no further reads this page load. */
let routeMissing = false

/** Test seam: forget a 404 seen by an earlier test. */
export function __resetMilestonesRouteMissing(): void { routeMissing = false }

/** The active slot's key, read at call time, for the server's restricted-session guard. */
function currentSessionKey(): string | undefined {
  const slot = store.getState().chat.activeSlot
  return slot ? `dashboard:${slot}` : undefined
}

/**
 * The milestones read. Keyed by the active session, because a temporary or
 * incognito session gets `{ enabled: false }` from the server. No retry: a 404
 * from a gateway without milestones is an answer, and every surface then shows
 * nothing.
 */
export function useMilestones() {
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const sessionKey = activeSlot ? `dashboard:${activeSlot}` : undefined
  return useQuery<Milestones>({
    queryKey: [...MILESTONES_QUERY_KEY, sessionKey ?? ''],
    // `enabled` is read at render, so a read already queued by the time the
    // first 404 lands could still go out; the function itself refuses too.
    queryFn: () => (routeMissing ? Promise.reject(new ApiError(404, 'milestones route missing')) : api.milestones(sessionKey)).catch((e: unknown) => {
      if (e instanceof ApiError && e.status === 404) routeMissing = true
      throw e
    }),
    // A gateway without the route answers 404 once; reading it again at every
    // turn end and page mount would only add the same 404 to the error journal.
    // A reload (which an upgraded gateway brings anyway) reads again.
    enabled: !routeMissing,
    staleTime: 60_000,
    retry: false,
    refetchOnWindowFocus: false,
    // Reads happen when asked (`refreshMilestones`: Explore opening, the
    // new-chat page, a turn ending), never because another reader mounted.
    // React Query re-fetches a failed read with no data on EVERY mount
    // (`retryOnMount`), and readers here mount and unmount as the shell
    // re-renders, so a failing gateway would otherwise get a request storm.
    refetchOnMount: false,
    retryOnMount: false,
    // A chat not read yet starts on a new key with no cache: keep showing the
    // last answer until its own lands, so the ring and an open Explore do not
    // blink out and back on every first visit to a chat.
    placeholderData: keepPreviousData,
  })
}

/** How many untried features "Try today" offers at once. */
export const TRY_TODAY_SIZE = 3

/** Whole local days since the epoch: the rotation key, stable for a calendar day. */
function localDayNumber(now: Date): number {
  return Math.floor(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()) / 86_400_000)
}

/**
 * "Try today": up to three untried features, picked from the untried list by
 * rotating on the local date, so the pick is the same all day and moves on the
 * next. A row that is done or skipped drops out at once. Computed here from the
 * read the server already sends; the server's own `recommended` is not used.
 */
export function tryToday(data: Milestones | undefined, now: Date = new Date()): MilestoneFeatureItem[] {
  if (!data?.enabled) return []
  // While "Getting started" shows, its two rows live there, not here.
  const pair = new Set(gettingStarted(data).length > 0 ? GETTING_STARTED_IDS : [])
  // The rotation runs over a STABLE ring: every row this device shows, done
  // or not, so finishing one row does not move the day's start and the other
  // picks stay put; only the finished row drops out and the next open row
  // after the last pick takes its place.
  const ring = featureItems(data).filter(item => item.state !== 'unavailable')
  if (ring.length === 0) return []
  const start = localDayNumber(now) % ring.length
  const picks: MilestoneFeatureItem[] = []
  for (let k = 0; k < ring.length && picks.length < TRY_TODAY_SIZE; k++) {
    const item = ring[(start + k) % ring.length]
    if (item.state === 'open' && !!item.starter && !pair.has(item.id)) picks.push(item)
  }
  return picks
}

/**
 * "Getting started": a first finished result and a morning brief, shown first
 * in Your progress during a user's first days. "First days" is read from the
 * state the server already sends: the pair shows while either row is still
 * untried, and goes away once both are done, skipped or unavailable.
 */
export function gettingStarted(data: Milestones | undefined): MilestoneFeatureItem[] {
  if (!data?.enabled) return []
  const rows = featureItems(data).filter(item => GETTING_STARTED_IDS.includes(item.id))
  if (!rows.some(item => item.state === 'open' && !!item.starter)) return []
  return GETTING_STARTED_IDS.map(id => rows.find(item => item.id === id)).filter((item): item is MilestoneFeatureItem => !!item && item.state !== 'unavailable')
}

/** Everything still left to try: the Getting started pair, then Try today.
 *  Drives the top-bar dot and the new-chat page's entry card. */
export function untriedNow(data: Milestones | undefined, now: Date = new Date()): MilestoneFeatureItem[] {
  return [...gettingStarted(data).filter(item => item.state === 'open'), ...tryToday(data, now)]
}

/** Is this page a frame whose router cannot reach the dashboard's routes
 *  (an embed, or a popped-out chat window)? `appPathname` strips the base path. */
export function inFrame(): boolean {
  return /^\/(embed|popout)\//.test(appPathname())
}

/**
 * "Try today" for the new-chat page's entry card. Reads again each time the
 * page mounts. Empty until the read answers, when the switch is off, and in an
 * embed or popout frame, where opening Explore cannot work.
 */
export function useTryTodayCount(): number {
  const queryClient = useQueryClient()
  useEffect(() => { refreshMilestonesOnMount(queryClient) }, [queryClient])
  const { data } = useMilestones()
  if (inFrame()) return 0
  // The number the card shows is the rows Try today lists, so the two agree;
  // only when Getting started holds the last untried rows does it count those.
  return tryToday(data).length || untriedNow(data).length
}

/** Ask every mounted milestones read to read again (Explore opening, a turn ending). */
export function refreshMilestones(queryClient: QueryClient): void {
  void queryClient.invalidateQueries({ queryKey: MILESTONES_QUERY_KEY })
}

/** How recent a read (success or failure) must be for a page mount to skip reading again. */
const MOUNT_REFRESH_MIN_MS = 10_000

/**
 * The page-mount read: read again only when the last answer, good or bad, is
 * older than a few seconds. A page that remounts (a failed read is journaled,
 * and the journal re-renders the shell) must not turn into a request loop.
 */
export function refreshMilestonesOnMount(queryClient: QueryClient, now: number = Date.now()): void {
  const last = Math.max(0, ...queryClient.getQueryCache().findAll({ queryKey: MILESTONES_QUERY_KEY })
    .map(q => Math.max(q.state.dataUpdatedAt, q.state.errorUpdatedAt)))
  if (now - last < MOUNT_REFRESH_MIN_MS) return
  refreshMilestones(queryClient)
}

/**
 * Rows this device may show. The Crew Mode row is left out while the Crew Mode
 * preview is off: that preview shows no Crew Mode surface at all, and the
 * server cannot see a per-device flag.
 */
export function rowShown(id: string): boolean {
  return !!FEATURE_COPY[id] && (id !== 'crew-mode' || crewModeEnabled())
}

export function featureItems(data: Milestones | undefined): MilestoneFeatureItem[] {
  const group = data?.groups?.find(g => g.id === 'features')
  return ((group?.items ?? []) as MilestoneFeatureItem[]).filter(item => rowShown(item.id))
}

/**
 * The ring's numbers: the server's, minus a counted row this device hides
 * (see `rowShown`), so the ring and the panel agree.
 */
export function featureProgress(data: Milestones | undefined): { done: number; total: number } {
  let { done, total } = data?.features ?? { done: 0, total: 0 }
  const group = data?.groups?.find(g => g.id === 'features')
  for (const item of (group?.items ?? []) as MilestoneFeatureItem[]) {
    if (rowShown(item.id) || !FEATURE_COPY[item.id] || item.state === 'unavailable') continue
    total -= 1
    if (item.state === 'done') done -= 1
  }
  return { done: Math.max(0, done), total: Math.max(0, total) }
}

/**
 * The read failed for a reason other than "this gateway has no such route"
 * (a 404 is the expected answer from a gateway without milestones, and means
 * show nothing). A real failure is shown, so it is not mistaken for "off".
 */
export function milestonesLoadFailed(isError: boolean, error: unknown): boolean {
  return isError && !(error instanceof ApiError && error.status === 404)
}

export function usageItem(data: Milestones | undefined, group: 'sessions' | 'streak'): MilestoneUsageItem | null {
  const g = data?.groups?.find(x => x.id === group)
  const item = (g?.items?.[0] ?? null) as MilestoneUsageItem | null
  return item && Array.isArray(item.tiers) && item.tiers.length > 0 ? item : null
}

/** One earned badge on the badge wall. */
export interface EarnedBadge { rowId: string; tier: number; kind: BadgeKind; at: number }

/**
 * The badge wall: every tier the server says was reached, for the rows that
 * carry badges, in the order they were earned. A tier with no badge style in
 * Ship 1 (`BADGE_TIERS`) shows no badge.
 */
export function earnedBadges(data: Milestones | undefined): EarnedBadge[] {
  const out: EarnedBadge[] = []
  for (const item of [usageItem(data, 'sessions'), usageItem(data, 'streak')]) {
    const kinds = item ? BADGE_TIERS[item.id] : undefined
    if (!item || !kinds) continue
    for (const tier of item.tiers) {
      const at = item.tiers_reached_at?.[String(tier)]
      const kind = kinds[tier]
      if (at && kind) out.push({ rowId: item.id, tier, kind, at })
    }
  }
  return out.sort((a, b) => a.at - b.at)
}

/** Post one metric event. Best-effort: a failure is already journaled by the client. */
export function postMilestoneEvent(id: string, action: MilestoneEventAction, surface?: MilestoneSurface): void {
  void api.milestoneEvent(id, action, currentSessionKey(), surface).catch(() => {})
}

// ── Panel open state ────────────────────────────────────────────────────────
// Module state, so the top-bar indicator, the phone menu row and the notice
// can all open the one panel the shell renders.

interface PanelState { open: boolean; focusId: string | null }
let panelState: PanelState = { open: false, focusId: null }
const listeners = new Set<() => void>()
function setPanel(next: PanelState) {
  panelState = next
  listeners.forEach(l => l())
}
export function openMilestonesPanel(focusId: string | null = null): void {
  if (!panelState.open) postMilestoneEvent('', 'panel_opened')
  setPanel({ open: true, focusId })
}
export function closeMilestonesPanel(): void {
  if (panelState.open) setPanel({ open: false, focusId: null })
}
export function toggleMilestonesPanel(): void {
  if (panelState.open) closeMilestonesPanel()
  else openMilestonesPanel()
}
export function useMilestonesPanel(): PanelState {
  return useSyncExternalStore(
    cb => { listeners.add(cb); return () => { listeners.delete(cb) } },
    () => panelState,
  )
}

// ── Actions ─────────────────────────────────────────────────────────────────

/** The marking write. It reads again once it settles. */
export function useMilestoneActions() {
  const queryClient = useQueryClient()
  const after = useCallback(<T,>(p: Promise<T>) => p.finally(() => refreshMilestones(queryClient)), [queryClient])
  return {
    markDone: useCallback((id: string) => after(api.milestoneMarkDone(id, currentSessionKey())), [after]),
  }
}

/** Is this route one the app can open itself (a path, never a scheme or another host)? */
function inAppRoute(route: string | undefined): route is string {
  return !!route && route.startsWith('/') && !route.startsWith('//') && !route.includes('\\')
}

/**
 * Run a row's starter task: open a page, or open a new chat and put the
 * starter prompt in its composer. Only the two low-cost rows also send it
 * (`DIRECT_SEND_ROWS`); every other prompt waits for the user to press send.
 * Clicking completes nothing by itself: the row is done when the feature
 * actually happens. Resolves once the chat exists; rejects when it could not
 * be created.
 */
export function useRunStarter() {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const mayLeave = useMayLeaveForNavigation()
  const stakeNow = useNavigationStakeNow()
  const isCurrentUrl = useIsCurrentUrl()
  const askingNavigate = useAskingNavigate()
  return useCallback(async (item: MilestoneFeatureItem): Promise<void> => {
    const starter = item.starter
    const copy = FEATURE_COPY[item.id]
    if (!starter || !copy) return
    const send = starter.send === 'send' && DIRECT_SEND_ROWS.has(item.id)
    const chatUrl = send ? '/chat?autoSend=1' : '/chat'
    // Ask the page on screen first, so a refusal ("keep my draft") runs
    // nothing: no chat is created, switched or seeded.
    // A route already on screen unmounts nothing; a prompt always switches the
    // chat, so it always asks.
    const sameRoute = starter.kind === 'route' && !!starter.route && isCurrentUrl(starter.route)
    // Read before the ask (`useNavigationStakeNow`): work at stake plus a yes
    // means the user already agreed to discard it, so the navigate after the
    // create carries `leaveGranted` instead of asking the same question twice.
    const agreedToLeave = !sameRoute && stakeNow()
    if (!sameRoute && !mayLeave()) return
    // Explore is the only place a starter runs; the new-chat card only opens it.
    postMilestoneEvent(item.id, 'starter_clicked', 'panel')
    if (starter.kind === 'route') {
      if (inAppRoute(starter.route)) navigate(starter.route)
      return
    }
    const prompt = i18nT(copy.starterPrompt)
    // Crew Mode is a chat on the conductor template, the same switch the
    // composer's Crew Mode chip makes; a gateway without Crew Mode gets a plain chat.
    const agent = starter.crew_mode && gatewayRunsCrewMode()
      ? { agent: crewModeAgent(), agent_kind: 'template' as const }
      : starter.agent ? { agent: starter.agent, agent_kind: 'member' as const } : {}
    // The foreground create path (`createChatForView`), as every "new chat
    // for this" action uses: the create itself opens the chat and records
    // `lastCreatedActivation`, so Split View steps aside for it, and a chat
    // the user opened meanwhile wins (`shown` is false). A fill-in prompt is
    // staged as the keyed prefill before the chat opens, so it lands once, on
    // the commit that opens it, and never over text typed while it loads.
    const view = await createChatForView(dispatch, store.getState, { defaultFolder: true, ...agent, ...(send ? {} : { prefill: prompt }) })
    if (!view.shown) return
    // A yes to the first ask covers this navigate; with nothing at stake then,
    // the live guard is asked now (a draft can start during the create).
    const go = (route: string): boolean => {
      if (!agreedToLeave) return askingNavigate(route)
      navigate(route, { state: { leaveGranted: true } })
      return true
    }
    if (!send) { showCreatedChat(view, go); return }
    // A direct send goes through the composer's `?autoSend=1` intake, which
    // sends the staged input at once, so nothing the user types can be lost.
    if (!go(chatUrl)) return
    dispatch(setPendingInput(prompt))
  }, [dispatch, navigate, mayLeave, stakeNow, isCurrentUrl, askingNavigate])
}
