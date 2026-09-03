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
  useSyncExternalStore,
} from 'react'
import { isRailSettling, RAIL_SETTLE_MS } from '../useRailWidth'
import { HeightIndex } from './HeightIndex'
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
} from './WindowCalculator'
import {
  computeAtBottom,
  isSelfScroll,
  SELF_SCROLL_EPSILON,
  heightAnchorStillUsable,
  repriceAboveFoldDelta,
  geometryCommitDeferred,
  resolveUserScrollStick,
  bottomTarget,
  evaluateAutoPin,
} from './FollowController'
import { noteUserScrollActivity } from '../../lib/scrollQuiet'
import type {
  UseVirtualChatOptions,
  UseVirtualChatReturn,
  VirtualItem,
  ScrollToIndexOptions,
} from './types'
import { composerExplainsViewportChange } from '../../utils/composerResize'

const DEFAULT_ESTIMATED = 80
const DEFAULT_OVERSCAN = 5

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
const DEFAULT_BOTTOM_THRESHOLD = 100
// After a genuine user scroll, suppress ResizeObserver-driven auto-pins for
// this long. Streaming/widget growth that should "follow" happens while the
// user is stationary at the bottom; a re-measuring widget that fires mid-fling
// must NOT yank the user (which also unmounts the rows they were scrolling
// through, leaving a blank flash). Explicit pins (slot entry, scrollToBottom,
// append) bypass this — only the RO follow path is gated.
export const SCROLL_SETTLE_MS = 150

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
/** A saved reading anchor must trace back to the USER's own scrolling. A
 *  self-inflicted displacement (a mis-clamped pin, native anchoring against a
 *  resizing neighbor) fires the same scroll events as a person and would
 *  persist the displaced position -- the next reload then restores it and the
 *  session "opens mid-transcript" with the displacement laundered into
 *  intent. Saving is therefore gated on HARD input (wheel / touch / scrollbar
 *  grab / scrolling keys -- attachUserScrollIntent's event set, plus a real
 *  grab that interrupts a smooth pin) within this window. CLEARING at the
 *  bottom stays unconditional: clearing only ever restores the default
 *  land-at-bottom, which is always safe. `lastUserScrollAtRef` is NOT usable
 *  here: the scroll handler stamps it for any non-clamp scroll event,
 *  including the browser's native-anchoring adjustments. */
const ANCHOR_SAVE_INTENT_WINDOW_MS = 3000

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
// qualifies or the environment has no layout (jsdom). `index` is the row's
// index as the mounted node carries it — the PREVIOUS commit's, for a caller
// resolving across a list change.
function captureTopAnchorFrom(
  el: HTMLDivElement,
  entries: Iterable<[Element, number]>,
  keyAt: (index: number) => string | null,
): { key: string; top: number; index: number } | null {
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
  return bestKey !== null ? { key: bestKey, top: bestTop, index: bestIdx } : null
}

/** Positional re-identification, shared by TRIGGER 1's and the splice
 *  triggers' no-surviving-key fallbacks: a row whose key did not survive the
 *  commit moved by as much as the NEAREST row (by old index) whose key did.
 *  Returns the displacement resolver — built once per commit, queried per
 *  mounted node. `survivors` is in ascending old index, so the nearest is one
 *  of the two neighbours of the change point; ties go to the row ABOVE the
 *  reader. Survivors before `boundary` (the first old index that changed
 *  hands) are excluded: they did not move, so their zero displacement says
 *  nothing about rows at or below the boundary — a splice landing exactly at
 *  the viewport top would otherwise borrow it and anchor the inserted row
 *  itself. TRIGGER 1 passes 0 (a prepend renames index 0, so nothing sits
 *  before its boundary). Only when no survivor remains does `noSurvivorShift`
 *  stand in (the caller's net count change) — the reader then keeps their
 *  distance from the END, the one thing a full re-identification of a chat
 *  transcript preserves, and for a contiguous splice it IS the displacement. */
