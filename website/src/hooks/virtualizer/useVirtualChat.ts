// useVirtualChat — measurement-first chat virtualizer hook.
//
// Composes HeightCache (persistent), WindowCalculator (pure window math),
// FollowController (pure stick-to-bottom decisions), and DOM observers
// (Intersection + Resize) to render a windowed view of `items`.
//
// FOLLOW / STICK-TO-BOTTOM
// ========================
// A single `stickRef` boolean is the source of truth for "keep the viewport
// pinned to the bottom". It is owned entirely by this hook (callers just use
// `scrollToBottom()` / `isAtBottom`). The decision logic lives in
// FollowController as pure functions and is race-proof against the
// ResizeObserver-vs-scroll-event ordering — see that module's header for the
// rationale. The two write sites are:
//   - automatic pins (RO callback + append layout effect) → `pinAuto()`
//   - explicit pins (slot entry + scrollToBottom API) → `forcePin()`
//
// INVARIANT — every programmatic `scrollTop` write MUST record itself in
// `lastWriteTopRef`. Read this before adding any code that moves the scroller.
//
// The stick-release guard distinguishes "the user scrolled" from "we scrolled"
// by comparing live `scrollTop` against the value we last wrote. An unrecorded
// write therefore looks exactly like user input and releases follow. The guard
// is reliable only because pins are instant, so there is no in-flight animation
// to desynchronise the reference. That makes the invariant load-bearing rather
// than hygienic: the anchor-compensation write has to honour it too, and so must
// any future one.
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
// The decision carries one obligation. Height truth spans the DOM, `HeightCache`,
// `OffsetIndex` and the memos derived from them, and stays coherent by convention
// — a version counter in the memo deps, plus a session guard held separately by
// the cache and by the offset tree — rather than by construction. Collapsing that
// to a single seam, with `OffsetIndex` the only read surface for heights and
// `HeightCache` purely its persistence backend, is the standing follow-through.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { isRailSettling, RAIL_SETTLE_MS } from '../useRailWidth'
import { HeightCache } from './HeightCache'
import {
  loadScrollAnchor,
  saveScrollAnchor,
  clearScrollAnchor,
  type ScrollAnchor,
} from './ScrollAnchorCache'
import { attachUserScrollIntent } from '../../utils/searchScroll'
import {
  computeWindow,
  computeJumpWindow,
  expandWindowUp,
  expandWindowDown,
  getOffset as getOffsetFn,
  getTotalHeight,
  OffsetIndex,
} from './WindowCalculator'
import {
  computeAtBottom,
  isSelfScroll,
  SELF_SCROLL_EPSILON,
  stickAfterUserScroll,
  bottomTarget,
  evaluateAutoPin,
} from './FollowController'
import type {
  UseVirtualChatOptions,
  UseVirtualChatReturn,
  VirtualItem,
  ScrollToIndexOptions,
} from './types'

const DEFAULT_ESTIMATED = 80
const DEFAULT_OVERSCAN = 5
const DEFAULT_BOTTOM_THRESHOLD = 100
// After a genuine user scroll, suppress ResizeObserver-driven auto-pins for
// this long. Streaming/widget growth that should "follow" happens while the
// user is stationary at the bottom; a re-measuring widget that fires mid-fling
// must NOT yank the user (which also unmounts the rows they were scrolling
// through, leaving a blank flash). Explicit pins (slot entry, scrollToBottom,
// append) bypass this — only the RO follow path is gated.
const SCROLL_SETTLE_MS = 150

// Heights are re-synced into the offset memos only after they've been STABLE
// for this long. A one-time shrink (streaming finalize, widget settle) syncs
// ~this-many ms later — briefly stale, then correct. A continuously
// oscillating row (e.g. an auto-height iframe whose content reflows when
// resized — the classic lava-lamp/responsive-canvas feedback loop) keeps
// resetting the timer, so it NEVER triggers a re-render: no storm, no spacer
// jitter. The virtualizer thus refuses to amplify a widget's own height
// feedback loop instead of re-rendering every frame.
const HEIGHT_SYNC_DEBOUNCE_MS = 120

// After the caller stops naming a row via `streamingIndex` (the turn closed —
// `isStreaming` flipped false), keep that row on the IMMEDIATE height-sync path
// for this long. A diff/code block wrapped in <SmoothResize> keeps easing its
// height toward the content height via a `height .32s` CSS transition, and the
// stream→complete flip is one more height change — all of which fire AFTER the
// last content byte streamed in. Without this grace those trailing resizes fall
// back to the debounce and re-create the very spacer lurch `streamingIndex`
// exists to prevent, at end-of-stream. Sized to comfortably cover SmoothResize's
// 320ms ease plus the completion snap. It is a FIXED window from the transition
// (never re-armed per resize), so an oscillating post-stream widget cannot hold
// the row on the immediate path indefinitely — after this window the row reverts
// to the debounced path and its render-storm protection is restored.
const STREAMING_SETTLE_GRACE_MS = 400

// Rows must drift this many items BEYOND the computed window before a
// SCROLL-path recompute will UNMOUNT them (mounting stays eager — no
// hysteresis). This deadband breaks a feedback loop seen when a widget sits at
// the window boundary: a 1px scrollTop nudge from native `overflow-anchor`
// (which fires every time a row mounts/unmounts) shifts the computed window by
// a single row, which unmounts/remounts the boundary widget (rebuilding its
// Tailwind iframe — expensive), whose height change nudges scrollTop again …
// 30+ times/s (diagnosed via scroll.event≈windowRange.change storms). Keeping
// boundary rows mounted within the band stops the flip-flop while still
// bounding the mounted set to roughly window + overscan + this margin.
const WINDOW_UNMOUNT_HYSTERESIS = 4

// Multiplier on `overscan` that defines the "near" band for a jump: a jump
// landing within this many overscan windows of the current range takes the
// union/glide path; farther jumps teleport (replace the window). Used by both
// the far-check and the setWindowRange near-check, which must stay in sync.
const NEAR_JUMP_OVERSCAN_MULT = 4

// Reading-position anchor persistence (see ScrollAnchorCache). The anchor is
// captured on scroll-SETTLE, not per scroll event: captureTopAnchor reads a
// getBoundingClientRect per mounted row, which is fine once per pause but not
// at scroll-event rate. Trailing-edge, non-resetting timer: it fires at most
// once per window even during a continuous scroll/stream, so "returned to the
// bottom" reliably clears the anchor instead of being starved by resets.
const ANCHOR_SAVE_DEBOUNCE_MS = 200

// After the restore's initial offset-math write, re-correct against the
// anchor row's LIVE DOM position for this many frames. The jump window has
// only just committed and rows above the anchor refine from estimates to
// measurements over the first frames, shifting the row on screen; the DOM
// delta correction re-pins it to the saved offset. Mirrors scrollToBottom's
// settle loop.
const ANCHOR_RESTORE_SETTLE_FRAMES = 3

// Capture the topmost visible mounted row (smallest index whose bottom edge
// is still below the viewport top) and its offset from the scroller's top.
// Pure over its inputs so it can run both from the hook's callbacks (live
// items) and from the slot-switch flush, which must resolve keys against the
// OUTGOING session's items snapshot. Returns null when no mounted row
// qualifies or the environment has no layout (jsdom).
function captureTopAnchorFrom(
  el: HTMLDivElement,
  entries: Iterable<[Element, number]>,
  keyAt: (index: number) => string | null,
): { key: string; top: number } | null {
  if (typeof el.getBoundingClientRect !== 'function') return null
  const srTop = el.getBoundingClientRect().top
  let bestIdx = Infinity
  let bestTop = 0
  let bestKey: string | null = null
  for (const [node, idx] of entries) {
    const rect = (node as HTMLElement).getBoundingClientRect()
    const top = rect.top - srTop
    // Skip rows fully above the viewport top — they aren't the anchor the
    // user is looking at (their screen position is off-screen).
    if (rect.bottom - srTop <= 0) continue
    if (idx < bestIdx) {
      const key = keyAt(idx)
      if (key === null) continue
      bestIdx = idx
      bestTop = top
      bestKey = key
    }
  }
  return bestKey !== null ? { key: bestKey, top: bestTop } : null
}

/** Screen offset of the mounted row whose key matches, relative to the
 *  scroller's top; null when it is not mounted. Pure over its inputs like the
 *  capture above, so both anchor consumers resolve a row the same way. */
function rowTopFrom(
  el: HTMLDivElement,
  entries: Iterable<[Element, number]>,
  keyAt: (index: number) => string | null,
  key: string,
): number | null {
  if (typeof el.getBoundingClientRect !== 'function') return null
  for (const [node, idx] of entries) {
    if (keyAt(idx) !== key) continue
    const srTop = el.getBoundingClientRect().top
    return (node as HTMLElement).getBoundingClientRect().top - srTop
  }
  return null
}

/** Border-box height at sub-pixel precision, quantized to quarter-pixels.
 *
 * `offsetHeight` ROUNDS to an integer, but real rows are fractional whenever
 * content scales to width (an image at 342px width and a 696:204 ratio is
 * 100.24px tall). Each row then contributes up to half a pixel of signed
 * error to the offset tree, and over a long list the accumulated drift (tens
 * of px across ~100 rows) cashes out at window boundaries as a few-pixel
 * hiccup — invisible on engines with native scroll anchoring, visible on iOS
 * Safari. The rect height carries the fraction; quarter-pixel quantization
 * (finer than any real DPR grid) keeps float noise from tripping the strict
 * height-change comparisons into churn. jsdom reports all-zero rects, so a
 * degenerate rect falls back to offsetHeight — test doubles that mock
 * offsetHeight keep working unchanged.
 */
function measureBorderBoxHeight(el: HTMLElement): number {
  if (typeof el.getBoundingClientRect === 'function') {
    const h = el.getBoundingClientRect().height
    if (h > 0) return Math.round(h * 4) / 4
  }
  return el.offsetHeight
}

