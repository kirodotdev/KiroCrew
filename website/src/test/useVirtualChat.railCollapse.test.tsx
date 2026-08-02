// The left rail's collapse animates `grid-template-columns` on the shell grid
// (App.tsx) for 150ms. That is a LAYOUT property, so the content column's width
// changes on every frame of the animation and every mounted transcript row
// rewraps — each producing a ResizeObserver entry with a new offsetHeight.
//
// Profiled in isolation (7 mounted markdown-weight rows, 8 collapse+expand
// cycles, headless Chromium), animating the track vs not multiplied this
// observer's fires and its forced offsetHeight reads by 13-18x, while the FINAL
// cached heights came out identical — every extra measurement is discarded. The
// damaging part is not the reads but the read/write interleave: each genuine
// change calls pinAuto(), a scrollTop WRITE, between those reads, and drives a
// height-sync re-render plus a window recompute.
//
// These tests pin the settle window that holds those three back for the duration
// of the animation and then runs exactly ONE sync (plus one re-pin, for a user
// who was following). They also pin what must NOT change: the actively-streaming
// row stays on its immediate path, because stalling ITS growth re-creates the
// spacer lurch `streamingIndex` exists to prevent.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { type RefObject } from 'react'
import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import type { UseVirtualChatOptions } from '../hooks/virtualizer/types'
import { setRailWidth, isRailSettling, railWidthFor, RAIL_SETTLE_MS, __resetRailWidth } from '../hooks/useRailWidth'

interface Geom { scrollTop: number; scrollHeight: number; clientHeight: number }

function makeScroller(initial: Geom) {
  const el = document.createElement('div')
  const state: Geom = { ...initial }
  const writes = { n: 0 }
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => state.scrollTop,
    set: (v: number) => { writes.n++; state.scrollTop = v },
  })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => state.scrollHeight })  // mutable via state
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => state.clientHeight })
  ;(el as unknown as { scrollTo: (o: { top: number }) => void }).scrollTo = (o) => { writes.n++; state.scrollTop = o.top }
  return { el, state, writes }
}

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

class FakeResizeObserver {
  static instances: FakeResizeObserver[] = []
  cb: ResizeObserverCallback
  observed = new Set<Element>()
  constructor(cb: ResizeObserverCallback) { this.cb = cb; FakeResizeObserver.instances.push(this) }
  observe(el: Element) { this.observed.add(el) }
  unobserve(el: Element) { this.observed.delete(el) }
  disconnect() { this.observed.clear() }
  fire(entries: Partial<ResizeObserverEntry>[]) {
    this.cb(entries as ResizeObserverEntry[], this as unknown as ResizeObserver)
  }
}

function setH(target: HTMLElement, height: number) {
  Object.defineProperty(target, 'offsetHeight', { configurable: true, get: () => height })
  return { target } as Partial<ResizeObserverEntry>
}

describe('useRailWidth: collapse settle window', () => {
  beforeEach(() => { __resetRailWidth() })
  afterEach(() => { __resetRailWidth() })

  it('is not settling at rest', () => {
    expect(isRailSettling()).toBe(false)
  })

  it('arms on a genuine track change', () => {
    setRailWidth(railWidthFor({ isMobile: false, collapsed: true }))
    expect(isRailSettling()).toBe(true)
  })

  it('does NOT arm when the width is unchanged', () => {
    // setRailWidth is called from an effect that re-runs on unrelated deps
    // (isMobile). Arming on a no-op write would hold the window open forever
    // under any churn and silently disable height syncing.
    setRailWidth(railWidthFor({ isMobile: false, collapsed: false })) // already the default
    expect(isRailSettling()).toBe(false)
  })

  it('closes after the window elapses', () => {
    vi.useFakeTimers()
    try {
      setRailWidth(74)
      expect(isRailSettling()).toBe(true)
      vi.advanceTimersByTime(RAIL_SETTLE_MS + 1)
      expect(isRailSettling()).toBe(false)
    } finally { vi.useRealTimers() }
  })
})

