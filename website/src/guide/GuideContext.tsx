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
 *   `viewedThread`);
 * - per slot, the OFFERED guide and the last guide that ended there (which the
 *   gateway keeps serving for a day, so a reload keeps it), which the
 *   slot's own chat renders as a card (`GuideOfferCard`): nothing navigates,
 *   pre-fills or claims until the human presses Start;
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
import { GUIDE_FOUND_MAX_ATTEMPTS } from './useGuideStepTracker'
import { TAB_ID } from '../api/tabId'
import {
  GUIDE_PENDING_QUERY_KEY,
  GUIDE_TERMINAL_STATUSES,
  guideApi,
  guideRequestHeaders,
  isGuide,
  mergeGuide,
  type Guide,
  type GuideMissingDetail,
  type GuideOutcome,
} from '../api/guide'
import { currentGuideViewport, guideClaimPlacements, replanPlacementFor, resolveGuideActions, subscribeGuideViewport, type GuideStepPlan, type ResolvedGuideAction } from './guideActions'
import { abortProbes, findReport } from './findByName'

/** A `ui.find` step's search, as the gateway may hear it (`findReport`); undefined for any other step. */
function findReportFor(step: GuideStepPlan | null): ReturnType<typeof findReport> | undefined {
  const t = step?.target
  if (t?.kind === 'find') return findReport(t.query, t.key)
  if (t?.kind === 'find-container' && step?.complete.kind === 'reach') {
    const inner = step.complete.targets[0]
    if (inner?.kind === 'find') return findReport(inner.query, inner.key)
  }
  return undefined
}

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
  /** Owned here, this tab has been on the current action's page during it,
   *  and is no longer there: the human navigated away mid-step. */
  offStepPage: boolean
  /** `offStepPage` on an active, entered step: shown at once as "you left this
   *  step", with the way back, instead of waiting out the missing-target bound. */
  leftStep: boolean
}

/** The pathname the action's own enter plan leads to: the page its steps are on. */
export function actionPathname(action: ResolvedGuideAction, here: { pathname: string; search: string }): string {
  return new URL(action.enter.to(here), 'http://guide.invalid').pathname
}

/** Whether *pathname* is that page, or a sub-route of it (a Settings sub-panel). */
export function isOnActionPage(pathname: string, page: string): boolean {
  const base = page.replace(/\/+$/, '')
  return pathname === page || pathname === base || pathname.startsWith(`${base}/`)
}

