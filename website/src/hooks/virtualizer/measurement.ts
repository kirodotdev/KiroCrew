// Measurement and height-cache orchestration for the chat virtualizer.
//
// Owns the per-scope HeightIndex (the single read surface for row heights --
// see HeightIndex.ts), the geometry the hook renders from it, and every writer
// of a measurement: the ResizeObserver's per-entry pass, the row ref seed, and
// the background measure farm. WHEN a written measurement becomes geometry is
// decided by geometryScheduling.ts; which rows' measurements are retired or
// renamed by a list change is planned by the shift capture
// (shiftCompensation.ts, via planHeightRetirement) and drained here.

import { useCallback, useLayoutEffect, useMemo, useRef, useSyncExternalStore, type MutableRefObject, type RefObject } from 'react'
import { isRailSettling } from '../useRailWidth'
import { inPlaceDeltaAbove, resizedInPlaceBelow } from './inPlaceResize'
import { HeightIndex } from './HeightIndex'
import { repriceAboveFoldDelta } from './FollowController'
import type { WindowRange } from './WindowCalculator'
import { composerExplainsViewportChange } from '../../utils/composerResize'
import type { ShiftCapture } from './shiftCompensation'
import type { GeometrySync, StreamingGrace } from './geometryScheduling'

type Ref<V> = MutableRefObject<V>

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
  return measureBorderBox(el).height
}

/** The height above plus the row's viewport top, from ONE rect read (a seed
 *  needs both, and a second read of the same node is a cost the row-ref path
 *  does not need to pay). `top` is null when the node cannot answer. */
function measureBorderBox(el: HTMLElement): { height: number; top: number | null } {
  if (typeof el.getBoundingClientRect === 'function') {
    const r = el.getBoundingClientRect()
    const height = r.height > 0 ? Math.round(r.height * 4) / 4 : el.offsetHeight
    return { height, top: r.top }
  }
  return { height: el.offsetHeight, top: null }
}

export interface HeightOwner {
  heightIndexRef: Ref<HeightIndex | null>
  heightIndex: HeightIndex
  getH: (index: number) => number
  /** The owner, re-synced for this render's row count and estimate. */
  offsetIndex: HeightIndex
  /** The owner's announced geometry version (a subscribed value). */
  heightCommit: number
  totalHeight: number
  offsetBefore: number
  offsetAfter: number
  /** Persist pending measurements (the unmount teardown). */
  flushHeights: () => void
}

export function useHeightOwner<T>(ctx: {
  sessionId: string
  heightScopeKey: string | undefined
  itemCount: number
  estimatedHeight: number
  itemsRef: Ref<T[]>
  getKeyRef: Ref<(item: T, index: number) => string>
  windowRange: WindowRange
  shift: Pick<ShiftCapture, 'renamedKeysRef' | 'retiredKeysRef'>
}): HeightOwner {
  const { sessionId, heightScopeKey, itemCount, estimatedHeight, itemsRef, getKeyRef, windowRange } = ctx
  const { renamedKeysRef, retiredKeysRef } = ctx.shift

  // ---- Height owner (single read surface for row heights) ----
  //
  // `HeightIndex` holds the persisted `HeightCache` AND the O(log N) prefix-sum
  // tree, and is the only thing this hook asks about heights. No owner
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
  // capture (useShiftCapture) already compensates. Retiring KEEPS the measurement itself (see
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

  // NOTE: geometry invalidation is NOT a piece of state here. It lives on the
  // height owner, which announces a change in the same call that mutates the
  // tree -- see the `useSyncExternalStore` subscription further down, and
  // HeightIndex.syncAndAnnounce. A local counter used to serve this role, which
  // meant every writer had to remember to bump it: after a content SHRINK
  // (streaming finalize, widget settle, markdown reflow) a missed bump left
  // `totalHeight` stale-large and inflated `offsetAfter` into a phantom bottom
  // spacer (the "blank space at the bottom" bug, and the "flicker when the
  // scroll stops"), with nothing to catch it.
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

  const flushHeights = useCallback(() => {
    heightIndexRef.current?.flush()
  }, [])

  return {
    heightIndexRef,
    heightIndex,
    getH,
    offsetIndex,
    heightCommit,
    totalHeight,
    offsetBefore,
    offsetAfter,
    flushHeights,
  }
}

