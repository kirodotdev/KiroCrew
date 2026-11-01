// Window and range calculation for the chat virtualizer.
//
// Owns the mounted window -- the contiguous `[start, end)` of rows rendered as
// real DOM, with everything else represented by the spacers -- and the rules
// that move it as the reader scrolls: the scroll-path recompute and its merge,
// the near/far jump rule behind `mountIndex`, the sentinel expansions, the
// coverage watchdog, the older-history trigger and the tail-append mount. Recomputing the window never
// writes `scrollTop`.
//
// The placements that move the scroller also mount a window of their own, from
// their own owners and through the same window math: follow's tail and jump
// placements and its bulk-prepend rebase (followPolicy.ts), the part-1 rebase
// (shiftCompensation.ts), and the entry, visibility and restore windows
// (readingPosition.ts). The pure window math is in WindowCalculator.ts.

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type MutableRefObject, type RefObject } from 'react'
import type { HeightIndex } from './HeightIndex'
import type { RetainedVirtualRange } from './types'
import {
  computeJumpWindow,
  computeWindow,
  expandWindowDown,
  expandWindowUp,
  getOffset as getOffsetFn,
  initialWindow,
  jumpIsNear,
  mergeWindowRange,
  type HeightGetter,
  type WindowRange,
} from './WindowCalculator'
import { SCROLL_SETTLE_MS, scrollerCollapsed } from './FollowController'
import type { FollowState, Pinning } from './followPolicy'
import type { ShiftCapture } from './shiftCompensation'

type Ref<V> = MutableRefObject<V>
type SetWindowRange = (next: WindowRange | ((prev: WindowRange) => WindowRange)) => void

/** Viewport-coverage watchdog cadence. Every self-motion source (pin writes,
 *  native anchoring, height repricing, landing splices) is supposed to leave
 *  the viewport inside the mounted window, and the scroll handler / resize
 *  observer re-derive the window on their own events. A displacement with no
 *  follow-up event -- observed live as 3+ seconds of bare spacer (skeleton
 *  bars) mid-stream -- has nobody responsible for re-covering the viewport:
 *  the reader sits still, nothing scrolls, no row resizes. The watchdog is
 *  the backstop, not the mechanism: two O(log N) lookups per tick, a state
 *  write only when the viewport actually lies outside the window's pixels. */
const VIEWPORT_COVERAGE_TICK_MS = 500
/** The watchdog YIELDS while any event-driven recompute ran this recently.
 *  During a streaming turn the offset tree's pricing legitimately trails the
 *  real DOM (measurements land in RO batches), so comparing tree pixels
 *  against live scrollTop reads as "uncovered" on every tick -- and each
 *  forced recompute then remounts rows against the stale prices, a visible
 *  bounce exactly while streaming. A recent recompute proves the responsible
 *  event paths (scroll handler, resize observer) are awake; the watchdog
 *  exists solely for the DEAD-AIR case where nothing else will ever run. */
const VIEWPORT_COVERAGE_YIELD_MS = 1200
/** Coverage slack: sub-pixel rounding and scrollbar-anchoring nudges must not
 *  count as uncovered. */
const VIEWPORT_COVERAGE_SLACK_PX = 8

// Lead distance for the BOTTOM sentinel, which expands the mounted window over
// rows already in memory. Local work, no network, so it needs only enough lead
// to keep a gap from painting.
const WINDOW_EXPAND_MARGIN_PX = 200

// Lead distance for the TOP sentinel, which is what STARTS the older-history
// fetch. It has a CEILING, learned the hard way: it was raised to 1500px to
// hide fetch latency, and because tool-call grouping can collapse a
// 100-message page into a few hundred px of display rows, the sentinel stayed
// inside the margin after every insert. `shouldPaginateOlder` gates
// concurrency, not recurrence, so page after page fired serially. The margin
// must stay below the height a typical page renders at, or pagination
// self-oscillates.
const OLDER_PREFETCH_MARGIN_PX = 200