interface GuideContextValue {
  view: GuideView | null
  busy: boolean
  /** The person pressed Cancel and the gateway has not answered yet: nothing
   *  on the page may be opened or searched meanwhile. */
  cancelling: boolean
  error: string | null
  /** Claim and enter a guide: *guide* when given (an in-chat offer card names
   *  its own slot's guide), else the one on screen. */
  start: (guide?: Guide) => void
  takeOver: () => void
  continueAction: () => void
  /** Bring the owner back to the current step's page, through the action's
   *  own enter plan (the same navigation Start and Continue use). */
  returnToStep: () => void
  /** Bumped by Go back to this step: the tracker re-arms even on the same page. */
  trackNonce: number
  /** The step's target was found on its page: from now on, navigating off
   *  that page reads as leaving the step. */
  notePageSeen: () => void
  /** Cancel *guide* when given, else the one on screen. */
  cancel: (guide?: Guide) => void
  /** Walk a completed show-me *guide* again from its first step. */
  replay: (guide: Guide) => void
  /** `resumeStepIndex`: with `target_found`, the EARLIER step of this action
   *  the page came back at (a remounted form starts over). */
  /** Returns whether a request was actually sent. `detail` names why a
   *  `target_missing` target is missing, when the page can tell. */
  report: (outcome: GuideOutcome, resumeStepIndex?: number, detail?: GuideMissingDetail) => boolean
  /** Headers for the committed save of *actionId*, or undefined. Read at
   *  submit time; calling it marks the step as submitted. */
  requestHeadersFor: (actionId: string) => Record<string, string> | undefined
  /** Resolves true once this tab's guide is on *actionId*'s commit step (so a
   *  save the human presses while the guide catches up is credited), false
   *  when no such guide exists or it has not got there within the cap. */
  awaitSync: (actionId: string) => Promise<boolean>
  /** *actionId*'s save succeeded without guide headers (the guide never caught
   *  up): the guide is ended, and its result says the change was made outside it. */
  noteSavedWithoutGuide: (actionId: string) => void
  /** The committed save of *actionId* was refused outright (a 4xx): nothing
   *  was made, so the step is no longer "submitted" and can follow the page. */
  noteSaveRefused: (actionId: string) => void
  /** Whether the current committed step's save was submitted from this tab. */
  submitted: boolean
  /** The current step's reports failed until the tracker gave up: the guide
   *  is stopped here until the human goes back to the step. */
  stalled: boolean
  /** The guide this tab last owned, once it ended (as the gateway says). */
  finished: Guide | null
  /** When this tab first saw `finished` end (epoch ms), null while none has.
   *  Held here, above the layer, so the finish chip's countdown runs from the
   *  end itself: a remount or a route change re-arms it for what is left,
   *  never for a fresh full delay. */
  finishedAt: number | null
  /** Why the pending-guides read failed, when it failed for a guide owner. */
  pendingError: string | null
  dismissFinished: () => void
  /** Every guide the pending list holds, for the in-chat offer cards. */
  guides: readonly Guide[]
  /** Whether the pending list has been read at least once. Until it has, a
   *  conversation row cannot tell a live offer from one the gateway forgot. */
  guidesLoaded: boolean
  /** The newest guide that ended in each slot, as the gateway still serves it
   *  (a day, until dismissed), so the chat keeps its result line across a reload. */
  records: Readonly<Record<string, Guide>>
  /** Hide *slotKey*'s result line, for every tab. */
  dismissRecord: (slotKey: string) => void
}

const GuideContext = createContext<GuideContextValue | null>(null)

export function useGuide(): GuideContextValue | null {
  return useContext(GuideContext)
}

/** Per-request guide headers for *actionId*'s committed save, read at submit
 *  time. Outside a provider (a test, a popout) always undefined. */
export function useGuideRequestHeaders(actionId: string): () => Record<string, string> | undefined {
  const ctx = useContext(GuideContext)
  const fn = ctx?.requestHeadersFor
  return useCallback(() => fn?.(actionId), [fn, actionId])
}

/** The guide's side of a committed save: wait for it to catch up before
 *  reading headers, and tell it when the save was refused. No-ops outside a
 *  provider. */
export function useGuideSaveLifecycle(actionId: string): { sync: () => Promise<boolean>; refused: () => void; savedUncredited: () => void } {
  const ctx = useContext(GuideContext)
  const awaitSync = ctx?.awaitSync
  const noteSaveRefused = ctx?.noteSaveRefused
  const noteSavedWithoutGuide = ctx?.noteSavedWithoutGuide
  const sync = useCallback(() => (awaitSync ? awaitSync(actionId) : Promise.resolve(false)), [awaitSync, actionId])
  const refused = useCallback(() => noteSaveRefused?.(actionId), [noteSaveRefused, actionId])
  const savedUncredited = useCallback(() => noteSavedWithoutGuide?.(actionId), [noteSavedWithoutGuide, actionId])
  return useMemo(() => ({ sync, refused, savedUncredited }), [sync, refused, savedUncredited])
}

/** How long a save waits for an in-flight guide report before going ahead. */
export const GUIDE_SYNC_WAIT_MS = 3_000
/** Cancel attempts, for the person's Cancel and for closing a guide whose save
 *  went through without it. After a 409 the next one waits for the re-read to
 *  land, not for a timer. */
export const GUIDE_CLOSE_ATTEMPTS = 3
/** Bound on each cancel's wait for a re-read after a 409. */
export const GUIDE_CLOSE_DEADLINE_MS = 15_000