/** One ResizeObserver fire, classified: what resized and what it moved. */
export interface ResizeBatch {
  /** A mounted row changed height (not a first measurement). */
  genuineResize: boolean
  /** A row was measured for the first time. */
  firstMount: boolean
  /** The scroller's own box shrank for a reason other than the composer. */
  viewportResized: boolean
  /** A resized row is the tail, the streaming row, or the row in its post-stream grace. */
  tailRowResized: boolean
  /** The trailing-chrome wrapper (the host's `belowRows`) changed height: the
   *  working footer mounting under a quiet reply, a survey card, a tail spacer.
   *  Content below the last row, so growth there is tail growth for follow. */
  trailingChromeResized: boolean
  /** The caller-designated streaming row (or the row in its grace) resized. */
  streamingRowResized: boolean
  /** The scrollTop write, in px, that puts the reader's row back where this
   *  fire's reprices moved it from -- the RESIDUAL after whatever the engine's
   *  own scroll anchoring already absorbed (see the anchor pass in
   *  measureResizeEntries). Zero when nothing above the reader repriced, when
   *  no anchor row is known, or when the reader's own input has moved the
   *  viewport since the row was last seen. */
  aboveFoldReprice: number
}

export interface RowMeasurement {
  /** The ResizeObserver's per-entry pass: record measurements, classify the fire. */
  measureResizeEntries: (entries: ResizeObserverEntry[], el: HTMLDivElement) => ResizeBatch
  measureRef: (index: number) => (el: HTMLElement | null) => void
  farmIsMeasured: (index: number) => boolean
  farmRecord: (index: number, key: string, px: number) => boolean
  farmRowMounted: (index: number) => boolean
  /** Seed the current owner from every mounted row's live height (gated). */
  reseedMounted: () => void
  /** Record where every mounted row sits in the viewport right now -- the
   *  positions the next fire's residual is measured against. Called from the
   *  scroll listener, so the record is the last frame the reader saw. */
  noteRowTops: (el: HTMLDivElement) => void
  /** The fire's own scrollTop write just moved every row up by `delta`:
   *  carry the record with it, without re-reading a frame that is still
   *  mid-delivery (see noteRowTops's doc). */
  shiftRowTops: (delta: number) => void
}