function nearestSurvivorShiftFrom<T>(
  prevItems: readonly T[],
  prevGetKey: (it: T, i: number) => string,
  newIndexByKey: ReadonlyMap<string, number>,
  noSurvivorShift: number,
  boundary = 0,
): (idx: number) => number {
  const survivors: Array<[oldIndex: number, shift: number]> = []
  for (let i = boundary; i < prevItems.length; i++) {
    const ni = newIndexByKey.get(prevGetKey(prevItems[i], i))
    if (ni !== undefined) survivors.push([i, ni - i])
  }
  return (idx: number): number => {
    if (!survivors.length) return noSurvivorShift
    let lo = 0
    let hi = survivors.length
    while (lo < hi) {
      const mid = (lo + hi) >> 1
      if (survivors[mid][0] < idx) lo = mid + 1
      else hi = mid
    }
    const above = survivors[lo - 1]
    const below = survivors[lo]
    if (!above) return below[1]
    if (!below) return above[1]
    return below[0] - idx < idx - above[0] ? below[1] : above[1]
  }
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
/**
 * Whether an automatic pin must yield right now.
 *
 * The base rule is a fixed window from the last hardware input: a reader whose
 * gesture is in flight outranks any correction. That window alone is too short
 * for a height change the input CAUSED. Opening a disclosure with many lines
 * (a tool's error output, "Worked through N steps") renders and re-measures in
 * a cascade that outlives the window, so the tail of the cascade escaped the
 * gate and pinned — the expanded content the user opened was dragged past the
 * viewport top, felt as bounce, and only for the long ones.
 *
 * So suppression is extended by the CASCADE, not the clock: each resize that
 * arrives while suppressed pushes the deadline out again. Streaming growth is
 * unaffected because no hardware input precedes it, so nothing is ever armed.
 */
export function pinSuppressedNow(
  now: number,
  lastHardInputAt: number,
  cascadeUntil: number,
  settleMs: number,
): boolean {
  return now - lastHardInputAt < settleMs || now < cascadeUntil
}

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
    heightScopeKey,
    estimatedHeight = DEFAULT_ESTIMATED,
    overscan = DEFAULT_OVERSCAN,
    followOutput = true,
    initialPlacement = 'bottom',
    eagerFirstMeasure = false,
    getStableId,
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
  /** Anchor-resolution identity: stable id when provided, display key otherwise. */
  const anchorIdOf = useCallback((item: T, index: number): string => {
    const f = getStableIdRef.current
    return f ? f(item, index) : getKeyRef.current(item, index)
  }, [])

  /** Collect up to 3 visible rows as height-anchor candidates, priced through
   *  anchorIdOf against the CURRENT items/elIndex pairing. Only meaningful when
   *  those two are consistent — i.e. not mid-prepend-commit, where itemsRef has
   *  already advanced while elIndex still carries pre-shift indices and every
   *  priced key is off by the inserted count (the capture tear behind the
   *  measured −721px dropped correction). */
  const captureAnchorCands = useCallback((el: HTMLElement): { key: string; top: number }[] => {
    if (typeof el.getBoundingClientRect !== 'function') return []
    const srTop = el.getBoundingClientRect().top
    const cands: { key: string; top: number }[] = []
    for (const [node, i] of elIndexRef.current.entries()) {
      if (typeof node.getBoundingClientRect !== 'function') continue
      const r = node.getBoundingClientRect()
      if (r.height <= 0 || r.bottom - srTop <= 0) continue
      const it = itemsRef.current[i]
      if (!it) continue
      cands.push({ key: anchorIdOf(it, i), top: r.top - srTop })
    }
    cands.sort((x, y) => x.top - y.top)
    return cands.slice(0, 3)
  }, [anchorIdOf])
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

  // ---- Persistent state ----

  // The height owner (HeightIndex) is created further down, immediately before
  // the height lookup that reads it: its key resolver reads `itemsRef` /
  // `getKeyRef`, which are assigned just below, so constructing it up here would
  // put those refs in scope before they hold anything.

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
  // `lastWriteClientHRef`: the scroller's `clientHeight` at the moment
  // `lastWriteTopRef` was recorded — i.e. the viewport box that value was a
  // bottom FOR. Kept in lockstep with it (`-1` alongside `-1`) so the pin
  // evaluation can tell how much of the current distance-from-bottom is our
  // own viewport shrink rather than the user's move (see evaluateAutoPin's
  // `viewportShrink`).
  const lastWriteClientHRef = useRef<number>(-1)
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
  // Starts at -Infinity: "no input yet" must never read as "input just
  // happened" (performance.now() can legitimately be near 0 early in a page's
  // life, and is under fake timers in tests).
  const lastUserScrollAtRef = useRef<number>(Number.NEGATIVE_INFINITY)
  // Hard-input-only sibling of lastUserScrollAtRef (see
  // ANCHOR_SAVE_INTENT_WINDOW_MS): written ONLY by attachUserScrollIntent's
  // hardware events and by the smooth-pin grab interrupts, never by scroll
  // events themselves.
  const lastHardInputAtRef = useRef<number>(Number.NEGATIVE_INFINITY)
  // Scroller height as of the last SCROLL event. Deliberately not
  // `viewportHeightRef`, which the ResizeObserver updates: the resize and the
  // clamp it causes are two separate events, and if the observer ran first the
  // growth would already be folded away by the time the clamp's scroll event
  // asked about it.
  const lastScrollClientHRef = useRef(0)
  // Has the offset tree been committed even once on this mount?
  //
  // Gates the motion deferral below. Deferring is about not moving a picture the
  // reader is already looking at; before the first commit there is no such
  // picture — the rows are priced at estimates — so postponing that commit only
  // keeps the estimates on screen longer. It also has to be gated on something:
  // a scroll event fires while the transcript takes its initial position, which
  // stamps `lastUserScrollAtRef`, so without this the first debounced commit
  // after every mount is pushed a debounce round later for no benefit.
  const geometryCommittedOnceRef = useRef(false)
  // scrollTop as of the last observed scroll event (self or user). Gives the
  // user-scroll stick decision its direction: a genuine upward move releases
  // follow even inside the 100px at-bottom band. `-1` = no observation yet.
  const lastObservedTopRef = useRef<number>(-1)

  // ---- Scroll-anchor preservation ----
  //
  // While the user is scrolled up reading history, content can grow ABOVE the
  // row they are reading, which moves that row down while scrollTop stays put —
  // that IS the jump. Native `overflow-anchor: auto` normally holds the viewport
  // steady, but a scroll-path recompute can UNMOUNT the browser's chosen anchor
  // node (rows past WINDOW_UNMOUNT_HYSTERESIS), collapsing anchoring. So the
  // hook carries its own anchor: the topmost visible row's key + screen offset,
  // captured BEFORE the shift, re-read after commit, and the delta paid back
  // into scrollTop. This REDUCES reliance on overflow-anchor (it does not
  // replace it — the CSS is owned by ChatPage and left alone).
  // Anchor captured by syncHeightsNow for a spacer-repricing commit. Kept
  // SEPARATE from shiftAnchorRef: that slot is consumed on a windowRange
  // commit, and window commits land constantly while rows
  // mount — sharing the slot lets an unrelated window commit consume (and
  // clear) the anchor before the height-sync commit it was captured for,
  // leaving the repricing shift uncompensated (observed as a nondeterministic
  // 170-190px lurch after a far jump with scroll anchoring unavailable).
  // A LIST of visible candidates, not one anchor. The consumer runs a
  // debounce-beat (120ms) after capture, and during active scrolling the
  // window can move past the single topmost row in that beat — the row
  // unmounts, rowTopFrom answers null, and the correction was dropped
  // wholesale (hLostRow). Measured under fixed-velocity scrolling: a reprice
  // of rows fully above the viewport slid content −721px in one frame with
  // hLostRow ticking and no compensation write. The first candidate still
  // mounted at consume time carries the correction instead; each candidate is
  // an equally valid witness of "how far did the content under the reader
  // move", so falling to the next loses nothing.
  const heightAnchorPendingRef = useRef<{ cands: { key: string; top: number }[]; at: number; scrollTop: number } | null>(null)
  // Deadline for cascade-extended pin suppression — see pinSuppressedNow.
  const pinCascadeUntilRef = useRef<number>(0)

  /**
   * ONE compensation routine, SIX triggers, ONE capture point.
   *
   *   TRIGGER 1 — prepend (load-older history): every index shifts up, so
   *     pre-existing rows move down by the inserted height.
   *   TRIGGER 2 — upward window shift (scroll recompute / top-sentinel
   *     expansion): rows mount above the viewport, and they are re-measured
   *     from the flat estimate, so the content above the reader changes height.
   *   TRIGGER 3 — tail append (a new message arrives while the reader is
   *     scrolled up): nothing is inserted above them, but the growth re-syncs
   *     the offset tree, and every row that has never been measured is
   *     re-priced from the running MEAN of the measured ones — so the height
   *     credited above the reader changes anyway and the transcript slides
   *     (measured in the harness: a row at screen offset 0 landed at 500).
   *   TRIGGER 4 — mid-list INSERT (a transient "thinking" row mounts between rows
   *     that are already on screen): the count grows and index 0 keeps its key,
   *     which reads exactly like TRIGGER 3, but every index from the splice point
   *     on MOVES. Left on trigger 3's path it anchored on a MIS-KEYED row,
   *     because that path resolves a mounted node's previous-commit index through
   *     the NEW items.
   *   TRIGGER 5 — mid-list REMOVE (that same row unmounts): the height above the
   *     reader SHRINKS and the transcript is pulled up under them. No trigger
   *     covered a shrink at all (issue #6076).
   *   TRIGGER 6 — mid-list SWAP: the thinking row leaves and its replacement
   *     arrives in ONE commit, which React batching makes the ordinary streaming
   *     shape. The net count is unchanged, so a count-delta trigger reads it as a
   *     no-op — and the single consumer is invalidated by `windowRange` and
   *     `itemCount`, neither of which an equal-count commit moves. So this
   *     trigger carries its OWN invalidation key (`spliceCommit`, bumped in the
   *     render that captures here), which is what makes the capture safe: the
   *     anchor is spent in the very commit it describes instead of sitting in the
   *     slot for an unrelated later commit — the stranded-anchor hazard the
   *     render-phase capture exists to remove. It participates in the height
   *     RETIREMENT below on the same commit.
   *
   * All six are "the height above the reader changed"; the correction is
   * identical, so they share this slot and the single consumer below. A parallel
   * path would fight this one for `scrollTop`, which is why append folds in here
   * rather than getting an anchor slot of its own.
   *
   * The capture is in the RENDER phase (getSnapshotBeforeUpdate idiom) for ALL.
   * That point is canonical rather than merely convenient: a post-commit read
   * cannot recover a pre-shift position — the row has already moved and the
   * delta reads zero — while a pre-shift capture is valid for the window shift
   * too, because the mounted nodes still carry the PREVIOUS commit's geometry
   * while the new range renders. Capturing here also removes the stale-anchor
   * hazard the callback capture had: an anchor taken when a shift was merely
   * SCHEDULED outlived a no-op window commit and was then applied to an
   * unrelated later one, yanking the viewport to a row nobody was reading.
   *
   * Arithmetic is no alternative: `getH` prices an unmeasured row from the
   * running MEAN of measured ones, so any measurement re-prices every unmeasured
   * row and the next sync re-reads them all (measured: a 1000px insert displaced
   * rows by 1500).
   *
   * Staged, because trigger 1 needs an extra commit before it can measure:
   *   'awaiting-rebase' — prepend captured; the window must be re-based first
   *                       (part 1) so the anchor row is still mounted to measure.
   *   'rebased'         — re-base committed; correct, then re-derive the window.
   *   'ready'           — window shift captured; correct only. No re-derive:
   *                       the shift already is the window's own decision.
   */
  const shiftAnchorRef = useRef<{ key: string; top: number } | null>(null)
  const shiftStageRef = useRef<'awaiting-rebase' | 'rebased' | 'ready' | null>(null)
  /** How far DOWN the anchored row moved in the list (new index minus old), set
   *  by TRIGGER 1's capture and consumed by part 1. Equal to the net count growth
   *  only for a pure front insert. */
  const prependCountRef = useRef(0)
  /**
   * TRIGGER 6's invalidation key for part 2, bumped in the render that captures a
   * swap anchor. Every other trigger rides a key part 2 already watches — a
   * prepend, splice or append moves `itemCount`, a window shift moves
   * `windowRange` — and an equal-count swap moves neither, so without this the
   * consumer would not run in the commit the anchor was taken for.
   *
   * Real state rather than a ref token, for the reason `heightCommit` is: a
   * counter this effect does not subscribe to is invisible to tooling and needs
   * an exhaustive-deps exemption to sit in the dep array at all. The bump is the
   * render-time state-update pattern the session and cache sentinels above use,
   * and terminates for the same reason they do: `prependPrevRef` has already
   * advanced by the time React re-invokes the render, so the re-render detects no
   * swap, captures nothing, and bumps nothing.
   *
   * Cost is one extra render pass, and only on a commit that actually captures:
   * the capture is gated on `!stickRef.current`, so the primary reading mode
   * (pinned to the bottom) never pays it, and a token append — the commit that
   * lands per streamed chunk — is not a swap and never reaches the bump.
   */
  const [spliceCommit, setSpliceCommit] = useState(0)
  /** One-shot latch for the spliceCommit bump. Main's original termination
   *  argument -- 'prependPrevRef has already advanced by the time React
   *  re-invokes the render' -- assumed a RENDER-phase mirror advance. The
   *  mirror now advances at COMMIT (a discarded concurrent attempt advancing
   *  it in render poisoned the baseline; phone rig: uncompensated landings),
   *  so a render-phase bump would re-detect the same swap on the re-invoked
   *  render and loop. The latch makes the bump once-per-commit; it is
   *  cleared where the mirror advances. */
  const spliceBumpLatchRef = useRef(false)
  /** Inserted count carried from part 1's rebase to part 2's consume, so a
   *  consume-time anchor miss (row unmounted between commits) can fall back
   *  to tree arithmetic instead of leaving the prepend uncompensated. Zero
   *  for the non-prepend triggers, which keeps the fallback inert there. */
  const shiftInsertedRef = useRef(0)
  /** scrollTop read at the trigger-1 capture render, pre-layout. -1 = unset. */
  const prependPreScrollTopRef = useRef(-1)
  /** Net count growth of the landing (itemCount - previous count), recorded
   *  at capture whether or not an anchor survived. Part 1's arithmetic
   *  fallback compensates by this when no row can be measured; the anchored
   *  path re-bases by the DISPLACEMENT in prependCountRef instead, which
   *  equals the net only for a pure front insert. */
  const prependNetRef = useRef(0)
  /** Set by part 1 in the commit it schedules a re-base in, cleared by part 2 in
   *  that same commit. Part 2 now also watches `itemCount` (for trigger 3), so
   *  it shares a commit with part 1 and would otherwise consume a prepend anchor
   *  before the re-base kept its row mounted — see part 2. */
  const rebaseScheduledRef = useRef(false)
  /** Keys of rows that LEFT the list in this render, handed to the height owner
   *  once it exists (it is constructed further down) — see its drain site. */
  const retiredKeysRef = useRef<string[] | null>(null)
  /** Display-key renames detected this render (stable id survived, key
   *  changed). Drained render-phase beside retiredKeysRef. */
  const renamedKeysRef = useRef<[string, string][] | null>(null)
  /** Previous render's identity. `items` is held because `itemsRef` has already
   *  advanced by the time the capture runs, while the mounted nodes still carry
   *  the PREVIOUS commit's indices. `getKey` is held WITH them: a caller's
   *  getKey may be index-addressed (ChatPage resolves a per-render deduped key
   *  LIST), so only the getKey of the same render prices these items correctly —
   *  the current render's closure would return the NEW list's key at the old
   *  index, misnaming the anchor by the inserted count. */
  const prependPrevRef = useRef<{
    session: string
    count: number
    firstKey: string | null
    items: T[]
    getKey: (it: T, i: number) => string
  }>({
    session: sessionId, count: itemCount, firstKey: null, items, getKey,
  })
  const prependPrev = prependPrevRef.current
  const prependFirstKey = itemCount > 0 ? getKey(items[0], 0) : null
  // Guards the shared slot: a prepend capture in THIS render must not then be
  // overwritten by the window-shift branch below (a re-base changes the range).
  let anchorCapturedThisRender = false
  // A front-insert grows the count AND changes index 0's key. A slot switch does
  // both, hence the session guard; a plain append leaves index 0 alone.
  const _t1Armed =
    itemCount > prependPrev.count &&
    prependPrev.session === sessionId &&
    prependPrev.firstKey !== null &&
    prependFirstKey !== prependPrev.firstKey &&
    !stickRef.current
  if (_t1Armed) {
    const prependEl = scrollerRef.current

    // Anchor identity for the CROSS-COMMIT prepend hop. Display keys are the
    // wrong currency here: a turn takes its LEAD item's key, so the page
    // joining the top turn renames it -- and when that giant turn is the ONLY
    // visible row (a phone viewport routinely shows one), there is no
    // surviving key to fall forward to (field counters: wLost=2 on a real
    // phone, each a full-page lurch). getStableId -- the row's TAIL message --
    // survives the regroup by construction, so the SAME giant row anchors
    // across the landing. Previous items resolve through the getKey captured
    // WITH them when no stable id is provided -- see prependPrevRef's doc.
    const stableFn = getStableIdRef.current
    const idOfPrev = (it: T, i: number) => (stableFn ? stableFn(it, i) : prependPrev.getKey(it, i))
    const idOfNew = (it: T, i: number) => (stableFn ? stableFn(it, i) : getKey(it, i))
    // Current id -> current index. Membership says the ROW survived; the index
    // says where it went. That DISPLACEMENT -- not the net count growth, which
    // equals it only for a pure front insert -- is what the re-base and the
    // correction move by: a rebuild that also grows the TAIL (a reconnect
    // catching up) moves the reader by less than the count grew. Last-wins on
    // a duplicate id; callers keep row identity unique.
    const newIndexById = new Map<string, number>()
    for (let i = 0; i < items.length; i++) newIndexById.set(idOfNew(items[i], i), i)
    let prependAnchor = prependEl
      ? captureTopAnchorFrom(prependEl, elIndexRef.current.entries(), (idx) => {
          const it = prependPrev.items[idx]
          if (!it) return null
          const k = idOfPrev(it, idx)
          return newIndexById.has(k) ? k : null
        })
      : null
    let prependShift = 0
    // Net count and pre-layout scrollTop are recorded whether or not an anchor
    // survived: the anchor-miss fallback in part 1 compensates by arithmetic
    // and must still fire (leaving the count at 0 on a miss made part 1 stand
    // down entirely -- phone rig: the reader took the full inserted height as
    // a visible lurch). The arithmetic subtracts whatever native CSS scroll
    // anchoring already corrected by consume time (WebKit ships none, so the
    // remainder there is the full height) -- writing the full height on top
    // of a native correction DOUBLES the compensation.
    const inserted = itemCount - prependPrev.count
    prependNetRef.current = inserted
    prependPreScrollTopRef.current = prependEl ? prependEl.scrollTop : -1
    if (prependAnchor) {
      prependShift = newIndexById.get(prependAnchor.key)! - prependAnchor.index
    } else if (prependEl) {
      // No visible row kept its key. That is the shape of a wholesale transcript
      // rebuild (the post-turn refresh re-identifying every row it streamed)
      // landing together with the front growth, and standing down here leaves the
      // window and scrollTop where they were — which, with rows now in front, is
      // the START of the transcript rather than the rows being read. So the
      // topmost visible row is re-identified by POSITION: it moved by as much as
      // the NEAREST row (by old index) whose key did survive, and its new key is
      // whatever now sits at old index + that displacement (see
      // nearestSurvivorShiftFrom).
      //
      // Passes `idOfPrev`, not `prependPrev.getKey`: in this scope identity is the
      // STABLE id when the caller supplied one, and `newIndexById` is keyed that
      // way. Handing the helper the plain key would look up ids that map is not
      // keyed by, find no survivors, and silently fall back to the net count.
      const shiftAt = nearestSurvivorShiftFrom(
        prependPrev.items, idOfPrev, newIndexById, inserted,
      )
      // No visible row kept its identity. That is the shape of a wholesale
      // transcript rebuild (the post-turn refresh re-identifying every row it
      // streamed) landing together with the front growth, and standing down
      // here leaves the window and scrollTop where they were -- which, with
      // rows now in front, is the START of the transcript rather than the
      // rows being read. So the topmost visible row is re-identified by
      // POSITION: it moved by as much as the NEAREST row (by old index) whose
      // identity did survive, and its new identity is whatever now sits at
      // old index + that displacement. Only when no identity survives
      // anywhere does the net count stand in -- the reader then keeps their
      // distance from the END, the one thing a full re-identification of a
      // chat transcript preserves.
      prependAnchor = captureTopAnchorFrom(prependEl, elIndexRef.current.entries(), (idx) => {
        const j = idx + shiftAt(idx)
        const it = items[j]
        return it ? idOfNew(it, j) : null
      })
      if (prependAnchor) prependShift = shiftAt(prependAnchor.index)
    }
    // Part 1 re-bases by the reader's own displacement, in either direction --
    // rows coalescing ABOVE the reader while the tail grows moves them UP even
    // though the count grew -- which is what keeps the anchored row mounted for
    // part 2 to measure. A displacement of zero leaves nothing to re-base; a
    // height change above an unmoved row is trigger 7's case, not this one.
    if (prependAnchor && prependShift !== 0) {
      shiftAnchorRef.current = prependAnchor
      shiftStageRef.current = 'awaiting-rebase'
      prependCountRef.current = prependShift
      anchorCapturedThisRender = true
    } else if (prependAnchor) {
      // Anchored but unmoved: the landing did not displace the reader's rows,
      // so the arithmetic fallback must not fire either (compensating an
      // insert that is not above the reader would itself be the lurch).
      prependNetRef.current = 0
      prependPreScrollTopRef.current = -1
    }
  }
  // ---- Count-change classification, read BEFORE the mirror advances ----
  //
  // What the branches below need is which PRE-EXISTING INDICES moved, because the
  // mounted nodes in `elIndexRef` carry the PREVIOUS commit's indices: a node's
  // index still names its own row after a tail append, and names the WRONG row
  // after any splice above it.
  const sameSessionCount = prependPrev.session === sessionId && prependPrev.firstKey !== null
  // A front insert renames index 0 (trigger 1's case) and a slot switch changes
  // the session; both are excluded from everything below.
  const frontKeyHeld = prependFirstKey === prependPrev.firstKey
  // Did any PRE-EXISTING position change hands? That one question separates a
  // tail append from a mid-list insert, and detects a same-count swap.
  //
  // Exact, not sampled. The cheap proxy this replaces read only the LAST
  // pre-existing index, which a replacement anywhere ABOVE it satisfies while
  // still stranding the replaced row's measurement -- so an artifact card
  // refreshed in place, or a row replaced while another is appended, left a
  // height in the mean that no live row justified.
  //
  // Cost is a scan, but not a re-keying one: the overwhelmingly common commit is
  // a token append, which rebuilds the array while REUSING every element object
  // except the streaming row's. Reference equality settles those rows without
  // calling `getKey` at all, so the usual commit costs N pointer comparisons and
  // zero allocation. A key is only computed for a position whose object actually
  // changed, which is the only place a departure can hide.
  const sharedCount = Math.min(prependPrev.count, itemCount)
  let movedIndex = -1
  for (let i = 0; i < sharedCount; i++) {
    const prevItem = prependPrev.items[i]
    const nextItem = items[i]
    if (prevItem === nextItem) continue
    if (prevItem === undefined || nextItem === undefined) { movedIndex = i; break }
    // The PREVIOUS item is priced through the getKey captured WITH it, the NEW
    // one through this render's closure: an index-addressed getKey (ChatPage's
    // deduped key list) returns the new list's key at an old index, which would
    // report every position as moved on an ordinary append.
    if (prependPrev.getKey(prevItem, i) !== getKey(nextItem, i)) { movedIndex = i; break }
  }
  const anyIndexMoved = movedIndex >= 0
  const grewInSession = itemCount > prependPrev.count && sameSessionCount && frontKeyHeld
  /** TRIGGER 3 — the count grew and nothing pre-existing moved. */
  const tailAppended = grewInSession && !anyIndexMoved
  /** TRIGGER 4 — a row appeared above at least one row that is already mounted. */
  const midListInserted = grewInSession && anyIndexMoved
  /** TRIGGER 5 — a row LEFT the list, with index 0 held. `frontKeyHeld` is the
   *  ANCHOR's requirement, not retirement's: a renamed index 0 means the mounted
   *  nodes' indices no longer name their own rows, so there is nothing to anchor
   *  on. Retirement has its own gate below and deliberately does not share this
   *  one. */
  const rowsRemoved = itemCount < prependPrev.count && sameSessionCount && frontKeyHeld
  /** TRIGGER 6 — an equal-count SWAP: a shared index changed hands while the count
   *  stood still, which is the placeholder leaving and its replacement arriving in
   *  one React-batched commit. Same `frontKeyHeld` requirement as 4 and 5, for the
   *  same reason (the mounted nodes' indices must still name their own rows), and
   *  the indices do not even shift here — only one row's identity does. */
  const rowSwapped = itemCount === prependPrev.count && sameSessionCount && frontKeyHeld && anyIndexMoved
  // TRIGGERS 4, 5 and 6 — a mid-list splice: a row in, a row out, or one row
  // traded for another. All three capture the anchor, through the one capture
  // point and the one consumer: each is "a row came or went above the reader",
  // and the correction part 2 already performs does not care which direction it
  // moved. Placed BEFORE trigger 2 so that in a render which does both, the
  // splice's key mapping wins over the window branch's live-items mapping — the
  // whole point being that live-items mapping is what is wrong here.
  //
  // The equal-count SWAP is here rather than excluded because it now brings its
  // own invalidation key (`spliceCommit`, bumped below). Before that key existed
  // the anchor had no consumer on a commit that moves neither `windowRange` nor
  // `itemCount`, so capturing would have stranded it in the slot for an unrelated
  // later commit to spend — see #7234, and `spliceCommit`'s own doc.
  //
  // Staged 'ready' (correct only, never a re-base): a transient row moves the
  // anchor by one index, so it stays inside the mounted window and is
  // measurable. A splice wide enough to unmount it leaves `rowTopFrom` unable to
  // resolve the row and part 2 stands down — the pre-existing behaviour for an
  // unmeasurable anchor, not a new failure mode.
  //
  // The GATE is departure, not any one trigger. Retirement kept escaping through
  // whichever count arithmetic a commit happened not to match -- an equal-count
  // swap, an interior replacement, and a full-transcript clear each reached this
  // point with a row's measurement still pricing the transcript. Those are one
  // defect with three faces, so the condition is stated once, at the level the
  // harm lives on: A ROW LEFT THIS SESSION. A departure requires either a
  // shrinking count or a shared index changing hands, so the streaming commit
  // (same rows, one more at the tail) still does no work here.
  //
  // `frontKeyHeld` is deliberately NOT part of it. It is the anchor's
  // requirement, and borrowing it for retirement is what let the clear through:
  // emptying the list renames index 0 exactly as head paging does, so the proxy
  // read a wipe as a page-out and kept every measurement.
  const rowDeparturePossible =
    sameSessionCount && (itemCount < prependPrev.count || anyIndexMoved)
  if (rowDeparturePossible) {
    const survivingKeys = new Set<string>()
    for (let i = 0; i < items.length; i++) survivingKeys.add(getKey(items[i], i))
    // The anchor keeps the narrower gate: it needs index 0 held (so the mounted
    // nodes' indices still name their own rows) and a commit whose consumer will
    // actually run — a count change for triggers 4 and 5, and for trigger 6 the
    // `spliceCommit` bump below, which is what an equal-count commit has instead.
    // Retirement has neither dependency, which is why it sits outside.
    if ((midListInserted || rowsRemoved || rowSwapped) && !anchorCapturedThisRender && !stickRef.current) {
      const spliceEl = scrollerRef.current
      let spliceAnchor = spliceEl
        ? captureTopAnchorFrom(spliceEl, elIndexRef.current.entries(), (idx) => {
            // PREVIOUS items at the node's PREVIOUS index, filtered to rows that
            // survive this commit — trigger 1's resolution, for the same reason:
            // it is the only mapping that names the row the node actually shows.
            const it = prependPrev.items[idx]
            if (!it) return null
            const k = prependPrev.getKey(it, idx)
            return survivingKeys.has(k) ? k : null
          })
        : null
      if (!spliceAnchor && spliceEl) {
        // No visible row kept its key — the splice landed together with a total
        // re-identification of the on-screen rows (the wholesale-rebuild shape
        // TRIGGER 1 already covers; the splice half was deferred there and is
        // #8033). Standing down leaves scrollTop where it was and displaces the
        // reader by the spliced rows' height, so the topmost visible row is
        // re-identified by POSITION, exactly as TRIGGER 1 does: for rows below
        // the splice point the displacement is the inserted-above count, which
        // the nearest surviving old-index neighbour carries whenever any row
        // between the splice and the reader keeps its key. `movedIndex` is the
        // change boundary: survivors above it did not move, and borrowing their
        // zero displacement would anchor a splice landing exactly at the
        // viewport top on the inserted row itself.
        const newIndexByKey = new Map<string, number>()
        for (let i = 0; i < items.length; i++) newIndexByKey.set(getKey(items[i], i), i)
        const shiftAt = nearestSurvivorShiftFrom(
          prependPrev.items, prependPrev.getKey, newIndexByKey,
          itemCount - prependPrev.count, Math.max(movedIndex, 0),
        )
        // The positional anchor names a row of the NEW list, so it is priced by
        // the CURRENT render's getKey paired with the current items — the same
        // pairing contract as TRIGGER 1's fallback.
        spliceAnchor = captureTopAnchorFrom(spliceEl, elIndexRef.current.entries(), (idx) => {
          const j = idx + shiftAt(idx)
          const it = items[j]
          return it ? getKey(it, j) : null
        })
      }
      if (spliceAnchor) {
        shiftAnchorRef.current = spliceAnchor
        shiftStageRef.current = 'ready'
        anchorCapturedThisRender = true
        // Only the swap needs the bump — the other two already move `itemCount`,
        // and bumping there would buy an extra render pass for a consumer that
        // was going to run anyway. The three shapes are mutually exclusive by
        // their count arithmetic, so this cannot double-fire.
        if (rowSwapped && !spliceBumpLatchRef.current) {
          spliceBumpLatchRef.current = true
          setSpliceCommit((n) => n + 1)
        }
      }
    }
    // Keyed on KEY DEPARTURE, not on the net count falling: the harm is a
    // measurement outliving its row, and a commit that drops the thinking row
    // while adding output nets to growth or to zero with the ghost's height still
    // pricing the transcript. `survivingKeys` is already built above for the
    // anchor, so the general detector costs one pass over the previous items and
    // no extra allocation. Independent of stick: a departed row's measurement is
    // wrong for a pinned reader too. Drained by the height owner further down.
    //
    // Head paging is the ONE departure that must not retire: the rows it drops
    // are coming back when the reader scrolls up, so their measurements must keep
    // pricing the region above. Recognising it starts from the CALLER, not from
    // the data: nothing pages unless the consumer asked to be told when the
    // reader reaches the top, so a consumer with no `onTopReached` has no
    // page-out to exempt and every departure it makes is final. That is what
    // separates the transcript (ChatPage, which wires it) from a filtered list
    // (the artifacts gallery, which does not): narrowing a search box drops a
    // leading run of cards and keeps later ones, which is indistinguishable from
    // a page-out by the shape of the departure alone, and those cards are not
    // coming back.
    //
    // Within a paging consumer, all three shape properties are still required,
    // because any two of them are also true of a departure that MUST retire:
    //
    //   the count FELL          -- a prepend regroup also drops a prefix row while
    //                              survivors remain, and it grows the count
    //   the departures are a    -- a tail truncation or an interior removal leaves
    //   contiguous PREFIX          a survivor ABOVE a departure
    //   a survivor REMAINS      -- a full clear departs a prefix and nothing else,
    //                              and its rows are not coming back
    //
    // Every other shape retires, and each is covered: interior removal (prefix
    // test), equal-count swap and interior-replacement-plus-append (count test),
    // tail truncation (prefix test), clear (survivor test), any departure at all
    // in a non-paging consumer (the capability test).
    //
    // In a paging consumer a single row leaving the very head IS a one-row
    // page-out by every one of these properties, so it is skipped, as it was
    // before this branch existed.
    // RENAMES, not departures: a landing regroup can absorb a page
    // boundary into an existing turn, changing that row's DISPLAY key (the
    // lead item moves) while the ROW itself survives. Its stable id -- the
    // same currency the prepend anchor survives regroups by -- still names
    // it. Retiring such a key sends a mounted giant row back to estimate
    // pricing and the transcript breathes by the difference on EVERY
    // landing (bottom rig: recurring multi-thousand-px doc-height flips).
    // Migrate the measurement to the new key instead; retire only rows
    // whose stable id truly left.
    const stableIdFn = getStableIdRef.current
    const newKeyByStableId = new Map<string, string>()
    if (stableIdFn) {
      for (let i = 0; i < items.length; i++) {
        newKeyByStableId.set(stableIdFn(items[i], i), getKey(items[i], i))
      }
    }
    const renamed: [string, string][] = []
    const departed: string[] = []
    let departedPrefixOnly = true
    let survivorSeen = false
    for (let i = 0; i < prependPrev.items.length; i++) {
      const it = prependPrev.items[i]
      if (!it) continue
      const k = prependPrev.getKey(it, i)
      if (survivingKeys.has(k)) { survivorSeen = true; continue }
      const newKey = stableIdFn ? newKeyByStableId.get(stableIdFn(it, i)) : undefined
      if (newKey !== undefined) {
        // Row survived under a new display key: still a survivor for the
        // prefix-shape analysis (it did not leave the list).
        survivorSeen = true
        renamed.push([k, newKey])
        continue
      }
      if (survivorSeen) departedPrefixOnly = false
      departed.push(k)
    }
    if (renamed.length > 0) renamedKeysRef.current = renamed
    // `onTopReached` is read from the prop, not its ref: the ref is refreshed in
    // an effect, so during the render a consumer first wires paging in it still
    // holds the previous value.
    const headPagedOut =
      onTopReached !== undefined &&
      itemCount < prependPrev.count &&
      departedPrefixOnly &&
      survivorSeen
    if (departed.length > 0 && !headPagedOut) retiredKeysRef.current = departed
  }
  // The mirror advances at COMMIT, not in render. Under concurrent
  // rendering (the transcript arrives through useDeferredValue) React can
  // run this body and then DISCARD the attempt: a render-phase advance in a
  // discarded attempt poisons the baseline, so the attempt that commits
  // compares against its own snapshot, arms nothing, and the landing goes
  // entirely uncompensated (phone rig: kilopixel shifts with no capture /
  // rebase events anywhere near them -- desktop CPUs rarely interrupt, so
  // the tear only surfaced under 4x throttle). Committing the advance also
  // makes interleaved renders CUMULATIVE: attempts A->B and A->C within one
  // commit both compare against A, so the committed capture spans every
  // page that landed, not just the last attempt's slice.
  const prependMirrorNext = { session: sessionId, count: itemCount, firstKey: prependFirstKey, items, getKey }
  useLayoutEffect(() => {
    prependPrevRef.current = prependMirrorNext
    spliceBumpLatchRef.current = false
  })

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
  // TRIGGER 2 capture. Read BEFORE the mirror advances, so the comparison is
  // against the range that is still on screen. Keyed on the range having
  // ACTUALLY moved up in committed state — not on a shift being scheduled —
  // which is what makes a no-op window commit incapable of stranding an anchor.
  // A re-base in flight owns the slot: part 1 moving the range UP (a negative
  // displacement) reads here exactly like a window shift, and capturing again
  // would replace the prepend anchor with a row of the not-yet-corrected frame.
  if (
    !anchorCapturedThisRender &&
    shiftStageRef.current !== 'rebased' &&
    windowRange.start < windowRangeRef.current.start &&
    !stickRef.current
  ) {
    const shiftEl = scrollerRef.current
    const shiftAnchor = shiftEl
      ? captureTopAnchorFrom(shiftEl, elIndexRef.current.entries(), (idx) => {
          const it = items[idx]
          return it ? (getStableIdRef.current ? getStableIdRef.current(it, idx) : getKey(it, idx)) : null
        })
      : null
    if (shiftAnchor) {
      shiftAnchorRef.current = shiftAnchor
      shiftStageRef.current = 'ready'
      anchorCapturedThisRender = true
    }
  }
  // TRIGGER 3 capture. Same slot, same stage as trigger 2: an append needs the
  // correction only, never a re-base — existing indices do not move, so the
  // anchor row is already mounted. Keyed on the window start having stayed put,
  // which is what separates this from trigger 2 (an upward shift) and keeps the
  // two from double-capturing in one render.
  //
  // This capture is what makes the correction possible at all: the DOM read here
  // is the PREVIOUS commit's geometry, so it records where the reader's row was
  // BEFORE the re-pricing lands. The offset tree is re-synced later in this same
  // render (see the `offsetIndex` memo), and after that commit the row has
  // already moved — a post-commit read would measure zero drift.
  if (!anchorCapturedThisRender && tailAppended && windowRange.start === windowRangeRef.current.start && !stickRef.current) {
    const appendEl = scrollerRef.current
    const appendAnchor = appendEl
      ? captureTopAnchorFrom(appendEl, elIndexRef.current.entries(), (idx) => {
          const it = items[idx]
          return it ? getKey(it, idx) : null
        })
      : null
    if (appendAnchor) {
      shiftAnchorRef.current = appendAnchor
      shiftStageRef.current = 'ready'
    }
  }
  // Advanced at COMMIT (same layout effect as the prepend mirror, and for
  // the same reason): a discarded concurrent attempt advancing this in
  // render made the committing attempt's trigger-2 comparison run against
  // a range that never reached the screen, silently skipping the capture.
  useLayoutEffect(() => {
    windowRangeRef.current = windowRange
  })

  // isAtBottom is the only render-affecting scroll state we expose (drives the
  // caller's jump-to-bottom pill).
  const [isAtBottom, setIsAtBottom] = useState<boolean>(true)

  // NOTE: geometry invalidation is NOT a piece of state here. It lives on the
  // height owner, which announces a change in the same call that mutates the
  // tree -- see the `useSyncExternalStore` subscription further down, and
  // HeightIndex.syncAndAnnounce. A local counter used to serve this role, which
  // meant every writer had to remember to bump it: after a content SHRINK
  // (streaming finalize, widget settle, markdown reflow) a missed bump left
  // `totalHeight` stale-large and inflated `offsetAfter` into a phantom bottom
  // spacer (the "blank space at the bottom" bug, and the "flicker when the
  // scroll stops"), with nothing to catch it.

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
    lastWriteClientHRef.current = -1
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
        // `stick` is the AUTHORITATIVE bottom truth here: while follow is
        // engaged the reader IS at the bottom semantically, even when the pin
        // trails the last streamed growth by a frame -- exactly the instant a
        // switch tends to interrupt. Trusting instantaneous geometry alone
        // persisted that transient as a reading anchor; switching back then
        // restored it, yanking the reader off a bottom they never left.
        if (stickRef.current || computeAtBottom(geom, bottomThreshold)) {
          clearScrollAnchor(prevSession)
        } else {
          const a = captureTopAnchorFrom(el, elIndexRef.current.entries(), (idx) => {
            const it = ctx.items[idx]
            return it ? ctx.getKey(it, idx) : null
          })
          // The capture's `index` is the outgoing commit's and means nothing
          // after a reload; persist the key/top pair only.
          if (a) saveScrollAnchor(prevSession, { key: a.key, top: a.top })
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

  // ---- Height owner (single read surface for row heights) ----
  //
  // `HeightIndex` holds the persisted `HeightCache` AND the O(log N) prefix-sum
  // tree, and is the only thing this hook asks about heights. Nothing below
  // reads `HeightCache` directly -- see HeightIndex's own doc for why the read
  // surface is three methods (resolved height vs measurement-or-undefined, and
  // promoting vs not) rather than one.
  //
  // The hot paths (per-rAF scroll window recompute, offset/total spacers, the
  // 120ms streaming tick) would otherwise walk all N rows via the O(N) free
  // functions (getOffset / getTotalHeight / computeWindow), which dominates
  // scroll frames on 5000+ row transcripts. The tree is synced HERE on an
  // itemCount / estimate change so the offset memos have fresh data on the same
  // render, and additionally on height changes by `scheduleHeightSync` (the
  // 120ms tick). It is NOT synced on the per-rAF scroll path (a same-count sync
  // still O(N)-scans the prefix).
  //
  // ONE session guard, and ONE record of session identity. Previously the cache
  // and the tree each carried their own guard and both had to agree: switching to
  // a different session with the SAME item count changes neither itemCount nor
  // the getter's identity, so a guard on only one of them left the tree serving
  // the previous transcript's heights -- a transcript opening at the wrong scroll
  // position. Because the owner holds both, the tree cannot outlive its cache.
  //
  // The guard reads the session off the OWNER rather than a parallel ref beside
  // it. A second spelling of the same identity is the very pattern this change
  // exists to remove, and it could drift from the owner it describes; asking the
  // owner what session it holds cannot. `?.` covers the first render, where the
  // absent owner reads as "not this session" and constructs.
  // Height identity = session PLUS the caller's height scope (width bucket).
  // Measured heights are only valid for the width they were measured at: a
  // phone loading a desktop-measured cache treats every wrong height as a
  // "measurement", and the per-row corrections on mount read as continuous
  // jumping (and near the top, as runaway pagination). Scroll restore and
  // prepend detection stay keyed on the pure sessionId -- a resize must
  // re-scope heights without yanking the reader's position.
  const heightScope = heightScopeKey ?? sessionId
  const heightIndexRef = useRef<HeightIndex | null>(null)
  if (heightIndexRef.current?.sessionId !== heightScope) {
    heightIndexRef.current?.flush()
    // Seed the row count so the eviction cap is size-aware from the first
    // measurement: a session longer than the baseline floor must be allowed to
    // retain its oldest heights, or scrolling back to the top re-enters
    // all-estimate territory even on a revisit. `itemCount` is legitimately 0
    // here when a slot switch changes sessionId before the transcript loads;
    // HeightCache treats that as "unknown" and sizes the cap from the persisted
    // blob instead, so no measurements are discarded before the real count
    // arrives via setRowCount() below.
    heightIndexRef.current = new HeightIndex(heightScope, {
      rowCount: itemCount,
      estimate: estimatedHeight,
      // Late-bound on purpose: resolved at call time from the live refs, so a
      // steered bubble's rewritten `ts` cannot orphan its measurement.
      keyAt: (i) => {
        const it = itemsRef.current[i]
        return it ? getKeyRef.current(it, i) : null
      },
    })
  } else {
    // Transcripts grow while mounted; keep the cap in step with the row count.
    heightIndexRef.current.setRowCount(itemCount)
    heightIndexRef.current.setEstimate(estimatedHeight)
  }
  const heightIndex = heightIndexRef.current

  // A transient row is MEASURED while it is mounted, and `getHeight` prices every
  // UNMEASURED row from the running MEAN of the measured ones — so a measurement
  // is never local to its own row. When the row then leaves the list its height
  // stays in the cache and goes on pricing the transcript, holding the height
  // credited above the reader at a value no live row justifies; a "thinking"
  // placeholder is a fraction of a real message tall, so everything above the
  // reader stays under-priced until the entry is evicted — and past a reload,
  // once the blob is persisted. Compensating the commit cannot reach that: the
  // reprice recurs on every later sync. Retire it instead, HERE — after the owner
  // exists — so the reprice lands in the SAME commit whose shift the splice
  // capture above already compensates. Retiring KEEPS the measurement itself (see
  // HeightCache.retire), which is what makes an optimistic removal the server
  // later refuses restorable rather than re-priced: regenerate and edit-resend
  // both snapshot, truncate, and replace the snapshot back on refusal.
  //
  // The tree is re-synced HERE rather than left to the `offsetIndex` memo below,
  // because that memo is keyed on `itemCount` and an equal-count SWAP moves none
  // of its dependencies: the memo body would not run, and the spacers this render
  // reads would keep prices the retirement just invalidated. A render-phase
  // `sync` is the same call the memo makes, at the same phase, so the geometry
  // read further down sees the corrected tree in this commit. On a commit that
  // DOES change the count the memo syncs too, which is idempotent -- a second
  // walk over the same heights.
  const renamedKeys = renamedKeysRef.current
  if (renamedKeys) {
    renamedKeysRef.current = null
    heightIndex.rename(renamedKeys)
    heightIndex.sync(itemCount)
  }
  const retiredKeys = retiredKeysRef.current
  if (retiredKeys) {
    retiredKeysRef.current = null
    heightIndex.retire(retiredKeys)
    heightIndex.sync(itemCount)
  }

  // ---- Height lookup ----
  // Kept as a stable getter because the O(N) free functions still take one.
  const getH = heightIndex.getHeight

  const offsetIndex = useMemo(() => {
    heightIndex.sync(itemCount)
    return heightIndex
    // `estimatedHeight` is an intentional invalidation key, not a value this body
    // reads: a changed estimate must re-sync so still-unmeasured rows pick up the
    // new placeholder height. eslint cannot see that because the estimate reaches
    // the tree through the owner (setEstimate above) rather than this closure.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [heightIndex, itemCount, estimatedHeight])

  // Debounced height sync. Cache writes (RO re-measure, measureRef seed) call
  // this; the owner announces the change (which invalidates the geometry reads)
  // only after heights have been STABLE
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
  const heightSyncTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const syncHeightsNow = useCallback(() => {
    const idx = heightIndexRef.current
    if (!idx) return
    // The owner mutates the tree, decides whether the total actually moved, and
    // announces it -- there is no version to bump here, so there is no bump to
    // forget. The callback runs only when a change IS being announced, after the
    // mutation and before subscribers see it.
    idx.syncAndAnnounce(itemsRef.current.length, () => {
      // Spacer repricing about to commit: rows ABOVE the viewport re-price
      // (estimates replaced by real heights), which moves everything below by
      // the delta. Chrome's native scroll anchoring absorbs that shift; iOS
      // Safari has none, so a reader sees the transcript slide under their
      // finger (measured 13-25px right after a far jump, when a whole streak
      // of first measurements lands in one sync). Capture the top visible row
      // now so the anchor-compensation layout effect below can hold it steady
      // across the commit. Skipped while stick is armed -- the bottom pin owns
      // positioning there.
      //
      // This capture can run MID-TRANSACTION: a prepend's eager first
      // measurements fire it after the DOM mutated but before the shift
      // effect writes its scrollTop correction, so the captured top reads
      // UNCOMPENSATED geometry. Left alone, consuming it after the shift
      // write measures the row "moved back" and reverses the correction —
      // net zero, reader dropped on the pagination sentinel, infinite
      // loading (measured: paired deltas [+2144,-2144], [+6680,-6680] per
      // page). The shift consumer therefore RE-BASELINES this anchor right
      // after its own write (see the window effect), so what lands here is
      // only the residual. Skipping the capture instead was tried and left a
      // hole: with continuous scrolling re-arming the stage every few frames,
      // whole 120ms re-measure batches went uncompensated — a controlled
      // fixed-velocity probe saw a 749px one-frame lurch with every anchor
      // counter silent.
      if (!stickRef.current && scrollerRef.current) {
        const a = captureTopAnchorFrom(scrollerRef.current, elIndexRef.current.entries(), (i) => {
          const it = itemsRef.current[i]
          return it ? getKeyRef.current(it, i) : null
        })
        if (a) heightAnchorPendingRef.current = { cands: captureAnchorCands(scrollerRef.current), at: performance.now(), scrollTop: scrollerRef.current.scrollTop }
      } else if (stickRef.current && scrollerRef.current) {
        // FOLLOWED reader: no anchor to capture (the bottom pin owns the
        // position), but the consumer's PRE-PAINT re-pin still has to run --
        // and its entry guard is this very slot. Leaving it empty starved that
        // branch, so a followed reader's repricing fell through to the
        // POST-PAINT pinAuto: one visible frame of displacement per landing
        // wave. That is the "opens, then starts jumping a moment later"
        // report — the measure farm reaches deep-idle ~5s after open and
        // reprices estimate-priced rows in batches from then on.
        //
        // A SENTINEL (no candidates) is what the slot needs: the stick branch
        // reads only live geometry (bottomTarget), never these candidates, so
        // an empty list is not a missing measurement — it says "a reprice is
        // committing, re-pin before paint".
        heightAnchorPendingRef.current = { cands: [], at: performance.now(), scrollTop: scrollerRef.current.scrollTop }
      }
    })
    // No `getH` dependency: the owner is read from its ref inside, and the tree
    // sync no longer takes a getter. Listing it here would tie this callback's
    // identity to the owner's, which the imperative writers must NOT rely on for
    // freshness (they resolve the owner at call time instead).
    // eslint-disable-next-line react-hooks/exhaustive-deps -- helpers above are read through refs at call time
  }, [scrollerRef])
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
      // THE INVARIANT: while the reader is in motion, nothing above them
      // changes. A commit landing mid-gesture can only be made invisible by a
      // `scrollTop` write, and on iOS Safari (no native scroll anchoring) such
      // a write either fights the finger or lands a frame late -- measured on
      // the device as a 108 CSS px step undone ~100ms later. So a BACKGROUND
      // reprice waits and re-arms; the spacers keep their estimates until the
      // reader is still, exactly as they already do for every row that was
      // never measured.
      //
      // Scoped to this debounced path on purpose. The `immediate` callers are
      // the streaming row and `eagerFirstMeasure`, which carry their own
      // compensation and exist to stop a large estimate error from persisting;
      // deferring those re-creates the spacer lurch they were added to remove.
      if (
        geometryCommittedOnceRef.current
        && geometryCommitDeferred({
          stick: stickRef.current,
          now: performance.now(),
          lastHardInputAt: lastHardInputAtRef.current,
          lastUserScrollAt: lastUserScrollAtRef.current,
          settleMs: SCROLL_SETTLE_MS,
        })
      ) {
        scheduleHeightSyncRef.current?.(false)
        return
      }
      geometryCommittedOnceRef.current = true
      syncHeightsNow()
    }, HEIGHT_SYNC_DEBOUNCE_MS)
  }, [syncHeightsNow])
  // Self-reference for the re-arm above: the callback cannot name itself in its
  // own body without tying its identity to a ref-free cycle.
  const scheduleHeightSyncRef = useRef<((immediate?: boolean) => void) | null>(null)
  scheduleHeightSyncRef.current = scheduleHeightSync

  // Geometry is READ, not memoized-and-invalidated. Subscribing to the owner is
  // what schedules a re-render when heights move; the three values below are then
  // read fresh during that render, so there is no invalidation token to list in a
  // dependency array and no way for one to go stale. `totalHeight()` is O(1) and
  // `offsetOf` is O(log N), so memoizing them was never buying much -- and what it
  // cost was a hand-maintained key that eslint could not see and review could not
  // check.
  const heightCommit = useSyncExternalStore(offsetIndex.subscribe, offsetIndex.getVersion)
  const totalHeight = offsetIndex.totalHeight()
  const offsetBefore = offsetIndex.offsetOf(windowRange.start)
  // Height of all items AFTER the window — used as the bottom spacer so the
  // scroll content keeps its full size while only the window renders real DOM.
  const offsetAfter = Math.max(0, totalHeight - offsetIndex.offsetOf(windowRange.end))

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
      // Save only positions the user put themselves at (see the constant's
      // doc). Self-scroll displacements must never become the restore target.
      if (performance.now() - lastHardInputAtRef.current > ANCHOR_SAVE_INTENT_WINDOW_MS) return
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
  // Written by every recomputeWindow entry; read only by the coverage
  // watchdog's yield check (see VIEWPORT_COVERAGE_YIELD_MS).
  const lastRecomputeAtRef = useRef<number>(Number.NEGATIVE_INFINITY)

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
      }
    } else {
      next = computeWindow(Math.max(0, el.scrollTop - leadingOffset(el)), el.clientHeight, count, getH, overscan)
    }
    // No anchor capture here. An upward shift is compensated from the
    // render-phase capture keyed on the range actually moving up (TRIGGER 2),
    // which cannot strand an anchor when this recompute's own update is merged
    // away to a no-op.
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
  }, [getH, overscan, scrollerRef, leadingOffset])

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
      lastWriteClientHRef.current = accounting === 'pin' ? el.clientHeight : -1
      // The direction reference must move WITH our own writes, synchronously.
      // A programmatic scroll's event lands asynchronously (and a fake scroller
      // in tests dispatches none), so leaving the reference to the scroll
      // handler alone would measure the user's next move against a position
      // from BEFORE our pin — an upward scroll right after a pin then reads as
      // downward and fails to release follow.
      lastObservedTopRef.current = top
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
          lastHardInputAtRef.current = lastUserScrollAtRef.current
          // Releasing `stick` alone is not enough: the browser's NATIVE smooth
          // animation keeps running and would still land at the bottom, so the
          // user's input appears ignored. Re-issuing an instant scroll to the
          // CURRENT position cancels the in-flight animation and freezes where
          // they are. lastWriteTop is reset because we are releasing follow.
          if (typeof el.scrollTo === 'function') el.scrollTo({ top: el.scrollTop, behavior: 'auto' })
          lastWriteTopRef.current = -1
          lastWriteClientHRef.current = -1
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
    const viewportShrink =
      lastWriteClientHRef.current >= 0 ? lastWriteClientHRef.current - geom.clientHeight : 0
    const result = evaluateAutoPin({
      stick: stickRef.current,
      geom,
      lastWriteTop: lastWriteTopRef.current,
      // Undefined = the caller gave no run signal, which the predicate reads as
      // "assume live" so an unaware caller keeps its behaviour.
      runActive: runActiveRef.current,
      // Chrome mounting below the transcript shrinks this box, often
      // spring-animated across many frames. Measure the scroll-up guard
      // against the box our reference was a bottom for, never the box the
      // animation just applied.
      viewportShrink,
    })
    stickRef.current = result.stick
    if (result.pin) {
      writeScrollTop(el, result.target, 'auto', 'pin')
    } else if (result.stick) {
      // Still following but already at the bottom (no write needed) — keep the
      // self-scroll reference aligned with the current bottom.
      lastWriteTopRef.current = result.target
      lastWriteClientHRef.current = geom.clientHeight
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

  // Live follow state for consumers. A stable callback rather than state:
  // `stick` flips inside hot paths (scroll handler, RO callback) where a
  // setState per tick would be waste, and the consumers are effect gates that
  // need the CURRENT value at fire time, not a render-synced snapshot.
  /** Last observed scroller clientHeight, so a viewport resize has a direction. */
  const viewportHeightRef = useRef(0)
  const getFollow = useCallback(() => stickRef.current, [])

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
          lastHardInputAtRef.current = lastUserScrollAtRef.current
          stickRef.current = false
          detachSmoothAbort()
        }
        prevSmoothTopRef.current = el.scrollTop
      } else if (!isSelfScroll(el.scrollTop, lastWriteTopRef.current)) {
        // A scroll we did not write that leaves us EXACTLY at the bottom was the
        // layout engine's: the browser clamps scrollTop when a shrinking
        // scrollHeight drops the maximum below it, and a spacer re-estimate does
        // the same.
        //
        // The test is the CLAMP — distance within SELF_SCROLL_EPSILON — and NOT
        // the 100px `atBottom` UI band. resolveUserScrollStick's bottom-epsilon
        // branch is what keeps `stick` armed across the clamp; widening this to
        // the 100px band would erase the only evidence evaluateAutoPin has of a
        // real 3-100px scroll-up.
        const clampedAtBottom =
          geom.scrollHeight - (geom.scrollTop + geom.clientHeight) <= SELF_SCROLL_EPSILON
        stickRef.current = resolveUserScrollStick({
          stick: stickRef.current,
          followOutput,
          scrollTop: el.scrollTop,
          prevScrollTop: lastObservedTopRef.current,
          geom,
          // How much viewport came BACK since the previous scroll event. A
          // deletion shrinks the composer, this grows, the maximum scrollTop
          // drops, and the engine clamps a near-bottom reader to the end with no
          // write to see. Without this the clamp reads as the reader returning to
          // the bottom and re-arms follow for someone who never touched it.
          viewportGrowth:
            lastScrollClientHRef.current > 0
              ? geom.clientHeight - lastScrollClientHRef.current
              : 0,
        })
        const layoutClamp = stickRef.current && clampedAtBottom
        // A clamp is OUR layout change, so it must not be stamped as input.
        // `lastUserScrollAtRef` arms the SCROLL_SETTLE_MS gate that holds
        // automatic pins off while a gesture is in flight, so stamping it for a
        // clamp spent the whole window on our own reflow. A send that queues
        // behind a busy turn does both halves at once: the queued row regroups
        // the turn and remounts tail rows (content shrinks — the clamp), and the
        // queue band mounts below the transcript and spring-animates the
        // scroller's box smaller over the following frames. Every one of those
        // viewport re-pins was then suppressed, so the transcript sat up to a
        // card-height below the bottom until the gate expired — measured on the
        // real build: the box shrank 617 -> 588 across 130ms with scrollTop
        // frozen, and the first pin landed at 154ms, one frame AFTER the last
        // shrink step. Whether the animation outlived the gate is what made the
        // defect intermittent.
        //
        // Genuine input keeps its own signal: the persistent intent listeners
        // below stamp at wheel/touch/key/scrollbar time, which is EARLIER than
        // the scroll event this branch handles, so nothing is lost by declining
        // to stamp here. `stick` and `lastWriteTop` already treat the clamp as
        // ours; the gate now agrees with them.
        if (!layoutClamp) lastUserScrollAtRef.current = performance.now()
        // Re-baseline the self-scroll reference to where the clamp left us —
        // otherwise it keeps pointing at our last write, and the next pin
        // evaluation reads that gap as a user scroll-up, releasing follow for the
        // rest of the turn with only a manual scroll back to the bottom able to
        // re-arm it.
        if (layoutClamp) {
          lastWriteTopRef.current = el.scrollTop
          lastWriteClientHRef.current = geom.clientHeight
        }
      }
      // Direction reference for the next event — updated for self-scrolls too,
      // so a user move right after our own pin is measured against where the
      // pin actually left the viewport.
      lastObservedTopRef.current = el.scrollTop
      lastScrollClientHRef.current = el.clientHeight
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
    const detachIntent = attachUserScrollIntent(el, () => {
      lastUserScrollAtRef.current = performance.now()
      lastHardInputAtRef.current = performance.now()
    })
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
  }, [scrollerEl, bottomThreshold, followOutput, recomputeWindow, detachSmoothAbort, pinAuto, scheduleAnchorSave])

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
      if (
        top < spanTop - VIEWPORT_COVERAGE_SLACK_PX ||
        bottom > spanBottom + VIEWPORT_COVERAGE_SLACK_PX
      ) {
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
  }, [scrollerEl, recomputeWindow, leadingOffset, forcePin])

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
      // True when a resized row is at the TAIL (last item, the streaming row,
      // or the row inside its post-stream grace) -- the only growth a
      // bottom-parked reader should be carried along by. See the assignment
      // below for why an arbitrary row's growth must not pin.
      let tailRowResized = false
      // True when one of the resized entries is the caller-designated
      // streaming row (see `streamingIndex` option / syncHeightsNow's doc).
      let streamingRowResized = false
      // Net reprice of rows lying entirely ABOVE the fold in this fire. Summed
      // across entries because a measure batch reprices several rows at once
      // and the reader is displaced by their total, not by the last one.
      let viewportResized = false
      let aboveFoldReprice = 0
      for (const entry of entries) {
        if (entry.target === el) {
          // Three cases, and the DIRECTION separates only the first from the other
          // two -- the CAUSE separates those.
          //
          // GROWTH (composer collapses, keyboard closes): a taller viewport LOWERS
          // the maximum scrollTop, so a bottom-flush reader is out of range and the
          // engine clamps them back to flush for free. A pin adds nothing there,
          // and for a reader parked above the bottom that same clamp is the yank --
          // which `resolveUserScrollStick` is told about via `viewportGrowth` so it
          // does not mistake the clamp for the reader coming back. Skipped here.
          //
          // SHRINK BY CHROME (a banner, an attachment strip, the queue band): a
          // shrink RAISES the maximum, and no engine ever pushes a reader DOWN, so
          // a flush follower is stranded above the new bottom with no native
          // mechanism that will ever move them (reported as a session not landing
          // at the end). This is the one case that REQUIRES a write, and it is
          // gated on `stick` so a reading user is never yanked.
          //
          // SHRINK BY THE COMPOSER (the reader's own typing taking the space):
          // identical geometry to the case above, so only the cause tells them
          // apart. Following it walks the transcript up by a line every few
          // characters -- the bounce reported from a real phone. Skipped.
          const prevCh = viewportHeightRef.current
          viewportHeightRef.current = el.clientHeight
          if (prevCh > 0 && el.clientHeight > prevCh) continue
          if (composerExplainsViewportChange()) continue
          viewportResized = true
          continue
        }
        const idx = elIndexRef.current.get(entry.target)
        if (idx === undefined) continue
        const it = itemsRef.current[idx]
        if (!it) continue
        const newH = measureBorderBoxHeight(entry.target as HTMLElement)
        // A 0 here is a hidden ancestor (display:none tab/panel makes the
        // observer report an empty content box), not a row height. Writing it
        // would poison the cache — persisted per session — pricing the whole
        // region at ~1px/row (heightAt's floor) until every row remounts, and
        // collapsing offsetBefore into the blank-above symptom. The measureRef
        // seed path applies the same h > 0 floor; skipping loses nothing
        // because re-showing the ancestor fires the observer again with the
        // real size.
        if (newH <= 0) continue
        // Resolved at call time, never captured: a callback that closed over the
        // owner would keep writing into the PREVIOUS session's heights after a
        // slot switch -- the same wrong-transcript class this owner exists to
        // close, reintroduced through a stale closure.
        const hi = heightIndexRef.current
        if (!hi) continue
        // readMeasured (promoting): this row is mounted, so the read is genuine
        // access. `undefined` MUST stay reachable here -- the branch below tells
        // a first mount apart from a genuine resize by exactly that, so a
        // resolved height would classify every scroll-driven mount as a resize.
        const prevH = hi.readMeasured(idx)
        if (prevH !== newH) {
          hi.setMeasured(idx, newH)
          // First-mount (prev undefined) happens during scroll-driven window
          // expansion; re-pinning then would interrupt the user's scroll. Only
          // genuine resizes (streaming growth, widget load) drive the pin —
          // EXCEPT while actively following (see below).
          if (prevH !== undefined) {
            genuineResize = true
            // Correcting a reprice ABOVE the reader belongs HERE, in the fire
            // that knows the row and both heights -- not in the index-keyed
            // effect, which cannot run until the debounced sync lands and so
            // leaves the reader displaced for that whole window (measured: one
            // 108 CSS px step, undone ~100ms later).
            aboveFoldReprice += repriceAboveFoldDelta({
              rowTop: (entry.target as HTMLElement).getBoundingClientRect().top,
              prevHeight: prevH,
              newHeight: newH,
              foldTop: el.getBoundingClientRect().top,
            })
            // Which row grew decides whether growth is FOLLOWABLE. Streaming
            // and widget-load growth happens at the TAIL, where following it
            // keeps a bottom-parked reader at the bottom. A disclosure the
            // user just opened mid-transcript ("Worked through N steps", a
            // tool's error output) grows an OLDER row by hundreds to thousands
            // of px, and a bottom pin then shoves the content they opened up
            // past the viewport top -- once per RO fire as the revealed lines
            // render, which is the bounce. A boolean OR across entries cannot
            // tell these apart, so keep the identity.
            if (
              idx >= itemsRef.current.length - 1 ||
              idx === streamingIndexRef.current ||
              idx === graceIndexRef.current
            ) {
              tailRowResized = true
            }
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
      // Hold a RELEASED reader against a reprice above them, in this fire. A
      // followed reader is deliberately excluded: the pin below already puts
      // them at the bottom, and adding this would move them twice.
      //
      // This runs BEFORE the rail-settle deferral because it is not part of the
      // write storm that deferral exists to hold back: it is one write per
      // batch, and deferring it is precisely the delay that makes the
      // displacement visible.
      if (!stickRef.current && Math.abs(aboveFoldReprice) > 0.5) {
        writeScrollTop(el, el.scrollTop + aboveFoldReprice, 'auto', 'pin')
      }
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
      // Only TAIL growth is followed. `genuineResize` alone would follow a
      // disclosure the user opened 50 messages up (see tailRowResized). A
      // viewport resize is followed only while FOLLOWING, and only for a shrink
      // (see the `entry.target === el` branch above for the direction argument).
      const shouldFollow =
        (genuineResize && tailRowResized) || ((firstMount || viewportResized) && stickRef.current)
      // The settle gate applies even while following: with `stick` armed the
      // old bypass meant every RO tick pinned instantly DURING an active
      // gesture — the pin write and the user's input fought over scrollTop
      // frame by frame (visible as jitter) until the scroll event finally
      // released `stick`. Intent listeners bump the timestamp at input time,
      // so the gate holds pins off from the first wheel/touch/key/scrollbar
      // event; a stationary reader at the bottom is untouched (no input →
      // timestamp stays old → pins flow).
      if (shouldFollow) {
        const now = performance.now()
        if (pinSuppressedNow(now, lastUserScrollAtRef.current, pinCascadeUntilRef.current, SCROLL_SETTLE_MS)) {
          // This resize belongs to a cascade the user's own input started —
          // hold the gate open past it rather than pinning into its tail.
          pinCascadeUntilRef.current = now + SCROLL_SETTLE_MS
        } else {
          pinAuto()
        }
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
  }, [recomputeWindow, pinAuto, scheduleHeightSync, syncHeightsNow, scrollerRef, writeScrollTop])

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
  useEffect(() => {
    const prev = prevWindowStartRef.current
    prevWindowStartRef.current = windowRange.start
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
  }, [windowRange.start, itemCount, prefetchStartIndex])

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
          setWindowRange((prev) => expandWindowUp(prev, overscan))
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
  }, [overscan, scrollerEl])

  /**
   * Part 1 — TRIGGER 1 only: re-base the window by the anchored row's own
   * displacement so the rows being read stay mounted, including the anchor row
   * that part 2 has to measure. Runs pre-paint, so the shifted-but-uncorrected frame is never
   * shown. A window shift needs no equivalent: it IS a range change already.
   */
  useLayoutEffect(() => {
    const armed = shiftStageRef.current === 'awaiting-rebase'
    const net = prependNetRef.current
    prependNetRef.current = 0
    if (!armed && net <= 0) return
    const shift = prependCountRef.current
    prependCountRef.current = 0
    const el = scrollerRef.current
    // Every exit from 'awaiting-rebase' clears the slot: an anchor left in that
    // stage is one part 2 never consumes.
    if (stickRef.current || !shiftAnchorRef.current || shift === 0) {
      if (!stickRef.current && el && net > 0) {
        // ANCHOR-MISS FALLBACK: no surviving row to measure against (and the
        // positional re-identification found nothing either), so compensate
        // by arithmetic instead of standing down. The offset tree was synced
        // render-phase this commit, and a top-walk page lands only on
        // farm-measured geometry, so the inserted block's height is exact
        // there (and a fair estimate elsewhere -- either beats a full-page
        // lurch). Same-commit pre-paint: rebase the window so mounted rows
        // keep their identity, then advance scrollTop by the block just
        // inserted above the reader.
        setWindowRange((r) => ({
          start: Math.min(itemCount, r.start + net),
          end: Math.min(itemCount, r.end + net),
        }))
        let insertedPx = 0
        for (let i = 0; i < net; i++) insertedPx += offsetIndex.getHeight(i)
        // Reading scrollTop forces layout, which is also when native scroll
        // anchoring applies its own correction -- so the read already
        // includes it. Write only what is still missing.
        const preTop = prependPreScrollTopRef.current
        const nativeAdj = preTop >= 0 ? el.scrollTop - preTop : 0
        const remainder = insertedPx - Math.max(0, nativeAdj)
        if (remainder > 0.5) writeScrollTop(el, el.scrollTop + remainder, 'auto', 'pin')
      }
      prependPreScrollTopRef.current = -1
      shiftAnchorRef.current = null
      shiftStageRef.current = null
      return
    }
    shiftStageRef.current = 'rebased'
    shiftInsertedRef.current = net
    rebaseScheduledRef.current = true
    // Signed: the anchored row's displacement, so the re-based range contains it
    // whichever way it moved. Clamped to the list on both ends.
    const clamp = (i: number) => Math.max(0, Math.min(itemCount, i))
    setWindowRange((r) => ({ start: clamp(r.start + shift), end: clamp(r.end + shift) }))
    // itemCount is the ONLY trigger by design: offsetIndex re-syncs in the
    // same render that changes itemCount (its memo keys on it), and the
    // scroller/write helpers are stable -- re-running on their identity
    // would re-fire a consumed prepend.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [itemCount])

  /**
   * Part 2 — the single consumer for ALL THREE triggers: re-read the anchor row in
   * the shifted DOM and move scrollTop by however far it travelled, which holds
   * the user's place whatever mix of inserted rows and re-estimated heights
   * caused the shift.
   *
   * Gated on the stage, not on this effect having run: the deps include
   * `recomputeWindow`, whose identity changes as heights are measured, so an
   * ungated read here would consume a prepend anchor before part 1 had re-based
   * the window to keep its row mounted.
   *
   * The scrollTop write is recorded in lastWriteTopRef so the passive scroll
   * listener classifies it as a self-scroll (isSelfScroll / SELF_SCROLL_EPSILON)
   * and does not release stick or treat it as user input.
   *
   * After a 'rebased' correction only, re-derive the window: the passive
   * itemCount recompute has already run by this point — React flushes it between
   * these two layout effects — so it sized the window from the PRE-correction
   * offset and its update lands after this write. Re-deriving from the corrected
   * scrollTop is what stops that stale range being the one left committed. A
   * 'ready' (window-shift) correction must NOT re-derive: that range is the
   * window's own decision, and recomputing it here would fight the scroll.
   */
  /**
   * Trigger-1 stage promotion: 'rebased' -> 'ready' on the commit whose
   * windowRange change IS part 1's re-base landing (rebaseScheduledRef
   * marks it). Keying on windowRange -- not on effect-run counting -- is
   * what makes the promotion immune to extra effect ticks: however many
   * times the consumer re-runs between part 1 and the re-based commit,
   * the stage stays 'rebased' and the anchor stays uncommitted until the
   * re-based rows are really in the DOM.
   */
  useLayoutEffect(() => {
    if (rebaseScheduledRef.current && shiftStageRef.current === 'rebased') {
      rebaseScheduledRef.current = false
      shiftStageRef.current = 'ready'
    }
    // windowRange is the invalidation key: the promotion must observe the
    // commit that applied part 1's setWindowRange.
  }, [windowRange])

  useLayoutEffect(() => {
    // Consume at 'ready' ONLY. A trigger-1 anchor is promoted to 'ready'
    // by the effect above, keyed on the windowRange commit that actually
    // mounted the re-based rows. The previous protocol accepted 'rebased'
    // here behind a one-shot stand-down flag; under a throttled CPU the
    // flag was consumed by an earlier same-flush run and the anchor was
    // then measured against the TRANSITIONAL DOM -- new items over the old
    // commit's node indices -- whose mixed coordinates either mis-bound at
    // the captured position (delta 0: correction swallowed, the landing
    // showed as a full-page lurch) or bound across the spacer (a
    // kilopixel over-correction, snapped back a frame later). Phone rig:
    // both signatures, per-landing.
    const stage = shiftStageRef.current
    if (stage !== 'ready') return
    shiftStageRef.current = null
    const pending = shiftAnchorRef.current
    shiftAnchorRef.current = null
    const insertedForFallback = shiftInsertedRef.current
    shiftInsertedRef.current = 0
    const prependPreTopForFallback = prependPreScrollTopRef.current
    prependPreScrollTopRef.current = -1
    const el = scrollerRef.current
    if (!el || stickRef.current) return
    // CONSUME-MISS FALLBACK: the anchor row can vanish between capture and
    // consume (unmounted by a concurrent recompute on a slow device). For a
    // prepend the compensation is still knowable by arithmetic -- the block
    // inserted above the reader -- so apply that instead of returning with
    // the reader uncompensated (phone rig: per-landing kilopixel shifts).
    const fallbackCompensate = () => {
      if (insertedForFallback <= 0) return
      let px = 0
      for (let i = 0; i < insertedForFallback; i++) px += offsetIndex.getHeight(i)
      // Subtract what native scroll anchoring already corrected since the
      // capture render (see prependPreScrollTopRef) -- a blind full-height
      // write on top of it doubles the compensation into a page-sized leap.
      const preTop = prependPreTopForFallback
      const nativeAdj = preTop >= 0 ? el.scrollTop - preTop : 0
      const remainder = px - Math.max(0, nativeAdj)
      if (remainder > 0.5) writeScrollTop(el, el.scrollTop + remainder, 'auto', 'pin')
    }
    if (!pending) { fallbackCompensate(); if (insertedForFallback > 0) recomputeWindow(); return }
    const newTop = rowTopFrom(el, elIndexRef.current.entries(), (idx) => {
      const it = itemsRef.current[idx]
      return it ? anchorIdOf(it, idx) : null
    }, pending.key)
    if (newTop === null) { fallbackCompensate(); if (insertedForFallback > 0) recomputeWindow(); return }
    const delta = newTop - pending.top
    // Instant, and accounted as a 'pin' write: this is our own correction, so
    // the follow guard must recognise the resulting scroll event as self-scroll
    // rather than user input. Routed through the chokepoint so the accounting
    // cannot be forgotten here (see writeScrollTop).
    if (Math.abs(delta) > 0.5) {
      writeScrollTop(el, el.scrollTop + delta, 'auto', 'pin')
      // Re-baseline a pending height anchor: its capture may have read the
      // UNCOMPENSATED geometry mid-transaction (see syncHeightsNow). After
      // this write the row sits where the reader sees it, so refreshing the
      // stored top makes the height consumer correct only the RESIDUAL —
      // neither reversing this write (the paired-delta loop) nor going blind
      // to the re-measure batch (the 749px uncompensated lurch).
      if (heightAnchorPendingRef.current) {
        // RE-CAPTURE, not re-price: the pending candidates may have been
        // captured mid-prepend-commit, where itemsRef had advanced while
        // elIndex still carried pre-shift indices — every priced key was off
        // by the inserted count, so mapping them forward preserves the tear.
        // Here the rebase has committed and this write just landed, so a
        // fresh capture reads a CONSISTENT pairing; the height consumer then
        // corrects only what moves after this point.
        heightAnchorPendingRef.current = { cands: captureAnchorCands(el), at: performance.now(), scrollTop: el.scrollTop }
      }
    }
    // Trigger-1 only (`insertedForFallback` marks it): the passive
    // itemCount recompute sized the window from the PRE-correction offset,
    // so re-derive from the corrected scrollTop. A window-shift ('ready'
    // from capture) correction must NOT re-derive -- that range is the
    // window's own decision, and recomputing would fight the scroll.
    if (insertedForFallback > 0) recomputeWindow()
    // `itemCount` is an invalidation key, not a value this body reads: TRIGGER 3
    // captures in a render that changes no windowRange, so without it the
    // correction would wait for an unrelated window commit and be applied to
    // geometry that had already drifted. `spliceCommit` is the same kind of key
    // for TRIGGER 6, which moves neither of the other two.
  // eslint-disable-next-line react-hooks/exhaustive-deps -- trigger set is deliberate (see comment above)
  }, [windowRange, itemCount, spliceCommit, scrollerRef, writeScrollTop, recomputeWindow])

  // Same correction for a HEIGHT-SYNC commit (spacer repricing), keyed on the
  // owner's announced version. See heightAnchorPendingRef for why this cannot
  // share the window effect's slot.
  //
  // `heightCommit` is the invalidation key: the effect must run in the commit the
  // announcement scheduled, and the version identifies it. Unlike the counter this
  // replaced, it cannot go stale or be forgotten -- the owner bumps it in the same
  // call that mutates the tree, so there is no bump site to miss. It is also a
  // real subscribed value rather than a token invisible to tooling, which is why
  // no exhaustive-deps exemption is needed here any more.
  useLayoutEffect(() => {
    const pending = heightAnchorPendingRef.current
    heightAnchorPendingRef.current = null
    if (!pending) return
    const el = scrollerRef.current
    if (!el || typeof el.getBoundingClientRect !== 'function') return
    if (stickRef.current) {
      // FOLLOWED reader: re-target the bottom PRE-PAINT in the same commit
      // that repriced the tree. pinAuto also does this, but post-paint --
      // which leaves ONE visible frame when a large reprice lands (idle
      // prefetch pages priced by a whale-skewed estimate, then collapsed
      // to farm-measured truth: the bottom rig recorded the spacer swinging
      // tens of thousands of px and the pinned reader teleporting with the
      // clamp -- the field report's parked-at-bottom self-bounce). Writing
      // here makes the whole reprice invisible to a bottom-pinned reader.
      //
      // SETTLE GATE, same principle as SCROLL_SETTLE_MS on the RO auto-pin:
      // a reader who has just started scrolling UP is still `stick` for the
      // few ms until the scroll handler's rAF evaluates and releases follow.
      // A repricing batch committing inside that gap would force them to the
      // bottom mid-gesture -- reported as "I was reading mid-transcript and it
      // suddenly jumped to the end". Real hardware input within the window
      // means the reader's own decision is in flight and outranks this
      // correction; the post-paint pinAuto still handles the genuinely-parked
      // case a frame later.
      if (pinSuppressedNow(performance.now(), lastHardInputAtRef.current, pinCascadeUntilRef.current, SCROLL_SETTLE_MS)) return
      const geom = { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }
      // A SHRINKING viewport is the composer growing under the reader's own
      // typing. The bottom moves DOWN by the shrink without anyone scrolling, so
      // re-targeting it walks the transcript up a line every few characters —
      // reported as the transcript springing back to the bottom while typing a
      // short way above it. `pinSuppressedNow` cannot catch this: the hardware
      // intent listeners are on the SCROLLER, and a keystroke in the composer
      // never reaches them. Growth (composer collapsing, keyboard closing) still
      // pins: that space is being given back.
      if (lastWriteClientHRef.current >= 0 && geom.clientHeight < lastWriteClientHRef.current) return
      // Delegate to the same predicate the post-paint pin uses instead of
      // hand-rolling a second gate here: this branch is the pre-paint half of one
      // decision, and a private copy of it is how the idle rule (`runActive`)
      // came to cover one half and not the other.
      const decision = evaluateAutoPin({
        stick: stickRef.current,
        geom,
        lastWriteTop: lastWriteTopRef.current,
        runActive: runActiveRef.current,
      })
      stickRef.current = decision.stick
      if (!decision.pin) return
      if (Math.abs(el.scrollTop - decision.target) > 0.5) writeScrollTop(el, decision.target, 'auto', 'pin')
      return
    }
    // STALE ANCHOR = GARBAGE, but staleness is about whether the VIEWPORT
    // moved, not about the clock. The hazard is a viewport-relative capture
    // consumed after the reader moved: correcting by that delta corrects their
    // own scrolling (the cold-cache walk teleport, 2706px on the phone rig,
    // three runs out of three). A wall-clock age was the first approximation
    // and it fails on the wrong side at the worst moment: a turn ending is the
    // busiest the main thread gets (transcript rebuild, regroup, a whole batch
    // of repricing), so this effect runs late, a STILL reader's anchor is
    // dropped by age, and they pay the entire reprice as one displacement --
    // reported as the transcript moving down a long way the moment a turn
    // ended. Deliberately BELOW the stick branch: the bottom re-pin reads only
    // LIVE geometry (bottomTarget), so staleness cannot mis-correct it.
    //
    // The exact discriminator is scrollTop: a reprice ABOVE the viewport moves
    // where rows sit, it does not move scrollTop. An unchanged scrollTop
    // therefore means the delta is entirely the reprice's and correcting it is
    // right however late it lands; any change means something else moved the
    // viewport -- the reader's finger, iOS momentum (which keeps moving with no
    // further hard input, so an input-timestamp gate would miss it), or
    // Chromium's native anchoring (which has already absorbed the shift, so
    // the correction is a no-op worth skipping anyway).
    if (!heightAnchorStillUsable(pending.scrollTop, el.scrollTop)) return
    // Released reader: the correction IS the candidates, so an empty list is
    // nothing to consume (the followed path above uses a candidate-free
    // sentinel and has already returned by here).
    if (pending.cands.length === 0) return
    let newTop: number | null = null
    let capturedTop = 0
    for (const cand of pending.cands) {
      const t = rowTopFrom(el, elIndexRef.current.entries(), (idx) => {
        const it = itemsRef.current[idx]
        return it ? anchorIdOf(it, idx) : null
      }, cand.key)
      if (t !== null) { newTop = t; capturedTop = cand.top; break }
    }
    if (newTop === null) return
    const delta = newTop - capturedTop
    if (Math.abs(delta) > 0.5) {
      writeScrollTop(el, el.scrollTop + delta, 'auto', 'pin')
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- heightCommit is the sole trigger; geometry is read live
  }, [heightCommit, scrollerRef, writeScrollTop])


  // ---- Follow-output: pin to bottom when items append ----
  const prevItemCountRef = useRef(itemCount)
  // Tail identity of the previous commit, session-scoped. What separates a
  // bulk PREPEND (idle history prefetch landing hundreds of rows ABOVE a
  // reader legitimately followed at the bottom — reading or typing) from a
  // bulk hydration REPLACE (thin optimistic list swapped for the full
  // conversation): a prepend keeps the tail item, a replace does not.
  const prevTailKeyRef = useRef<{ session: string; key: string } | null>(null)
  useLayoutEffect(() => {
    const el = scrollerRef.current
    if (!el) return
    const growth = itemCount - prevItemCountRef.current
    prevItemCountRef.current = itemCount
    const tail = itemCount > 0 ? itemsRef.current[itemCount - 1] : undefined
    const tailKey = tail !== undefined ? getKeyRef.current(tail, itemCount - 1) : null
    const prevTail = prevTailKeyRef.current
    prevTailKeyRef.current = tailKey !== null ? { session: sessionId, key: tailKey } : null
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
    // A bulk PREPEND with an unchanged tail must NOT take the force-pin path:
    // the reader's bottom content is untouched (the prepend compensation holds
    // the view), and force-pinning both remounts the tail window (a visible
    // flash under a reader typing at the bottom) and races the async scroll
    // event of a reader who JUST started scrolling up — yanking them back to
    // the bottom. Only a REPLACED tail is hydration.
    const bulkPrepend = prevTail !== null && prevTail.session === sessionId && prevTail.key === tailKey
    if (growth > overscan + 1 && stickRef.current && !bulkPrepend) {
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
    // A prepend with an unchanged tail never takes the pin/force-pin paths --
    // but a reader FOLLOWED AT THE BOTTOM still needs their view held: the
    // upward-shift compensations (triggers 2/3) are gated on !stick, so with
    // an early return alone nobody re-anchored the bottom and every idle
    // prefetch landing shoved the parked view by the prepended height
    // (momentum rig: 32 anchor jumps, worst ~3700px, all while parked).
    // Re-target the bottom SYNCHRONOUSLY in this same layout effect -- pre-
    // paint, so the parked reader never sees an intermediate frame.
    if (bulkPrepend && growth > 0) {
      // STICK ONLY: rebase the numeric window and re-target the bottom
      // pre-paint (a followed reader has no other guardian; the anchor
      // compensations are !stick by design and pinAuto is post-paint).
      // NOT-STICK is owned by the part-1/part-2 anchor machinery -- doing
      // it here too double-shifted the window (anti-loop test pins this).
      if (stickRef.current) {
        setWindowRange((r) => ({
          start: Math.min(itemCount, r.start + growth),
          end: Math.min(itemCount, r.end + growth),
        }))
        const target = bottomTarget({ scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight })
        writeScrollTop(el, target, 'auto', 'pin')
      }
      return
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
  // eslint-disable-next-line react-hooks/exhaustive-deps -- re-pin triggers are deliberate; the rest resolves via refs
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
      const idxTree = heightIndexRef.current
      const off = idxTree ? idxTree.offsetOf(index) : getOffsetFn(index, count, getH)
      const target = Math.max(0, off - anchor.top)
      writeScrollTop(el, target, 'auto', 'pin')
      // The write clamps against the CURRENT (pre-jump-window) geometry; align
      // the self-scroll reference with the value that actually landed so the
      // resulting scroll event is classified as ours, not user input (which
      // would trip the settle frames' user-scroll abort below).
      lastWriteTopRef.current = el.scrollTop
      lastWriteClientHRef.current = el.clientHeight
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
        // reach the tree: this seed is the SECOND cache writer (besides the RO)
        // and the RO won't re-fire for a value we just seeded, so without this
        // the geometry keeps a stale height and leaves a phantom spacer.
        const it = itemsRef.current[index]
        if (it) {
          const h = measureBorderBoxHeight(el)
          // Owner resolved at call time, not captured -- see the ResizeObserver
          // callback above for why a closed-over owner is a wrong-session write.
          const hi = heightIndexRef.current
          if (hi && h > 0 && hi.readMeasured(index) !== h) {
            hi.setMeasured(index, h)
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
      heightIndexRef.current?.flush()
    }
  }, [detachSmoothAbort])

  // ---- Measure-farm API ----
  // Background off-screen measurement writes real heights for rows the
  // reader has not reached yet, so estimate territory shrinks to zero in
  // idle time instead of being corrected under the reader's finger.
  //
  // `farmRecord` revalidates identity at write time: a page landing can
  // shift indices between the farm picking a target and its measurement
  // committing, and an index-keyed write would then price the WRONG row.
  // The stale write is dropped (returns false) — the row stays unmeasured
  // and a later sweep picks it up under its new index.
  const farmIsMeasured = useCallback((index: number): boolean => {
    return heightIndexRef.current?.peekMeasured(index) !== undefined
  }, [])
  // MOUNTED rows belong to the ResizeObserver, exclusively. The farm
  // renders a row in its DEFAULT disclosure state, which can differ from
  // the live row's by thousands of px (a collapsed tool group); letting
  // both write the same cache slot made them overwrite each other through
  // the remount cycle their own announcements caused -- the bottom rig
  // recorded a persistent ±2666px oscillation against a parked reader.
  const farmRowMounted = useCallback((index: number): boolean => {
    const r = windowRangeRef.current
    return index >= r.start && index < r.end
  }, [])
  const farmRecord = useCallback((index: number, key: string, px: number): boolean => {
    if (px <= 0) return false
    // A row that mounted between pick and measure is the RO's now: drop
    // the farm's reading (see farmRowMounted).
    const r = windowRangeRef.current
    if (index >= r.start && index < r.end) return false
    const it = itemsRef.current[index]
    if (!it || getKeyRef.current(it, index) !== key) return false
    const hi = heightIndexRef.current
    if (!hi) return false
    if (hi.readMeasured(index) !== px) {
      hi.setMeasured(index, px)
      // Debounced, never eager: farm writes are background geometry — the
      // anchor-compensated debounced sync is exactly the safe landing path.
      scheduleHeightSync(false)
    }
    return true
  }, [scheduleHeightSync])

  return {
    farmIsMeasured,
    farmRecord,
    farmRowMounted,
    scrollerRef,
    contentRef,
    topSentinelRef,
    bottomSentinelRef,
    virtualItems,
    offsetBefore,
    offsetAfter,
    totalHeight,
    isAtBottom,
    getFollow,
    scrollToIndex,
    scrollToBottom,
    mountIndex,
    measureRef,
  }
}
