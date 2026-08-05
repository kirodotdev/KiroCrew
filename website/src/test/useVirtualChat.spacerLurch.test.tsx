// Feature: chat-virtualizer — spacer-lurch fix for the "flash while scrolled
// up during streaming" report.
//
// CONTEXT: a separate flash at the streaming TEXT EDGE (MarkdownRenderer's
// `.ft-word` mount/opacity churn) is scoped entirely to the visible glyphs at
// the tail of the streaming message. That mechanism cannot explain a flash
// reported while the user is SCROLLED UP, with the streaming message below the
// fold and its edge off-screen.
//
// ROOT CAUSE (confirmed below): useVirtualChat's height→offset sync is
// debounced (HEIGHT_SYNC_DEBOUNCE_MS = 120ms, see scheduleHeightSync). While
// a message streams, its ResizeObserver-reported height changes
// continuously, which keeps RESETTING that debounce timer rather than firing
// it — so `offsetAfter`/`totalHeight` (which back the bottom spacer's
// rendered height) stay STALE for a while and then jump in one large
// discrete step once growth pauses long enough for the timer to fire. For a
// user scrolled up reading history, that spacer is directly below their
// viewport: a single large, infrequent size change is exactly the kind of
// layout lurch that reads as a "flash".
//
// FIX: the new `streamingIndex` option (see types.ts) lets a caller name the
// one row that is guaranteed to keep resizing for the duration of the turn.
// Height changes for that index bypass the debounce and sync immediately
// (still batched to once per animation frame by the RO callback's existing
// rAF coalescing), so the spacer tracks growth smoothly instead of freezing
// then jumping. Every other row keeps the debounced path — the render-storm
// protection it exists for is untouched.
//
// The first `describe` block below is regression coverage for the
// DEFAULT (no `streamingIndex` supplied) behavior — every row not named by
// the caller must still get debounced, coalescing protection. The second
// `describe` block proves the fix: naming the streaming row makes its growth
// track live instead of coalescing.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { type RefObject } from 'react'
import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import type { UseVirtualChatOptions } from '../hooks/virtualizer/types'

interface Geom { scrollTop: number; scrollHeight: number; clientHeight: number }

function makeScroller(initial: Geom) {
  const el = document.createElement('div')
  const state: Geom = { ...initial }
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => state.scrollTop,
    set: (v: number) => { state.scrollTop = v },
  })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => state.scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => state.clientHeight })
  ;(el as unknown as { scrollTo: (o: { top: number }) => void }).scrollTo = (o) => { state.scrollTop = o.top }
  return { el, state }
}

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

// A controllable ResizeObserver stand-in. Real RO batches native layout
// changes into callback ticks; this fake lets the test fire ticks on demand
// (`fireResize`) to simulate the streaming message's element growing every
// animation frame, exactly like MarkdownRenderer's DOM growing char-by-char
// while `useSmoothStream` reveals text.
class FakeResizeObserver {
  static instances: FakeResizeObserver[] = []
  cb: ResizeObserverCallback
  observed = new Set<Element>()
  constructor(cb: ResizeObserverCallback) {
    this.cb = cb
    FakeResizeObserver.instances.push(this)
  }
  observe(el: Element) { this.observed.add(el) }
  unobserve(el: Element) { this.observed.delete(el) }
  disconnect() { this.observed.clear() }
  fire(entries: Partial<ResizeObserverEntry>[]) {
    this.cb(entries as ResizeObserverEntry[], this as unknown as ResizeObserver)
  }
}

function mkEntry(target: HTMLElement, height: number): Partial<ResizeObserverEntry> {
  Object.defineProperty(target, 'offsetHeight', { configurable: true, get: () => height })
  return { target }
}