// Fallback prefetch lead when the caller does not supply `prefetchStartIndex`:
// start the older-history fetch while this many DISPLAY ROWS remain above the
// window start. The real contract is the caller's — ChatPage passes the index
// of the SECOND USER MESSAGE from the top ("start loading while I am still two
// of MY OWN messages away"), which display rows only approximate (a row can be
// a nudge, a group, a lone tool card).
//
// Index-based, deliberately not pixels. A pixel margin was tried at 1500px and
// oscillated: tool-call grouping renders a 100-message page only a few
// hundred px tall, the sentinel never left the margin, and pages fired
// serially. An index trigger cannot loop by construction — the landing shifts
// every index by the inserted count, moving the trigger far away until the
// reader scrolls up through the new page themselves.
const OLDER_PREFETCH_START_ROWS = 8

export interface WindowState {
  windowRange: WindowRange
  setWindowRange: SetWindowRange
  retainRange: (range: RetainedVirtualRange | null) => void
  /** The range `setWindowRange(next)` commits: `next` widened to keep any
   *  retained range mounted. A render-phase writer compares against this, not
   *  against `next`, or it never sees its own update land. */
  resolveWindowRange: (next: WindowRange) => WindowRange
  /** Live mirror of the COMMITTED window. The facade advances it in a layout
   *  effect, right after the shift capture's own baseline mirror. */
  windowRangeRef: Ref<WindowRange>
}

export function useWindowState(
  itemCount: number,
  overscan: number,
  initialPlacement: 'top' | 'bottom',
  sessionId?: string,
): WindowState {
  const countRef = useRef(itemCount)
  countRef.current = itemCount
  const retainedRangeRef = useRef<RetainedVirtualRange | null>(null)
  const sessionRef = useRef(sessionId)
  if (sessionRef.current !== sessionId) {
    sessionRef.current = sessionId
    retainedRangeRef.current = null
  }
  // Window range for what is currently mounted. Initial state is the TAIL of
  // the list (last ~overscan+1 items) — chat sessions always open at the
  // bottom, and starting here avoids a commit-timing race where the slot-entry
  // pin runs before the tail items have rendered.
  const [windowRange, setWindowRangeState] = useState<WindowRange>(() =>
    initialWindow(itemCount, overscan, initialPlacement),
  )
  // Every window write goes through here, so a retained range (a native
  // selection's rows) stays mounted whichever owner moved the window:
  // unmounting a selection endpoint lets the browser re-root the selection.
  const resolveWindowRange = useCallback((next: WindowRange): WindowRange => {
    const retained = retainedRangeRef.current
    if (!retained) return next
    const count = countRef.current
    const start = Math.max(0, Math.min(retained.start, count))
    const end = Math.max(start, Math.min(retained.end, count))
    return end > start
      ? { start: Math.min(next.start, start), end: Math.max(next.end, end) }
      : next
  }, [])
  const setWindowRange: SetWindowRange = useCallback((update) => {
    setWindowRangeState((previous) => resolveWindowRange(typeof update === 'function' ? update(previous) : update))
  }, [resolveWindowRange])
  const retainRange = useCallback((range: RetainedVirtualRange | null) => {
    const count = countRef.current
    const next = range ? {
      start: Math.max(0, Math.min(Math.floor(range.start), count)),
      end: Math.max(0, Math.min(Math.floor(range.end), count)),
    } : null
    const normalized = next && next.end > next.start ? next : null
    const previous = retainedRangeRef.current
    if (previous?.start === normalized?.start && previous?.end === normalized?.end) return
    retainedRangeRef.current = normalized
    if (normalized) setWindowRange((current) => current)
  }, [setWindowRange])
  // Live mirror of windowRange for imperative reads (debug probe).
  const windowRangeRef = useRef(windowRange)
  return { windowRange, setWindowRange, windowRangeRef, retainRange, resolveWindowRange }
}

