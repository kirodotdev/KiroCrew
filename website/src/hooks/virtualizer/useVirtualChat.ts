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
// ResizeObserver-vs-scroll-event ordering that plagued the old ref soup — see
// that module's header for the rationale. The two write sites are:
//   - automatic pins (RO callback + append layout effect) → `pinAuto()`
//   - explicit pins (slot entry + scrollToBottom API) → `forcePin()`
//
// Visual stability while scrolled up (window expansion, async widget resizes
// above the viewport) is delegated to the browser's native CSS
// `overflow-anchor: auto`.
//
// Render contract for callers:
//   - Wrap the scroll container with `scrollerRef`
//   - Render the items in `virtualItems`: when `item.mounted` is true render
//     the real component wrapped in a div with `ref={measureRef(item.index)}`;
//     when false render a placeholder `<div style={{ height: item.height }} />`
//   - Place `topSentinelRef` / `bottomSentinelRef` at the list ends for
//     window expansion.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { HeightCache } from './HeightCache'
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
  stickAfterUserScroll,
  bottomTarget,
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
    bottomThreshold = DEFAULT_BOTTOM_THRESHOLD,
    isSticky,
    externalScrollerRef,
  } = opts

  const itemCount = items.length

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
    cacheRef.current = new HeightCache(sessionId)
    cacheSessionRef.current = sessionId
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
  // Timestamp (performance.now) of the last genuine USER scroll. Used to gate
  // RO-driven follow pins so they don't fire mid-fling — see SCROLL_SETTLE_MS.
  const lastUserScrollAtRef = useRef<number>(0)

  // Window range for what is currently mounted. Initial state is the TAIL of
  // the list (last ~overscan+1 items) — chat sessions always open at the
  // bottom, and starting here avoids a commit-timing race where the slot-entry
  // pin runs before the tail items have rendered.
  const [windowRange, setWindowRange] = useState<{ start: number; end: number }>(() => {
    const tailSize = Math.min(itemCount, overscan + 1)
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
    sessionIdRef.current = sessionId
    const tailSize = Math.min(itemCount, overscan + 1)
    setWindowRange({ start: Math.max(0, itemCount - tailSize), end: itemCount })
    stickRef.current = followOutput
    lastWriteTopRef.current = -1
    setIsAtBottom(true)
  }

  // ---- Height lookup ----
  const getH = useCallback(
    (i: number) => {
      const it = itemsRef.current[i]
      if (!it) return estimatedHeight
      const k = getKeyRef.current(it, i)
      const cached = cacheRef.current!.get(k)
      // Math.max(h, 1) so zero-height items still register with IO.
      return cached !== undefined ? Math.max(cached, 1) : estimatedHeight
    },
    [estimatedHeight],
  )

  // Debounced height→memo sync. Cache writes (RO re-measure, measureRef seed)
  // call this instead of bumping heightVersion directly. The bump (which
  // invalidates the offset memos) fires only after heights have been STABLE
  // for HEIGHT_SYNC_DEBOUNCE_MS, and only if the total actually changed. This
  // (a) corrects a one-time shrink's phantom spacer a beat later, and
  // (b) refuses to re-render during a continuous height oscillation (an
  // auto-height widget iframe whose content reflows when resized), which would
  // otherwise be a per-frame render storm + a spacer that jitters ±Δ.
  const lastSyncedTotalRef = useRef(-1)
  const heightSyncTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const scheduleHeightSync = useCallback(() => {
    if (heightSyncTimerRef.current) clearTimeout(heightSyncTimerRef.current)
    heightSyncTimerRef.current = setTimeout(() => {
      heightSyncTimerRef.current = null
      const total = getTotalHeight(itemsRef.current.length, getH)
      if (Math.abs(total - lastSyncedTotalRef.current) > 1) {
        lastSyncedTotalRef.current = total
        setHeightVersion((v) => v + 1)
      }
    }, HEIGHT_SYNC_DEBOUNCE_MS)
  }, [getH])

  // NOTE: `heightVersion` is an intentional manual-invalidation key in the
  // three memos below — it is not referenced in the bodies, so eslint flags it
  // as "unnecessary", but removing it reintroduces the stale-spacer bug
  // (memos reading the mutable HeightCache via the stable `getH` never
  // recompute on a height change). Do NOT remove it.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const totalHeight = useMemo(() => getTotalHeight(itemCount, getH), [itemCount, getH, heightVersion])
  const offsetBefore = useMemo(
    () => getOffsetFn(windowRange.start, itemCount, getH),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [windowRange.start, itemCount, getH, heightVersion],
  )
  // Height of all items AFTER the window — used as the bottom spacer so the
  // scroll content keeps its full size while only the window renders real DOM.
  const offsetAfter = useMemo(
    () => Math.max(0, totalHeight - getOffsetFn(windowRange.end, itemCount, getH)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [windowRange.end, itemCount, getH, totalHeight, heightVersion],
  )

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
    const next = computeWindow(
      el.scrollTop,
      el.clientHeight,
      itemsRef.current.length,
      getH,
      overscan,
    )
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
  }, [getH, overscan, scrollerRef])

  // ---- Pin helpers (the only code that writes el.scrollTop for follow) ----

  // Automatic pin: called when content changed (RO / append). When stick is
  // armed, always scrolls to the bottom. Stick is released ONLY by the scroll
  // handler detecting a genuine user scroll-up — never here.
  const pinAuto = useCallback(() => {
    const el = scrollerRef.current
    if (!el) return
    const geom = { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }
    const target = Math.max(0, geom.scrollHeight - geom.clientHeight)
    if (!stickRef.current) return
    // Always pin when stick is armed. The scroll handler is the sole arbiter of
    // releasing stick (it detects genuine user scroll-up). Previous approach
    // tried to release here via a scrollTop-vs-lastWriteTop guard, but smooth
    // scroll lag causes persistent false positives: the animation hasn't caught
    // up to the last target when content grows and a new pinAuto fires, so it
    // looks like "scrollTop < lastWriteTop" even though it's just animation lag.
    if (Math.abs(geom.scrollTop - target) > 0.5) {
      smoothPinActiveRef.current = true
      prevSmoothTopRef.current = geom.scrollTop
      el.scrollTo({ top: target, behavior: 'smooth' })
      lastWriteTopRef.current = target
    } else {
      lastWriteTopRef.current = target
    }
  }, [scrollerRef])

  // Forced pin: explicit jump-to-bottom (slot entry, scrollToBottom API,
  // jump-to-latest pill). Always lands at the bottom and (re-)arms follow.
  const forcePin = useCallback(() => {
    const el = scrollerRef.current
    if (!el) return
    stickRef.current = followOutput
    const target = bottomTarget({ scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight })
    el.scrollTop = target
    lastWriteTopRef.current = target
  }, [followOutput, scrollerRef])

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
        if (atBottom) smoothPinActiveRef.current = false
        // If the user grabs the page mid-animation and scrolls up,
        // scrollTop moves backward. Normal forward animation progress
        // always increases scrollTop toward the target.
        else if (el.scrollTop < prevSmoothTopRef.current - 1) {
          smoothPinActiveRef.current = false
          lastUserScrollAtRef.current = performance.now()
          stickRef.current = false
        }
        prevSmoothTopRef.current = el.scrollTop
      } else if (!isSelfScroll(el.scrollTop, lastWriteTopRef.current)) {
        lastUserScrollAtRef.current = performance.now()
        stickRef.current = stickAfterUserScroll(atBottom, followOutput)
      }
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
  }, [scrollerEl, bottomThreshold, followOutput, recomputeWindow])

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
      for (const entry of entries) {
        const idx = elIndexRef.current.get(entry.target)
        if (idx === undefined) continue
        const it = itemsRef.current[idx]
        if (!it) continue
        const newH = (entry.target as HTMLElement).offsetHeight
        const k = getKeyRef.current(it, idx)
        const prevH = cacheRef.current!.get(k)
        if (prevH !== newH) {
          cacheRef.current!.set(k, newH)
          // First-mount (prev undefined) happens during scroll-driven window
          // expansion; re-pinning then would interrupt the user's scroll. Only
          // genuine resizes (streaming growth, widget load) drive the pin —
          // EXCEPT while actively following (see below).
          if (prevH !== undefined) genuineResize = true
          else firstMount = true
        }
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
      const shouldFollow = genuineResize || (firstMount && stickRef.current)
      if (shouldFollow && (stickRef.current || performance.now() - lastUserScrollAtRef.current >= SCROLL_SETTLE_MS)) {
        pinAuto()
      }

      // A measured height changed in place — schedule a debounced re-sync of
      // the offset memos (see scheduleHeightSync). Debounced so a continuously
      // oscillating widget can't drive a per-frame render storm.
      if (genuineResize || firstMount) {
        scheduleHeightSync()
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
    return () => {
      ro.disconnect()
      // Cancel a frame queued by the last resize so it can't fire a
      // setWindowRange after the observer is torn down.
      if (rafId) cancelAnimationFrame(rafId)
      resizeObserverRef.current = null
    }
  }, [recomputeWindow, pinAuto, scheduleHeightSync, scrollerRef])

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
            setWindowRange((prev) => expandWindowUp(prev, overscan))
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
  }, [overscan, scrollerEl])

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

  // ---- Slot entry: force the scroller to the true bottom ----
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
  const slotPinDoneRef = useRef<string | null>(null)
  useLayoutEffect(() => {
    if (slotPinDoneRef.current && slotPinDoneRef.current !== sessionId) {
      slotPinDoneRef.current = null
    }
    if (slotPinDoneRef.current === sessionId) return
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
          const h = el.offsetHeight
          if (h > 0 && cacheRef.current!.get(k) !== h) {
            cacheRef.current!.set(k, h)
            scheduleHeightSync()
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
        if (typeof el.scrollTo === 'function') el.scrollTo({ top: scrollTop, behavior })
        else el.scrollTop = scrollTop
        // Jumping to a specific index is an explicit "stop following" intent.
        stickRef.current = false
        lastWriteTopRef.current = -1
      })
    },
    [overscan, getH, scrollerRef],
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
      lastWriteTopRef.current = -1
      if (typeof el.scrollTo === 'function') el.scrollTo({ top, behavior: 'smooth' })
      else el.scrollTop = top
    },
    [getH, scrollerRef],
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
        if (typeof el.scrollTo === 'function') el.scrollTo({ top: target, behavior: b })
        else el.scrollTop = target
        stickRef.current = followOutput
        lastWriteTopRef.current = target
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
    [overscan, followOutput, scrollerRef],
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
      if (heightSyncTimerRef.current) clearTimeout(heightSyncTimerRef.current)
      cacheRef.current?.flush()
    }
  }, [])

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