const NO_GUIDES: readonly Guide[] = []

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

const finishedTime = (g: Guide): number => {
  const t = typeof g.finished_at === 'number' ? g.finished_at : Number(g.finished_at)
  return Number.isFinite(t) ? t : 0
}

/** Each slot's newest ended, undismissed guide: the result line its chat shows.
 *  A slot with a guide still in progress shows that one instead. */
export function endedBySlot(guides: readonly Guide[], dismissed: ReadonlySet<string>): Record<string, Guide> {
  const live = new Set(guides.filter(g => LIVE.has(g.status)).map(g => g.slot_key))
  const out: Record<string, Guide> = {}
  for (const g of guides) {
    if (!GUIDE_TERMINAL_STATUSES.has(g.status) || g.dismissed || dismissed.has(g.guide_id) || live.has(g.slot_key)) continue
    const held = out[g.slot_key]
    if (!held || finishedTime(held) <= finishedTime(g)) out[g.slot_key] = g
  }
  return out
}

/** A fresh read of the pending list, keeping any row this tab already holds at
 *  a HIGHER revision: a read that left before a cancel answered must not bring
 *  the cancelled offer back. */
export function reconcilePending(prev: readonly Guide[] | undefined, fresh: readonly Guide[]): Guide[] {
  const held = new Map((prev ?? []).map(g => [g.guide_id, g]))
  return fresh.map(g => {
    const mine = held.get(g.guide_id)
    return mine && mine.revision > g.revision ? mine : g
  })
}
const stepKey = (g: Guide) => `${g.guide_id}:${g.action_index}:${g.step_index}`

