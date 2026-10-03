/**
 * Registered-action guide: the state a tab holds about the guides its owner
 * has been offered, and the seams the pages it walks through read.
 *
 * Authority is the gateway's. This module caches `GET /api/guide/pending` in
 * React Query (so a reload rehydrates), folds each owner `guide_update` frame
 * into that cache, and derives three things from it:
 *
 * - the guide THIS tab owns (`owner_tab === TAB_ID`), which stays on screen on
 *   every route until it ends — navigating to Settings does not drop it, and
 *   the guide never moves the user to some other session;
 * - otherwise the guide for the slot the user is actually looking at (a chat
 *   route's `activeSlot`, or the member thread the Crewmates page registered in
 *   `viewedThread`), which is only OFFERED: nothing navigates, pre-fills or
 *   claims until the human presses Start;
 * - for the pages a guide walks through, a per-request header set for the ONE
 *   save a committed step names.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useAppSelector } from '../store'
import { isChatPath } from '../hooks/notificationBanner'
import { getViewedThreadSlot, subscribeViewedThreadSlot } from '../lib/viewedThread'
import { useGuardedLeave } from '../components/NavigationLeaveGuard'
import { ApiError } from '../api/apiError'
import { TAB_ID } from '../api/tabId'
import {
  GUIDE_PENDING_QUERY_KEY,
  GUIDE_TERMINAL_STATUSES,
  guideApi,
  guideRequestHeaders,
  isGuide,
  mergeGuide,
  type Guide,
  type GuideOutcome,
} from '../api/guide'
import { resolveGuideActions, type GuideStepPlan, type ResolvedGuideAction } from './guideActions'

/** Heartbeat cadence: well inside the gateway's 45 s owner lease. */
export const GUIDE_HEARTBEAT_MS = 15_000

/** The slot the user can actually see: a chat route's `activeSlot`, the
 *  Crewmates page's registered thread, or none. `activeSlot` is RETAINED across
 *  navigation, so it answers only on a chat route. */
export function useViewedSlot(): string | null {
  const { pathname } = useLocation()
  const activeSlot = useAppSelector(state => state.chat.activeSlot)
  const viewedThread = useSyncExternalStore(subscribeViewedThreadSlot, getViewedThreadSlot, getViewedThreadSlot)
  if (isChatPath(pathname)) return activeSlot ?? null
  return viewedThread
}

export interface GuideView {
  guide: Guide
  /** The guide's actions resolved against the registry, or why not. */
  resolved: ReturnType<typeof resolveGuideActions>
  ownedHere: boolean
  /** Owned here but the current action has not been entered in this tab. */
  needsEnter: boolean
  action: ResolvedGuideAction | null
  step: GuideStepPlan | null
}

interface GuideContextValue {
  view: GuideView | null
  busy: boolean
  error: string | null
  start: () => void
  takeOver: () => void
  continueAction: () => void
  cancel: () => void
  report: (outcome: GuideOutcome) => void
  /** Headers for the committed save of *actionId*, or undefined. Read at
   *  submit time; calling it marks the step as submitted. */
  requestHeadersFor: (actionId: string) => Record<string, string> | undefined
  /** Whether the current committed step's save was submitted from this tab. */
  submitted: boolean
  /** Drop the submitted mark after the server definitively refused the save,
   *  so the pill stops waiting on it and the missing-target timer runs again. */
  clearSubmitted: () => void
  /** The guide this tab last owned, once it ended (as the gateway says). */
  finished: Guide | null
  /** Why the pending-guides read failed, when it failed for a guide owner. */
  pendingError: string | null
  dismissFinished: () => void
}

const GuideContext = createContext<GuideContextValue | null>(null)

export function useGuide(): GuideContextValue | null {
  return useContext(GuideContext)
}

/** Clears the submitted mark after a save the server refused. Outside a
 *  provider (a test, a popout) a no-op. */
export function useGuideSubmitRefused(): () => void {
  const ctx = useContext(GuideContext)
  const fn = ctx?.clearSubmitted
  return useCallback(() => fn?.(), [fn])
}

/** Per-request guide headers for *actionId*'s committed save, read at submit
 *  time. Outside a provider (a test, a popout) always undefined. */
export function useGuideRequestHeaders(actionId: string): () => Record<string, string> | undefined {
  const ctx = useContext(GuideContext)
  const fn = ctx?.requestHeadersFor
  return useCallback(() => fn?.(actionId), [fn, actionId])
}

const LIVE: ReadonlySet<string> = new Set(['offered', 'active', 'target_missing'])

