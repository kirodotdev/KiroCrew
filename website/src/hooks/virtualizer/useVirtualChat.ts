// useVirtualChat — measurement-first chat virtualizer hook.
//
// A narrow React facade. It adapts the caller's options, holds the row-identity
// vocabulary (getKey / getStableId / getAltId), and composes the owners below
// in the ORDER React must run them. Each owner holds one responsibility:
//
//   windowRange.ts         the mounted window as the reader scrolls: recompute,
//                          jump rule, sentinels, watchdog, older-history trigger
//   measurement.ts         the per-scope HeightIndex, the geometry read from it,
//                          and every writer of a measurement
//   geometryScheduling.ts  when a measurement becomes geometry: debounce,
//                          deferral, streaming grace, rail-collapse settle
//   observers.ts           the scroller element, the mounted-row registry, the
//                          scroll listener and the shared ResizeObserver
//   shiftCompensation.ts   holding a scrolled-up reader still while the height
//                          above them changes (capture in render, consume
//                          pre-paint)
//   readingPosition.ts     the persisted reading position: entry latch, save,
//                          leave flush, visibility re-placement, restore + settle
//   followPolicy.ts        follow / pin / reader intent, and the one chokepoint
//                          through which every scrollTop write passes
//
// The helpers they share, none of which holds React state: FollowController.ts
// (follow and position-owner decisions), WindowCalculator.ts (window math),
// HeightIndex.ts over HeightCache.ts (height truth and its persistence),
// ScrollAnchorCache.ts (anchor persistence) and anchorGeometry.ts (reader-row
// DOM geometry).
//
// PHASE ORDER
// ===========
// React runs a component's render-phase code, its layout effects and its
// passive effects each in hook-call order, and several of these owners
// cooperate through that order: the shift capture must read the DOM before
// the height owner reprices the tree in the same render; the compensation's
// pre-paint writes must precede follow's placement pins, which precede the
// slot-entry placement; the scroller-element sync must follow the height
// owner's store subscription. The calls below are in that order, and
// `virtualizerOwnership.test.ts` pins it. Reordering them is a behaviour
// change even where every owner is untouched.
//
// Visual stability while scrolled up (window expansion, async widget resizes
// above the viewport) uses native CSS `overflow-anchor: auto` PLUS an explicit
// anchor-preservation pass: an upward window shift can unmount the very node the
// browser chose as its anchor, which resets anchoring and jumps the viewport, so
// the top visible row's offset is captured before the commit and `scrollTop` is
// compensated after it. The CSS is retained — reliance on it is reduced, not
// replaced.
//
// Render contract for callers:
//   - Wrap the scroll container with `scrollerRef`
//   - Render the items in `virtualItems`: when `item.mounted` is true render
//     the real component wrapped in a div with `ref={measureRef(item.index)}`;
//     when false render a placeholder `<div style={{ height: item.height }} />`
//   - Place `topSentinelRef` / `bottomSentinelRef` at the list ends for
//     window expansion.
//
// WHY THIS IS IN-HOUSE (build-vs-buy — decided, not assumed)
// ==========================================================
// This module re-implements machinery that react-virtuoso and @tanstack/virtual
// ship battle-tested (dynamic measurement, prefix-sum offsets, follow-output
// pinning, anchor stability). Owning it is a deliberate maintainer decision
// rather than a default that accumulated. The chat-specific requirements a
// drop-in library does not cover today:
//   - Widget iframes: rows contain sandboxed iframes that lose all internal
//     state on unmount and rebuild slowly (PROGRAMMATIC_BUILD_DELAY_MS), which
//     is why `isSticky` exists to exempt chosen rows from windowing entirely.
//   - Identity that is not the array index: a steered bubble's `ts` is rewritten
//     by the server echo, so height-cache identity must key on `meta.clientTs`
//     (see ChatPage `stableMsgKey`); a library keyed on index or item identity
//     would orphan the measurement.
//   - Turn regrouping: a `single` row promotes into a grouped `turn` mid-stream,
//     changing row composition without changing the underlying messages.
//   - Cross-session persistence: heights survive in localStorage per session,
//     partitioned by `sessionId`, so a revisit is warm.
// None of these is proven *fundamental* — they are integration costs, not
// impossibilities, so the decision is revisitable and this list is what any
// future migration would have to satisfy. Such a revisit should weigh that
// react-virtuoso is already a dependency serving other virtualized surfaces, so
// the question is convergence between two strategies rather than first-time
// adoption.
//
// The decision carries one obligation, and it is now DISCHARGED. Height truth
// spans the DOM, `HeightCache`, the offset tree and the geometry derived from
// them; it used to stay coherent by convention -- a hand-bumped version counter
// in memo dependency arrays, plus a session guard held separately by the cache
// and by the tree. `HeightIndex` now owns all of it:
//   - it holds the cache and the tree, and is the only surface this hook reads
//     heights through, so the two-readers seam and the duplicated session guard
//     are gone (one guard, and the tree cannot outlive its cache);
//   - it announces a geometry change in the same call that mutates the tree, so
//     the invalidation is subscribed to rather than maintained by hand -- there
//     is no bump site left to forget.

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import type { ScrollAnchor } from './ScrollAnchorCache'
import { getOffset as getOffsetFn, getTotalHeight } from './WindowCalculator'
import { anchorMatchesRow, resolveAnchorRow, DEFAULT_BOTTOM_THRESHOLD } from './FollowController'
import type {
  UseVirtualChatOptions,
  UseVirtualChatReturn,
  VirtualItem,
} from './types'
import { useScrollerElement, useScrollListener, useResizeObserver } from './observers'
import { useFollowState, usePinning, useFollowPlacementPins } from './followPolicy'
import { useWindowState, useWindowOperations, useCoverageWatchdog, useWindowEdgeTriggers } from './windowRange'
import { useShiftCapture, useShiftCompensation } from './shiftCompensation'
import { useReadingPositionEntry, useVisibilityReplacement, useReadingPositionRestore } from './readingPosition'
import { useHeightOwner, useRowMeasurement } from './measurement'
import { useStreamingSettleGrace, useGeometrySync } from './geometryScheduling'