export function useVirtualChat<T>(
  opts: UseVirtualChatOptions<T>,
): UseVirtualChatReturn<T> {
  const {
    items,
    getKey,
    sessionId,
    estimatedHeight = DEFAULT_ESTIMATED,
    overscan = DEFAULT_OVERSCAN,
    followOutput = true,
    initialPlacement = 'bottom',
    eagerFirstMeasure = false,
    bottomThreshold = DEFAULT_BOTTOM_THRESHOLD,
    isSticky,
    externalScrollerRef,
    streamingIndex,
    onTopReached,
  } = opts

  const itemCount = items.length
  // Live ref for the RO callback (a stable-identity effect — see its own
  // deps) so a caller updating `streamingIndex` every render (typical: it
  // tracks "index of the last item while it has role streaming") doesn't
  // force the ResizeObserver to be torn down and reattached.
  const streamingIndexRef = useRef(streamingIndex)
  streamingIndexRef.current = streamingIndex
  // Live ref for the same reason: the RO callback and the measureRef factory
  // are stable-identity, so they read the option through a ref.
  const eagerFirstMeasureRef = useRef(eagerFirstMeasure)
  eagerFirstMeasureRef.current = eagerFirstMeasure

  // Same reasoning for the IntersectionObserver effect: keeping the callback in a
  // ref keeps it out of that effect's deps, so it never re-subscribes per render.
  const onTopReachedRef = useRef(onTopReached)
  useEffect(() => {
    onTopReachedRef.current = onTopReached
  }, [onTopReached])

  // ---- Streaming-settle grace ----
  // When `streamingIndex` goes undefined (the turn closed — `isStreaming`
  // flipped false), the row it named often keeps resizing for a short while:
  // a diff/code <SmoothResize> wrapper eases its height toward the content
  // height (`height .32s`) and the stream→complete flip is one more change.
  // Keep that row on the IMMEDIATE-sync path for STREAMING_SETTLE_GRACE_MS so
  // those trailing resizes don't fall back to the debounce and lurch the
  // spacer under a scrolled-up user.
  const graceIndexRef = useRef<number | undefined>(undefined)
  const graceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const clearStreamingGrace = useCallback(() => {
    if (graceTimerRef.current) {
      clearTimeout(graceTimerRef.current)
      graceTimerRef.current = null
    }
    graceIndexRef.current = undefined
  }, [])
  const armStreamingGrace = useCallback((idx: number) => {
    graceIndexRef.current = idx
    if (graceTimerRef.current) clearTimeout(graceTimerRef.current)
    graceTimerRef.current = setTimeout(() => {
      graceTimerRef.current = null
      graceIndexRef.current = undefined
    }, STREAMING_SETTLE_GRACE_MS)
  }, [])
  // Detect the streaming→idle transition: arm the grace when streaming stops,
  // and clear it while streaming is active (the streamingIndexRef path covers
  // that case directly). A LAYOUT effect (not passive) so grace is armed
  // synchronously at the transition commit — before the ResizeObserver delivers
  // the completion resize for that same frame, which would otherwise be
  // debounced (arriving before a passive effect ran) and preserve the lurch.
  const prevStreamingIndexRef = useRef(streamingIndex)
  useLayoutEffect(() => {
    const prev = prevStreamingIndexRef.current
    prevStreamingIndexRef.current = streamingIndex
    if (streamingIndex !== undefined) {
      clearStreamingGrace()
    } else if (prev !== undefined) {
      armStreamingGrace(prev)
    }
  }, [streamingIndex, armStreamingGrace, clearStreamingGrace])

  // ---- DOM refs ----
  const internalScrollerRef = useRef<HTMLDivElement | null>(null)
  // Stable RefObject identity: memoized on `externalScrollerRef` so it only
  // changes when the caller swaps the external ref (never on ordinary
  // re-renders). Keeping the identity stable lets the callbacks/effects below
  // list `scrollerRef` in their deps without recreating on every render (which
  // would re-attach the scroll/Resize/Intersection observers each frame).
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
  // same scroll positions every time. scrollToIndexSmooth already derives
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

  // ---- Persistent state ----

  // HeightCache is created once per sessionId and disposed when sessionId changes.
  const cacheRef = useRef<HeightCache | null>(null)
  const cacheSessionRef = useRef<string | null>(null)
  if (cacheRef.current === null || cacheSessionRef.current !== sessionId) {
    cacheRef.current?.flush()
    // Seed the row count so the eviction cap is size-aware from the first
    // measurement: a session longer than the baseline floor must be allowed to
    // retain its oldest heights, or scrolling back to the top re-enters
    // all-estimate territory even on a revisit. `itemCount` is legitimately 0
    // here when a slot switch changes sessionId before the transcript loads;
    // HeightCache treats that as "unknown" and sizes the cap from the persisted
    // blob instead, so no measurements are discarded before the real count
    // arrives via setRowCount() below.
    cacheRef.current = new HeightCache(sessionId, { rowCount: itemCount })
    cacheSessionRef.current = sessionId
  } else {
    // Transcripts grow while mounted; keep the cap in step with the row count.
    cacheRef.current.setRowCount(itemCount)
  }

  // One shared ResizeObserver; Element → index map resolves heights cheaply.
  const elIndexRef = useRef<Map<Element, number>>(new Map())
  const resizeObserverRef = useRef<ResizeObserver | null>(null)

  // Live items array (lets imperative callbacks read current state).
  const itemsRef = useRef(items)
  itemsRef.current = items
  const getKeyRef = useRef(getKey)
  getKeyRef.current = getKey

  // ---- Follow / stick-to-bottom state (see FollowController) ----
  //
  // `stickRef`: should the viewport stay pinned to the bottom. Turned OFF only
  // by a genuine user scroll-up; turned ON only by the user returning to the
  // bottom or an explicit/forced pin (slot entry, scrollToBottom).
  //
  // `lastWriteTopRef`: the scrollTop value we last WROTE programmatically.
  // `-1` means "nothing written this session" (resets the race guard on slot
  // switch). Used to (a) recognise our own scroll events and (b) detect, at
  // pin time, that the user scrolled up since our last write — synchronously,
  // beating the RO-vs-scroll-event race.
  const stickRef = useRef<boolean>(followOutput)
  const lastWriteTopRef = useRef<number>(-1)
  // True while a smooth scrollTo animation (from pinAuto) is in flight.
  // During this period, scroll events are NOT treated as user-scrolls — they
  // are intermediate frames of our own programmatic smooth-pin.
  const smoothPinActiveRef = useRef(false)
  // Previous scrollTop during smooth-pin animation. Used to detect genuine
  // user scroll-up (scrollTop decreased) vs normal forward animation progress.
  const prevSmoothTopRef = useRef(0)
  // Detaches the current smooth-glide abort listeners. Held in a ref so the
  // glide can be torn down from wherever it ends: user input, natural arrival
  // at the bottom, a replacing glide, or unmount.
  const smoothAbortDetachRef = useRef<(() => void) | null>(null)
  const detachSmoothAbort = useCallback(() => {
    smoothAbortDetachRef.current?.()
  }, [])
  // Timestamp (performance.now) of the last genuine USER scroll. Used to gate
  // RO-driven follow pins so they don't fire mid-fling — see SCROLL_SETTLE_MS.
  const lastUserScrollAtRef = useRef<number>(0)

  // ---- Scroll-anchor preservation ----
  //
  // While the user is scrolled up reading history, an UPWARD window shift
  // mounts rows above the viewport (recomputeWindow / top-sentinel expansion).
  // Native `overflow-anchor: auto` normally holds the viewport steady, but a
  // scroll-path recompute can UNMOUNT the browser's chosen anchor node (rows
  // past WINDOW_UNMOUNT_HYSTERESIS), collapsing anchoring and jumping the
  // viewport. This ref carries the topmost visible row's key + its screen
  // offset, captured BEFORE an upward shift; a layout effect after commit
  // re-reads that row and corrects scrollTop by the delta. This REDUCES
  // reliance on overflow-anchor (it does not replace it — the CSS is owned by
  // ChatPage and left alone).
  const anchorPendingRef = useRef<{ key: string; top: number } | null>(null)

  /**
   * Prepend compensation (load-older history).
   *
   * A prepend shifts every index up, so pre-existing rows move down by the
   * inserted height while scrollTop stays put — that IS the jump. The snapshot
   * must be taken in the RENDER phase (getSnapshotBeforeUpdate idiom): a layout
   * effect runs after commit, when the row has moved and the delta reads zero.
   *
   * Arithmetic is no alternative: `getH` prices an unmeasured row from the
   * running MEAN of measured ones, so any measurement re-prices every unmeasured
   * row and the next sync re-reads them all (measured: a 1000px insert displaced
   * rows by 1500). These refs stay private rather than reusing `anchorPendingRef`,
   * which the passive itemCount recompute overwrites with its own capture.
   */
  const prependAnchorRef = useRef<{ key: string; top: number } | null>(null)
  const prependCountRef = useRef(0)
  const prependRefineRef = useRef(false)
  /** Previous render's identity. `items` is held because `itemsRef` has already
   *  advanced by the time the capture runs, while the mounted nodes still carry
   *  the PREVIOUS commit's indices. */
  const prependPrevRef = useRef<{ session: string; count: number; firstKey: string | null; items: T[] }>({
    session: sessionId, count: itemCount, firstKey: null, items,
  })
  const prependPrev = prependPrevRef.current
  const prependFirstKey = itemCount > 0 ? getKey(items[0], 0) : null
  // A front-insert grows the count AND changes index 0's key. A slot switch does
  // both, hence the session guard; a plain append leaves index 0 alone.
  if (
    itemCount > prependPrev.count &&
    prependPrev.session === sessionId &&
    prependPrev.firstKey !== null &&
    prependFirstKey !== prependPrev.firstKey &&
    !stickRef.current
  ) {
    const prependEl = scrollerRef.current
    // A turn takes its LEAD item's key, so a prepended message joining the top turn
    // renames that row: skip keys the new set retired and anchor on the next survivor.
    const survivingKeys = new Set<string>()
    for (let i = 0; i < items.length; i++) survivingKeys.add(getKey(items[i], i))
    const prependAnchor = prependEl
      ? captureTopAnchorFrom(prependEl, elIndexRef.current.entries(), (idx) => {
          const it = prependPrev.items[idx]
          if (!it) return null
          const k = getKey(it, idx)
          return survivingKeys.has(k) ? k : null
        })
      : null
    if (prependAnchor) {
      prependAnchorRef.current = prependAnchor
      prependCountRef.current = itemCount - prependPrev.count
    }
  }
  prependPrevRef.current = { session: sessionId, count: itemCount, firstKey: prependFirstKey, items }

  // Window range for what is currently mounted. Initial state is the TAIL of
  // the list (last ~overscan+1 items) — chat sessions always open at the
  // bottom, and starting here avoids a commit-timing race where the slot-entry
  // pin runs before the tail items have rendered.
  const [windowRange, setWindowRange] = useState<{ start: number; end: number }>(() => {
    const tailSize = Math.min(itemCount, overscan + 1)
    if (initialPlacement === 'top') return { start: 0, end: tailSize }
    return { start: Math.max(0, itemCount - tailSize), end: itemCount }
  })
  // Live mirror of windowRange for imperative reads (debug probe).
  const windowRangeRef = useRef(windowRange)
  windowRangeRef.current = windowRange

  // isAtBottom is the only render-affecting scroll state we expose (drives the
  // caller's jump-to-bottom pill).
  const [isAtBottom, setIsAtBottom] = useState<boolean>(true)

  // Bumped whenever a mounted row's measured height changes IN PLACE (a
  // ResizeObserver re-measure with no window/itemCount change). The offset
  // memos below read the mutable HeightCache through `getH`, whose identity is
  // stable, so they only recompute when windowRange/itemCount change — NOT
  // when heights change. After a content SHRINK (streaming finalize, widget
  // settle, markdown reflow) `totalHeight` would stay stale-large and
  // `offsetAfter = totalHeight - offset(end)` would inflate into a phantom
  // bottom spacer (the "blank space at the bottom" bug, and the "flicker when
  // the scroll stops"). Including this version in the memo deps forces a
  // recompute on every genuine height change so the spacers track reality.
  const [heightVersion, setHeightVersion] = useState(0)

  // ---- Reading-position anchor (persisted; see ScrollAnchorCache) ----
  //
  // `pendingRestoreRef` latches the saved anchor for the CURRENT session the
  // moment the session is entered (first mount or slot switch), BEFORE any
  // pin can fire. Latching is what makes the restore immune to the entry
  // pin's own scroll events: a bottom pin marks the session "at bottom",
  // whose debounced save would clear the very anchor being restored.
  // `undefined` means "not yet latched for this session" (first render).
  const pendingRestoreRef = useRef<ScrollAnchor | null | undefined>(undefined)
  // Debounced-save bookkeeping: one trailing, NON-resetting timer, plus the
  // last state actually written per session so streaming (which fires the
  // timer repeatedly while pinned to the bottom) doesn't spam localStorage.
  const anchorSaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const anchorSavedStateRef = useRef<{ session: string; state: string } | null>(null)
  // Identity context of the last scroll burst: which session it belonged to
  // and how to resolve row keys for it. Reference-only (no rect reads), set on
  // every scroll event. The slot-switch flush below needs it because during
  // the switch RENDER, itemsRef/getKeyRef may already hold the INCOMING
  // session's data while the DOM (elIndexRef nodes, scroller geometry) still
  // shows the outgoing one — resolving keys through the live refs there would
  // save the wrong keys under the old session id.
  const lastScrollCtxRef = useRef<{
    session: string
    items: readonly T[]
    getKey: (it: T, i: number) => string
  } | null>(null)
  if (pendingRestoreRef.current === undefined) {
    // First render: latch any saved anchor for the initial session. Reading
    // localStorage during render matches the HeightCache constructor above.
    pendingRestoreRef.current = loadScrollAnchor(sessionId)
    if (pendingRestoreRef.current) stickRef.current = false
  }

  // Reset window + follow state to the tail/bottom when the session changes.
  // useState's lazy initializer only runs on first mount, so without this the
  // second visit to a slot would carry over the last window/stick state,
  // defeating the "open at bottom" contract (and causing the "lands in the
  // middle" bug). Render-time sentinel pattern (mirrors the HeightCache reset
  // above); React permits state updates during render when guarded by a
  // "props changed" check. lastWriteTopRef is reset to -1 so the leftover
  // scrollTop from the previous session is not mistaken for a user scroll-up.
  const sessionIdRef = useRef<string>(sessionId)
  if (sessionIdRef.current !== sessionId) {
    const prevSession = sessionIdRef.current
    sessionIdRef.current = sessionId
    const tailSize = Math.min(itemCount, overscan + 1)
    setWindowRange(
      initialPlacement === 'top'
        ? { start: 0, end: tailSize }
        : { start: Math.max(0, itemCount - tailSize), end: itemCount },
    )
    lastWriteTopRef.current = -1
    setIsAtBottom(true)
    // A pending debounced save belongs to the OUTGOING session: flush it NOW,
    // synchronously, instead of dropping it — a scroll-then-switch inside the
    // debounce window must not lose the newest reading position. This render
    // has not committed, so the DOM still shows the outgoing session
    // (elIndexRef nodes, scroller geometry), and lastScrollCtxRef resolves
    // row keys against ITS items — the live itemsRef may already hold the
    // incoming session's data here. Once-per-switch rect reads over the
    // mounted window (~2×overscan rows) — negligible. Skipped while a restore
    // for the outgoing session was still pending (transitional geometry).
    if (anchorSaveTimerRef.current !== null) {
      clearTimeout(anchorSaveTimerRef.current)
      anchorSaveTimerRef.current = null
      const ctx = lastScrollCtxRef.current
      const el = scrollerRef.current
      if (ctx && ctx.session === prevSession && el && !pendingRestoreRef.current) {
        const geom = { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }
        if (computeAtBottom(geom, bottomThreshold)) {
          clearScrollAnchor(prevSession)
        } else {
          const a = captureTopAnchorFrom(el, elIndexRef.current.entries(), (idx) => {
            const it = ctx.items[idx]
            return it ? ctx.getKey(it, idx) : null
          })
          if (a) saveScrollAnchor(prevSession, a)
        }
      }
    }
    lastScrollCtxRef.current = null
    // Latch the entered session's saved reading position (if any). With an
    // anchor pending, follow starts RELEASED so the bulk-hydration path below
    // doesn't tail-pin before the restore runs; without one, the default
    // open-at-bottom contract stands.
    pendingRestoreRef.current = loadScrollAnchor(sessionId)
    stickRef.current = pendingRestoreRef.current ? false : followOutput
  }

  // ---- Height lookup ----
  const getH = useCallback(
    (i: number) => {
      const it = itemsRef.current[i]
      if (!it) return estimatedHeight
      const k = getKeyRef.current(it, i)
      const cache = cacheRef.current!
      // peek(), not get(): this closure feeds the BULK scans (OffsetIndex.sync,
      // window/offset math), which touch every row. Promoting on those reads
      // would rewrite LRU order into transcript-index order and evict rows the
      // user just viewed. Genuine access is recorded by set() on measurement.
      const cached = cache.peek(k)
      // Math.max(h, 1) so zero-height items still register with IO.
      if (cached !== undefined) return Math.max(cached, 1)
      // Unmeasured row: estimate from the running mean of MEASURED heights,
      // capped by MAX_MEAN_PX so one pathological row cannot inflate every
      // unmeasured historical row. averageHeight() falls back to the
      // configured estimate only while nothing has been measured at all — it
      // deliberately does NOT hold back for a minimum sample, because measuring
      // that gate in a browser made the drift ~7.5x worse (see HeightCache).
      return cache.averageHeight(estimatedHeight)
    },
    [estimatedHeight],
  )

  // ---- Offset index (O(log N) prefix-sum tree over row heights) ----
  //
  // The hot paths (per-rAF scroll window recompute, offset/total spacers, the
  // 120ms streaming tick) would each walk all N rows via the O(N) free functions
  // (getOffset / getTotalHeight / computeWindow), which dominates scroll frames
  // on 5000+ row transcripts. `OffsetIndex` answers the same questions in
  // O(log N) / O(1). It is the authoritative tree, synced HERE on an itemCount
  // (or getH) change so the offset memos have fresh data on the same render,
  // and additionally on height changes by `scheduleHeightSync` (the 120ms tick
  // — the sync point OffsetIndex's own doc prescribes). It is NOT synced on the
  // per-rAF scroll path (a same-count sync still O(N)-scans the prefix).
  //
  // `sessionId` is a dependency because the tree caches heights read through
  // the (session-scoped) HeightCache: switching to a different session with the
  // SAME item count changes neither itemCount nor getH's identity, so without
  // this the tree would keep serving the previous transcript's heights and
  // render wrong spacers until the next measurement tick corrected it. A
  // session change invalidates every height, so rebuild rather than sync.
  const offsetIndexRef = useRef<OffsetIndex | null>(null)
  const offsetIndexSessionRef = useRef<string | null>(null)
  const offsetIndex = useMemo(() => {
    if (offsetIndexRef.current === null || offsetIndexSessionRef.current !== sessionId) {
      offsetIndexRef.current = new OffsetIndex(itemCount, getH)
      offsetIndexSessionRef.current = sessionId
    } else {
      offsetIndexRef.current.sync(itemCount, getH)
    }
    return offsetIndexRef.current
  }, [itemCount, getH, sessionId])

  // Debounced height→memo sync. Cache writes (RO re-measure, measureRef seed)
  // call this instead of bumping heightVersion directly. The bump (which
  // invalidates the offset memos) fires only after heights have been STABLE
  // for HEIGHT_SYNC_DEBOUNCE_MS, and only if the total actually changed. This
  // (a) corrects a one-time shrink's phantom spacer a beat later, and
  // (b) refuses to re-render during a continuous height oscillation (an
  // auto-height widget iframe whose content reflows when resized), which would
  // otherwise be a per-frame render storm + a spacer that jitters ±Δ.
  //
  // This debounced tick is also the OffsetIndex sync point (per its doc): it
  // reconciles the tree with the batch of measurements that landed, then reads
  // the new total in O(1) — no O(N) getTotalHeight walk ~8x/sec while
  // streaming.
  //
  // `immediate` bypasses the debounce for the CALLER-DESIGNATED streaming row
  // (see `streamingIndex` option). That row's height changes constantly while
  // text reveals — debouncing it means the offset memos sit frozen at a stale
  // value for as long as growth keeps arriving, then jump by the ENTIRE
  // accumulated backlog in one commit the moment growth pauses. For a user
  // scrolled up reading history, that spacer sits directly below their
  // viewport, so the jump reads as a visible flash (see
  // useVirtualChat.spacerLurch.test.tsx). Syncing immediately instead tracks
  // growth every RO tick (already rAF-coalesced by the caller — see the RO
  // callback below), trading nothing for the general oscillating-widget case:
  // debounce still applies to every OTHER row, so a re-measuring widget
  // elsewhere in the transcript still gets the render-storm protection this
  // mechanism exists for.
  // ---- Rail-collapse settle window (see the RO callback) ----
  // One pending timer at a time; `follow` remembers whether we were pinned to
  // the bottom when the window opened, so the single post-window re-pin only
  // fires for a user who was actually following.
  const railSettleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const railSettleFollowRef = useRef(false)
  const lastSyncedTotalRef = useRef(-1)
  const heightSyncTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const syncHeightsNow = useCallback(() => {
    const idx = offsetIndexRef.current
    if (!idx) return
    idx.sync(itemsRef.current.length, getH)
    const total = idx.totalHeight()
    if (Math.abs(total - lastSyncedTotalRef.current) > 1) {
      lastSyncedTotalRef.current = total
      setHeightVersion((v) => v + 1)
    }
  }, [getH])
  const scheduleHeightSync = useCallback((immediate = false) => {
    if (heightSyncTimerRef.current) {
      clearTimeout(heightSyncTimerRef.current)
      heightSyncTimerRef.current = null
    }
    if (immediate) {
      syncHeightsNow()
      return
    }
    heightSyncTimerRef.current = setTimeout(() => {
      heightSyncTimerRef.current = null
      syncHeightsNow()
    }, HEIGHT_SYNC_DEBOUNCE_MS)
  }, [syncHeightsNow])

  // NOTE: `heightVersion` is an intentional manual-invalidation key in the
  // three memos below — it is not referenced in the bodies, so eslint flags it
  // as "unnecessary", but removing it reintroduces the stale-spacer bug
  // (memos reading the mutable OffsetIndex never recompute on a height change).
  // Do NOT remove it. `offsetIndex` has a STABLE ref identity (it is the same
  // tree object mutated in place by sync), so it is listed for clarity but the
  // real triggers are itemCount / heightVersion / windowRange.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const totalHeight = useMemo(() => offsetIndex.totalHeight(), [offsetIndex, itemCount, heightVersion])
  const offsetBefore = useMemo(
    () => offsetIndex.offsetOf(windowRange.start),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [offsetIndex, windowRange.start, itemCount, heightVersion],
  )
  // Height of all items AFTER the window — used as the bottom spacer so the
  // scroll content keeps its full size while only the window renders real DOM.
  const offsetAfter = useMemo(
    () => Math.max(0, offsetIndex.totalHeight() - offsetIndex.offsetOf(windowRange.end)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [offsetIndex, windowRange.end, itemCount, totalHeight, heightVersion],
  )

  // Topmost visible mounted row, resolved against the LIVE items. Used by the
  // scroll-anchor preservation path and the debounced reading-position save.
  // (The slot-switch flush calls captureTopAnchorFrom directly with a
  // snapshot resolver instead — see the session sentinel.)
  const captureTopAnchor = useCallback((): { key: string; top: number } | null => {
    const el = scrollerRef.current
    if (!el) return null
    return captureTopAnchorFrom(el, elIndexRef.current.entries(), (idx) => {
      const it = itemsRef.current[idx]
      return it ? getKeyRef.current(it, idx) : null
    })
  }, [scrollerRef])

  // ---- Reading-position anchor: debounced save on scroll settle ----
  //
  // Fired from the passive scroll listener. At settle time (not per event —
  // captureTopAnchor reads a rect per mounted row) the live geometry decides:
  //   - at the bottom → the anchor must be ABSENT ("no anchor" is what makes
  //     the next slot entry take the default pin-to-bottom path), so clear it;
  //   - scrolled up → persist the topmost visible row's key + viewport offset.
  // Self-scrolls schedule saves too, deliberately: a programmatic jump/pin
  // still changes the truth being persisted. The fire-time session guard
  // covers a timer surviving into a slot switch.
  const scheduleAnchorSave = useCallback(() => {
    if (anchorSaveTimerRef.current !== null) return
    const scheduledSession = sessionIdRef.current
    anchorSaveTimerRef.current = setTimeout(() => {
      anchorSaveTimerRef.current = null
      if (sessionIdRef.current !== scheduledSession) return
      // While a restore is still pending (items not yet arrived), the
      // geometry is transitional — don't let it overwrite the saved anchor.
      if (pendingRestoreRef.current) return
      const el = scrollerRef.current
      if (!el) return
      const geom = { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }
      const saved = anchorSavedStateRef.current
      if (computeAtBottom(geom, bottomThreshold)) {
        if (saved?.session !== scheduledSession || saved.state !== '') {
          clearScrollAnchor(scheduledSession)
          anchorSavedStateRef.current = { session: scheduledSession, state: '' }
        }
        return
      }
      const a = captureTopAnchor()
      if (!a) return
      const state = `${a.key}@${Math.round(a.top)}`
      if (saved?.session === scheduledSession && saved.state === state) return
      saveScrollAnchor(scheduledSession, a)
      anchorSavedStateRef.current = { session: scheduledSession, state }
    }, ANCHOR_SAVE_DEBOUNCE_MS)
  }, [bottomThreshold, scrollerRef, captureTopAnchor])

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
    const count = itemsRef.current.length
    const idx = offsetIndexRef.current
    // Window bounds in O(log N) via the OffsetIndex prefix-sum tree rather than
    // the O(N) computeWindow linear scan — this is the per-rAF scroll hot path.
    // Fall back to computeWindow only if the tree is somehow absent.
    let next: { start: number; end: number }
    if (count <= 0) {
      next = { start: 0, end: 0 }
    } else if (idx) {
      // Convert the scroller's scrollTop into LIST content coordinates before
      // asking the offset tree: content above the list (page header, toolbars
      // — see leadingOffset) is not the tree's to know about.
      const lead = leadingOffset(el)
      const top = Math.max(0, el.scrollTop - lead)
      const bottom = top + Math.max(0, el.clientHeight)
      const overscanN = Math.max(0, Math.floor(overscan))
      const firstVisible = idx.indexAt(top)
      const lastVisible = idx.indexAt(bottom)
      next = {
        start: Math.max(0, firstVisible - overscanN),
        end: Math.min(count, lastVisible + 1 + overscanN),
      }
    } else {
      next = computeWindow(Math.max(0, el.scrollTop - leadingOffset(el)), el.clientHeight, count, getH, overscan)
    }
    // On an upward shift (window start moving up → more rows mount above
    // the viewport) while the user is scrolled up (stick released), record the
    // top anchor so the post-commit layout effect can hold the viewport steady.
    // Skipped while stick is armed — the pin path owns positioning then.
    const cur = windowRangeRef.current
    if (next.start < cur.start && !stickRef.current) {
      const a = captureTopAnchor()
      if (a) anchorPendingRef.current = a
    }
    setWindowRange((prev) => {
      let merged: { start: number; end: number }
      if (expandOnly) {
        merged = { start: Math.min(prev.start, next.start), end: Math.max(prev.end, next.end) }
      } else {
        // Mount eagerly (next extends the window → adopt it immediately), but
        // only UNMOUNT once a row has drifted past WINDOW_UNMOUNT_HYSTERESIS
        // beyond the current edge. This keeps a boundary widget mounted across
        // the ±1-row jitter that overflow-anchor scroll nudges produce, which
        // is what was thrashing widget iframes 30+/s (see constant).
        const start =
          next.start < prev.start
            ? next.start
            : next.start > prev.start + WINDOW_UNMOUNT_HYSTERESIS
              ? next.start
              : prev.start
        const end =
          next.end > prev.end
            ? next.end
            : next.end < prev.end - WINDOW_UNMOUNT_HYSTERESIS
              ? next.end
              : prev.end
        merged = { start, end }
      }
      if (prev.start === merged.start && prev.end === merged.end) return prev
      return merged
    })
  }, [getH, overscan, scrollerRef, captureTopAnchor, leadingOffset])

  // ---- Pin helpers (the only code that writes el.scrollTop for follow) ----

  // Automatic pin: called when content changed (RO / append / streaming).
  // DELEGATES the decision to FollowController.evaluateAutoPin — the pure,
  // unit-tested race-proof core. evaluateAutoPin reads the LIVE geometry and
  // (a) never pins when stick is released, (b) releases stick synchronously if
  // the user has scrolled up since our last write (scrollTop < lastWriteTop and
  // still away from the bottom — the distance guard tolerates mid-stream
  // shrink), and (c) otherwise pins to the bottom. Its at-bottom test uses the
  // DPR-aware epsilon, so this and the delegated core share one gate.
  //
  // The pin WRITE is INSTANT (behavior:'auto'), not smooth: a streaming
  // response grows the bottom target every token, and a fresh smooth scroll
  // CANCELS the in-flight one and restarts toward the moving target, so on a
  // tall transcript it chases the bottom and never converges. Smooth is
  // reserved for the explicit "jump to latest" path (scrollToBottom).
  //
  // The synchronous scroll-up release is reliable only with the instant write:
  // there is no animation lag, so scrollTop == lastWriteTop right after each pin.
   // ---- The single chokepoint for programmatic scroll writes ----
  //
  // Enforces the follow invariant STRUCTURALLY rather than by convention: you
  // cannot move the scroller without stating how the follow guard should account
  // for it, because `accounting` is a required argument.
  //   - 'pin'     — we are pinning; the guard remembers this position, so the
  //                 resulting scroll event is recognised as our own.
  //   - 'release' — we are deliberately leaving the bottom (explicit
  //                 navigation); reset the guard sentinel, follow is off anyway.
  // An unaccounted write is indistinguishable from user input and would release
  // follow spuriously. Making the argument mandatory means a future contributor
  // has to make a choice rather than forget one.
  const writeScrollTop = useCallback(
    (
      el: HTMLDivElement,
      top: number,
      behavior: ScrollBehavior,
      accounting: 'pin' | 'release',
    ) => {
      if (typeof el.scrollTo === 'function') el.scrollTo({ top, behavior })
      else el.scrollTop = top
      lastWriteTopRef.current = accounting === 'pin' ? top : -1
      // A SMOOTH pin animates toward `top` over many frames, and every
      // intermediate scroll event carries a scrollTop that differs from the
      // recorded target — so the passive listener would read those frames as
      // user input and release follow, and a mid-animation append would then be
      // skipped by auto-pin, landing short of the new bottom. Arm the
      // smooth-pin guard so the listener tolerates the glide (it disarms on
      // arrival, or on a genuine upward move: see the scroll handler).
      //
      // Only the explicit "jump to latest" path is smooth; the streaming pin
      // is instant, so this guard only needs to cover the jump-to-latest glide.
      if (accounting === 'pin' && behavior === 'smooth') {
        smoothPinActiveRef.current = true
        prevSmoothTopRef.current = el.scrollTop
        // ...but the guard must yield to REAL input. Its only other release
        // condition is "scrollTop moved backward", which a wheel cannot satisfy
        // while a fast animation is still driving scrollTop forward — so a user
        // wheeling up mid-glide was ignored and still ended up pinned to the
        // bottom (verified in a real browser). A one-shot input listener
        // disarms the guard and releases follow, matching how the jump/search
        // convergence polls already abort on user input.
        const abort = () => {
          // Stale-invocation guard: if the glide already finished, these
          // listeners are leftovers — detach and do nothing. Without this a
          // completed jump left handlers behind that a later, unrelated wheel
          // would fire, releasing follow while no smooth scroll was active.
          if (!smoothPinActiveRef.current) {
            detachSmoothAbort()
            return
          }
          smoothPinActiveRef.current = false
          stickRef.current = false
          lastUserScrollAtRef.current =
            typeof performance !== 'undefined' ? performance.now() : Date.now()
          // Releasing `stick` alone is not enough: the browser's NATIVE smooth
          // animation keeps running and would still land at the bottom, so the
          // user's input appears ignored. Re-issuing an instant scroll to the
          // CURRENT position cancels the in-flight animation and freezes where
          // they are. lastWriteTop is reset because we are releasing follow.
          if (typeof el.scrollTo === 'function') el.scrollTo({ top: el.scrollTop, behavior: 'auto' })
          lastWriteTopRef.current = -1
          detachSmoothAbort()
        }
        // Replace any previous glide's listeners rather than stacking them:
        // repeated jump-to-latest presses would otherwise accumulate handlers.
        // attachUserScrollIntent is the shared input set, so a scrollbar drag
        // or a keyboard scroll aborts the glide too — wheel/touch alone let the
        // animation override both.
        detachSmoothAbort()
        const detachIntent = attachUserScrollIntent(el, abort)
        smoothAbortDetachRef.current = () => {
          detachIntent()
          smoothAbortDetachRef.current = null
        }
      }
    },
    [detachSmoothAbort],
  )

 const pinAuto = useCallback(() => {
    const el = scrollerRef.current
    if (!el) return
    // An in-flight smooth pin is OUR scroll, and mid-glide `scrollTop` sits
    // below the recorded target while still being meaningfully away from the
    // bottom — which is exactly evaluateAutoPin's user-scroll-up signature. A
    // ResizeObserver tick during the glide (streaming output resizes constantly)
    // therefore released follow and left the rest of the response behind. The
    // scroll handler already exempts in-flight glides; this path did not.
    //
    // Preserve follow and do NOT write: re-issuing a smooth scroll every resize
    // tick would cancel and restart the animation each time. Content appended
    // mid-glide is instead re-targeted the
    // moment the glide lands — the arrival branch of the scroll handler runs
    // pinAuto(), which then snaps instantly to the new bottom.
    if (smoothPinActiveRef.current) return
    const geom = { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }
    const result = evaluateAutoPin({
      stick: stickRef.current,
      geom,
      lastWriteTop: lastWriteTopRef.current,
    })
    stickRef.current = result.stick
    if (result.pin) {
      writeScrollTop(el, result.target, 'auto', 'pin')
    } else if (result.stick) {
      // Still following but already at the bottom (no write needed) — keep the
      // self-scroll reference aligned with the current bottom.
      lastWriteTopRef.current = result.target
    }
  }, [scrollerRef, writeScrollTop])

  // Forced pin: explicit jump-to-bottom (slot entry, scrollToBottom API,
  // jump-to-latest pill). Always lands at the bottom and (re-)arms follow.
  const forcePin = useCallback(() => {
    const el = scrollerRef.current
    if (!el) return
    stickRef.current = followOutput
    const target = bottomTarget({ scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight })
    writeScrollTop(el, target, 'auto', 'pin')
  }, [followOutput, scrollerRef, writeScrollTop])

  // Keep the tracked scroller element in sync after every commit, so the
  // observer effects below re-attach the moment the node appears (or changes).
  useEffect(() => {
    syncScrollerEl()
  })

  // ---- Passive scroll listener: isAtBottom + user-scroll stick update ----
  const scrollRafScheduledRef = useRef(false)
  useEffect(() => {
    const el = scrollerEl
    if (!el) return
    let rafId = 0
    const onScroll = () => {
      const geom = { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }
      const atBottom = computeAtBottom(geom, bottomThreshold)
      setIsAtBottom((prev) => {
        if (prev === atBottom) return prev
        return atBottom
      })
      // Only a genuine USER scroll updates stick. Our own programmatic pins
      // fire scroll events too; isSelfScroll filters them out so they never
      // flip stick. (Releasing on user scroll-up also happens synchronously
      // inside pinAuto via the live-scrollTop guard — this handler covers the
      // common case and re-arming when the user returns to the bottom.)
      // During a smooth-pin animation, intermediate scroll events are ours —
      // don't treat them as user scrolls.
      if (smoothPinActiveRef.current) {
        // Arrived: the glide is over, so drop its abort listeners.
        //
        // Arrival is measured against the value we actually WROTE
        // (`lastWriteTopRef`), not `atBottom`. `atBottom` uses the 100px UI
        // threshold, which the glide enters while the native animation still
        // has up to 100px to run; disarming there left the remaining animation
        // un-abortable, so a user grabbing the page inside that band would be
        // scrolled to the bottom anyway. `isSelfScroll` compares against the
        // pin target within SELF_SCROLL_EPSILON, so we disarm only once the
        // animation has genuinely landed. `bottomAnchored` is the fallback for
        // a pin whose target was clamped by the browser (a shrinking
        // scrollHeight can leave scrollTop short of the requested value
        // forever, which would otherwise leak the listeners).
        const bottomAnchored =
          geom.scrollHeight - (geom.scrollTop + geom.clientHeight) <= SELF_SCROLL_EPSILON
        if (isSelfScroll(el.scrollTop, lastWriteTopRef.current) || bottomAnchored) {
          smoothPinActiveRef.current = false
          detachSmoothAbort()
          // Content appended DURING the glide moved the bottom, and pinAuto
          // deliberately declined to re-target mid-animation (restarting a
          // smooth scroll every resize tick stutters). Now that the animation
          // has landed, correct the shortfall instantly.
          pinAuto()
        }
        // If the user grabs the page mid-animation and scrolls up,
        // scrollTop moves backward. Normal forward animation progress
        // always increases scrollTop toward the target.
        else if (el.scrollTop < prevSmoothTopRef.current - 1) {
          smoothPinActiveRef.current = false
          lastUserScrollAtRef.current = performance.now()
          stickRef.current = false
          detachSmoothAbort()
        }
        prevSmoothTopRef.current = el.scrollTop
      } else if (!isSelfScroll(el.scrollTop, lastWriteTopRef.current)) {
        lastUserScrollAtRef.current = performance.now()
        stickRef.current = stickAfterUserScroll(atBottom, followOutput)
        // A scroll we did not write that leaves us EXACTLY at the bottom was the
        // layout engine's: the browser clamps scrollTop when a shrinking
        // scrollHeight drops the maximum below it, and a spacer re-estimate does
        // the same. Re-baseline the self-scroll reference to where we now are —
        // otherwise it keeps pointing at our last write, and the next pin
        // evaluation reads that gap as a user scroll-up, releasing follow for the
        // rest of the turn with only a manual scroll back to the bottom able to
        // re-arm it.
        //
        // The test is the CLAMP — distance within SELF_SCROLL_EPSILON — and NOT
        // the 100px `atBottom` UI band. A real 3-100px scroll-up also keeps
        // `stick` armed via stickAfterUserScroll, so re-baselining across that
        // band would erase the only evidence evaluateAutoPin has of it and yank
        // the user back to the bottom on the next append.
        const clampedAtBottom =
          geom.scrollHeight - (geom.scrollTop + geom.clientHeight) <= SELF_SCROLL_EPSILON
        if (stickRef.current && clampedAtBottom) lastWriteTopRef.current = el.scrollTop
      }
      // Persist the reading position once this scroll burst settles (also
      // clears it when the burst ends at the bottom). Scheduled for self-
      // scrolls too — see scheduleAnchorSave. The context snapshot is what
      // lets a slot switch inside the debounce window flush this burst
      // against the OUTGOING session's items (see the session sentinel).
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
    onScroll()
    return () => {
      el.removeEventListener('scroll', onScroll)
      // Cancel any frame queued by the last scroll so it can't fire a
      // setWindowRange after unmount/re-run. Reset the ref too, or a re-run
      // would see it stuck true and never schedule again.
      if (rafId) cancelAnimationFrame(rafId)
      scrollRafScheduledRef.current = false
    }
  }, [scrollerEl, bottomThreshold, followOutput, recomputeWindow, detachSmoothAbort, pinAuto, scheduleAnchorSave])

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

      let genuineResize = false
      let firstMount = false
      // True when the SCROLLER's own box resized (the observer watches it
      // alongside the rows). Chrome around the transcript changes the viewport
      // height with no scroll event and no row resize — the composer autosizes
      // when a slot switch restores a long draft, attachment strips and
      // banners mount, the browser window resizes. A viewport SHRINK while
      // pinned leaves scrollTop at the old, now-too-small bottom target — the
      // view rests slightly above the latest message ("switching sessions
      // doesn't land at the bottom"). A GROW is clamped by the browser itself.
      // Routed through pinAuto below, so the race-proof guard still applies:
      // with follow released (reading history, anchor restore in flight) a
      // viewport resize never moves the viewport.
      let viewportResized = false
      // True when one of the resized entries is the caller-designated
      // streaming row (see `streamingIndex` option / syncHeightsNow's doc).
      let streamingRowResized = false
      for (const entry of entries) {
        if (entry.target === el) {
          viewportResized = true
          continue
        }
        const idx = elIndexRef.current.get(entry.target)
        if (idx === undefined) continue
        const it = itemsRef.current[idx]
        if (!it) continue
        const newH = measureBorderBoxHeight(entry.target as HTMLElement)
        const k = getKeyRef.current(it, idx)
        const prevH = cacheRef.current!.get(k)
        if (prevH !== newH) {
          cacheRef.current!.set(k, newH)
          // First-mount (prev undefined) happens during scroll-driven window
          // expansion; re-pinning then would interrupt the user's scroll. Only
          // genuine resizes (streaming growth, widget load) drive the pin —
          // EXCEPT while actively following (see below).
          if (prevH !== undefined) {
            genuineResize = true
            // Immediate (non-debounced) sync for the actively-streaming row OR
            // the row still inside its post-stream settle grace. The
            // grace is a FIXED window from stream completion and is deliberately
            // NOT re-armed here: re-arming per resize would let an oscillating
            // auto-height widget in a just-ended message keep the row immediate
            // forever, defeating the debounce's render-storm protection.
            if (idx === streamingIndexRef.current || idx === graceIndexRef.current) {
              streamingRowResized = true
            }
          } else {
            firstMount = true
          }
        }
      }

      // ---- Rail-collapse settle window ----
      // The shell animates `grid-template-columns` for 150ms, so the content
      // column's width changes on EVERY frame of the collapse and every mounted
      // row rewraps. Measured in isolation, that multiplies this observer's
      // fires and its forced `offsetHeight` reads by 13-18x per toggle — and the
      // final cached heights come out identical, so all of the extra work is
      // discarded. The cache updates above are kept (layout is already dirty, so
      // reading is cheap, and this leaves no stale heights); what is held back
      // is the part that thrashes: the `pinAuto()` scrollTop WRITE interleaved
      // between those reads, the height-sync re-render, and the window
      // recompute. Exactly one sync — plus one re-pin if we were following —
      // runs when the window closes.
      //
      // The actively-streaming row is deliberately EXEMPT: stalling ITS growth
      // for the length of the animation re-creates the spacer lurch that
      // `streamingIndex`'s immediate path exists to prevent. Collapsing the rail
      // mid-turn is rare; a visible lurch is not an acceptable trade for it.
      //
      // The viewport entry takes this deferral too: the animation resizes the
      // scroller's box on every frame, and a per-frame viewport pin is exactly
      // the write storm this window exists to hold back.
      if ((genuineResize || firstMount || viewportResized) && !streamingRowResized && isRailSettling()) {
        railSettleFollowRef.current = railSettleFollowRef.current || stickRef.current
        if (railSettleTimerRef.current === null) {
          railSettleTimerRef.current = setTimeout(() => {
            railSettleTimerRef.current = null
            const shouldRepin = railSettleFollowRef.current
            railSettleFollowRef.current = false
            syncHeightsNow()
            if (shouldRepin) pinAuto()
            recomputeWindow(true)
          }, RAIL_SETTLE_MS)
        }
        return
      }

      // Follow streaming/widget growth — but only while the user is NOT
      // actively scrolling. A widget that re-measures mid-fling must not yank
      // the user to the bottom (which would also unmount the rows they were
      // scrolling through). pinAuto itself is still race-proof for the
      // stationary case.
      //
      // A first-mount normally must NOT pin (it fires during scroll-up window
      // expansion and would yank the user). But while we're actively following
      // (stick armed), a freshly mounted tall row at the bottom is genuinely
      // new content to follow — e.g. a widget rendering inside the streaming
      // message right as the turn re-keys (single → grouped turn) and remounts
      // the row, which otherwise looks like a first-mount and skips the pin.
      // pinAuto still releases if the live geometry shows a real scroll-up.
      // A viewport resize is likewise only followed while following — with
      // stick released it must never move a reading user.
      const shouldFollow =
        genuineResize || ((firstMount || viewportResized) && stickRef.current)
      if (shouldFollow && (stickRef.current || performance.now() - lastUserScrollAtRef.current >= SCROLL_SETTLE_MS)) {
        pinAuto()
      }

      // A measured height changed in place — schedule a re-sync of the offset
      // memos (see scheduleHeightSync). Debounced by default so a continuously
      // oscillating widget can't drive a per-frame render storm; the
      // caller-designated streaming row bypasses that debounce (immediate)
      // since ITS growth needs to track every tick, not settle-then-jump.
      // Under `eagerFirstMeasure` a FIRST measurement bypasses it too: it
      // happens once per row, so it cannot be an oscillation, and debouncing
      // it lets a scroll-driven mounting streak starve the sync (see the seed
      // path in measureRef and the option doc).
      if (genuineResize || firstMount) {
        scheduleHeightSync(streamingRowResized || (firstMount && eagerFirstMeasureRef.current))
      }

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
    // Observe the scroller's own box (the viewport branch above) — after the
    // rows, so row-position assumptions about observation order keep holding.
    // A re-created observer must re-observe it here; the `scrollerEl` effect
    // below covers a scroller that mounts later than this effect.
    if (scrollerRef.current) ro.observe(scrollerRef.current)
    return () => {
      ro.disconnect()
      // Cancel a frame queued by the last resize so it can't fire a
      // setWindowRange after the observer is torn down.
      if (rafId) cancelAnimationFrame(rafId)
      // Same for the rail-settle timer: it calls syncHeightsNow / pinAuto /
      // recomputeWindow, all of which touch state and the scroller, so a
      // survivor would run against a torn-down consumer.
      if (railSettleTimerRef.current) {
        clearTimeout(railSettleTimerRef.current)
        railSettleTimerRef.current = null
      }
      railSettleFollowRef.current = false
      resizeObserverRef.current = null
    }
  }, [recomputeWindow, pinAuto, scheduleHeightSync, syncHeightsNow, scrollerRef])

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
  }, [scrollerEl])

  // ---- IntersectionObserver: top/bottom sentinels for window expansion ----
  useEffect(() => {
    const root = scrollerEl
    if (!root) return
    if (typeof IntersectionObserver === 'undefined') return

    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue
          if (entry.target === topSentinelRef.current) {
            // Upward expansion mounts rows above the viewport. Capture
            // the top anchor first (while stick is released — reading history)
            // so the compensation layout effect can hold the viewport steady.
            //
            // Only when there is actually room to expand: at start === 0
            // expandWindowUp is a no-op, so a captured anchor would never be
            // consumed by an upward shift and would instead be applied to the
            // NEXT layout pass — a downward expansion — yanking the viewport
            // back to a stale row.
            if (!stickRef.current && windowRangeRef.current.start > 0) {
              const a = captureTopAnchor()
              if (a) anchorPendingRef.current = a
            }
            setWindowRange((prev) => expandWindowUp(prev, overscan))
            onTopReachedRef.current?.()
          } else if (entry.target === bottomSentinelRef.current) {
            setWindowRange((prev) => expandWindowDown(prev, itemsRef.current.length, overscan))
          }
        }
      },
      { root, rootMargin: '200px 0px' },
    )

    if (topSentinelRef.current) io.observe(topSentinelRef.current)
    if (bottomSentinelRef.current) io.observe(bottomSentinelRef.current)
    return () => io.disconnect()
  }, [overscan, scrollerEl, captureTopAnchor])

  // ---- Scroll-anchor preservation: hold the viewport steady across an
  //      upward window shift ----
  //
  // Runs after every windowRange commit. If recomputeWindow / top-sentinel
  // expansion captured a top anchor (only on an upward shift while stick is
  // released), re-read that row's screen position in the freshly-committed DOM
  // and correct scrollTop by the delta so the row the user is looking at does
  // not jump when rows mount/unmount above it. Skipped when stick is armed (the
  // pin path owns positioning) or nothing was captured. The scrollTop write is
  // recorded in lastWriteTopRef so the passive scroll listener classifies it as
  // a self-scroll (isSelfScroll / SELF_SCROLL_EPSILON) and does not release
  // stick or treat it as a user scroll.
  useLayoutEffect(() => {
    const pending = anchorPendingRef.current
    anchorPendingRef.current = null
    if (!pending) return
    const el = scrollerRef.current
    if (!el || stickRef.current || typeof el.getBoundingClientRect !== 'function') return
    let node: HTMLElement | null = null
    for (const [n, idx] of elIndexRef.current.entries()) {
      const it = itemsRef.current[idx]
      if (it && getKeyRef.current(it, idx) === pending.key) {
        node = n as HTMLElement
        break
      }
    }
    if (!node) return
    const srTop = el.getBoundingClientRect().top
    const newTop = node.getBoundingClientRect().top - srTop
    const delta = newTop - pending.top
    if (Math.abs(delta) > 0.5) {
      // Instant, and accounted as a 'pin' write: this is our own correction, so
      // the follow guard must recognise the resulting scroll event as self-scroll
      // rather than user input. Routed through the chokepoint so the accounting
      // cannot be forgotten here (see writeScrollTop).
      writeScrollTop(el, el.scrollTop + delta, 'auto', 'pin')
    }
  }, [windowRange, scrollerRef, writeScrollTop])

  /**
   * Prepend compensation, part 1: re-base the window by the inserted count so
   * the rows being read stay mounted — including the anchor row, which part 2
   * has to measure. Runs pre-paint, so the shifted-but-uncorrected frame is
   * never shown.
   */
  useLayoutEffect(() => {
    const inserted = prependCountRef.current
    if (inserted <= 0) return
    prependCountRef.current = 0
    if (stickRef.current || !prependAnchorRef.current) {
      prependAnchorRef.current = null
      return
    }
    prependRefineRef.current = true
    setWindowRange((r) => ({
      start: Math.min(itemCount, r.start + inserted),
      end: Math.min(itemCount, r.end + inserted),
    }))
  }, [itemCount])

  /**
   * Prepend compensation, part 2: re-read the anchor row in the shifted DOM and
   * move scrollTop by however far it travelled, which holds the user's place
   * whatever mix of inserted rows and re-estimated heights caused the shift.
   *
   * INVARIANT — the passive itemCount recompute runs BETWEEN these two layout
   * effects; part 2 must re-derive the window after correcting scrollTop.
   *
   * Then recompute the window, because the passive itemCount recompute has
   * already run by this point — React flushes it between these two layout
   * effects — so it sized the window from the PRE-correction offset and its
   * update lands after this write. Re-deriving from the corrected scrollTop is
   * what stops that stale range being the one left committed.
   */
  useLayoutEffect(() => {
    if (!prependRefineRef.current) return
    prependRefineRef.current = false
    const pending = prependAnchorRef.current
    prependAnchorRef.current = null
    const el = scrollerRef.current
    if (!pending || !el || stickRef.current) return
    const newTop = rowTopFrom(el, elIndexRef.current.entries(), (idx) => {
      const it = itemsRef.current[idx]
      return it ? getKeyRef.current(it, idx) : null
    }, pending.key)
    if (newTop === null) return
    const delta = newTop - pending.top
    if (Math.abs(delta) > 0.5) writeScrollTop(el, el.scrollTop + delta, 'auto', 'pin')
    recomputeWindow()
  }, [windowRange, scrollerRef, writeScrollTop, recomputeWindow])

  // ---- Follow-output: pin to bottom when items append ----
  const prevItemCountRef = useRef(itemCount)
  useLayoutEffect(() => {
    const el = scrollerRef.current
    if (!el) return
    const growth = itemCount - prevItemCountRef.current
    prevItemCountRef.current = itemCount
    if (growth <= 0) return
    // BULK growth while followed is history hydration, not streaming: the
    // slot-detail fetch resolving and REPLACING a thin optimistic list (e.g.
    // a lone WS streaming bubble that landed before the fetch — it consumed
    // the slot-entry one-shot pin) with the full conversation. Routing that
    // through pinAuto smooth-glides from the top across hundreds of
    // virtualized rows, visibly "paging" through the conversation and often
    // landing short while heights are still estimates. Treat it like slot
    // entry instead: remount the tail window and force-pin instantly.
    // Gated on stick so a "load older" prepend while the user reads history
    // is never yanked to the bottom.
    if (growth > overscan + 1 && stickRef.current) {
      setWindowRange({ start: Math.max(0, itemCount - (overscan + 1)), end: itemCount })
      forcePin()
      const id = requestAnimationFrame(() => {
        // Recheck stick: the user can scroll up between the synchronous pin
        // and this frame — the scroll handler releases stick, and an
        // unconditional forcePin here would yank them back and re-arm follow.
        if (!el.isConnected || !stickRef.current) return
        forcePin()
      })
      return () => cancelAnimationFrame(id)
    }
    // Pin synchronously (pre-paint) so a new message appears at the bottom
    // without a flicker, then once more next frame after its real height is
    // known. Both go through the race-proof pinAuto.
    pinAuto()
    const id = requestAnimationFrame(() => {
      if (!el.isConnected) return
      pinAuto()
    })
    return () => cancelAnimationFrame(id)
  }, [itemCount, overscan, pinAuto, forcePin, scrollerRef])

  // ---- Reading-position restore (see ScrollAnchorCache) ----

  /** Index of the row whose virtual key matches `key`, or -1. O(N), runs at
   *  most once per slot entry. */
  const findAnchorIndex = useCallback((key: string): number => {
    const its = itemsRef.current
    for (let i = 0; i < its.length; i++) {
      if (getKeyRef.current(its[i], i) === key) return i
    }
    return -1
  }, [])

  // Restore a saved reading position: mount a window around the anchored row
  // and place it back at the saved viewport offset — instead of the slot-entry
  // bottom pin. Positioning is anchored to the ROW, not a raw scrollTop: a raw
  // pixel offset is meaningless before rows are measured (the historical
  // "lands in the middle" bug), while the row's content offset is exact once
  // its window commits, warm from the persisted HeightCache on a revisit, and
  // corrected against live DOM geometry by the settle frames below.
  //
  // Follow stays RELEASED (the restore is mid-history by definition):
  // streaming output must not pull the view down — the jump-to-latest pill is
  // the way back, mirroring how a manual scroll-up behaves.
  //
  // Returns the settle-frame cleanup for the calling layout effect.
  const restoreAnchor = useCallback(
    (index: number, anchor: ScrollAnchor): (() => void) | undefined => {
      const el = scrollerRef.current
      if (!el) return undefined
      const count = itemsRef.current.length
      setWindowRange(computeJumpWindow(index, count, overscan))
      stickRef.current = false
      setIsAtBottom(false)
      // Initial position from offset math, synchronously (pre-paint — the
      // first painted frame is already at the restored position, no flash):
      // scrollTop such that the row's content offset sits `anchor.top` px
      // below the viewport top. The browser clamps an out-of-range value
      // against the not-yet-committed jump window; the settle frames re-land
      // it once the new spacers have committed.
      //
      // Accounted as 'pin': this is OUR positioning write, so the follow
      // guard must classify the resulting scroll event as self-scroll rather
      // than user input (stick is already false; recording the position does
      // not re-arm it — evaluateAutoPin never pins with stick released).
      const idxTree = offsetIndexRef.current
      const off = idxTree ? idxTree.offsetOf(index) : getOffsetFn(index, count, getH)
      const target = Math.max(0, off - anchor.top)
      writeScrollTop(el, target, 'auto', 'pin')
      // The write clamps against the CURRENT (pre-jump-window) geometry; align
      // the self-scroll reference with the value that actually landed so the
      // resulting scroll event is classified as ours, not user input (which
      // would trip the settle frames' user-scroll abort below).
      lastWriteTopRef.current = el.scrollTop
      // Settle: for a few frames, correct against the anchor row's LIVE DOM
      // position as measurements land (rows above it refine from estimates).
      // Aborts on a genuine user scroll (lastUserScrollAtRef — restore writes
      // are accounted as self-scrolls, so only real input trips it), a session
      // change, a disconnected scroller, or the row's key no longer matching.
      // A degenerate rect (height 0 — jsdom, or not yet laid out) skips the
      // correction rather than applying garbage.
      const startedAt = typeof performance !== 'undefined' ? performance.now() : Date.now()
      const session = sessionIdRef.current
      let raf = 0
      let n = 0
      const settle = () => {
        raf = 0
        if (!el.isConnected || sessionIdRef.current !== session) return
        if (lastUserScrollAtRef.current > startedAt) return
        const it = itemsRef.current[index]
        if (!it || getKeyRef.current(it, index) !== anchor.key) return
        let node: HTMLElement | null = null
        for (const [nEl, i] of elIndexRef.current.entries()) {
          if (i === index) { node = nEl as HTMLElement; break }
        }
        if (
          node &&
          typeof node.getBoundingClientRect === 'function' &&
          typeof el.getBoundingClientRect === 'function'
        ) {
          const rect = node.getBoundingClientRect()
          if (rect.height > 0) {
            const delta = rect.top - el.getBoundingClientRect().top - anchor.top
            if (Math.abs(delta) > 0.5) writeScrollTop(el, el.scrollTop + delta, 'auto', 'pin')
          }
        }
        if (++n < ANCHOR_RESTORE_SETTLE_FRAMES) raf = requestAnimationFrame(settle)
      }
      raf = requestAnimationFrame(settle)
      return () => { if (raf) cancelAnimationFrame(raf) }
    },
    [overscan, getH, scrollerRef, writeScrollTop],
  )

  // ---- Slot entry: restore the saved reading position, else force the
  //      scroller to the true bottom ----
  // Runs after the new session's tail window has committed (windowRange reset
  // during render), before paint. Deterministic — does not inherit the
  // previous session's scrollTop (fixes the "second visit lands in the middle"
  // bug). Subsequent async widget growth is then followed by the RO via
  // pinAuto. A follow-up rAF settles after first-frame measurement.
  //
  // ALSO re-runs when items first arrive for a freshly-entered slot
  // (`sessionId` flips synchronously on slot switch, BEFORE the messages
  // HTTP fetch resolves — without the itemCount trigger forcePin would only
  // run against an empty list, leaving pinAuto to smooth-animate the
  // viewport down once content lands. That smooth scroll is the visible
  // "content scrolls from top to bottom" CX bug — and a late widget/image
  // measurement during the animation can land it short of the true bottom).
  // `slotPinDoneRef` guarantees the instant re-pin fires at most once per
  // slot entry; subsequent streaming appends still go through pinAuto.
  //
  // A latched reading-position anchor (pendingRestoreRef) takes precedence:
  // once items are present and the anchored row is found, restoreAnchor runs
  // INSTEAD of the bottom pin. While waiting for items, nothing pins — a
  // bottom pin's scroll events would let the debounced save clear the very
  // anchor being restored. An anchor whose row no longer exists (edited /
  // truncated transcript, or a non-durable minted key, or a race where the
  // key arrives with a later hydration chunk) falls back to the default pin.
  const slotPinDoneRef = useRef<string | null>(null)
  useLayoutEffect(() => {
    if (slotPinDoneRef.current && slotPinDoneRef.current !== sessionId) {
      slotPinDoneRef.current = null
    }
    if (slotPinDoneRef.current === sessionId) return
    const anchor = pendingRestoreRef.current
    if (anchor) {
      if (itemCount === 0) return // wait for content; effect re-runs when items arrive
      const idx = findAnchorIndex(anchor.key)
      pendingRestoreRef.current = null
      if (idx >= 0) {
        slotPinDoneRef.current = sessionId
        return restoreAnchor(idx, anchor)
      }
      // Anchored row not found — re-arm follow (the sentinel released it in
      // anticipation of a restore) and take the default placement path.
      stickRef.current = followOutput
    }
    if (initialPlacement === 'top') {
      // Head placement: a fresh scroller already sits at 0, but an INHERITED
      // one (externalScrollerRef pointing at a page column that outlives this
      // hook) can carry leftover scrollTop from whatever it showed before.
      // Write 0 explicitly — accounted as 'pin' so the follow guard reads the
      // resulting scroll event as ours. No second-frame write is needed: at
      // the head there is nothing above the viewport to re-clamp against.
      if (itemCount === 0) return // wait for content; effect re-runs when items arrive
      slotPinDoneRef.current = sessionId
      const el = scrollerRef.current
      if (el && el.scrollTop !== 0) writeScrollTop(el, 0, 'auto', 'pin')
      return
    }
    forcePin()
    if (itemCount === 0) return  // wait for content; effect re-runs when items arrive
    slotPinDoneRef.current = sessionId
    const id = requestAnimationFrame(() => {
      const el = scrollerRef.current
      if (el && el.isConnected) forcePin()
    })
    return () => cancelAnimationFrame(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, scrollerEl, itemCount])

  // ---- Recompute window when item count changes ----
  useEffect(() => {
    recomputeWindow()
  }, [itemCount, recomputeWindow])

  // ---- measureRef: per-item ref callback (memoized per index) ----
  //
  // Returning a STABLE function identity for a given index is critical. React
  // only re-invokes a ref callback when its identity changes (or the element
  // mounts/unmounts). The naive `(index) => (el) => …` minted a fresh closure
  // on every render, so React detached (called with null) and reattached every
  // mounted row each render — and each reattach runs unobserve()+observe() on
  // the shared ResizeObserver. The chat re-renders on every streaming chunk,
  // so that fired synchronous RO churn for all mounted rows each frame, a
  // measurable source of scroll jank. Caching the callback by index means a
  // row that stays mounted keeps the same ref and React never re-invokes it;
  // observe/unobserve then happen only on genuine mount/unmount. Indices are
  // positional and reused across sessions, so the cache stays bounded by the
  // max item count and the closures read live state through refs.
  const measureRefCacheRef = useRef<Map<number, (el: HTMLElement | null) => void>>(new Map())
  const measureRef = useCallback((index: number) => {
    const cache = measureRefCacheRef.current
    const existing = cache.get(index)
    if (existing) return existing
    const fn = (el: HTMLElement | null) => {
      const ro = resizeObserverRef.current
      for (const [oldEl, oldIdx] of elIndexRef.current.entries()) {
        if (oldIdx === index && oldEl !== el) {
          elIndexRef.current.delete(oldEl)
          ro?.unobserve(oldEl)
        }
      }
      if (el) {
        elIndexRef.current.set(el, index)
        ro?.observe(el)
        // Seed the cache with the current height so the next render has a real
        // height for placeholders. A changed value must also bump
        // heightVersion: this seed is the SECOND cache writer (besides the RO)
        // and the RO won't re-fire for a value we just seeded, so without this
        // the offset memos keep a stale height and leave a phantom spacer.
        const it = itemsRef.current[index]
        if (it) {
          const k = getKeyRef.current(it, index)
          const h = measureBorderBoxHeight(el)
          if (h > 0 && cacheRef.current!.get(k) !== h) {
            cacheRef.current!.set(k, h)
            // Eager (per the option): this branch fires at most once per row
            // (the guard above skips re-attaches whose height is already
            // cached), so it cannot be the render storm the debounce guards
            // against. Under a scroll-driven mounting streak the debounced
            // path starves — each seed resets the timer — leaving the offset
            // tree frozen at estimates for the whole gesture; see the option
            // doc on UseVirtualChatOptions.eagerFirstMeasure. Default (chat)
            // keeps the debounce so the upward-anchor compensation's commit
            // ordering is untouched.
            scheduleHeightSync(eagerFirstMeasureRef.current)
          }
        }
      }
    }
    cache.set(index, fn)
    return fn
  }, [scheduleHeightSync])

  // ---- scrollToIndex / scrollToBottom imperative APIs ----

  const scrollToIndex = useCallback(
    (index: number, options?: ScrollToIndexOptions) => {
      const el = scrollerRef.current
      if (!el) return
      const count = itemsRef.current.length
      if (count === 0) return
      const t = Math.max(0, Math.min(count - 1, Math.floor(index)))
      setWindowRange(computeJumpWindow(t, count, overscan))
      requestAnimationFrame(() => {
        const off = getOffsetFn(t, count, getH)
        const align = options?.align ?? 'start'
        const behavior = options?.behavior ?? 'auto'
        const itemH = getH(t)
        let scrollTop = off
        if (align === 'center') scrollTop = off - el.clientHeight / 2 + itemH / 2
        else if (align === 'end') scrollTop = off - el.clientHeight + itemH
        scrollTop = Math.max(0, Math.min(el.scrollHeight - el.clientHeight, scrollTop))
        // Jumping to a specific index is an explicit "stop following" intent.
        stickRef.current = false
        writeScrollTop(el, scrollTop, behavior, 'release')
      })
    },
    [overscan, getH, scrollerRef, writeScrollTop],
  )

  // "Human-like" smooth scroll to a (possibly off-window) index. UNLIKE
  // scrollToIndex/mountIndex, it does NOT pre-mount a window: it computes the
  // target's pixel scrollTop from cached heights and animates the scroller
  // there, letting the passive scroll listener's full (shrinking) recompute
  // mount rows progressively and keep the window TIGHT — exactly like a user
  // dragging the scrollbar. mountIndex's wide union, combined with expand-only
  // RO recompute, would instead leave a broad span of rows mounted; any
  // animated/auto-height widget in that span keeps oscillating and
  // re-rendering long after the jump. Progressive mounting avoids that.
  const scrollToIndexSmooth = useCallback(
    (index: number, options?: { align?: 'start' | 'center'; offset?: number }) => {
      const el = scrollerRef.current
      if (!el) return
      const count = itemsRef.current.length
      if (count === 0) return
      const t = Math.max(0, Math.min(count - 1, Math.floor(index)))
      // Derive the header offset (px from the scroller's scroll origin to the
      // start of list content) from any currently-mounted row, so the target
      // scrollTop is accurate without hardcoding the header spacer height.
      // Derive headerPx (px from the scroller's scroll origin to the start of
      // list content) from the LOWEST-index mounted row, not the first Map
      // entry — Map iteration is insertion order, effectively arbitrary among
      // mounted rows. For a correctly-measured row the value is invariant, but
      // pinning the reference to the smallest index keeps far smooth-jumps
      // deterministic and reproducible even if some row's cached height is
      // momentarily stale mid-resize.
      let headerPx = 0
      const srTop = el.getBoundingClientRect().top
      let refIdx = Infinity
      let refNode: HTMLElement | null = null
      for (const [node, idx] of elIndexRef.current.entries()) {
        if (idx < refIdx) { refIdx = idx; refNode = node as HTMLElement }
      }
      if (refNode) {
        headerPx = refNode.getBoundingClientRect().top - srTop + el.scrollTop - getOffsetFn(refIdx, count, getH)
      }
      const off = getOffsetFn(t, count, getH)
      const itemH = getH(t)
      let top = headerPx + off
      if (options?.align === 'center') top = top - el.clientHeight / 2 + itemH / 2
      top += options?.offset ?? 0
      top = Math.max(0, Math.min(el.scrollHeight - el.clientHeight, top))
      // Explicit navigation — stop following.
      stickRef.current = false
      writeScrollTop(el, top, 'smooth', 'release')
    },
    [getH, scrollerRef, writeScrollTop],
  )

  const scrollToBottom = useCallback(
    (behavior: ScrollBehavior = 'auto') => {
      const el = scrollerRef.current
      if (!el) return
      const count = itemsRef.current.length
      if (count === 0) return
      // Mount the tail so the bottom items have real heights, then force-pin.
      setWindowRange({ start: Math.max(0, count - (overscan + 1)), end: count })
      // Arm follow immediately so a streaming chunk that lands between now and
      // the rAF is also followed.
      stickRef.current = followOutput
      const pinToBottom = (b: ScrollBehavior) => {
        const target = bottomTarget({ scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight })
        stickRef.current = followOutput
        writeScrollTop(el, target, b, 'pin')
      }
      requestAnimationFrame(() => {
        pinToBottom(behavior)
        // Settle: the tail window only just committed and its rows (widgets,
        // markdown) may finish measuring over the next few frames, moving the
        // true bottom down — otherwise an instant jump lands on a stale,
        // slightly-short target ("doesn't reach the end"). Re-pin over a few
        // frames so it lands exactly at the bottom. Skipped for smooth scrolls
        // (an instant re-pin mid-glide would cut the animation short); ongoing
        // streaming growth is handled by the ResizeObserver follow instead.
        if (behavior !== 'auto') return
        let n = 0
        const settle = () => {
          if (!el.isConnected || !stickRef.current) return
          pinToBottom('auto')
          if (++n < 3) requestAnimationFrame(settle)
        }
        requestAnimationFrame(settle)
      })
    },
    [overscan, followOutput, scrollerRef, writeScrollTop],
  )

  // Ensure `index` is mounted (in the window) so callers can scroll to an
  // off-window target. Near targets union with the current window (no flash);
  // far targets jump (replace) to avoid mounting thousands of rows in between.
  //
  // Returns `true` when it took the FAR path (window replaced, leaving an
  // unmounted gap between the old viewport and the target). Callers use this
  // to pick scroll behavior: a smooth glide across a far jump would scrub the
  // scroller through blank spacer (visible flicker), so callers should
  // teleport (instant) on a far jump and only glide on a near one.
  const mountIndex = useCallback(
    (index: number): boolean => {
      const count = itemsRef.current.length
      if (count === 0) return false
      const t = Math.max(0, Math.min(count - 1, Math.floor(index)))
      const jump = computeJumpWindow(t, count, overscan)
      // Decide near/far from the latest committed window (ref, not `prev`) so
      // we can return the decision synchronously to the caller.
      const cur = windowRangeRef.current
      const far = !(jump.start <= cur.end + overscan * NEAR_JUMP_OVERSCAN_MULT && jump.end >= cur.start - overscan * NEAR_JUMP_OVERSCAN_MULT)
      setWindowRange((prev) => {
        const near = jump.start <= prev.end + overscan * NEAR_JUMP_OVERSCAN_MULT && jump.end >= prev.start - overscan * NEAR_JUMP_OVERSCAN_MULT
        if (near) return { start: Math.min(prev.start, jump.start), end: Math.max(prev.end, jump.end) }
        return jump
      })
      return far
    },
    [overscan],
  )

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
      const cached = cacheRef.current!.get(key)
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
  }, [items, itemCount, windowRange.start, windowRange.end, getKey, estimatedHeight, isSticky])

  // ---- Debug probe (zero behavior change) ----
  // Exposes window.__vcSnapshot() for diagnosing scroll/geometry bugs (e.g.
  // the blank-space-after-jump regression). Call it in devtools the moment the
  // bug is visible to dump live geometry + a cached-vs-DOM height comparison.
  // Harmless in prod (a single tiny global); install last-mount-wins.
  useEffect(() => {
    if (typeof window === 'undefined') return
    const snapshot = () => {
      const el = scrollerRef.current
      const count = itemsRef.current.length
      // Mounted rows: read true DOM height vs what the cache believes.
      const rows: { index: number; cached: number | undefined; dom: number; delta: number }[] = []
      for (const [node, idx] of elIndexRef.current.entries()) {
        const it = itemsRef.current[idx]
        const key = it ? getKeyRef.current(it, idx) : ''
        const cached = key ? cacheRef.current!.get(key) : undefined
        const dom = (node as HTMLElement).offsetHeight
        rows.push({ index: idx, cached, dom, delta: dom - (cached ?? estimatedHeight) })
      }
      rows.sort((a, b) => a.index - b.index)
      // How many of ALL items have a real measurement vs fall back to estimate.
      let measured = 0
      for (let i = 0; i < count; i++) {
        const it = itemsRef.current[i]
        if (it && cacheRef.current!.get(getKeyRef.current(it, i)) !== undefined) measured++
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
        stick: stickRef.current,
        lastWriteTop: lastWriteTopRef.current,
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

  useEffect(() => {
    return () => {
      detachSmoothAbort()
      if (heightSyncTimerRef.current) clearTimeout(heightSyncTimerRef.current)
      if (graceTimerRef.current) clearTimeout(graceTimerRef.current)
      // Drop (not flush) a pending anchor save: at unmount time the rows'
      // layout is no longer trustworthy, and the last settled save already
      // captured the position the user actually read at.
      if (anchorSaveTimerRef.current) {
        clearTimeout(anchorSaveTimerRef.current)
        anchorSaveTimerRef.current = null
      }
      cacheRef.current?.flush()
    }
  }, [detachSmoothAbort])

  return {
    scrollerRef,
    contentRef,
    topSentinelRef,
    bottomSentinelRef,
    virtualItems,
    offsetBefore,
    offsetAfter,
    totalHeight,
    isAtBottom,
    scrollToIndex,
    scrollToIndexSmooth,
    scrollToBottom,
    mountIndex,
    measureRef,
  }
}