describe('useVirtualChat: default path still debounces (no streamingIndex)', () => {
  let origRO: typeof ResizeObserver | undefined
  let origRaf: typeof requestAnimationFrame

  beforeEach(() => {
    localStorage.clear()
    FakeResizeObserver.instances = []
    origRO = globalThis.ResizeObserver
    globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver
    // rAF fires synchronously so the RO callback's own coalesced window
    // recompute doesn't need a real frame pump.
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    globalThis.ResizeObserver = origRO as typeof ResizeObserver
    globalThis.requestAnimationFrame = origRaf
  })

  function mount(sessionId: string, geom: Geom, items: Item[]) {
    const { el, state } = makeScroller(geom)
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const view = renderHook(
      (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
      { initialProps: { items, sessionId, getKey, externalScrollerRef: ref } },
    )
    // Measure every HISTORY row at a fixed 100px so the adaptive
    // averageHeight() estimate for any (there shouldn't be any) unmeasured
    // row is stable and uninvolved in the assertions below — isolating the
    // ONE variable under test (the streaming row's own height) from
    // HeightCache's unrelated adaptive-mean behavior for unmeasured rows.
    const HISTORY_H = 100
    for (let i = 0; i < items.length - 1; i++) {
      const node = document.createElement('div')
      Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => HISTORY_H })
      act(() => { view.result.current.measureRef(i)(node) })
    }
    // Register a real DOM node for the last item as the "streaming message"
    // element so measureRef seeds it and the RO can target it directly.
    const streamNode = document.createElement('div')
    Object.defineProperty(streamNode, 'offsetHeight', { configurable: true, get: () => 40 })
    const lastIdx = items.length - 1
    act(() => { view.result.current.measureRef(lastIdx)(streamNode) })
    // Every seed measurement above schedules its own debounced sync (see
    // measureRef). Flush it now so the "baseline" the tests diff against is
    // the SETTLED post-mount state, not a pre-sync snapshot — otherwise a
    // pending 120ms timer would land partway through the test and inflate
    // the measured delta by an amount that has nothing to do with the
    // hypothesis under test.
    act(() => { vi.advanceTimersByTime(120) })
    return { el, state, view, streamNode }
  }

  it('coalesces continuous streaming growth below the fold into ONE deferred spacer update, not a live-tracking one', () => {
    // 20 history rows (scrolled up reading them) + 1 streaming row at the tail.
    const { view, streamNode } = mount(
      'spacer-lurch-coalesce',
      { scrollTop: 0, scrollHeight: 3000, clientHeight: 400 },
      mkItems(21),
    )
    const ro = FakeResizeObserver.instances[FakeResizeObserver.instances.length - 1]
    const baselineAfter = view.result.current.offsetAfter
    const baselineTotal = view.result.current.totalHeight

    // Simulate 10 streaming growth ticks (~char-by-char reveal), each firing
    // WELL inside the 120ms debounce window (as continuous token/char streaming
    // does — many ticks per second). Per the debounce's own contract, none of
    // these should individually reach the offset memos.
    let h = 40
    for (let i = 0; i < 10; i++) {
      h += 8
      act(() => { ro.fire([mkEntry(streamNode, h)]) })
      act(() => { vi.advanceTimersByTime(20) }) // << 120ms — timer keeps resetting
      // While growth is still arriving inside the debounce window, the spacer
      // memo has NOT moved yet — it is still serving the stale pre-growth value.
      expect(view.result.current.offsetAfter).toBe(baselineAfter)
      expect(view.result.current.totalHeight).toBe(baselineTotal)
    }

    // Growth pauses (model idles / stream ends). Only NOW, after the full
    // debounce window elapses with no further resets, does the sync fire —
    // and it applies the ENTIRE accumulated 80px delta (10 * 8) in one commit.
    //
    // The streaming row is the LAST item and sits inside the mounted window
    // (windowRange.end === itemCount), so its growth is credited to
    // `totalHeight` but not to `offsetAfter` (offsetAfter = totalHeight -
    // offsetOf(windowRange.end), and the row is BEFORE that boundary, not
    // after it). offsetAfter would only carry this delta for an off-window
    // streaming row — asserting on totalHeight here isolates the debounce
    // mechanism itself from that unrelated windowing detail.
    act(() => { vi.advanceTimersByTime(120) })
    expect(view.result.current.totalHeight).toBe(baselineTotal + 80)
  })

  it('the single deferred commit is proportional to the FULL accumulated backlog, not one growth tick', () => {
    // Same setup, but drive many more, smaller ticks to show the deferred jump
    // scales with however long growth kept resetting the timer — an
    // unbounded-size discrete step is the shape a "flash" complaint fits,
    // versus a bounded per-frame increment which would not visibly lurch.
    const { view, streamNode } = mount(
      'spacer-lurch-magnitude',
      { scrollTop: 0, scrollHeight: 3000, clientHeight: 400 },
      mkItems(21),
    )
    const ro = FakeResizeObserver.instances[FakeResizeObserver.instances.length - 1]
    const baselineTotal = view.result.current.totalHeight

    let h = 40
    const TICKS = 40 // a longer streamed message
    for (let i = 0; i < TICKS; i++) {
      h += 3
      act(() => { ro.fire([mkEntry(streamNode, h)]) })
      act(() => { vi.advanceTimersByTime(16) }) // one rAF frame, << 120ms
    }
    // Still nothing committed — every one of those 40 ticks reset the timer.
    expect(view.result.current.totalHeight).toBe(baselineTotal)

    act(() => { vi.advanceTimersByTime(120) })
    // One commit, carrying the entire 120px backlog (40 * 3) at once.
    expect(view.result.current.totalHeight).toBe(baselineTotal + TICKS * 3)
  })

  it('does not move scrollTop for the scrolled-up user even though the spacer jumps (rules out a pin/anchor cause)', () => {
    // Isolates the spacer-memo mechanism from the pin/anchor machinery: if
    // scrollTop is provably untouched by this path, a visible "flash" for the
    // scrolled-up user can only be a layout/paint effect of the spacer element's
    // height changing under them — corroborating the spacer-lurch theory rather
    // than pointing back at pinAuto or the anchor-preservation effect.
    const { el, state, view, streamNode } = mount(
      'spacer-lurch-no-scroll-side-effect',
      { scrollTop: 500, scrollHeight: 3000, clientHeight: 400 },
      mkItems(21),
    )
    const ro = FakeResizeObserver.instances[FakeResizeObserver.instances.length - 1]
    // Establish the user as scrolled up and away from the bottom.
    act(() => { state.scrollTop = 500; el.dispatchEvent(new Event('scroll')) })
    expect(view.result.current.isAtBottom).toBe(false)

    let h = 40
    for (let i = 0; i < 10; i++) {
      h += 8
      act(() => { ro.fire([mkEntry(streamNode, h)]) })
      act(() => { vi.advanceTimersByTime(20) })
    }
    act(() => { vi.advanceTimersByTime(120) })

    // The spacer moved…
    expect(view.result.current.totalHeight).toBeGreaterThan(0)
    // …but the scrolled-up user's viewport position was never written.
    expect(el.scrollTop).toBe(500)
  })
})

