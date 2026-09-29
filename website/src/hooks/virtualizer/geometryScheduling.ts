// Resize and geometry scheduling for the chat virtualizer.
//
// A measurement is written the moment it is read (measurement.ts); this module
// decides WHEN that written height becomes geometry the reader sees: the
// debounced height sync and its deferral while the reader is in motion, the
// immediate path for the streaming row and its post-stream grace, and the
// rail-collapse settle window that holds a resize storm back until the
// animation ends.

import { useCallback, useLayoutEffect, useRef, type MutableRefObject } from 'react'
import { isRailSettling, RAIL_SETTLE_MS } from '../useRailWidth'
import { geometryCommitDeferred, SCROLL_SETTLE_MS } from './FollowController'
import type { HeightIndex } from './HeightIndex'
import type { FollowState, Pinning } from './followPolicy'
import type { ResizeBatch } from './measurement'
import type { ShiftCapture } from './shiftCompensation'
import type { WindowOperations } from './windowRange'

type Ref<V> = MutableRefObject<V>

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

export interface StreamingGrace {
  /** The row still inside its post-stream settle grace, if any. */
  graceIndexRef: Ref<number | undefined>
  /** Clear the grace timer (the unmount teardown). */
  cancelGraceTimer: () => void
}

export function useStreamingSettleGrace(streamingIndex: number | undefined): StreamingGrace {
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

  const cancelGraceTimer = useCallback(() => {
    if (graceTimerRef.current) clearTimeout(graceTimerRef.current)
  }, [])

  return { graceIndexRef, cancelGraceTimer }
}

export interface GeometrySync {
  syncHeightsNow: () => void
  scheduleHeightSync: (immediate?: boolean) => void
  /** The rail-collapse settle window: true when this fire was held back. */
  deferForRailSettle: (batch: ResizeBatch) => boolean
  /** Schedule the height sync a resize fire owes (debounced or immediate). */
  scheduleResizeSync: (batch: ResizeBatch) => void
  /** Drop a pending rail-settle sync (the resize observer's teardown). */
  cancelRailSettle: () => void
  /** Drop a pending debounced height sync (the unmount teardown). */
  cancelHeightSync: () => void
}

export function useGeometrySync<T>(ctx: {
  itemsRef: Ref<T[]>
  eagerFirstMeasureRef: Ref<boolean>
  heightIndexRef: Ref<HeightIndex | null>
  shift: Pick<ShiftCapture, 'captureHeightSyncAnchor'>
  follow: Pick<FollowState, 'stickRef' | 'lastHardInputAtRef' | 'lastUserScrollAtRef'>
  pinning: Pick<Pinning, 'pinAuto'>
  ops: Pick<WindowOperations, 'recomputeWindow'>
}): GeometrySync {
  const { itemsRef, eagerFirstMeasureRef, heightIndexRef } = ctx
  const { captureHeightSyncAnchor } = ctx.shift
  const { stickRef, lastHardInputAtRef, lastUserScrollAtRef } = ctx.follow
  const { pinAuto } = ctx.pinning
  const { recomputeWindow } = ctx.ops

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

  // ---- Rail-collapse settle window (see deferForRailSettle) ----
  // One pending timer at a time; `follow` remembers whether we were pinned to
  // the bottom when the window opened, so the single post-window re-pin only
  // fires for a user who was actually following.
  const railSettleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const railSettleFollowRef = useRef(false)
  const heightSyncTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

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
  // growth every RO tick (already rAF-coalesced by the caller — see the
  // resize observer in observers.ts), trading nothing for the general oscillating-widget case:
  // debounce still applies to every OTHER row, so a re-measuring widget
  // elsewhere in the transcript still gets the render-storm protection this
  // mechanism exists for.
  const syncHeightsNow = useCallback(() => {
    const idx = heightIndexRef.current
    if (!idx) return
    // The owner mutates the tree, decides whether the total actually moved, and
    // announces it -- there is no version to bump here, so there is no bump to
    // forget. The callback runs only when a change IS being announced, after the
    // mutation and before subscribers see it.
    idx.syncAndAnnounce(itemsRef.current.length, captureHeightSyncAnchor)
    // No `getH` dependency: the owner is read from its ref inside, and the tree
    // sync no longer takes a getter. Listing it here would tie this callback's
    // identity to the owner's, which the imperative writers must NOT rely on for
    // freshness (they resolve the owner at call time instead). The capture
    // varies only with the scroller ref, exactly as this callback always has.
  }, [captureHeightSyncAnchor, heightIndexRef, itemsRef])
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
  }, [syncHeightsNow, stickRef, lastHardInputAtRef, lastUserScrollAtRef])
  // Self-reference for the re-arm above: the callback cannot name itself in its
  // own body without tying its identity to a ref-free cycle.
  const scheduleHeightSyncRef = useRef<((immediate?: boolean) => void) | null>(null)
  scheduleHeightSyncRef.current = scheduleHeightSync

  // ---- Rail-collapse settle window ----
  // The shell animates `grid-template-columns` for 150ms, so the content
  // column's width changes on EVERY frame of the collapse and every mounted
  // row rewraps. Measured in isolation, that multiplies this observer's
  // fires and its forced `offsetHeight` reads by 13-18x per toggle — and the
  // final cached heights come out identical, so all of the extra work is
  // discarded. The cache updates (measureResizeEntries) are kept (layout is already dirty, so
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
  const deferForRailSettle = useCallback((batch: ResizeBatch): boolean => {
    const { genuineResize, firstMount, viewportResized, trailingChromeResized, streamingRowResized } = batch
    if ((genuineResize || firstMount || viewportResized || trailingChromeResized) && !streamingRowResized && isRailSettling()) {
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
      return true
    }
    return false
  }, [syncHeightsNow, pinAuto, recomputeWindow, stickRef])

  const scheduleResizeSync = useCallback((batch: ResizeBatch) => {
    const { genuineResize, firstMount, streamingRowResized } = batch
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
  }, [scheduleHeightSync, eagerFirstMeasureRef])

  const cancelRailSettle = useCallback(() => {
    // The rail-settle timer calls syncHeightsNow / pinAuto /
    // recomputeWindow, all of which touch state and the scroller, so a
    // survivor would run against a torn-down consumer.
    if (railSettleTimerRef.current) {
      clearTimeout(railSettleTimerRef.current)
      railSettleTimerRef.current = null
    }
    railSettleFollowRef.current = false
  }, [])

  const cancelHeightSync = useCallback(() => {
    if (heightSyncTimerRef.current) clearTimeout(heightSyncTimerRef.current)
  }, [])

  return { syncHeightsNow, scheduleHeightSync, deferForRailSettle, scheduleResizeSync, cancelRailSettle, cancelHeightSync }
}