/**
 * Mount rows appended at the TAIL under a followed reader in the render that
 * first sees them.
 *
 * The window is recomputed only from a scroll event or a resize, so a row
 * appended under a reader following the tail -- the prompt they just sent, the
 * reply that starts under it -- otherwise commits OUTSIDE the window, priced
 * into the bottom spacer at the running height estimate. The append pin then
 * carries the reader to the bottom of that estimate, and a frame later the
 * pin's own scroll event mounts the row at its real height and the bottom
 * moves back: under long replies a short prompt is estimated hundreds of px
 * too tall, so every send moved the transcript up and then back down.
 *
 * Extending the range here makes React re-run the render before committing,
 * so the first commit holding the rows renders them as rows and the append
 * pin lands on their real bottom. Scoped to exactly that case: following a
 * tail that already holds rows (the first rows of an empty list are the
 * entry placement's) in a box with a real height, the committed window already
 * reaching the committed tail, the tail key changed (an append; a prepend
 * keeps the tail and is the follow owner's bulk-prepend shift or the re-base),
 * and a growth small enough not to be the hydration replace the follow owner
 * force-pins on its own (see useFollowPlacementPins). A reader who scrolled up
 * owns their window, and the row stays off it as before.
 */
export function useTailAppendMount<T>(ctx: {
  items: T[]
  getKey: (item: T, index: number) => string
  itemCount: number
  overscan: number
  sessionId: string
  windowRange: WindowRange
  setWindowRange: SetWindowRange
  resolveWindowRange: (next: WindowRange) => WindowRange
  scrollerRef: RefObject<HTMLDivElement | null>
  shift: Pick<ShiftCapture, 'shiftStageRef'>
  follow: Pick<FollowState, 'stickRef'>
}): void {
  const { items, getKey, itemCount, overscan, sessionId, windowRange, setWindowRange, resolveWindowRange } = ctx
  const tailKey = itemCount > 0 ? getKey(items[itemCount - 1], itemCount - 1) : null
  // The committed tail, advanced at COMMIT for the same reason as the shift
  // capture's mirror: a discarded concurrent attempt must not move it.
  const committedRef = useRef<{ session: string; count: number; tailKey: string | null } | null>(null)
  const prev = committedRef.current
  if (
    prev !== null &&
    prev.session === sessionId &&
    prev.count > 0 &&
    ctx.follow.stickRef.current &&
    // Defence in depth: the prepend re-base arms only for a released reader.
    ctx.shift.shiftStageRef.current !== 'awaiting-rebase' &&
    itemCount > prev.count &&
    itemCount - prev.count <= overscan + 1 &&
    tailKey !== prev.tailKey &&
    windowRange.end >= prev.count &&
    windowRange.end < itemCount &&
    // A collapsed box (backgrounded tab, display:none pane) writes no pin, so
    // no scroll event ever trims the window's start behind these rows: every
    // append there would stay mounted. The visibility return re-places it.
    // Read LAST: it is a layout read, and every streamed token re-renders a
    // followed transcript without appending a row.
    ctx.scrollerRef.current !== null && !scrollerCollapsed(ctx.scrollerRef.current)
  ) {
    // Compared as the setter commits it (a retained selection widens every
    // write). The re-run ends on `windowRange.end < itemCount` either way.
    const target = resolveWindowRange({ start: windowRange.start, end: itemCount })
    if (windowRange.start !== target.start || windowRange.end !== target.end) setWindowRange(() => target)
  }
  const next = { session: sessionId, count: itemCount, tailKey }
  useLayoutEffect(() => {
    committedRef.current = next
  })
}

/** How many rows above the first visible one may mount on each frame while a
 *  landed older page is staged in; after the last step the full overscan
 *  applies again. A landing re-bases the window onto the reader's rows and then
 *  widens it by the whole overscan above them, and mounting those rows in the
 *  same task as the prepend commit was the one long task a phone fling showed
 *  (7 rows, 51-55 ms, most of it row render, markdown and layout). The rows
 *  are off-screen above the reader, so staging them across frames changes no
 *  pixel the reader sees; the visible rows are never capped. */
export const LANDING_LEADING_STEPS: readonly number[] = [1, 3]