export function GuideProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const guardedLeave = useGuardedLeave()
  const location = useLocation()
  const viewedSlot = useViewedSlot()
  const [busy, setBusy] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [trackNonce, setTrackNonce] = useState(0)
  // Failed reports per step: the tracker re-offers a failed write a bounded
  // number of times, and only once those run out has the guide stopped --
  // an unrelated failure (a lease heartbeat) or a first retry never says so.
  const [reportFailures, setReportFailures] = useState<{ key: string; count: number } | null>(null)
  const [entered, setEntered] = useState<ReadonlySet<string>>(() => new Set())
  const [submittedKey, setSubmittedKey] = useState<string | null>(null)
  /** The action (`guide:action`) whose page this tab has been on, with the
   *  step's target found there: only a tab that reached the step can be said
   *  to have LEFT it. A page that redirects on arrival, or never shows the
   *  target, therefore never reads as the user leaving. */
  const [seenPage, setSeenPage] = useState<string | null>(null)
  const { data: guides = NO_GUIDES, error: pendingFailure, isFetched: guidesLoaded } = useQuery({
    queryKey: GUIDE_PENDING_QUERY_KEY,
    queryFn: async () => {
      const r = await guideApi.pending()
      const fresh = Array.isArray(r?.guides) ? r.guides.filter(isGuide) : []
      return reconcilePending(queryClient.getQueryData<Guide[]>(GUIDE_PENDING_QUERY_KEY), fresh)
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
  const here = { pathname: location.pathname, search: location.search }
  const resolvedHere = guide ? resolveGuideActions(guide.actions, guide.guide_id) : null
  const currentAction = resolvedHere?.ok && guide ? resolvedHere.actions[guide.action_index] ?? null : null
  const onPage = !!currentAction && isOnActionPage(here.pathname, actionPathname(currentAction, here))
  const onPageRef = useRef(onPage)
  onPageRef.current = onPage
  const view = useMemo<GuideView | null>(() => {
    if (!guide) return null
    const resolved = resolveGuideActions(guide.actions, guide.guide_id)
    const ownedHere = guide.owner_tab === TAB_ID && (guide.status === 'active' || guide.status === 'target_missing')
    const action = resolved.ok ? resolved.actions[guide.action_index] ?? null : null
    const step = action ? action.steps[guide.step_index] ?? null : null
    const needsEnter = ownedHere && !entered.has(actionKey(guide))
    const offStepPage = ownedHere && !!action && !onPage && seenPage === actionKey(guide)
    const leftStep = offStepPage && !needsEnter && guide.status === 'active'
    return { guide, resolved, ownedHere, needsEnter, action, step, offStepPage, leftStep }
  }, [guide, entered, onPage, seenPage])

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
  const enter = useCallback((g: Guide, leaveGranted = false) => {
    const resolved = resolveGuideActions(g.actions, g.guide_id)
    if (!resolved.ok) return
    const action = resolved.actions[g.action_index]
    if (!action) return
    setEntered(prev => new Set(prev).add(actionKey(g)))
    // `leaveGranted`: the page's guards were asked just now, in this same
    // synchronous step, so the landing page must not ask the same question
    // again. A caller that awaited a request after asking passes false: a
    // draft typed meanwhile was never part of that answer.
    navigate(
      action.enter.to({ pathname: locationRef.current.pathname, search: locationRef.current.search }),
      leaveGranted ? { state: { leaveGranted: true } } : undefined,
    )
  }, [navigate])

  const claim = useCallback((takeOver: boolean, target?: Guide) => {
    const g = target ?? viewRef.current?.guide
    if (!g || busy || !resolveGuideActions(g.actions).ok) return
    // Ask the page on screen FIRST (a confirm only an event handler may pop):
    // a refusal leaves the guide unclaimed and the page untouched.
    guardedLeave(async () => {
      setBusy(true)
      setError(null)
      try {
        const next = await guideApi.claim(g, takeOver, guideClaimPlacements(g.actions))
        store(next)
        // A missing-target guide taken over is entered too: the explicit
        // claim is the human asking to be walked back to its step. Through
        // the page's guards again: a draft typed while the claim was out was
        // never part of the first answer. Refused, the guide stays claimed and
        // its panel offers Continue.
        if (next && next.owner_tab === TAB_ID && (next.status === 'active' || next.status === 'target_missing')) guardedLeave(() => enter(next))
      } catch (err) {
        refused(err)
      } finally {
        setBusy(false)
      }
    })
  }, [busy, guardedLeave, store, enter, refused])

  const start = useCallback((g?: Guide) => claim(false, g), [claim])

  // One report per step: a second tick must not repeat a write the gateway
  // already accepted, and an owner that lost the claim never writes at all.
  const reportedRef = useRef<Set<string>>(new Set())

  const replay = useCallback((g: Guide) => {
    if (busy || !resolveGuideActions(g.actions).ok) return
    guardedLeave(async () => {
      setBusy(true)
      setError(null)
      try {
        const offered = await guideApi.replay(g)
        store(offered)
        if (!offered) return
        // A replay walks the same step and action ids again: forget this
        // guide's earlier reports and entered actions, or its Done would be
        // swallowed as already sent and a later action would skip its page.
        const prefix = `${g.guide_id}:`
        reportedRef.current = new Set([...reportedRef.current].filter(k => !k.startsWith(prefix)))
        setEntered(prev => new Set([...prev].filter(k => !k.startsWith(prefix))))
        const next = await guideApi.claim(offered, false, guideClaimPlacements(offered.actions))
        store(next)
        if (next && next.owner_tab === TAB_ID && next.status === 'active') guardedLeave(() => enter(next))
      } catch (err) {
        refused(err)
      } finally {
        setBusy(false)
      }
    })
  }, [busy, guardedLeave, store, enter, refused])
  const takeOver = useCallback(() => claim(true), [claim])


  const continueAction = useCallback(() => {
    const v = viewRef.current
    if (!v?.ownedHere || !v.needsEnter) return
    const g = v.guide
    guardedLeave(() => enter(g, true))
  }, [guardedLeave, enter])

  const returnToStep = useCallback(() => {
    const v = viewRef.current
    if (!v?.ownedHere) return
    const g = v.guide
    // Already on the page, the tracker may have spent its attempts: a fresh
    // run lets the press report again instead of doing nothing.
    setTrackNonce(n => n + 1)
    setReportFailures(null)
    // The stopped reports' error belongs to the attempt being restarted.
    setError(null)
    guardedLeave(() => enter(g, true))
  }, [guardedLeave, enter])

  const notePageSeen = useCallback(() => {
    const v = viewRef.current
    if (v?.ownedHere && onPageRef.current) setSeenPage(actionKey(v.guide))
  }, [])

  // Progress reports still in flight, and saves waiting for them to land.
  const inflightRef = useRef(0)
  const waitersRef = useRef<Array<() => boolean>>([])
  const [settleTick, setSettleTick] = useState(0)
  // Set synchronously on the Cancel press and held until the cancel settles:
  // no progress report, recovery, heartbeat or re-plan is sent meanwhile, so
  // nothing this tab does moves the guide on past the press.
  const cancellingRef = useRef(false)

  const cancel = useCallback((target?: Guide) => {
    const first = target ?? viewRef.current?.guide
    if (!first || busy || cancellingRef.current) return
    cancellingRef.current = true
    // Stopped here and now, not when the gateway answers: a probe holding a
    // container open restores it and opens nothing more.
    abortProbes()
    setCancelling(true)
    setBusy(true)
    setError(null)
    const guideId = first.guide_id
    const finish = () => { cancellingRef.current = false; setBusy(false); setCancelling(false) }
    const latest = (): Guide | null => {
      const cur = viewRef.current?.guide
      if (cur && cur.guide_id === guideId) return cur
      return queryClient.getQueryData<Guide[]>(GUIDE_PENDING_QUERY_KEY)?.find(g => g.guide_id === guideId) ?? null
    }
    let attempts = 0
    // A report that left before the press can still move the revision: the
    // cancel waits for it to land, and a 409 (the revision moved anyway, by
    // another tab or the agent) is retried at the re-read revision, a bounded
    // number of times. The guide is never resumed in between.
    const attempt = () => {
      const g = latest() ?? first
      if (GUIDE_TERMINAL_STATUSES.has(g.status)) { finish(); return }
      attempts += 1
      const sentRevision = g.revision
      guideApi.cancel(g).then((next) => { store(next); finish() }, (err) => {
        if (!(err instanceof ApiError && err.status === 409) || attempts >= GUIDE_CLOSE_ATTEMPTS) {
          refused(err)
          finish()
          return
        }
        const deadline = Date.now() + GUIDE_CLOSE_DEADLINE_MS
        const moved = () => {
          const now = latest()
          if (now && now.revision === sentRevision && Date.now() <= deadline) return false
          if (!now) { finish(); return true }
          attempt()
          return true
        }
        void queryClient.invalidateQueries({ queryKey: GUIDE_PENDING_QUERY_KEY }).then(() => {
          if (!moved()) waitersRef.current.push(moved)
        })
      })
    }
    const startWhenSettled = () => {
      if (inflightRef.current > 0) return false
      attempt()
      return true
    }
    if (!startWhenSettled()) waitersRef.current.push(startWhenSettled)
  }, [busy, store, refused, queryClient])

  const report = useCallback((outcome: GuideOutcome, resumeStepIndex?: number, detail?: GuideMissingDetail): boolean => {
    const v = viewRef.current
    if (!v?.ownedHere || cancellingRef.current) return false
    if (outcome === 'target_found') {
      // Recovery is the one write a missing guide makes, and only it.
      if (v.guide.status !== 'target_missing') return false
      if (resumeStepIndex !== undefined && resumeStepIndex >= v.guide.step_index) resumeStepIndex = undefined
    } else if (v.guide.status !== 'active' || v.needsEnter) return false
    // Keyed by revision too: a step that went missing, came back and went
    // missing again reports each change once, never one write twice.
    const key = `${stepKey(v.guide)}:${v.guide.revision}:${outcome}`
    if (reportedRef.current.has(key)) return false
    reportedRef.current.add(key)
    // The step is over (Next, Done, or its control pressed): a probe still
    // looking for it stops now rather than when the gateway answers.
    if (outcome === 'observed') abortProbes()
    const g = v.guide
    inflightRef.current += 1
    guideApi.progress(g, outcome, resumeStepIndex, detail, v.action?.id === 'ui.find' ? findReportFor(v.step) : undefined).finally(() => {
      inflightRef.current -= 1
      setSettleTick(n => n + 1)
    }).then((next) => {
      store(next)
      // An earlier failed attempt's error is stale once a report lands.
      setError(null)
      setReportFailures(null)
      // The human is back on the step's page: its action counts as entered.
      if (outcome === 'target_found' && next?.status === 'active') setEntered(prev => new Set(prev).add(actionKey(g)))
    }, (err) => {
      reportedRef.current.delete(key)
      const failedKey = stepKey(g)
      setReportFailures(prev => (prev && prev.key === failedKey ? { key: failedKey, count: prev.count + 1 } : { key: failedKey, count: 1 }))
      refused(err)
    })
    return true
  }, [store, refused])

  // A save waits for the condition it needs, not merely for the network to go
  // quiet: this tab owns the guide, and its current step IS *actionId*'s commit
  // step. Re-checked after every render (so the view read is the gateway's
  // answer), resolved false at once when no live guide of this tab is on that
  // action, and false on the cap -- the caller then saves uncredited and closes
  // the guide honestly instead of leaving it asking for a press already made.
  useEffect(() => {
    if (!waitersRef.current.length) return
    waitersRef.current = waitersRef.current.filter(w => !w())
  }, [view, settleTick])
  const awaitSync = useCallback((actionId: string) => new Promise<boolean>((resolve) => {
    const verdict = (): boolean | null => {
      const v = viewRef.current
      if (!v?.ownedHere || v.action?.id !== actionId) return false
      if (v.guide.status !== 'active' && v.guide.status !== 'target_missing') return false
      if (v.guide.status === 'active' && !v.needsEnter && v.step?.complete.kind === 'committed') return true
      return null
    }
    const first = verdict()
    if (first !== null) { resolve(first); return }
    let done = false
    const check = () => {
      if (done) return true
      const r = verdict()
      if (r === null) return false
      done = true
      resolve(r)
      return true
    }
    waitersRef.current.push(check)
    setTimeout(() => { if (!done) { done = true; resolve(false) } }, GUIDE_SYNC_WAIT_MS)
  }), [])
  const noteSavedWithoutGuide = useCallback((actionId: string) => {
    const v = viewRef.current
    if (!v?.ownedHere || v.action?.id !== actionId || GUIDE_TERMINAL_STATUSES.has(v.guide.status)) return
    // The save happened but the gateway holds no evidence for this guide: end
    // it, and say why on its result, never "completed" and never "cancelled".
    // Pinned to THIS guide: a late progress report can move its revision on
    // the gateway first (the cancel then answers 409 and the guide is re-read),
    // so it retries at the re-read revision -- a bounded number of times, and
    // only while the same guide is still this tab's, live, on the same action.
    const guideId = v.guide.guide_id
    const deadline = Date.now() + GUIDE_CLOSE_DEADLINE_MS
    let attempts = 0
    const attempt = () => {
      const cur = viewRef.current
      if (!cur?.ownedHere || cur.guide.guide_id !== guideId || cur.action?.id !== actionId) return
      if (GUIDE_TERMINAL_STATUSES.has(cur.guide.status)) return
      attempts += 1
      const sentRevision = cur.guide.revision
      guideApi.cancel(cur.guide, 'saved_without_guide').then(store, (err) => {
        if (!(err instanceof ApiError && err.status === 409) || attempts >= GUIDE_CLOSE_ATTEMPTS) {
          refused(err)
          return
        }
        setError(err.message)
        // Retry only once the re-read has LANDED and shows this guide past
        // the revision that was refused: a timer alone can fire while a slow
        // read is still in flight, and spend every attempt on the stale one.
        // The wait itself costs no attempt; the whole close is still bounded.
        const moved = () => {
          if (Date.now() > deadline) return true
          const now = viewRef.current
          if (now?.guide.guide_id === guideId && now.guide.revision === sentRevision) return false
          attempt()
          return true
        }
        void queryClient.invalidateQueries({ queryKey: GUIDE_PENDING_QUERY_KEY }).then(() => {
          if (!moved()) waitersRef.current.push(moved)
        })
      })
    }
    attempt()
  }, [store, refused, queryClient])
  const noteSaveRefused = useCallback((actionId: string) => {
    const v = viewRef.current
    if (v?.action?.id === actionId) setSubmittedKey(null)
  }, [])

  const requestHeadersFor = useCallback((actionId: string) => {
    const v = viewRef.current
    if (!v?.ownedHere || v.guide.status !== 'active' || v.needsEnter) return undefined
    if (v.action?.id !== actionId || v.step?.complete.kind !== 'committed') return undefined
    setSubmittedKey(stepKey(v.guide))
    return guideRequestHeaders(v.guide)
  }, [])

  // A guide this bundle cannot show because the gateway accepted it against
  // another build's index: say so to the gateway once per revision, so
  // `guide_status` can tell the agent (a reload fixes it). Only the reason
  // moves; nothing is claimed, cancelled or advanced.
  const refusedRef = useRef<Set<string>>(new Set())
  // These two writes are best effort: a 409 (the guide moved, or the gateway
  // declined, e.g. ``replan_not_at_boundary``) is designed to change nothing
  // and stays quiet. Anything else -- a 5xx, a 400, no network -- is a real
  // failure and is said in the guide's error line like every other write.
  const quietUnless409 = useCallback((err: unknown) => {
    if (err instanceof ApiError && err.status === 409) return
    refused(err)
  }, [refused])
  const mismatched = view && !view.resolved.ok && view.resolved.reason === 'build_mismatch'
    && view.guide.reason !== 'build_mismatch' && !GUIDE_TERMINAL_STATUSES.has(view.guide.status)
    && (view.guide.owner_tab === null || view.guide.owner_tab === TAB_ID)
    ? view.guide : null
  useEffect(() => {
    if (!mismatched) return
    const key = `${mismatched.guide_id}:${mismatched.revision}`
    if (refusedRef.current.has(key)) return
    refusedRef.current.add(key)
    guideApi.refuse(mismatched, 'build_mismatch').then(store, quietUnless409)
  }, [mismatched, store, quietUnless409])

  // The viewport class changed under a guide this tab is walking: its
  // `ui.show` action asks the gateway to walk the new viewport's placement
  // from here. Asked once per guide revision and placement; the gateway allows
  // it only at a step boundary both placements share, and a refusal changes
  // nothing (the step then shows its target missing, as before).
  const viewport = useSyncExternalStore(subscribeGuideViewport, currentGuideViewport, currentGuideViewport)
  const replanRef = useRef<Set<string>>(new Set())
  const replanTo = view?.ownedHere && !view.needsEnter && (view.guide.status === 'active' || view.guide.status === 'target_missing')
    ? replanPlacementFor(view.guide.actions[view.guide.action_index], viewport)
    : null
  const replanGuide = replanTo ? view!.guide : null
  useEffect(() => {
    if (!replanTo || !replanGuide) return
    const key = `${replanGuide.guide_id}:${replanGuide.revision}:${replanTo}`
    if (replanRef.current.has(key) || cancellingRef.current) return
    replanRef.current.add(key)
    guideApi.replan(replanGuide, replanTo).then(store, quietUnless409)
  }, [replanTo, replanGuide, store, quietUnless409])

  // Keep the owner lease alive while this tab owns a live guide.
  const ownedLive = !!view?.ownedHere
  const ownedId = view?.ownedHere ? view.guide.guide_id : null
  useEffect(() => {
    if (!ownedLive || !ownedId) return
    const id = setInterval(() => {
      const v = viewRef.current
      if (!v?.ownedHere || cancellingRef.current) return
      guideApi.heartbeat(v.guide).then(store, refused)
    }, GUIDE_HEARTBEAT_MS)
    return () => clearInterval(id)
  }, [ownedLive, ownedId, store, refused])

  useEffect(() => {
    if (view && GUIDE_TERMINAL_STATUSES.has(view.guide.status)) setSubmittedKey(null)
  }, [view])

  const submitted = !!view && submittedKey === stepKey(view.guide)
  const stalled = !!view && !!reportFailures && reportFailures.key === stepKey(view.guide) && reportFailures.count >= GUIDE_FOUND_MAX_ATTEMPTS

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
  // Read in the same render the guide ends, not one render later: the floating
  // panel turns into the finish chip as one element, never unmounting between.
  const endedNow = !finished && lastOwnedId && !view?.ownedHere
    ? guides.find(g => g.guide_id === lastOwnedId && GUIDE_TERMINAL_STATUSES.has(g.status)) ?? null
    : null
  const finishedShown = view?.ownedHere ? null : finished ?? endedNow
  // Stamped in the render the end is first seen (a ref, so stamping does not
  // re-render), keyed by guide and status so a replayed guide that ends again
  // gets its own countdown.
  const finishedStamp = useRef<{ key: string; at: number } | null>(null)
  const finishedStampKey = finishedShown ? `${finishedShown.guide_id}:${finishedShown.status}` : ''
  if (!finishedStampKey) finishedStamp.current = null
  else if (finishedStamp.current?.key !== finishedStampKey) finishedStamp.current = { key: finishedStampKey, at: Date.now() }
  const finishedAt = finishedStamp.current?.at ?? null

  // A guide that ended stays in the gateway's pending list (its slot's newest,
  // for a day, until dismissed), so the slot's chat result line is DERIVED from
  // that list and survives a reload the way a change card's does. A dismissal
  // is hidden here at once and recorded by the gateway for every tab.
  const [dismissed, setDismissed] = useState<ReadonlySet<string>>(() => new Set())
  const records = useMemo<Readonly<Record<string, Guide>>>(() => endedBySlot(guides, dismissed), [guides, dismissed])
  const dismissRecord = useCallback((slotKey: string) => {
    const held = endedBySlot(queryClient.getQueryData<Guide[]>(GUIDE_PENDING_QUERY_KEY) ?? [], new Set())[slotKey]
    if (!held) return
    setDismissed(prev => new Set(prev).add(held.guide_id))
    // A failed write only means the line comes back on the next read; the
    // user can dismiss it again. Nothing else rides on it.
    guideApi.dismiss(held).then(store, () => undefined)
  }, [queryClient, store])

  // A failed read of the pending list would otherwise hide an announced guide
  // with no sign. Only the gateway's own error answer is this layer's to
  // report: a 403 is the owner-only route refusing a session that can hold no
  // guide, and a dropped connection is already said by the app's connection
  // banner.
  const pendingError = pendingFailure instanceof ApiError && pendingFailure.status !== 403 && !pendingFailure.authRequired
    ? pendingFailure.message
    : null

  const value = useMemo<GuideContextValue>(() => ({
    view, busy, cancelling, error, start, takeOver, continueAction, returnToStep, trackNonce, notePageSeen, cancel, replay, report,
    requestHeadersFor, awaitSync, noteSaveRefused, noteSavedWithoutGuide, submitted, stalled, finished: finishedShown, finishedAt, dismissFinished,
    pendingError, guides, guidesLoaded, records, dismissRecord,
  }), [view, busy, cancelling, error, start, takeOver, continueAction, returnToStep, trackNonce, notePageSeen, cancel, replay, report, requestHeadersFor, awaitSync, noteSaveRefused, noteSavedWithoutGuide, submitted, stalled, finishedShown, finishedAt, dismissFinished, pendingError, guides, guidesLoaded, records, dismissRecord])

  return <GuideContext.Provider value={value}>{children}</GuideContext.Provider>
}