describe('useVirtualChat: streamingIndex fix — the named row tracks live instead of coalescing', () => {
  let origRO: typeof ResizeObserver | undefined
  let origRaf: typeof requestAnimationFrame

  beforeEach(() => {
    localStorage.clear()
    FakeResizeObserver.instances = []
    origRO = globalThis.ResizeObserver
    globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    globalThis.ResizeObserver = origRO as typeof ResizeObserver
    globalThis.requestAnimationFrame = origRaf
  })

  function mountStreaming(sessionId: string, geom: Geom, items: Item[]) {
    const { el, state } = makeScroller(geom)
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const lastIdx = items.length - 1
    const view = renderHook(
      (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
      { initialProps: { items, sessionId, getKey, externalScrollerRef: ref, streamingIndex: lastIdx } },
    )
    const HISTORY_H = 100
    for (let i = 0; i < lastIdx; i++) {
      const node = document.createElement('div')
      Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => HISTORY_H })
      act(() => { view.result.current.measureRef(i)(node) })
    }
    const streamNode = document.createElement('div')
    Object.defineProperty(streamNode, 'offsetHeight', { configurable: true, get: () => 40 })
    act(() => { view.result.current.measureRef(lastIdx)(streamNode) })
    // The seed measurement (first-mount) is still debounced regardless of
    // streamingIndex — only GENUINE resizes (prevH !== undefined) bypass the
    // debounce. Flush it to settle the baseline before the ticks under test.
    act(() => { vi.advanceTimersByTime(120) })
    return { el, state, view, streamNode }
  }

  it('tracks EVERY streaming growth tick immediately — no frozen-then-jump', () => {
    const { view, streamNode } = mountStreaming(
      'streaming-index-live-track',
      { scrollTop: 0, scrollHeight: 3000, clientHeight: 400 },
      mkItems(21),
    )
    const ro = FakeResizeObserver.instances[FakeResizeObserver.instances.length - 1]
    const baselineTotal = view.result.current.totalHeight

    let h = 40
    let expected = baselineTotal
    for (let i = 0; i < 10; i++) {
      h += 8
      expected += 8
      // Advance the timer by less than the debounce window on every tick —
      // if this were still debounced, the pre-fix test above shows the
      // memo would stay frozen at `baselineTotal` through this whole loop.
      act(() => { ro.fire([mkEntry(streamNode, h)]) })
      act(() => { vi.advanceTimersByTime(20) })
      // FIXED behavior: each tick is reflected immediately, not deferred.
      expect(view.result.current.totalHeight).toBe(expected)
    }
    // No trailing debounced commit is pending — the value at the last tick
    // IS the final value; advancing further changes nothing.
    act(() => { vi.advanceTimersByTime(120) })
    expect(view.result.current.totalHeight).toBe(expected)
  })

  it('a NON-streaming row (index not named by streamingIndex) still debounces normally', () => {
    // Guards against an overbroad fix: naming row N as streaming must not
    // accidentally make every row's resizes immediate.
    const { view } = mountStreaming(
      'streaming-index-scoped',
      { scrollTop: 0, scrollHeight: 3000, clientHeight: 400 },
      mkItems(21),
    )
    const baselineTotal = view.result.current.totalHeight
    const nonStreamingNode = document.createElement('div')
    Object.defineProperty(nonStreamingNode, 'offsetHeight', { configurable: true, get: () => 100 })
    act(() => { view.result.current.measureRef(0)(nonStreamingNode) }) // pre-existing measured seed
    // Resize a HISTORY row (index 0, not the streamingIndex = 20).
    const ro = FakeResizeObserver.instances[FakeResizeObserver.instances.length - 1]
    const historyNode = [...ro.observed][0] as HTMLElement
    Object.defineProperty(historyNode, 'offsetHeight', { configurable: true, get: () => 150 })
    act(() => { ro.fire([{ target: historyNode }]) })
    // Still within the debounce window — must NOT have committed yet.
    act(() => { vi.advanceTimersByTime(20) })
    expect(view.result.current.totalHeight).toBe(baselineTotal)
    // After the full debounce window, the deferred commit lands.
    act(() => { vi.advanceTimersByTime(120) })
    expect(view.result.current.totalHeight).toBeGreaterThan(baselineTotal)
  })

  it('does not move scrollTop for the scrolled-up user (fix has no scroll side effect)', () => {
    const { el, state, view, streamNode } = mountStreaming(
      'streaming-index-no-scroll-side-effect',
      { scrollTop: 500, scrollHeight: 3000, clientHeight: 400 },
      mkItems(21),
    )
    const ro = FakeResizeObserver.instances[FakeResizeObserver.instances.length - 1]
    act(() => { state.scrollTop = 500; el.dispatchEvent(new Event('scroll')) })
    expect(view.result.current.isAtBottom).toBe(false)

    let h = 40
    for (let i = 0; i < 10; i++) {
      h += 8
      act(() => { ro.fire([mkEntry(streamNode, h)]) })
      act(() => { vi.advanceTimersByTime(20) })
    }
    expect(view.result.current.totalHeight).toBeGreaterThan(0)
    expect(el.scrollTop).toBe(500)
  })
})
