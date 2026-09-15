/**
 * Progressive mount for the Pierre surfaces a transcript renders — the diff
 * rows of a turn's file changes, and the fenced code blocks in its markdown.
 *
 * Why this exists: every one of those mounts the Pierre/Shiki runtime, and even
 * a COLLAPSED diff row (the transcript's default — a header-only row) costs real
 * main-thread time. A switch into a session whose recent turns edited many files
 * mounts all of them in one commit, measured at ~950ms of blocking with a single
 * 474ms task across 28 rows. They are not the payload and not the network; they
 * are one long task, and a long task is what makes the switch feel stuck.
 *
 * The shape is a budget, not a virtualizer: the first `EAGER_ROWS` of a burst
 * mount in the same commit as everything else, and the rest are released
 * `STAGE_BATCH` at a time from an idle callback. Nothing is dropped and nothing
 * is conditional on visibility — every registrant is released, so one that is
 * never scrolled to still ends up mounted and searchable.
 *
 * ONE queue serves both surfaces on purpose: the budget being rationed is the
 * main thread, which they share. A transcript heavy in both must not get two
 * independent budgets that together blow the ceiling either one respected.
 *
 * LIFO, deliberately. React mounts in DOM order, so the LAST registrant is the
 * bottom-most one — the newest turn, which is what the transcript is pinned to
 * and what the reader is actually looking at. Draining newest-first spends the
 * eager budget where it shows.
 *
 * The eager budget resets whenever the queue empties, so it is per-burst rather
 * than per-page-load. Without that reset the first switch would spend the whole
 * budget and every later registrant — including the single row a live turn
 * appends — would wait for an idle slice it does not need.
 *
 * THE INVARIANT, for anyone adding a surface: the placeholder rendered while
 * staged must be the SAME HEIGHT as the mounted thing it stands in for.
 * Staging trades a paint for a later paint; if the two differ in height, that
 * trade also moves the scroll position, which is a worse bug than the long task
 * this module exists to break up. Both current callers pin that height in a
 * test, and the numbers came from measuring the real render, not from reading
 * the CSS.
 */

import { createContext, useContext, useEffect, useState } from 'react'

/** Registrants released in the mounting commit, before any staging begins. */
export const EAGER_ROWS = 4

/** Registrants released per idle slice. Small enough that a slice stays short. */
export const STAGE_BATCH = 3

/** Upper bound on how long a slice may wait for idle before it is forced.
 *  A busy main thread must not strand a registrant indefinitely. */
export const STAGE_TIMEOUT_MS = 200

type Release = () => void

/** Newest-last, drained from the end — see the LIFO note above. */
const waiting: Release[] = []
let admitted = 0
let scheduled = false

type IdleFn = (cb: () => void, opts?: { timeout: number }) => number

/** `requestIdleCallback` where it exists, a macrotask everywhere else (Safari
 *  did not ship it, and jsdom has neither). The fallback still yields to paint,
 *  which is the property that matters here. */
function afterPaint(run: () => void): void {
  const rIC = (globalThis as unknown as { requestIdleCallback?: IdleFn }).requestIdleCallback
  if (typeof rIC === 'function') rIC(() => run(), { timeout: STAGE_TIMEOUT_MS })
  else setTimeout(run, 0)
}

/** The drain holds while ANY scroller has scrolled within this window. A
 * release mounts real Pierre content (~90ms main thread) and repaints the
 * released surface; on a phone's momentum scroll both are felt as hitching
 * ("elements changing under my finger"), and none of it is urgent — the
 * stand-ins are readable. Scroll listeners do not bubble, so this is a
 * document-level CAPTURE listener, installed once, lazily, from the first
 * schedule() — module scope, same lifetime as the queue itself. */
export const STAGE_SCROLL_HOLD_MS = 250
let lastAnyScrollTs = 0
let scrollListenerInstalled = false
function ensureScrollListener(): void {
  if (scrollListenerInstalled || typeof document === 'undefined') return
  scrollListenerInstalled = true
  document.addEventListener('scroll', () => { lastAnyScrollTs = Date.now() }, { capture: true, passive: true })
}

/** Per-slice time budget for the drain. A release triggers the registrant's
 * REAL mount synchronously (a Pierre code surface is ~90ms), so a count-based
 * batch of 3 could stack ~270ms+ into one slice — measured as a 381ms long
 * task while scrolling once viewport gating moved queueing into scroll time.
 * Budgeted draining releases at least one registrant per slice and stops the
 * moment the slice's spent time crosses the budget, so a slice costs one
 * mount (~90ms) in the worst case instead of a batch of them. */
export const STAGE_SLICE_BUDGET_MS = 8

function schedule(): void {
  if (scheduled) return
  ensureScrollListener()
  scheduled = true
  afterPaint(() => {
    scheduled = false
    // Mid-scroll: hold the whole drain and retry after a full hold window.
    // A DELAYED retry, never an immediate re-arm: Safari has no
    // requestIdleCallback, so afterPaint degrades to setTimeout(0) and an
    // immediate re-arm would busy-loop the main thread during the scroll it
    // is trying to protect.
    if (Date.now() - lastAnyScrollTs < STAGE_SCROLL_HOLD_MS) {
      scheduled = true
      setTimeout(() => { scheduled = false; schedule() }, STAGE_SCROLL_HOLD_MS)
      return
    }
    const t0 = performance.now()
    // At least one release per slice (guaranteed progress), then keep going
    // only while the budget holds — a single heavy mount ends the slice.
    do {
      const release = waiting.pop()
      if (!release) break
      release()
    } while (waiting.length > 0 && performance.now() - t0 < STAGE_SLICE_BUDGET_MS)
    if (waiting.length > 0) schedule()
    // The burst is over: the next registrant is a fresh burst and gets the
    // eager budget again.
    else admitted = 0
  })
}