export interface WindowOperations {
  recomputeWindow: (expandOnly?: boolean) => void
  lastRecomputeAtRef: Ref<number>
  /** Rows above the first visible one a recompute may mount, or null for the
   *  full overscan. Armed by `stageLeadingOverscan`. */
  leadingCapRef: Ref<number | null>
  /** Mount the overscan above the reader over the next frames instead of in
   *  this task (see LANDING_LEADING_STEPS). */
  stageLeadingOverscan: () => void
  mountIndex: (index: number, opts?: { unionOnly?: boolean }) => boolean
  estimateRowTop: (index: number) => number | null
}

export function useWindowOperations<T>(ctx: {
  overscan: number
  getH: HeightGetter
  scrollerRef: RefObject<HTMLDivElement | null>
  leadingOffset: (el: HTMLElement) => number
  itemsRef: Ref<T[]>
  heightIndexRef: Ref<HeightIndex | null>
  view: Pick<WindowState, 'setWindowRange' | 'windowRangeRef'>
  follow: Pick<FollowState, 'stickRef'>
}): WindowOperations {
  const { overscan, getH, scrollerRef, leadingOffset, itemsRef, heightIndexRef } = ctx
  const { setWindowRange, windowRangeRef } = ctx.view
  const { stickRef } = ctx.follow

  // Written by every recomputeWindow entry; read only by the coverage
  // watchdog's yield check (see VIEWPORT_COVERAGE_YIELD_MS).
  const lastRecomputeAtRef = useRef<number>(Number.NEGATIVE_INFINITY)
  const leadingCapRef = useRef<number | null>(null)
  const stageRafRef = useRef<number | null>(null)

  // ---- Window recomputation (pure; never touches scrollTop) ----
  //
  // `expandOnly` (used by the ResizeObserver path) unions the computed window
  // with the current one so a height change can only MOUNT more rows, never
  // unmount. This breaks a stationary 2-cycle thrash: an animated/auto-height
  // widget at the window's bottom edge would otherwise be unmounted by an RO
  // recompute, immediately remount (rebuild its iframe → re-report a slightly
  // different height), and flip the boundary back — forever, never letting the
  // height (and thus the offset memos) settle. Only an actual SCROLL recompute
  // (full, can shrink) unmounts rows, so once a boundary widget is mounted it
  // stays mounted, its height stabilizes, and the flip stops.
  const recomputeWindow = useCallback((expandOnly = false) => {
    const el = scrollerRef.current
    if (!el) return
    lastRecomputeAtRef.current = performance.now()
    const count = itemsRef.current.length
    const idx = heightIndexRef.current
    // Window bounds in O(log N) via the OffsetIndex prefix-sum tree rather than
    // the O(N) computeWindow linear scan — this is the per-rAF scroll hot path.
    // Fall back to computeWindow only if the tree is somehow absent.
    let next: { start: number; end: number }
    if (count <= 0) {
      next = { start: 0, end: 0 }
    } else if (idx) {
      const overscanN = Math.max(0, Math.floor(overscan))
      if (stickRef.current) {
        // FOLLOWING = reading the tail, so derive the window from the tree's
        // OWN tail instead of mapping scrollTop through it. During streaming
        // the bottom pin writes scrollTop against live DOM geometry while the
        // tree's prices legitimately lag (measurements land in batches);
        // mapping one coordinate system through the other lands mid-tree and
        // unmounts the very rows being streamed — the viewport sits on spacer
        // skeleton until measurements catch up, then content snaps back (the
        // "jumps + skeleton while streaming" defect). Anchoring at the tail is
        // internally consistent: the last row is always mounted and the window
        // extends upward one viewport + overscan, in tree coordinates only.
        const viewTop = Math.max(0, idx.totalHeight() - Math.max(0, el.clientHeight))
        next = {
          start: Math.max(0, idx.indexAt(viewTop) - overscanN),
          end: count,
        }
      } else {
        // Convert the scroller's scrollTop into LIST content coordinates before
        // asking the offset tree: content above the list (page header, toolbars
        // — see leadingOffset) is not the tree's to know about.
        const lead = leadingOffset(el)
        const top = Math.max(0, el.scrollTop - lead)
        const bottom = top + Math.max(0, el.clientHeight)
        const firstVisible = idx.indexAt(top)
        const lastVisible = idx.indexAt(bottom)
        next = {
          start: Math.max(0, firstVisible - overscanN),
          end: Math.min(count, lastVisible + 1 + overscanN),
        }
        const cap = leadingCapRef.current
        if (cap !== null && cap < overscanN) {
          // Staged landing: mount at most `cap` rows above the reader now. A
          // row above that is already mounted stays mounted -- unmounting it
          // only to mount it again a frame later is the cost being split.
          const cappedStart = Math.max(0, firstVisible - cap)
          const fullStart = next.start
          const end = next.end
          setWindowRange((prev) => mergeWindowRange(prev, {
            start: Math.min(cappedStart, Math.max(fullStart, prev.start)),
            end,
          }, expandOnly))
          return
        }
      }
    } else {
      next = computeWindow(Math.max(0, el.scrollTop - leadingOffset(el)), el.clientHeight, count, getH, overscan)
    }
    // No anchor capture here. An upward shift is compensated from the
    // render-phase capture keyed on the range actually moving up (TRIGGER 2),
    // which cannot strand an anchor when this recompute's own update is merged
    // away to a no-op.
    setWindowRange((prev) => mergeWindowRange(prev, next, expandOnly))
  }, [getH, overscan, scrollerRef, leadingOffset, itemsRef, heightIndexRef, stickRef, setWindowRange])

  // Each step lands on a LATER frame: a frame callback requested from this
  // task runs before this frame paints, so the step waits for the callback
  // after it. A new landing restarts the stages from the first.
  // A step reads the CURRENT recompute: its identity changes as heights are
  // measured, and the stages outlive the render that armed them.
  const recomputeRef = useRef(recomputeWindow)
  useEffect(() => { recomputeRef.current = recomputeWindow }, [recomputeWindow])
  const stageLeadingOverscan = useCallback(() => {
    if (LANDING_LEADING_STEPS.length === 0 || typeof requestAnimationFrame !== 'function') return
    if (stageRafRef.current !== null) cancelAnimationFrame(stageRafRef.current)
    let step = 0
    leadingCapRef.current = LANDING_LEADING_STEPS[0]
    const nextFrame = (fn: () => void) => {
      stageRafRef.current = requestAnimationFrame(() => {
        stageRafRef.current = requestAnimationFrame(() => { stageRafRef.current = null; fn() })
      })
    }
    const advance = () => {
      step++
      leadingCapRef.current = step < LANDING_LEADING_STEPS.length ? LANDING_LEADING_STEPS[step] : null
      recomputeRef.current(true)
      if (leadingCapRef.current !== null) nextFrame(advance)
    }
    nextFrame(advance)
  }, [])
  useEffect(() => () => {
    if (stageRafRef.current !== null) cancelAnimationFrame(stageRafRef.current)
    stageRafRef.current = null
    leadingCapRef.current = null
  }, [])

  // Ensure `index` is mounted (in the window) so callers can scroll to an
  // off-window target. Near targets union with the current window (no flash);
  // far targets jump (replace) to avoid mounting thousands of rows in between.
  //
  // Returns `true` when the target is FAR. By default the window is then
  // REPLACED, leaving an unmounted gap between the old viewport and the target,
  // and callers should teleport (instant) rather than glide a native smooth
  // scroll through blank spacer. With `unionOnly` a far target is left alone —
  // nothing is mounted and the window stays where the reader is — for a caller
  // that drives the scroll itself frame by frame (a converging glide): the
  // scroll listener's `recomputeWindow` then follows each write, mounting rows
  // as the viewport reaches them, exactly as it does under a fling. Replacing
  // the window first would blank the rows under the reader for the frame before
  // the first write pulls the window back.
  const mountIndex = useCallback(
    (index: number, opts?: { unionOnly?: boolean }): boolean => {
      const count = itemsRef.current.length
      if (count === 0) return false
      const t = Math.max(0, Math.min(count - 1, Math.floor(index)))
      const jump = computeJumpWindow(t, count, overscan)
      // Decide near/far from the latest committed window (ref, not `prev`) so
      // we can return the decision synchronously to the caller.
      const cur = windowRangeRef.current
      const far = !jumpIsNear(jump, cur, overscan)
      if (far && opts?.unionOnly) return true
      setWindowRange((prev) => {
        const near = jumpIsNear(jump, prev, overscan)
        if (near) return { start: Math.min(prev.start, jump.start), end: Math.max(prev.end, jump.end) }
        return jump
      })
      return far
    },
    [overscan, itemsRef, windowRangeRef, setWindowRange],
  )

  // Scroller-coordinate top of row `index` from the height index alone, so a
  // caller can steer toward a row that is NOT mounted. Rows above it that are
  // still unmeasured contribute their estimate, so the value refines as the
  // viewport passes them and they measure in — a caller that re-reads it every
  // frame converges on the true position; one that reads it once lands on the
  // estimate. `leadingOffset` is the chrome between the scroller's content
  // origin and the list's first row, which the index does not know about.
  const estimateRowTop = useCallback(
    (index: number): number | null => {
      const el = scrollerRef.current
      const count = itemsRef.current.length
      if (!el || count === 0) return null
      const t = Math.max(0, Math.min(count - 1, Math.floor(index)))
      const idxTree = heightIndexRef.current
      const off = idxTree ? idxTree.offsetOf(t) : getOffsetFn(t, count, getH)
      return leadingOffset(el) + off
    },
    [getH, leadingOffset, scrollerRef, itemsRef, heightIndexRef],
  )

  return { recomputeWindow, lastRecomputeAtRef, leadingCapRef, stageLeadingOverscan, mountIndex, estimateRowTop }
}