// The hook's public helpers, defined where their rules live and re-exported
// here, the module every caller imports.
export { SCROLL_SETTLE_MS, shiftCompensationAllowed, pinSuppressedNow } from './FollowController'

const DEFAULT_ESTIMATED = 80
const DEFAULT_OVERSCAN = 5

export function useVirtualChat<T>(
  opts: UseVirtualChatOptions<T>,
): UseVirtualChatReturn<T> {
  const {
    items,
    getKey,
    sessionId,
    heightScopeKey,
    estimatedHeight = DEFAULT_ESTIMATED,
    overscan = DEFAULT_OVERSCAN,
    followOutput = true,
    initialPlacement = 'bottom',
    eagerFirstMeasure = false,
    getStableId,
    getAltId,
    prefetchStartIndex,
    bottomThreshold = DEFAULT_BOTTOM_THRESHOLD,
    isSticky,
    externalScrollerRef,
    streamingIndex,
    runActive,
    onTopReached,
  } = opts

  const itemCount = items.length
  // Live ref for the RO callback (a stable-identity effect — see its own
  // deps) so a caller updating `streamingIndex` every render (typical: it
  // tracks "index of the last item while it has role streaming") doesn't
  // force the ResizeObserver to be torn down and reattached.
  const streamingIndexRef = useRef(streamingIndex)
  streamingIndexRef.current = streamingIndex
  // Same reason, same shape: the automatic-pin path is a stable callback, so the
  // live run state reaches it through a ref rather than a dependency.
  const runActiveRef = useRef(runActive)
  runActiveRef.current = runActive
  // Live ref for the same reason: the RO callback and the measureRef factory
  // are stable-identity, so they read the option through a ref.
  const getStableIdRef = useRef(getStableId)
  getStableIdRef.current = getStableId
  const getAltIdRef = useRef(getAltId)
  getAltIdRef.current = getAltId
  const eagerFirstMeasureRef = useRef(eagerFirstMeasure)
  eagerFirstMeasureRef.current = eagerFirstMeasure

  // Same reasoning for the IntersectionObserver effect: keeping the callback in a
  // ref keeps it out of that effect's deps, so it never re-subscribes per render.
  const onTopReachedRef = useRef(onTopReached)
  useEffect(() => {
    onTopReachedRef.current = onTopReached
  }, [onTopReached])

  // Live items array (lets imperative callbacks read current state).
  const itemsRef = useRef(items)
  itemsRef.current = items
  const getKeyRef = useRef(getKey)
  getKeyRef.current = getKey

  // ---- Row identity ----
  //
  // One vocabulary for "which row is this": the display key (getKey) for React
  // reconciliation and the height cache, the STABLE id (getStableId, the row's
  // tail message) for anchors, and the ALT id (getAltId, its lead message) as an
  // anchor's second identity. The owners receive these resolvers rather than
  // re-deriving them.
  /** Anchor-resolution identity: stable id when provided, display key otherwise. */
  const anchorIdOf = useCallback((item: T, index: number): string => {
    const f = getStableIdRef.current
    return f ? f(item, index) : getKeyRef.current(item, index)
  }, [])
  /** The row's LEAD-message id, or null when the caller supplied no alt fn. */
  const altIdAtIndex = useCallback((idx: number): string | null => {
    const fn = getAltIdRef.current
    if (!fn) return null
    const it = itemsRef.current[idx]
    return it ? fn(it, idx) : null
  }, [])

  /** Index of the row whose virtual key matches `key`, or -1. O(N), runs at
   *  most once per slot entry. */
  // A PERSISTED anchor is resolved by the row's STABLE id, never by `getKey`.
  // `getKey` is priced against the displayItems of ONE render (ChatPage indexes
  // a deduped `rowKeys` array), so the same message answers to a different key
  // after a switch re-enters the slot with a different window -- the lookup then
  // misses on every row, the restore falls back to the bottom pin, and the
  // ensuing at-bottom save CLEARS the anchor it failed to use. `getStableId`
  // identifies a row by its TAIL message, which a page landing does not rename.
  // The `getKey` fallback covers a caller that supplies no stable id; such a
  // caller keeps the old behaviour rather than losing anchoring altogether.
  const findAnchorIndex = useCallback((anchor: ScrollAnchor): number => {
    const its = itemsRef.current
    const idFn = getStableIdRef.current
    return resolveAnchorRow({
      count: its.length,
      anchor,
      tailIdAt: (i: number) => (idFn ? idFn(its[i], i) : getKeyRef.current(its[i], i)),
      altIdAt: altIdAtIndex,
    })
  }, [altIdAtIndex])

  /** Does row `index` still answer to `anchor`? The restore's settle loop asks
   *  this every frame, and aborts when hydration has moved another row into
   *  `index`. `rowId` is the stable-id resolution the check compared, returned
   *  for the loop's diagnostic. */
  const anchoredRowIdentity = useCallback((index: number, anchor: ScrollAnchor): { rowId: string | null; matches: boolean } => {
    const it = itemsRef.current[index]
    const idFn = getStableIdRef.current
    const rowId = it ? (idFn ? idFn(it, index) : getKeyRef.current(it, index)) : null
    return { rowId, matches: anchorMatchesRow({ anchor, tailId: rowId, altId: it ? altIdAtIndex(index) : null }) }
  }, [altIdAtIndex])

  // ---- Composition, in phase order (see the header) ----

  const grace = useStreamingSettleGrace(streamingIndex)
  const {
    scrollerRef,
    contentRef,
    topSentinelRef,
    bottomSentinelRef,
    leadingOffset,
    scrollerEl,
    syncScrollerEl,
    elIndexRef,
    resizeObserverRef,
  } = useScrollerElement(externalScrollerRef)
  const follow = useFollowState(followOutput)
  const view = useWindowState(itemCount, overscan, initialPlacement)
  const { windowRange, setWindowRange, windowRangeRef } = view
  // Render phase: TRIGGERS 1-6 capture the reader's row against the PREVIOUS
  // commit's DOM, and plan this commit's height retirements.
  const shift = useShiftCapture({
    items, getKey, sessionId, itemCount, onTopReached, windowRange, windowRangeRef,
    scrollerRef, elIndexRef, itemsRef, getKeyRef, getStableIdRef, anchorIdOf, follow,
  })
  // The committed-window mirror advances at COMMIT, right after the shift
  // capture's own baseline mirror and for the same reason: a discarded
  // concurrent attempt advancing it in render made the committing attempt's
  // trigger-2 comparison run against a range that never reached the screen,
  // silently skipping the capture.
  useLayoutEffect(() => {
    windowRangeRef.current = windowRange
  })

  // isAtBottom is the only render-affecting scroll state we expose (drives the
  // caller's jump-to-bottom pill).
  const [isAtBottom, setIsAtBottom] = useState<boolean>(true)

  // Render phase: latch the entered session's reading position, and flush the
  // outgoing one's on a switch -- before the height owner changes scope.
  const reading = useReadingPositionEntry({
    sessionId, itemCount, overscan, initialPlacement, followOutput, bottomThreshold,
    scrollerRef, elIndexRef, itemsRef, getKeyRef, getStableIdRef, getAltIdRef, altIdAtIndex,
    setWindowRange, setIsAtBottom, follow,
  })
  // Render phase: the per-scope height owner, the retirement drain and the
  // geometry this render reads. Its store subscription registers the first
  // passive effects after the option-ref sync above.
  const heights = useHeightOwner({
    sessionId, heightScopeKey, itemCount, estimatedHeight, itemsRef, getKeyRef, windowRange, shift,
  })
  const { heightIndexRef, heightIndex, getH, offsetIndex, heightCommit, totalHeight, offsetBefore, offsetAfter } = heights

  // Keep the tracked scroller element in sync after every commit, so the
  // observer effects below re-attach the moment the node appears (or changes).
  useEffect(() => {
    syncScrollerEl()
  })

  const ops = useWindowOperations({ overscan, getH, scrollerRef, leadingOffset, itemsRef, heightIndexRef, view, follow })
  const pinning = usePinning({
    followOutput, overscan, scrollerRef, itemsRef, runActiveRef, getH, setWindowRange, follow, reading,
  })
  const sync = useGeometrySync({ itemsRef, eagerFirstMeasureRef, heightIndexRef, shift, follow, pinning, ops })
  const measurement = useRowMeasurement({
    itemsRef, getKeyRef, streamingIndexRef, eagerFirstMeasureRef, elIndexRef, resizeObserverRef,
    heightIndexRef, windowRangeRef, grace, sync,
  })

  // Layout effects, pre-paint: the shift compensation's consumers, then
  // follow's placement pins.
  const compensation = useShiftCompensation({
    itemCount, windowRange, heightCommit, offsetIndex, scrollerRef, elIndexRef, itemsRef, anchorIdOf,
    setWindowRange, shift, follow, pinning, reading, ops,
  })
  useFollowPlacementPins({
    itemCount, overscan, sessionId, scrollerRef, leadingOffset, itemsRef, getKeyRef, setWindowRange, follow, pinning,
  })

  // Passive effects: the DOM observers.
  useVisibilityReplacement({
    sessionId, followOutput, bottomThreshold, overscan, scrollerRef, itemsRef, setWindowRange, reading, follow, pinning,
  })
  useScrollListener({ scrollerEl, bottomThreshold, setIsAtBottom, itemsRef, getKeyRef, follow, pinning, reading, ops })
  useCoverageWatchdog({ scrollerEl, leadingOffset, itemsRef, heightIndexRef, windowRangeRef, ops, follow, pinning })
  useResizeObserver({ scrollerRef, scrollerEl, elIndexRef, resizeObserverRef, measurement, compensation, sync, pinning, ops })
  useWindowEdgeTriggers({
    windowRange, itemCount, overscan, prefetchStartIndex, sessionId, scrollerRef, scrollerEl,
    topSentinelRef, bottomSentinelRef, onTopReachedRef, itemsRef, setWindowRange,
  })

  // The slot-entry placement: the LAST layout effect, after every compensation
  // and pin of the same commit.
  useReadingPositionRestore({
    sessionId, scrollerEl, itemCount, overscan, initialPlacement, followOutput, scrollerRef, elIndexRef,
    itemsRef, getKeyRef, getStableIdRef, heightIndexRef, getH, findAnchorIndex, anchoredRowIdentity,
    setWindowRange, setIsAtBottom, reading, shift, follow, pinning,
  })

  // ---- Recompute window when item count changes ----
  const { recomputeWindow } = ops
  useEffect(() => {
    recomputeWindow()
  }, [itemCount, recomputeWindow])

  // ---- Build virtualItems list ----
  //
  // Only MOUNTED items are emitted. Off-window items are represented by the
  // offsetBefore / offsetAfter spacers, so there is no need to materialise a
  // VirtualItem (string key + height-cache lookup) for every one of N rows on
  // each window shift. On the fast path (no isSticky predicate) this is
  // O(window) ≈ 2*overscan entries instead of O(N); during a fling the window
  // recomputes every few frames, so dropping the per-frame N allocations (and
  // the matching N React children to reconcile) removes a real source of
  // GC-driven jank on long sessions.
  const virtualItems = useMemo<VirtualItem<T>[]>(() => {
    const out: VirtualItem<T>[] = []
    const start = Math.max(0, windowRange.start)
    const end = Math.min(itemCount, windowRange.end)
    const emit = (i: number) => {
      const it = items[i]
      const key = getKey(it, i)
      // readMeasured (promoting): this row is rendering, which is genuine
      // access. The unmeasured fallback stays the FLAT `estimatedHeight` rather
      // than the running mean the offset math uses -- preserved verbatim; the
      // two disagreeing for an unmeasured row is a pre-existing divergence, not
      // something this refactor should quietly change.
      const cached = heightIndex.readMeasured(i)
      const height = cached !== undefined ? Math.max(cached, 1) : estimatedHeight
      out.push({ data: it, index: i, key, mounted: true, height })
    }
    if (!isSticky) {
      // Fast path: only the contiguous mounted window.
      for (let i = start; i < end; i++) emit(i)
      return out
    }
    // isSticky present: a sticky item may live outside the window and must
    // still render (in index order), so fall back to a full scan. Off-window
    // non-sticky items remain omitted (covered by the spacers).
    for (let i = 0; i < itemCount; i++) {
      if ((i >= start && i < end) || isSticky(items[i], i)) emit(i)
    }
    return out
    // `heightIndex` is a real dependency: its identity changes on a session
    // switch, and the emitted placeholder heights must be re-derived from the
    // new session's measurements rather than the previous transcript's.
  }, [
    heightIndex,
    items,
    itemCount,
    windowRange.start,
    windowRange.end,
    getKey,
    estimatedHeight,
    isSticky,
  ])

  // ---- Debug probe (dev builds only, zero behavior change) ----
  // Exposes window.__vcSnapshot() for diagnosing scroll/geometry bugs (e.g.
  // the blank-space-after-jump regression). Call it in devtools the moment the
  // bug is visible to dump live geometry + a cached-vs-DOM height comparison.
  // Install last-mount-wins.
  //
  // DEV ONLY. "Harmless in prod (a single tiny global)" was the earlier pin and
  // it undersold what the global DOES: called, it reports the session id and
  // the whole transcript's shape, and it console.logs and console.tables them
  // unconditionally. That is a diagnostic surface a release build has no reader
  // for, reachable from any page script. `import.meta.env.DEV` is statically
  // replaced at build time, so the probe leaves the production bundle entirely
  // rather than being installed and left unused.
  useEffect(() => {
    if (!import.meta.env.DEV) return
    if (typeof window === 'undefined') return
    const snapshot = () => {
      const el = scrollerRef.current
      const count = itemsRef.current.length
      // Mounted rows: read true DOM height vs what the cache believes.
      // peekMeasured, NOT readMeasured: this probe is a devtools observer and
      // must not perturb the LRU order it is reporting on. (Before the read
      // surface named promotion explicitly, this path promoted -- the one
      // deliberate behaviour change here, devtools-only and unreachable in
      // normal operation.)
      const rows: { index: number; cached: number | undefined; dom: number; delta: number }[] = []
      const hi = heightIndexRef.current
      for (const [node, idx] of elIndexRef.current.entries()) {
        const cached = hi?.peekMeasured(idx)
        const dom = (node as HTMLElement).offsetHeight
        rows.push({ index: idx, cached, dom, delta: dom - (cached ?? estimatedHeight) })
      }
      rows.sort((a, b) => a.index - b.index)
      // How many of ALL items have a real measurement vs fall back to estimate.
      let measured = 0
      for (let i = 0; i < count; i++) {
        if (hi?.peekMeasured(i) !== undefined) measured++
      }
      // Direct children of the scroller (header / spacers / footer) so we can
      // see exactly what occupies space below the last mounted row.
      const children = el
        ? Array.from(el.children).map((c) => ({
            tag: (c as HTMLElement).tagName.toLowerCase(),
            aria: (c as HTMLElement).getAttribute('aria-hidden'),
            h: (c as HTMLElement).offsetHeight,
            cls: (c as HTMLElement).className?.toString().slice(0, 40),
          }))
        : []
      const geom = el
        ? {
            scrollTop: el.scrollTop,
            scrollHeight: el.scrollHeight,
            clientHeight: el.clientHeight,
            distanceFromBottom: el.scrollHeight - el.scrollTop - el.clientHeight,
          }
        : null
      const result = {
        sessionId,
        count,
        measured,
        estimated: count - measured,
        estimatedHeight,
        windowRange: { start: windowRangeRef.current.start, end: windowRangeRef.current.end },
        endIsCount: windowRangeRef.current.end === count,
        offsetBefore: getOffsetFn(windowRangeRef.current.start, count, getH),
        offsetAfter: Math.max(0, getTotalHeight(count, getH) - getOffsetFn(windowRangeRef.current.end, count, getH)),
        totalHeight: getTotalHeight(count, getH),
        geom,
        children,
        mountedRows: rows,
        stick: follow.stickRef.current,
        lastWriteTop: follow.lastWriteTopRef.current,
      }
      // eslint-disable-next-line no-console
      console.log('[vcSnapshot]', result)
      // eslint-disable-next-line no-console
      if (rows.length) console.table(rows)
      return result
    }
    ;(window as unknown as { __vcSnapshot?: () => unknown }).__vcSnapshot = snapshot
    return () => {
      if ((window as unknown as { __vcSnapshot?: () => unknown }).__vcSnapshot === snapshot) {
        delete (window as unknown as { __vcSnapshot?: () => unknown }).__vcSnapshot
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, getH, estimatedHeight])

  const { detachSmoothAbort } = follow
  const { cancelHeightSync } = sync
  const { cancelGraceTimer } = grace
  const { dropPendingAnchorSave } = reading
  const { flushHeights } = heights
  useEffect(() => {
    return () => {
      detachSmoothAbort()
      cancelHeightSync()
      cancelGraceTimer()
      dropPendingAnchorSave()
      flushHeights()
    }
  }, [detachSmoothAbort, cancelHeightSync, cancelGraceTimer, dropPendingAnchorSave, flushHeights])

  return {
    farmIsMeasured: measurement.farmIsMeasured,
    farmRecord: measurement.farmRecord,
    farmRowMounted: measurement.farmRowMounted,
    scrollerRef,
    contentRef,
    topSentinelRef,
    bottomSentinelRef,
    virtualItems,
    offsetBefore,
    offsetAfter,
    totalHeight,
    isAtBottom,
    getFollow: follow.getFollow,
    scrollToIndex: pinning.scrollToIndex,
    scrollToBottom: pinning.scrollToBottom,
    mountIndex: ops.mountIndex,
    estimateRowTop: ops.estimateRowTop,
    measureRef: measurement.measureRef,
    /** True while an anchored entry is still waiting for its row to hydrate.
     *  The caller should cover the transcript with a skeleton for exactly this
     *  window: the rows underneath are a partial, unpositioned transcript, and
     *  showing them means the reader watches it assemble and then jump. Entry
     *  with NO saved anchor never raises this -- that case is placed at the live
     *  end on the first commit, with nothing to wait for. */
    restoreGate: reading.restoreGateNow(),
  }
}