export function useRowMeasurement<T>(ctx: {
  itemsRef: Ref<T[]>
  getKeyRef: Ref<(item: T, index: number) => string>
  streamingIndexRef: Ref<number | undefined>
  eagerFirstMeasureRef: Ref<boolean>
  elIndexRef: Ref<Map<Element, number>>
  resizeObserverRef: Ref<ResizeObserver | null>
  trailingRef: RefObject<HTMLDivElement>
  heightIndexRef: Ref<HeightIndex | null>
  canMeasure?: () => boolean
  windowRangeRef: Ref<WindowRange>
  scrollerRef: RefObject<HTMLDivElement | null>
  grace: Pick<StreamingGrace, 'graceIndexRef'>
  sync: Pick<GeometrySync, 'scheduleHeightSync'>
}): RowMeasurement {
  const {
    itemsRef, getKeyRef, streamingIndexRef, eagerFirstMeasureRef, elIndexRef, resizeObserverRef,
    trailingRef, heightIndexRef, windowRangeRef, canMeasure, scrollerRef,
  } = ctx
  const { graceIndexRef } = ctx.grace
  const { scheduleHeightSync } = ctx.sync
  // During a debounced width transition the DOM has reflowed but the owner
  // still holds the old width. All writers must refuse that measurement.
  const canMeasureRef = useRef(canMeasure)
  canMeasureRef.current = canMeasure
  const measurementIndex = useCallback(() =>
    canMeasureRef.current?.() === false ? null : heightIndexRef.current, [heightIndexRef])

  // ---- Live height, per mounted node ----
  // The height the DOM last showed for a mounted row, whichever scope owns the
  // cache. Classifying a fire -- first mount vs resize, and the above-fold
  // delta the compensation adds to scrollTop -- needs the row's height at its
  // PREVIOUS fire, and the persisted cache cannot supply it during a width
  // transition: the gate refuses the write, so the cache keeps the old width's
  // height for the whole drag plus the settle debounce. Reading that refused
  // height would price every fire from the OLD width (100 -> 120 -> 140
  // credited 20 + 40 rather than 20 + 20, a repeated 140 credited 40 rather
  // than 0), each added straight to scrollTop; skipping classification would
  // leave the reader displaced by every re-wrap above them, since WebKit has
  // no native anchor to fall back on. Keyed by node, not index: it follows a
  // row across an index shift and dies with the element -- the ref callback
  // drops the entry when the node detaches, and a remounted row is a new node
  // with no history.
  const liveHeightRef = useRef<Map<Element, number>>(new Map())

  // ---- Last seen top, per mounted node ----
  // Where each mounted row sat in the viewport (relative to the scroller's
  // top) the last time this hook saw the frame: at its seed, after a fire's
  // own write, and at every scroll event. Paired with the live height above,
  // it is what lets a fire measure how far the reader's row ACTUALLY moved
  // rather than summing how far the batch's heights say it should have.
  //
  // The distinction is the engine's native scroll anchoring. Chromium adjusts
  // scrollTop for a reprice above the viewport DURING the layout that
  // reprices it -- before any ResizeObserver callback, before its own scroll
  // event -- so by the time the fire arrives the reader's row may already be
  // held and the rects read here already include that adjustment. Summing
  // the batch's growth and adding it to scrollTop then pays a second time for
  // what the engine already paid (probe: three rows +320 above the reader,
  // engine +960, hook +960 again), and classifying rows by their post-layout
  // rects reads a row native pushed under the fold as straddling (+320 more).
  // The residual against the row's last seen position is exact whatever the
  // engine did: nothing (WebKit), everything (Chromium holding this row), or
  // something else (Chromium holding a different row).
  //
  // Node-keyed like the height, and dropped with it when the node detaches.
  //
  // WHY THE RECORD IS NEVER STALE AGAINST THE READER'S OWN INPUT. The user's
  // scroll -- wheel, touch, key, scrollbar -- reaches the main thread's
  // scrollTop at the start of a rendering update, and its scroll event is
  // dispatched in that same update's scroll steps, BEFORE style, layout and
  // the ResizeObserver callbacks; every scroll event re-reads this record. So
  // by the time a fire reads rects, any user scroll those rects reflect has
  // already refreshed the record. What can change scrollTop between the
  // scroll steps and the fire is layout itself -- the engine's scroll
  // anchoring -- and that is exactly the part the residual must net out. An
  // earlier version guarded the fire with follow's hardware-input stamp
  // instead; a click or a key that scrolls nothing left the stamp newer than
  // the record forever and every later fire stood down (probe: click, wait
  // 2s, resize -- 1280px never paid), and a PageDown whose animation had not
  // yet moved scrollTop dropped the payment for a scroll that landed a frame
  // later. Neither input had moved the rows the record describes. A record
  // is invalid only when its row is gone (row identity: the node is no
  // longer mounted).
  //
  // Re-read at SCROLL EVENTS and seeds only, never at the end of a fire. A
  // scroll event is dispatched for the previous frame's position with layout
  // clean, so the rects read then are the frame the reader saw. A fire is
  // mid-frame: the observer delivers one layout's notifications over several
  // callbacks (the scroller's own box came alone, the rows an iteration
  // later, in the width probe), and every rect already shows the whole
  // reflow -- re-reading there would baseline the rows' fire on the reflowed
  // positions and measure no displacement at all (probe: the hook paid 0 of
  // 1280 with anchoring off). The fire's own write instead carries the record
  // along arithmetically (shiftRowTops), and the scroll event that write
  // raises re-reads everything a frame later.
  const liveTopRef = useRef<Map<Element, number>>(new Map())
  /** The reader's row the last fire measured, and where its write puts it. */
  const firedAnchorRef = useRef<{ node: Element; wantedTop: number } | null>(null)
  const noteRowTops = useCallback((el: HTMLDivElement) => {
    if (typeof el.getBoundingClientRect !== 'function') return
    const foldTop = el.getBoundingClientRect().top
    const tops = liveTopRef.current
    for (const node of elIndexRef.current.keys()) {
      tops.set(node, node.getBoundingClientRect().top - foldTop)
    }
    // A whole frame read afresh: the reader's row is chosen anew next fire.
    firedAnchorRef.current = null
  }, [elIndexRef])
  const shiftRowTops = useCallback((delta: number) => {
    const tops = liveTopRef.current
    for (const [node, top] of tops) tops.set(node, top - delta)
    // The reader's row is the one the write was computed for, so its
    // position after it is known exactly; the others are carried by the
    // write alone and re-read at the scroll event it raises.
    const fired = firedAnchorRef.current
    if (fired && tops.has(fired.node)) tops.set(fired.node, fired.wantedTop)
  }, [])

  /** Last observed scroller clientHeight, so a viewport resize has a direction. */
  const viewportHeightRef = useRef(0)

  // ---- ResizeObserver: the per-entry pass ----
  // Feeds the height cache and classifies the fire; the observer's own callback
  // (observers.ts) then routes the result to the compensation, the rail-settle
  // deferral, the follow decision and the sync scheduling, in that order.
  const measureResizeEntries = useCallback((entries: ResizeObserverEntry[], el: HTMLDivElement): ResizeBatch => {
    let genuineResize = false
    let firstMount = false
    // True when a resized row is at the TAIL (last item, the streaming row,
    // or the row inside its post-stream grace) -- the only growth a
    // bottom-parked reader should be carried along by. See the assignment
    // below for why an arbitrary row's growth must not pin.
    let tailRowResized = false
    // True when one of the resized entries is the caller-designated
    // streaming row (see `streamingIndex` option / syncHeightsNow's doc).
    let streamingRowResized = false
    // True when the trailing-chrome wrapper resized. It holds no row, so it
    // feeds no height into the cache; it only tells follow that content grew
    // BELOW the tail. A footer mounting there is what a reader parked at the
    // bottom is waiting to see, and no engine carries anyone down to it (see
    // the observer in observers.ts).
    let trailingChromeResized = false
    // True when the SCROLLER's own box resized (the observer watches it
    // alongside the rows). Chrome around the transcript changes the viewport
    // height with no scroll event and no row resize — the composer autosizes
    // when a slot switch restores a long draft, attachment strips and
    // banners mount, the browser window resizes. A viewport SHRINK while
    // pinned leaves scrollTop at the old, now-too-small bottom target — the
    // view rests slightly above the latest message ("switching sessions
    // doesn't land at the bottom"). A GROW is clamped by the browser itself.
    // Routed through pinAuto, so the race-proof guard still applies:
    // with follow released (reading history, anchor restore in flight) a
    // viewport resize never moves the viewport.
    let viewportResized = false
    // What this fire owes the reader in scrollTop -- see the anchor pass after
    // the loop. Only an in-place note ABOVE the fold in the reader's own row
    // is credited by arithmetic here; everything else is measured.
    let aboveFoldReprice = 0
    const foldTop = el.getBoundingClientRect().top
    // THE READER'S ROW, chosen BEFORE this fire's heights land: of the rows
    // whose last seen box reached below the fold, the one seen highest. Its
    // own reprice in this batch (if any) is classified against its LAST SEEN
    // top by the straddling-row rules below, and after the loop its actual
    // displacement decides the write.
    const seenTops = liveTopRef.current
    const seenHeights = liveHeightRef.current
    let anchor: { node: Element; prevTop: number; prevHeight: number } | null = null
    // Until the next scroll event re-reads the frame, a fire that already
    // wrote keeps its reader's row: after that write only ITS position on
    // record is exact (shiftRowTops), so choosing afresh could pick a row
    // whose carried position is off by the layout pushes it did not see.
    const fired = firedAnchorRef.current
    if (fired && seenTops.has(fired.node) && elIndexRef.current.has(fired.node)) {
      const prevHeight = seenHeights.get(fired.node)
      if (prevHeight !== undefined && prevHeight > 0) anchor = { node: fired.node, prevTop: seenTops.get(fired.node)!, prevHeight }
    }
    if (!anchor) {
      for (const [node, prevTop] of seenTops) {
        const prevHeight = seenHeights.get(node)
        if (prevHeight === undefined || prevHeight <= 0) continue
        if (prevTop + prevHeight <= 0 || prevTop >= el.clientHeight) continue
        if (!anchor || prevTop < anchor.prevTop) anchor = { node, prevTop, prevHeight }
      }
    }
    // The reader's row's own credited change, per the straddling rules.
    let anchorOwnReprice = 0
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
      if (trailingRef.current !== null && entry.target === trailingRef.current) {
        trailingChromeResized = true
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
      // What the DOM showed for this node at its last fire, then this one:
      // read whether or not the scope may be written.
      const live = liveHeightRef.current
      const prevLive = live.get(entry.target)
      live.set(entry.target, newH)
      // Resolved at call time, never captured: a callback that closed over the
      // owner would keep writing into the PREVIOUS session's heights after a
      // slot switch -- the same wrong-transcript class this owner exists to
      // close, reintroduced through a stale closure. Null while the width
      // transition is unsettled: the cache keeps the old width's height, and
      // ONLY the write below stands down for it.
      const hi = measurementIndex()
      // readMeasured (promoting): this row is mounted, so the read is genuine
      // access. `undefined` MUST stay reachable here -- the branch below tells
      // a first mount apart from a genuine resize by exactly that, so a
      // resolved height would classify every scroll-driven mount as a resize.
      const prevCached = hi?.readMeasured(idx)
      if (hi && prevCached !== newH) hi.setMeasured(idx, newH)
      // Classify against what the DOM showed last; a node with no live height
      // (its seed measured 0 under a hidden ancestor) falls back to the owner's
      // measurement.
      const prevH = prevLive ?? prevCached
      if (prevH !== newH) {
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
          // Only the READER'S row is classified: a row above it is measured
          // by how far it actually pushed the reader (the pass after the
          // loop), and a row below it moves nothing the reader sees. The
          // rules for the reader's own row are the recorded straddler rules,
          // applied to where it was LAST SEEN -- its post-layout rect already
          // carries this batch's displacement and any native adjustment.
          if (anchor && entry.target === anchor.node) {
            const inPlace = resizedInPlaceBelow(entry.target, foldTop)
            // An on-screen disclosure change is not compensated; a change noted
            // above the fold in the same row still is, by exactly its size.
            if (inPlace) aboveFoldReprice += inPlaceDeltaAbove(entry.target, foldTop)
            anchorOwnReprice = repriceAboveFoldDelta({
              rowTop: foldTop + anchor.prevTop,
              prevHeight: prevH,
              newHeight: newH,
              foldTop,
              // The streaming row (and the row in its post-stream settle grace)
              // grows by APPENDING at its bottom. Same identity the immediate
              // sync below keys on; a straddling row growing this way moves
              // nothing above the fold, so the predicate must not compensate
              // it (#10810 -- the "pushed up while reading the middle" drift).
              //
              // EXCEPT during the rail's collapse animation: for those ~150ms
              // the content column's width changes every frame and the row
              // RE-WRAPS, so its height change is a reprice distributed over
              // the whole row -- including the part above the fold -- not an
              // append. Keep the straddling-row compensation for that window
              // (WebKit has no native anchor to fall back on); the per-token
              // drift it re-admits is bounded by RAIL_SETTLE_MS.
              //
              // A disclosure the reader toggled on screen (see inPlaceResize)
              // changes the row below the fold only, the same geometry as an
              // append: compensating it would scroll the page by its height.
              appendsAtBottom:
                ((idx === streamingIndexRef.current || idx === graceIndexRef.current) && !isRailSettling())
                || inPlace,
            })
          }
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
    // THE RESIDUAL. Where the reader's row should be after this fire is its
    // last seen top, moved by whatever its own reprice is credited (a
    // re-wrapped straddler keeps its BOTTOM, so its top moves up by its
    // growth; an appending or in-place one keeps its top). Where it IS is its
    // rect now, after layout and after any native adjustment. The difference
    // is the one write that holds the reader, however the engine split the
    // work: on an engine with no scroll anchoring it equals the batch's whole
    // growth above the reader plus the row's own credit -- the arithmetic
    // this replaces -- and on Chromium it is only what native left undone.
    //
    // Only a row that is no longer mounted stands this down (see the record's
    // doc for why the reader's own input cannot make the record stale).
    if (anchor && genuineResize && elIndexRef.current.has(anchor.node)) {
      const nowTop = anchor.node.getBoundingClientRect().top - foldTop
      const wantedTop = anchor.prevTop - anchorOwnReprice
      aboveFoldReprice += nowTop - wantedTop
      firedAnchorRef.current = { node: anchor.node, wantedTop }
    }
    return { genuineResize, firstMount, viewportResized, tailRowResized, trailingChromeResized, streamingRowResized, aboveFoldReprice }
  }, [elIndexRef, itemsRef, measurementIndex, streamingIndexRef, graceIndexRef, trailingRef])

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
          // The node's live height and top go with it: a remount is a new
          // node, and a re-keyed row is re-seeded below in this same commit.
          liveHeightRef.current.delete(oldEl)
          liveTopRef.current.delete(oldEl)
          ro?.unobserve(oldEl)
        }
      }
      if (el) {
        elIndexRef.current.set(el, index)
        ro?.observe(el)
        // Seed the cache with the current height so the next render has a real
        // height for placeholders. A changed value must also reach the tree:
        // this seed is the SECOND cache writer (besides the RO)
        // and the RO won't re-fire for a value we just seeded, so without this
        // the geometry keeps a stale height and leaves a phantom spacer.
        const it = itemsRef.current[index]
        if (it) {
          const box = measureBorderBox(el)
          const h = box.height
          // Recorded even while the gate refuses the cache write below, so the
          // observer's first fire on this node is classified against the height
          // shown at mount rather than as a first mount.
          if (h > 0) liveHeightRef.current.set(el, h)
          // Its position too, so a row that mounts and then reprices before
          // any scroll or fire has a last seen top to measure against. A
          // scroller that cannot answer leaves the row anchorless until the
          // first scroll event.
          const sc = scrollerRef.current
          if (box.top !== null && sc && typeof sc.getBoundingClientRect === 'function') {
            liveTopRef.current.set(el, box.top - sc.getBoundingClientRect().top)
          }
          // Owner resolved at call time, not captured -- see the ResizeObserver
          // callback above for why a closed-over owner is a wrong-session write.
          const hi = measurementIndex()
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
  }, [scheduleHeightSync, resizeObserverRef, elIndexRef, itemsRef, measurementIndex, eagerFirstMeasureRef, scrollerRef])

  // A scope switch does not remount stable row refs, and the observer may have
  // already reported their new size while the old scope was still active (and
  // was refused above). Seed the new owner from live DOM; never copy the
  // refused measurements across scopes.
  const reseedMounted = useCallback(() => {
    const hi = measurementIndex()
    if (!hi) return
    for (const [node, index] of elIndexRef.current) {
      const height = measureBorderBoxHeight(node as HTMLElement)
      if (height > 0 && hi.peekMeasured(index) !== height) hi.setMeasured(index, height)
    }
    // Immediate, not debounced: this runs in the swap commit's layout phase,
    // where the DOM shows the cold owner's flat-estimate spacer with the reader's
    // rows displaced under it. Announcing now schedules the reseeded prices in
    // the same layout phase, and the height-sync consumer pays the swap in that
    // re-render from the anchor TRIGGER 7 captured against the last committed
    // frame (shiftCompensation.ts); the Chromium probe sampled no frame showing
    // the cold spacer. Debounced, that frame is shown for the 120ms window, and
    // every window recompute inside it maps scrollTop through a tree whose
    // mounted rows are still estimates. A one-shot per scope change, so it is
    // not the render storm the debounce protects against.
    scheduleHeightSync(true)
  }, [measurementIndex, elIndexRef, scheduleHeightSync])

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
  }, [heightIndexRef])
  // MOUNTED rows belong to the ResizeObserver, exclusively. The farm
  // renders a row in its DEFAULT disclosure state, which can differ from
  // the live row's by thousands of px (a collapsed tool group); letting
  // both write the same cache slot made them overwrite each other through
  // the remount cycle their own announcements caused -- the bottom rig
  // recorded a persistent ±2666px oscillation against a parked reader.
  const farmRowMounted = useCallback((index: number): boolean => {
    const r = windowRangeRef.current
    return index >= r.start && index < r.end
  }, [windowRangeRef])
  const farmRecord = useCallback((index: number, key: string, px: number): boolean => {
    if (px <= 0) return false
    // A row that mounted between pick and measure is the RO's now: drop
    // the farm's reading (see farmRowMounted).
    const r = windowRangeRef.current
    if (index >= r.start && index < r.end) return false
    const it = itemsRef.current[index]
    if (!it || getKeyRef.current(it, index) !== key) return false
    const hi = measurementIndex()
    if (!hi) return false
    if (hi.readMeasured(index) !== px) {
      hi.setMeasured(index, px)
      // Debounced, never eager: farm writes are background geometry — the
      // anchor-compensated debounced sync is exactly the safe landing path.
      scheduleHeightSync(false)
    }
    return true
  }, [scheduleHeightSync, windowRangeRef, itemsRef, getKeyRef, measurementIndex])

  return { measureResizeEntries, measureRef, farmIsMeasured, farmRecord, farmRowMounted, reseedMounted, noteRowTops, shiftRowTops }
}

/** Reseed mounted rows when the height scope or its measurement gate changes.
 *
 * Called by the facade after the reading-position restore, so the slot-entry
 * placement and every compensation of the commit have already run: this only
 * writes measurements and announces them (the anchor-compensated sync, in this
 * commit's layout phase rather than debounced -- see reseedMounted), never a
 * scroll position. */
export function useMeasurementScopeReseed(ctx: {
  heightIndex: HeightIndex
  canMeasure: (() => boolean) | undefined
  measurement: Pick<RowMeasurement, 'reseedMounted'>
}): void {
  const { heightIndex, canMeasure } = ctx
  const { reseedMounted } = ctx.measurement
  useLayoutEffect(() => {
    if (!canMeasure) return
    reseedMounted()
  }, [heightIndex, canMeasure, reseedMounted])
}
