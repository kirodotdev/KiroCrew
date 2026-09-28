// Observer lifecycle for the chat virtualizer.
//
// Owns the scroller element and the registry of mounted row elements, and
// registers, orders and tears down the DOM observers that read them: the
// passive scroll listener with its hardware-intent listeners, and the shared
// ResizeObserver over the rows and the scroller's own box. The callbacks only
// ROUTE: what a scroll event means for follow is the follow policy's
// (followPolicy.ts), what a resize measured is measurement's, and what the
// measurement then triggers is the compensation's, the geometry scheduler's
// and follow's -- called here in a fixed order. The sentinel
// IntersectionObservers are the window's own edge triggers (windowRange.ts).

import { useCallback, useEffect, useMemo, useRef, useState, type MutableRefObject, type RefObject } from 'react'
import { attachUserScrollIntent } from '../../utils/searchScroll'
import { computeAtBottom, isSelfScroll } from './FollowController'
import { noteUserScrollActivity } from '../../lib/scrollQuiet'
import { devWatchScroller } from '../../dev/scrollInspector'
import type { FollowState, Pinning } from './followPolicy'
import type { GeometrySync } from './geometryScheduling'
import type { RowMeasurement } from './measurement'
import type { ReadingPositionEntry } from './readingPosition'
import type { ShiftCompensation } from './shiftCompensation'
import type { WindowOperations } from './windowRange'

type Ref<V> = MutableRefObject<V>

export interface ScrollerElement {
  scrollerRef: RefObject<HTMLDivElement | null>
  contentRef: RefObject<HTMLDivElement>
  topSentinelRef: RefObject<HTMLDivElement>
  bottomSentinelRef: RefObject<HTMLDivElement>
  leadingOffset: (el: HTMLElement) => number
  /** The scroller node as state, so the element-keyed observers re-attach. */
  scrollerEl: HTMLDivElement | null
  /** Promote the current scroller node to state (run after every commit). */
  syncScrollerEl: () => void
  /** Element → index registry of the mounted rows. */
  elIndexRef: Ref<Map<Element, number>>
  /** The one shared ResizeObserver, while it exists. */
  resizeObserverRef: Ref<ResizeObserver | null>
}