/** Pick what this tab shows: its own guide first, else the viewed slot's. */
export function selectGuide(guides: readonly Guide[], viewedSlot: string | null): Guide | null {
  const owned = guides.filter(g => g.owner_tab === TAB_ID && (g.status === 'active' || g.status === 'target_missing'))
  if (owned.length) return owned[owned.length - 1]
  if (!viewedSlot) return null
  const forSlot = guides.filter(g => g.slot_key === viewedSlot && LIVE.has(g.status))
  return forSlot.length ? forSlot[forSlot.length - 1] : null
}

const actionKey = (g: Guide) => `${g.guide_id}:${g.action_index}`
const stepKey = (g: Guide) => `${g.guide_id}:${g.action_index}:${g.step_index}`

export function GuideProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const guardedLeave = useGuardedLeave()
  const location = useLocation()
  const viewedSlot = useViewedSlot()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [entered, setEntered] = useState<ReadonlySet<string>>(() => new Set())
  const [submittedKey, setSubmittedKey] = useState<string | null>(null)

  const { data: guides = [], error: pendingFailure } = useQuery({
    queryKey: GUIDE_PENDING_QUERY_KEY,
    queryFn: async () => {
      const r = await guideApi.pending()
      return Array.isArray(r?.guides) ? r.guides.filter(isGuide) : []
    },
    // Socket frames are one-shot; a reload, a reconnect or a focus returns
    // here. A live guide is also re-read on a slow tick, since a lease that
    // lapsed server-side sends no frame to the tab that lost it.
    refetchOnWindowFocus: true,
    refetchInterval: q => (q.state.data ?? []).some(g => LIVE.has(g.status)) ? 30_000 : false,
    retry: false,
    staleTime: 5_000,
  })

  const guide = selectGuide(guides, viewedSlot)
  const view = useMemo<GuideView | null>(() => {
    if (!guide) return null
    const resolved = resolveGuideActions(guide.actions)
    const ownedHere = guide.owner_tab === TAB_ID && (guide.status === 'active' || guide.status === 'target_missing')
    const action = resolved.ok ? resolved.actions[guide.action_index] ?? null : null
    const step = action ? action.steps[guide.step_index] ?? null : null
    return { guide, resolved, ownedHere, needsEnter: ownedHere && !entered.has(actionKey(guide)), action, step }
  }, [guide, entered])

  const viewRef = useRef(view)
  viewRef.current = view

  const locationRef = useRef(location)
  locationRef.current = location

  const store = useCallback((g: Guide | null) => {
    if (g && isGuide(g)) queryClient.setQueryData<Guide[]>(GUIDE_PENDING_QUERY_KEY, prev => mergeGuide(prev, g))
  }, [queryClient])

  const refused = useCallback((err: unknown) => {
    // A 409 means another tab or the agent moved the guide: re-read it
    // rather than guessing, and say so without claiming anything happened.
    if (err instanceof ApiError && err.status === 409) void queryClient.invalidateQueries({ queryKey: GUIDE_PENDING_QUERY_KEY })
    setError(err instanceof Error ? err.message : String(err))
  }, [queryClient])

  /** Navigate for the owned guide's current action. Only ever runs after
   *  this tab holds the claim. */
  const enter = useCallback((g: Guide) => {
    const resolved = resolveGuideActions(g.actions)
    if (!resolved.ok) return
    const action = resolved.actions[g.action_index]
    if (!action) return
    setEntered(prev => new Set(prev).add(actionKey(g)))
    navigate(action.enter.to({ pathname: locationRef.current.pathname, search: locationRef.current.search }))
  }, [navigate])

  const claim = useCallback((takeOver: boolean) => {
    const v = viewRef.current
    if (!v || !v.resolved.ok || busy) return
    const g = v.guide
    // Ask the page on screen FIRST (a confirm only an event handler may pop):
    // a refusal leaves the guide unclaimed and the page untouched.
    guardedLeave(async () => {
      setBusy(true)
      setError(null)
      try {
        const next = await guideApi.claim(g, takeOver)
        store(next)
        if (next && next.owner_tab === TAB_ID && next.status === 'active') enter(next)
      } catch (err) {
        refused(err)
      } finally {
        setBusy(false)
      }
    })
  }, [busy, guardedLeave, store, enter, refused])

  const start = useCallback(() => claim(false), [claim])
  const takeOver = useCallback(() => claim(true), [claim])

  const continueAction = useCallback(() => {
    const v = viewRef.current
    if (!v?.ownedHere || !v.needsEnter) return
    const g = v.guide
    guardedLeave(() => enter(g))
  }, [guardedLeave, enter])

  const cancel = useCallback(() => {
    const v = viewRef.current
    if (!v || busy) return
    setBusy(true)
    setError(null)
    guideApi.cancel(v.guide).then(store, refused).finally(() => setBusy(false))
  }, [busy, store, refused])

  // One report per step: a second tick must not repeat a write the gateway
  // already accepted, and an owner that lost the claim never writes at all.
  const reportedRef = useRef<Set<string>>(new Set())
  const report = useCallback((outcome: GuideOutcome) => {
    const v = viewRef.current
    if (!v?.ownedHere || v.needsEnter) return
    // A missing target resumes only by being seen again: `target_missing`
    // reports `observed` and nothing else.
    if (v.guide.status !== 'active' && !(v.guide.status === 'target_missing' && outcome === 'observed')) return
    const key = `${stepKey(v.guide)}:${outcome}`
    if (reportedRef.current.has(key)) return
    reportedRef.current.add(key)
    guideApi.progress(v.guide, outcome).then(store, (err) => {
      reportedRef.current.delete(key)
      refused(err)
    })
  }, [store, refused])

  const requestHeadersFor = useCallback((actionId: string) => {
    const v = viewRef.current
    // `target_missing` still counts, as the gateway's `begin_commit` allows: a
    // committed step's control can be absent past the wait (a reload re-enters
    // at the goal step) and only the save itself can complete it.
    if (!v?.ownedHere || v.needsEnter) return undefined
    if (v.guide.status !== 'active' && v.guide.status !== 'target_missing') return undefined
    if (v.action?.id !== actionId || v.step?.complete.kind !== 'committed') return undefined
    setSubmittedKey(stepKey(v.guide))
    return guideRequestHeaders(v.guide)
  }, [])

  // Keep the owner lease alive while this tab owns a live guide.
  const ownedLive = !!view?.ownedHere
  const ownedId = view?.ownedHere ? view.guide.guide_id : null
  useEffect(() => {
    if (!ownedLive || !ownedId) return
    const id = setInterval(() => {
      const v = viewRef.current
      if (!v?.ownedHere) return
      guideApi.heartbeat(v.guide).then(store, refused)
    }, GUIDE_HEARTBEAT_MS)
    return () => clearInterval(id)
  }, [ownedLive, ownedId, store, refused])

  useEffect(() => {
    if (view && GUIDE_TERMINAL_STATUSES.has(view.guide.status)) setSubmittedKey(null)
  }, [view])

  const submitted = !!view && submittedKey === stepKey(view.guide)
  const clearSubmitted = useCallback(() => setSubmittedKey(null), [])

  // The end of a guide this tab drove is said once, in the gateway's words
  // (completed / cancelled / expired) -- never inferred from a click.
  // The terminal row is held here once seen: the pending list serves live
  // guides only, so a later refetch drops it while the user is still reading.
  const [lastOwnedId, setLastOwnedId] = useState<string | null>(null)
  const [finished, setFinished] = useState<Guide | null>(null)
  useEffect(() => {
    if (view?.ownedHere) {
      setLastOwnedId(view.guide.guide_id)
      setFinished(null)
    }
  }, [view?.ownedHere, view?.guide.guide_id])
  useEffect(() => {
    if (!lastOwnedId || view?.ownedHere) return
    const ended = guides.find(g => g.guide_id === lastOwnedId && GUIDE_TERMINAL_STATUSES.has(g.status))
    if (ended) setFinished(ended)
  }, [guides, lastOwnedId, view?.ownedHere])
  const dismissFinished = useCallback(() => {
    setLastOwnedId(null)
    setFinished(null)
  }, [])

  // A failed read of the pending list would otherwise hide an announced guide
  // with no sign. Only the gateway's own error answer is this layer's to
  // report: a 403 is the owner-only route refusing a session that can hold no
  // guide, and a dropped connection is already said by the app's connection
  // banner.
  const pendingError = pendingFailure instanceof ApiError && pendingFailure.status !== 403 && !pendingFailure.authRequired
    ? pendingFailure.message
    : null

  const value = useMemo<GuideContextValue>(() => ({
    view, busy, error, start, takeOver, continueAction, cancel, report,
    requestHeadersFor, submitted, clearSubmitted, finished: view?.ownedHere ? null : finished, dismissFinished,
    pendingError,
  }), [view, busy, error, start, takeOver, continueAction, cancel, report, requestHeadersFor, submitted, clearSubmitted, finished, dismissFinished, pendingError])

  return <GuideContext.Provider value={value}>{children}</GuideContext.Provider>
}