/**
 * Ask to mount. Calls `release` when this registrant's turn comes —
 * synchronously for the first few of a burst, from an idle slice afterwards.
 *
 * KNOWN DEFECT, measured but not yet fixed: the eager grant is first-come, and
 * registrations arrive in DOM order (top-most first) because they come from one
 * commit's effects. So the eager budget goes to the rows FURTHEST from a
 * bottom-pinned reader, and the LIFO drain below only reorders the leftovers —
 * the blocks actually on screen can wait behind ones nobody can see. Measured on
 * a 2.48MB transcript: 11 registrants, ~90ms of Pierre mount each, 1.1s until the
 * last landed. Fixing it means deciding the grant one microtask later, once the
 * whole commit has registered, so `pop()` picks the bottom-most ones; that turns
 * the eager release asynchronous and needs the callers' tests to flush a
 * microtask, which is why it is written down here rather than half-applied.
 *
 * Returns an unsubscribe for the caller's effect cleanup. A registrant that
 * unmounts while queued must not be released, or React warns about a state
 * update on an unmounted component and the slice is spent on nothing.
 */
export function requestStage(release: Release): () => void {
  if (admitted < EAGER_ROWS) {
    admitted++
    release()
    return () => {}
  }
  waiting.push(release)
  schedule()
  return () => {
    const at = waiting.indexOf(release)
    if (at >= 0) waiting.splice(at, 1)
  }
}

/** Rows already admitted once, by caller-supplied identity. A virtualized
 * transcript REMOUNTS a row every time it scrolls back into the window, and
 * re-queueing it each time starves the queue permanently on a scroll-churny
 * surface (measured on a phone: whole screens of chip stand-ins that never
 * resolve — the reader reports "text vanished to background"). Admission is a
 * one-way latch per row identity: the FIRST mount pays the staging queue (that
 * is what keeps slot-switch paint fast), every remount mounts at once. Capped
 * so a pathological session cannot grow it unbounded; clearing merely costs
 * one re-stage. */
const latched = new Set<string>()
const LATCH_CAP = 4000

/**
 * Whether this caller may mount its Pierre surface yet.
 *
 * `immediate` is the caller's own "staging would be wrong here" escape — an
 * opened row whose body is the point, or content that measures zero and would
 * therefore GAIN height on release. It is read on every commit, so a row the
 * user opens mid-queue mounts at once rather than waiting out the queue.
 *
 * `latchKey` is the row's stable identity across remounts (see `latched`).
 *
 * `hold` keeps the caller OUT of the queue entirely (viewport gating): a
 * surface far from the viewport should not spend a queue slot it may never
 * need — the burst this queue spreads out is exactly the mount-everything
 * commit, and most of those surfaces are off-screen. A held caller joins the
 * queue the commit after its gate opens; a latched one ignores `hold` (its
 * content is already paid for, and the virtualizer only mounts rows near the
 * viewport anyway).
 */
/**
 * True inside the off-screen measure farm. Farm renders exist ONLY to be
 * measured: Pierre surfaces must stay on their equal-metrics text stand-ins
 * (the stand-in IS the measured geometry), never queue for staging, and
 * never write the latch -- a latched key would make the later REAL mount
 * skip the queue and pay its Pierre cost during scroll.
 */
export const PierreFarmHoldContext = createContext(false)

export function useStagedMount(immediate: boolean, latchKey?: string, hold = false): boolean {
  const farm = useContext(PierreFarmHoldContext)
  const [ready, setReady] = useState(!farm && (immediate || (latchKey !== undefined && latched.has(latchKey))))
  useEffect(() => {
    // Farm render: stand-in only, no queue slot, no latch write.
    if (farm) return
    if (immediate || (latchKey !== undefined && latched.has(latchKey))) {
      if (latchKey !== undefined) {
        if (latched.size >= LATCH_CAP) latched.clear()
        latched.add(latchKey)
      }
      setReady(true)
      return
    }
    if (hold) return
    // Registers on mount and releases in queue order; the returned cleanup
    // withdraws a registrant that unmounted while still queued.
    return requestStage(() => {
      if (latchKey !== undefined) {
        if (latched.size >= LATCH_CAP) latched.clear()
        latched.add(latchKey)
      }
      setReady(true)
    })
  }, [immediate, latchKey, hold, farm])
  return ready
}

/** How far outside the viewport a surface still counts as "near": queued
 * before the reader arrives, invisible churn beyond it. */
export const VIEWPORT_PRELOAD_MARGIN_PX = 600


/** Test seam: the queue is module state shared by every registrant, so a test
 *  that exhausts the eager budget would leak that into the next test. */
export function __resetStagingForTests(): void {
  lastAnyScrollTs = 0
  waiting.length = 0
  admitted = 0
  scheduled = false
  latched.clear()
}

/** Test seam: how many registrants are still waiting. */
export function __stagedWaitingCount(): number {
  return waiting.length
}