export function useCoverageWatchdog<T>(ctx: {
  scrollerEl: HTMLDivElement | null
  leadingOffset: (el: HTMLElement) => number
  itemsRef: Ref<T[]>
  heightIndexRef: Ref<HeightIndex | null>
  windowRangeRef: Ref<WindowRange>
  ops: Pick<WindowOperations, 'recomputeWindow' | 'lastRecomputeAtRef'>
  follow: Pick<FollowState, 'stickRef' | 'lastHardInputAtRef'>
  pinning: Pick<Pinning, 'forcePin'>
}): void {
  const { scrollerEl, leadingOffset, itemsRef, heightIndexRef, windowRangeRef } = ctx
  const { recomputeWindow, lastRecomputeAtRef } = ctx.ops
  const { stickRef, lastHardInputAtRef } = ctx.follow
  const { forcePin } = ctx.pinning

  // ---- Viewport-coverage watchdog (see VIEWPORT_COVERAGE_TICK_MS) ----
  useEffect(() => {
    const el = scrollerEl
    if (!el) return
    const id = window.setInterval(() => {
      // Somebody responsible ran recently: the event-driven paths are alive
      // and their pricing may legitimately trail the DOM mid-stream. Stand
      // down (see VIEWPORT_COVERAGE_YIELD_MS).
      if (performance.now() - lastRecomputeAtRef.current < VIEWPORT_COVERAGE_YIELD_MS) return
      const idx = heightIndexRef.current
      const count = itemsRef.current.length
      if (!idx || count <= 0) return
      const lead = leadingOffset(el)
      const top = Math.max(0, el.scrollTop - lead)
      const bottom = top + Math.max(0, el.clientHeight)
      const { start, end } = windowRangeRef.current
      // The mounted window's pixel span in list coordinates. An empty window
      // has zero span and is uncovered by construction.
      const spanTop = idx.offsetOf(start)
      const spanBottom = end > start ? idx.offsetOf(end - 1) + idx.getHeight(end - 1) : spanTop
      // A side the window has already reached the list's end on is covered by
      // construction: there is no row left to mount there. Without this the
      // chrome that shares the scroller with the rows -- the tail spacer and
      // bottom sentinel below the last row, the paging bar above the first --
      // sits inside the viewport whenever the reader is at an end, reads as
      // uncovered pixels past the span, and made this fire forcePin every tick
      // for a reader parked at the bottom: a same-value scrollTo each 500ms
      // that re-armed follow the idle rule had just released and, on WebKit,
      // cut every rubber-band and momentum tail short.
      const uncoveredAbove = start > 0 && top < spanTop - VIEWPORT_COVERAGE_SLACK_PX
      const uncoveredBelow = end < count && bottom > spanBottom + VIEWPORT_COVERAGE_SLACK_PX
      if (uncoveredAbove || uncoveredBelow) {
        // Recovery is stick-aware. FOLLOWING: the window is tail-anchored, so
        // remounting rows at the displaced position would endorse a position
        // the reader never chose — force-pin back to the bottom (stick is the
        // authoritative bottom truth). forcePin, not pinAuto: the watchdog
        // only fires in dead air (no events for the whole yield window), so
        // the displacement cannot be the user's — but evaluateAutoPin would
        // read the displaced scrollTop as a user scroll-up and RELEASE follow.
        // RELEASED: the reader owns the position; re-cover it where it lies.
        //
        // Settle gate as on the pre-paint re-pin: forcePin RE-ARMS follow, so
        // firing it while the reader's own upward gesture is still in flight
        // (stick not yet released) would both teleport them to the end AND
        // re-engage following. Recovering in place is the safe reading in that
        // window — the position is covered either way.
        const gestureInFlight = performance.now() - lastHardInputAtRef.current < SCROLL_SETTLE_MS
        if (stickRef.current && !gestureInFlight) forcePin()
        else recomputeWindow()
      }
    }, VIEWPORT_COVERAGE_TICK_MS)
    return () => window.clearInterval(id)
  }, [
    scrollerEl, recomputeWindow, leadingOffset, forcePin,
    lastRecomputeAtRef, heightIndexRef, itemsRef, windowRangeRef, stickRef, lastHardInputAtRef,
  ])
}