export function useScrollerElement(
  externalScrollerRef: RefObject<HTMLDivElement | null> | RefObject<HTMLDivElement> | undefined,
): ScrollerElement {
  // ---- DOM refs ----
  const internalScrollerRef = useRef<HTMLDivElement | null>(null)
  // Stable RefObject identity: memoized on `externalScrollerRef` so it only
  // changes when the caller swaps the external ref (never on ordinary
  // re-renders). Keeping the identity stable lets every owner's callbacks and
  // effects list `scrollerRef` in their deps without recreating on every render
  // (which would re-attach the scroll/Resize/Intersection observers each frame).
  const scrollerRef = useMemo(
    () => (externalScrollerRef ?? internalScrollerRef) as React.RefObject<HTMLDivElement | null>,
    [externalScrollerRef],
  )
  const contentRef = useRef<HTMLDivElement>(null)
  const topSentinelRef = useRef<HTMLDivElement>(null)
  const bottomSentinelRef = useRef<HTMLDivElement>(null)

  // ---- Leading offset: px from the scroller's scroll origin to the start of
  // list content. In the chat transcript the list IS the scroller's content,
  // so this is 0 and every scrollTop↔offset conversion below is exact. A
  // caller windowing against a shared page column (externalScrollerRef) can
  // have arbitrary non-list content ABOVE the list — page header, toolbars —
  // and treating raw scrollTop as a list offset then shifts the whole window
  // by that height: rows unmount while still visible and remount late, at the
  // same scroll positions every time. The caller-side glide already derives
  // exactly this correction (its `headerPx`) from a mounted row; this is the
  // same quantity for the hot path, read from the list container itself.
  //
  // Measured lazily per call rather than observed: getBoundingClientRect on
  // two elements is cheap, the value only changes when leading content
  // resizes, and a stale cached value would reintroduce the shifted-window
  // bug it exists to fix. Prefers the caller's list container (the parent of
  // the top sentinel — LibraryList's own wrapper) and falls back to 0 when
  // geometry is unavailable (jsdom, detached nodes), which restores today's
  // chat behavior exactly.
  const leadingOffset = useCallback((el: HTMLElement): number => {
    const anchor = topSentinelRef.current
    if (!anchor || typeof anchor.getBoundingClientRect !== 'function' || typeof el.getBoundingClientRect !== 'function') return 0
    const a = anchor.getBoundingClientRect()
    const s = el.getBoundingClientRect()
    // Degenerate rects (jsdom reports all-zero) resolve to 0 with a zero
    // scrollTop — harmless. Real geometry: distance from the scroll origin
    // (viewport top + scrollTop) down to the sentinel, clamped so a mid-list
    // sentinel mismeasure can never produce a negative offset.
    return Math.max(0, a.top - s.top + el.scrollTop)
  }, [])

  // The scroller node, promoted to state so the observer effects (scroll
  // listener / ResizeObserver / IntersectionObserver) RE-ATTACH whenever the
  // element mounts or changes. The scroller (or an ancestor) can be rendered
  // AFTER our first commit — conditional loaders, route transitions, etc. —
  // and refs don't trigger effect re-runs, so effects keyed only on mount
  // would silently never attach (frozen isAtBottom, no follow, no window
  // recompute during scroll). `syncScrollerEl` below keeps this in step.
  const [scrollerEl, setScrollerEl] = useState<HTMLDivElement | null>(null)
  const syncScrollerEl = useCallback(() => {
    setScrollerEl((prev) => (prev === scrollerRef.current ? prev : scrollerRef.current))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // One shared ResizeObserver; Element → index map resolves heights cheaply.
  const elIndexRef = useRef<Map<Element, number>>(new Map())
  const resizeObserverRef = useRef<ResizeObserver | null>(null)

  return {
    scrollerRef,
    contentRef,
    topSentinelRef,
    bottomSentinelRef,
    leadingOffset,
    scrollerEl,
    syncScrollerEl,
    elIndexRef,
    resizeObserverRef,
  }
}

export function useScrollListener<T>(ctx: {
  scrollerEl: HTMLDivElement | null
  bottomThreshold: number
  setIsAtBottom: (update: (prev: boolean) => boolean) => void
  itemsRef: Ref<T[]>
  getKeyRef: Ref<(item: T, index: number) => string>
  follow: Pick<FollowState, 'smoothPinActiveRef' | 'lastWriteTopRef' | 'lastObservedTopRef' | 'noteHardInput'>
  pinning: Pick<Pinning, 'onFollowScroll'>
  reading: Pick<ReadingPositionEntry<T>, 'lastScrollCtxRef' | 'sessionIdRef' | 'scheduleAnchorSave'>
  ops: Pick<WindowOperations, 'recomputeWindow'>
}): void {
  const { scrollerEl, bottomThreshold, setIsAtBottom, itemsRef, getKeyRef } = ctx
  const { smoothPinActiveRef, lastWriteTopRef, lastObservedTopRef, noteHardInput } = ctx.follow
  const { onFollowScroll } = ctx.pinning
  const { lastScrollCtxRef, sessionIdRef, scheduleAnchorSave } = ctx.reading
  const { recomputeWindow } = ctx.ops

  // ---- Passive scroll listener: isAtBottom + user-scroll stick update ----
  const scrollRafScheduledRef = useRef(false)
  useEffect(() => {
    const el = scrollerEl
    if (!el) return
    let rafId = 0
    const onScroll = () => {
      const geom = { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }
      // Quiescence signal for the older-page flush hold (scrollQuiet.ts).
      // Self-scroll pin writes are excluded: our own corrections must not
      // hold a fetched page hostage -- only the READER's activity defers it.
      if (!smoothPinActiveRef.current && !isSelfScroll(geom.scrollTop, lastWriteTopRef.current)) {
        noteUserScrollActivity()
      }
      const atBottom = computeAtBottom(geom, bottomThreshold)
      setIsAtBottom((prev) => {
        if (prev === atBottom) return prev
        return atBottom
      })
      // Only a genuine USER scroll updates stick -- the follow policy's
      // decision, including the smooth-glide guard and the direction baseline.
      onFollowScroll(el, geom)
      // Persist the reading position once this scroll burst settles (also
      // clears it when the burst ends at the bottom). Scheduled for self-
      // scrolls too — see scheduleAnchorSave. The context snapshot is what
      // lets a slot switch inside the debounce window flush this burst
      // against the OUTGOING session's items (see the session sentinel).
      devWatchScroller(el, itemsRef.current.length)
      lastScrollCtxRef.current = {
        session: sessionIdRef.current,
        items: itemsRef.current,
        getKey: getKeyRef.current,
      }
      scheduleAnchorSave()
      if (!scrollRafScheduledRef.current) {
        scrollRafScheduledRef.current = true
        rafId = requestAnimationFrame(() => {
          scrollRafScheduledRef.current = false
          recomputeWindow()
        })
      }
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    // A fresh element has no direction history — do not measure its first user
    // scroll against a previous scroller's position.
    lastObservedTopRef.current = -1
    // Persistent input-intent listeners (wheel / touch / scrollbar grab /
    // scrolling keys). They only bump the settle timestamp — the stick decision
    // itself stays with the scroll handler above. This closes a race the scroll
    // event cannot: input lands BEFORE its scroll event dispatches, so an RO
    // tick between the two saw a stale "settled" timestamp and pinned against
    // the gesture (fighting a trackpad fling frame by frame). Suppression is
    // harmless when the input does not scroll (a click, a wheel at the bottom):
    // follow resumes SCROLL_SETTLE_MS later.
    const detachIntent = attachUserScrollIntent(el, noteHardInput)
    onScroll()
    return () => {
      el.removeEventListener('scroll', onScroll)
      detachIntent()
      // Cancel any frame queued by the last scroll so it can't fire a
      // setWindowRange after unmount/re-run. Reset the ref too, or a re-run
      // would see it stuck true and never schedule again.
      if (rafId) cancelAnimationFrame(rafId)
      scrollRafScheduledRef.current = false
    }
  }, [
    scrollerEl, bottomThreshold, onFollowScroll, noteHardInput, recomputeWindow, scheduleAnchorSave,
    smoothPinActiveRef, lastWriteTopRef, lastObservedTopRef, lastScrollCtxRef, sessionIdRef, itemsRef, getKeyRef,
    setIsAtBottom,
  ])
}

export function useResizeObserver(ctx: {
  scrollerRef: RefObject<HTMLDivElement | null>
  scrollerEl: HTMLDivElement | null
  elIndexRef: Ref<Map<Element, number>>
  resizeObserverRef: Ref<ResizeObserver | null>
  measurement: Pick<RowMeasurement, 'measureResizeEntries'>
  compensation: Pick<ShiftCompensation, 'compensateAboveFold'>
  sync: Pick<GeometrySync, 'deferForRailSettle' | 'scheduleResizeSync' | 'cancelRailSettle'>
  pinning: Pick<Pinning, 'followResizeBatch'>
  ops: Pick<WindowOperations, 'recomputeWindow'>
}): void {
  const { scrollerRef, scrollerEl, elIndexRef, resizeObserverRef } = ctx
  const { measureResizeEntries } = ctx.measurement
  const { compensateAboveFold } = ctx.compensation
  const { deferForRailSettle, scheduleResizeSync, cancelRailSettle } = ctx.sync
  const { followResizeBatch } = ctx.pinning
  const { recomputeWindow } = ctx.ops

  // ---- ResizeObserver: track mounted-item heights + follow streaming/widgets ----
  // Native overflow-anchor handles visual stability when scrolled up; this
  // callback (a) feeds the height cache and (b) re-pins to the bottom while
  // following (pinAuto is race-proof, so a late widget load can't yank a user
  // who scrolled up).
  useEffect(() => {
    if (typeof ResizeObserver === 'undefined') return
    let scheduled = false
    let rafId = 0
    const ro = new ResizeObserver((entries) => {
      const el = scrollerRef.current
      if (!el) return

      // Record every measurement and classify the fire, then act on it in this
      // order: hold a released reader against a reprice above them; let the
      // rail-collapse window take the fire whole; follow tail growth; schedule
      // the height sync the measurements owe.
      const batch = measureResizeEntries(entries, el)
      compensateAboveFold(el, batch.aboveFoldReprice)
      if (deferForRailSettle(batch)) return
      followResizeBatch(batch)
      scheduleResizeSync(batch)

      // Coalesce cascading resizes into one window recompute next frame.
      // Expand-only: a height change must not unmount rows (see recomputeWindow).
      if (!scheduled) {
        scheduled = true
        rafId = requestAnimationFrame(() => {
          scheduled = false
          recomputeWindow(true)
        })
      }
    })
    resizeObserverRef.current = ro
    // Back-fill rows that registered before this observer existed. Row ref
    // callbacks run in the COMMIT phase, this effect runs after paint, so any
    // row mounted in the same commit reached `measureRef` while
    // `resizeObserverRef` was still null and its `ro?.observe` was a no-op.
    // `measureRef` returns a STABLE per-index callback (so a row that stays
    // mounted never churns observe/unobserve), which means React will not
    // re-invoke it — without this pass such a row is never measured again and
    // its streaming growth never reaches the follow pin. `elIndexRef` holds
    // exactly the currently-mounted rows (the null-element branch deletes on
    // unmount), so iterating it cannot resurrect a detached node.
    for (const el of elIndexRef.current.keys()) ro.observe(el)
    // Observe the scroller's own box (the viewport branch of measureResizeEntries) — after the
    // rows, so row-position assumptions about observation order keep holding.
    // A re-created observer must re-observe it here; the `scrollerEl` effect
    // below covers a scroller that mounts later than this effect.
    if (scrollerRef.current) ro.observe(scrollerRef.current)
    return () => {
      ro.disconnect()
      // Cancel a frame queued by the last resize so it can't fire a
      // setWindowRange after the observer is torn down.
      if (rafId) cancelAnimationFrame(rafId)
      cancelRailSettle()
      resizeObserverRef.current = null
    }
  }, [
    scrollerRef, measureResizeEntries, compensateAboveFold, deferForRailSettle, followResizeBatch,
    scheduleResizeSync, cancelRailSettle, recomputeWindow, elIndexRef, resizeObserverRef,
  ])

  // Late-mounting scroller: the RO effect above observes `scrollerRef.current`
  // at setup, but a scroller (or an ancestor) rendered AFTER that effect ran
  // would never be observed — same rationale as the `scrollerEl` state for the
  // scroll/IO listeners. observe() is idempotent, so the overlap with the
  // setup-time observe is harmless.
  useEffect(() => {
    const el = scrollerEl
    if (!el) return
    resizeObserverRef.current?.observe(el)
    return () => { resizeObserverRef.current?.unobserve(el) }
  }, [scrollerEl, resizeObserverRef])
}