describe('useVirtualChat: rail-collapse resize storm', () => {
  let origRO: typeof ResizeObserver | undefined
  let origRaf: typeof requestAnimationFrame

  beforeEach(() => {
    localStorage.clear()
    __resetRailWidth()
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
    __resetRailWidth()
  })

  /** Mount with NO streamingIndex — an idle transcript, which is the case the
   *  rail collapse actually happens in. */
  function mountIdle(sessionId: string, geom: Geom, items: Item[]) {
    const { el, state, writes } = makeScroller(geom)
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const baseProps: UseVirtualChatOptions<Item> = { items, sessionId, getKey, externalScrollerRef: ref }
    const view = renderHook((p: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(p), { initialProps: baseProps })
    const nodes: HTMLElement[] = []
    for (let i = 0; i < items.length; i++) {
      const node = document.createElement('div')
      Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => 100 })
      act(() => { view.result.current.measureRef(i)(node) })
      nodes.push(node)
    }
    act(() => { vi.advanceTimersByTime(200) })   // settle the first-mount seed
    return { el, state, view, nodes, baseProps, writes }
  }

  it('does NOT write scrollTop on every frame of the animation (the thrash)', () => {
    // This is the cost the window removes. The height sync was ALREADY protected
    // by HEIGHT_SYNC_DEBOUNCE_MS (120ms) coalescing inside the 150ms animation —
    // what was unprotected is pinAuto(), a scrollTop WRITE issued on every
    // genuine resize, interleaved between the forced offsetHeight reads. Read /
    // write / read across ~9 frames is the layout thrash.
    const { view, nodes, writes, state } = mountIdle(
      'rail-thrash', { scrollTop: 500, scrollHeight: 900, clientHeight: 400 }, mkItems(7),
    )
    const ro = FakeResizeObserver.instances[FakeResizeObserver.instances.length - 1]
    writes.n = 0

    act(() => { setRailWidth(74) })
    for (let f = 0; f < 9; f++) {
      const h = 120 + f * 10
      // The content genuinely grows as rows rewrap narrower, so the bottom-pin
      // target moves each frame — which is what makes pinAuto actually write.
      state.scrollHeight = h * 7
      act(() => { ro.fire(nodes.map((n) => setH(n, h))) })
    }

    // Without the settle window this is one scrollTop write per frame (9). With
    // the window it is zero until the animation settles.
    expect(writes.n).toBe(0)

    act(() => { vi.advanceTimersByTime(RAIL_SETTLE_MS + 20) })
    // Exactly one re-pin after the window, for the user who was following.
    expect(writes.n).toBe(1)
    void view
  })

  it('does not leave heights stale — the post-window sync reflects the FINAL width', () => {
    // The window must not silently drop measurements: whatever the rows measure
    // at the settled width has to be what the offsets use.
    const { view, nodes } = mountIdle('rail-final', { scrollTop: 0, scrollHeight: 900, clientHeight: 400 }, mkItems(5))
    const ro = FakeResizeObserver.instances[FakeResizeObserver.instances.length - 1]

    act(() => { setRailWidth(74) })
    act(() => { ro.fire(nodes.map((n) => setH(n, 999))) })   // transitional, discarded
    act(() => { ro.fire(nodes.map((n) => setH(n, 137))) })   // final width
    act(() => { vi.advanceTimersByTime(RAIL_SETTLE_MS + 20) })

    expect(view.result.current.totalHeight).toBe(137 * 5)
  })

  it('does not suppress anything once the window has closed', () => {
    const { view, nodes } = mountIdle('rail-after', { scrollTop: 0, scrollHeight: 900, clientHeight: 400 }, mkItems(4))
    const ro = FakeResizeObserver.instances[FakeResizeObserver.instances.length - 1]

    act(() => { setRailWidth(74) })
    act(() => { vi.advanceTimersByTime(RAIL_SETTLE_MS + 20) })   // window closes with no resizes

    // A later widget resize takes the normal debounced path, unchanged.
    act(() => { ro.fire(nodes.map((n) => setH(n, 210))) })
    act(() => { vi.advanceTimersByTime(200) })
    expect(view.result.current.totalHeight).toBe(210 * 4)
  })

  it('leaves the ACTIVELY STREAMING row on its immediate path during the animation', () => {
    // Collapsing mid-turn must not stall the streaming row: that is exactly the
    // spacer lurch streamingIndex's immediate sync exists to prevent.
    const { el, state } = makeScroller({ scrollTop: 0, scrollHeight: 900, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const items = mkItems(5)
    const lastIdx = items.length - 1
    const view = renderHook(
      (p: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(p),
      { initialProps: { items, sessionId: 'rail-streaming', getKey, externalScrollerRef: ref, streamingIndex: lastIdx } as UseVirtualChatOptions<Item> },
    )
    const nodes: HTMLElement[] = []
    for (let i = 0; i < items.length; i++) {
      const node = document.createElement('div')
      Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => 100 })
      act(() => { view.result.current.measureRef(i)(node) })
      nodes.push(node)
    }
    act(() => { vi.advanceTimersByTime(200) })
    const ro = FakeResizeObserver.instances[FakeResizeObserver.instances.length - 1]
    const before = view.result.current.totalHeight

    act(() => { setRailWidth(74) })
    // The streaming row grows while the rail animates — this must land NOW.
    act(() => { ro.fire([setH(nodes[lastIdx], 260)]) })

    expect(view.result.current.totalHeight).not.toBe(before)
    void state
  })

  it('cancels the pending settle sync on unmount', () => {
    // The window schedules a timer that calls syncHeightsNow / pinAuto /
    // recomputeWindow — all of which touch state and the scroller. A survivor
    // would run against a torn-down consumer.
    const { view, nodes } = mountIdle('rail-unmount', { scrollTop: 0, scrollHeight: 900, clientHeight: 400 }, mkItems(3))
    const ro = FakeResizeObserver.instances[FakeResizeObserver.instances.length - 1]

    act(() => { setRailWidth(74) })
    act(() => { ro.fire(nodes.map((n) => setH(n, 175))) })   // arms the settle timer
    expect(vi.getTimerCount()).toBeGreaterThan(0)

    view.unmount()
    act(() => { vi.advanceTimersByTime(RAIL_SETTLE_MS + 50) })   // must not throw
    expect(true).toBe(true)
  })
})