/** The window's two edges: the row-index older-history trigger and the
 *  sentinel IntersectionObservers that widen the mounted window. */
export function useWindowEdgeTriggers<T>(ctx: {
  windowRange: WindowRange
  itemCount: number
  overscan: number
  prefetchStartIndex: number | undefined
  sessionId: string
  scrollerRef: RefObject<HTMLDivElement | null>
  scrollerEl: HTMLDivElement | null
  topSentinelRef: RefObject<HTMLDivElement>
  bottomSentinelRef: RefObject<HTMLDivElement>
  onTopReachedRef: Ref<(() => void) | undefined>
  itemsRef: Ref<T[]>
  setWindowRange: SetWindowRange
  ops: Pick<WindowOperations, 'leadingCapRef'>
}): void {
  const {
    windowRange, itemCount, overscan, prefetchStartIndex, sessionId,
    scrollerRef, scrollerEl, topSentinelRef, bottomSentinelRef, onTopReachedRef, itemsRef, setWindowRange,
  } = ctx
  const { leadingCapRef } = ctx.ops

  // ---- Row-count prefetch: fire the older-history fetch EARLY ----
  // The sentinel below still exists as the backstop (a fast fling can outrun
  // any lead), but this is the primary trigger. `handleTopReached` guards on
  // loadingOlder/slotHasMore, so a fire here is at most one request.
  // Fires on the DOWNWARD CROSSING of the row lead, not on being inside it:
  // a chat opens at the tail (start large) and only genuine upward travel
  // crosses the boundary, while a session opened at the head mounts AT
  // start 0 and never crosses — so opening a short transcript cannot fetch a
  // page nobody approached. A landing re-bases start far upward, re-arming
  // the crossing for the next page. (A user-scroll timestamp gate was tried
  // first: the scroll listener's attach-time synthetic onScroll() stamps it
  // at mount, so it never gated anything.)
  const prevWindowStartRef = useRef<number | null>(null)
  /** Scroller height as of the last window-crossing evaluation. Its own ref:
   *  the other viewport baselines are advanced by the scroll handler and the
   *  resize observer, which run on different schedules than this effect. */
  const topCrossingClientHRef = useRef(0)
  /** Session this effect's baselines belong to. `windowRange.start` is only
   *  comparable against a PREVIOUS value from the SAME transcript: on a session
   *  switch the new transcript's window starts wherever its own bounded page
   *  puts it, and comparing that against the outgoing session's residual value
   *  manufactures a downward crossing with nobody having scrolled — which
   *  admits an older-history fetch at the instant of entry. Re-baseline and
   *  skip, exactly as the viewport-growth cause below does. */
  const topCrossingSessionRef = useRef<string | null>(null)
  useEffect(() => {
    const prev = prevWindowStartRef.current
    prevWindowStartRef.current = windowRange.start
    const prevSession = topCrossingSessionRef.current
    topCrossingSessionRef.current = sessionId
    if (prevSession !== sessionId) {
      topCrossingClientHRef.current = scrollerRef.current?.clientHeight ?? 0
      return
    }
    if (itemCount === 0 || prev === null) return
    // A scroller with no laid-out height cannot have a reader travelling in
    // it: zero-layout environments (jsdom, a hidden pane) collapse the window
    // to start 0 at mount, and firing there would fetch a page on open.
    const el = scrollerRef.current
    if (!el || el.clientHeight <= 0) return
    // `windowRange.start` decreasing has TWO causes and only one is a request for
    // history: the reader travelling upward, and the viewport GROWING, which makes
    // the window extend upward to fill the taller box. Closing the soft keyboard
    // grows it by the keyboard's whole height (~300px on a phone), which mounts
    // several rows above and walks `start` down across the lead all by itself —
    // reported from a real device as history loading on keyboard dismissal, in any
    // language. Skip the crossing and re-baseline: `prevWindowStartRef` is already
    // advanced above, and a genuine climb crosses again on the next evaluation.
    const prevCh = topCrossingClientHRef.current
    topCrossingClientHRef.current = el.clientHeight
    if (prevCh > 0 && el.clientHeight > prevCh + 1) return
    const lead = prefetchStartIndex ?? OLDER_PREFETCH_START_ROWS
    if (prev > lead && windowRange.start <= lead) {
      onTopReachedRef.current?.()
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- trigger set is deliberate; the rest is read through refs
  }, [windowRange.start, itemCount, prefetchStartIndex, sessionId])

  // ---- IntersectionObserver: top/bottom sentinels for window expansion ----
  useEffect(() => {
    const root = scrollerEl
    if (!root) return
    if (typeof IntersectionObserver === 'undefined') return

    // TWO observers: `rootMargin` is per-observer, and the two sentinels race
    // different things (network fetch vs local window expansion).
    const ioTop = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue
          if (entry.target !== topSentinelRef.current) continue
          // Upward expansion mounts rows above the viewport, and TRIGGER 2
          // compensates it from the render phase. Nothing to capture here:
          // at start === 0 expandWindowUp is a no-op, and keying the capture
          // on the committed range moving up makes that case a non-event
          // instead of something this site has to screen for.
          // A staged landing widens by its current step, not the overscan.
          const cap = leadingCapRef.current
          setWindowRange((prev) => expandWindowUp(prev, cap === null ? overscan : Math.max(1, Math.min(overscan, cap))))
          onTopReachedRef.current?.()
        }
      },
      { root, rootMargin: `${OLDER_PREFETCH_MARGIN_PX}px 0px` },
    )
    const ioBottom = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue
          if (entry.target !== bottomSentinelRef.current) continue
          setWindowRange((prev) => expandWindowDown(prev, itemsRef.current.length, overscan))
        }
      },
      { root, rootMargin: `${WINDOW_EXPAND_MARGIN_PX}px 0px` },
    )

    if (topSentinelRef.current) ioTop.observe(topSentinelRef.current)
    if (bottomSentinelRef.current) ioBottom.observe(bottomSentinelRef.current)
    return () => { ioTop.disconnect(); ioBottom.disconnect() }
  }, [overscan, scrollerEl, topSentinelRef, bottomSentinelRef, onTopReachedRef, itemsRef, setWindowRange, leadingCapRef])
}
