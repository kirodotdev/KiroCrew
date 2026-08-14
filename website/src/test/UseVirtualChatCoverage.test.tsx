// Feature: chat-virtualizer — useVirtualChat surface NOT covered by the
// existing suites.
//
// The follow/pin wiring lives in useVirtualChat.integration.test.tsx and the
// height-sync debounce in useVirtualChat.spacerLurch / .postStreamLurch. What
// this file exercises is everything those leave cold:
//   - the imperative jump APIs (scrollToIndex, scrollToIndexSmooth, mountIndex)
//   - the isSticky full-scan path in virtualItems
//   - the scrollToBottom settle frames (and their user-scroll bail-out)
//   - the post-stream STREAMING_SETTLE_GRACE_MS timer expiring / being cancelled
//   - ResizeObserver first-mount follow
//   - the window.__vcSnapshot diagnostic probe
//
// happy-dom has no layout engine, so scroll geometry is faked on a controlled
// scroller passed via `externalScrollerRef` (same harness as the sibling
// suites), and getBoundingClientRect is stubbed where the code under test
// reads it. `estimatedHeight` is pinned to ITEM_H so every row's offset is
// exactly index * ITEM_H whether or not it has been measured — that makes the
// jump-target arithmetic assertable rather than approximate.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { type RefObject } from 'react'
import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import type { UseVirtualChatOptions } from '../hooks/virtualizer/types'

interface Geom { scrollTop: number; scrollHeight: number; clientHeight: number }
interface Write { top: number; behavior?: string }

/** A div with controllable scroll geometry, recording every scrollTo write. */
function makeScroller(initial: Geom, opts: { withScrollTo?: boolean } = {}) {
  const { withScrollTo = true } = opts
  const el = document.createElement('div')
  const state: Geom = { ...initial }
  const writes: Write[] = []
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => state.scrollTop,
    set: (v: number) => { state.scrollTop = v },
  })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => state.scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => state.clientHeight })
  if (withScrollTo) {
    ;(el as unknown as { scrollTo: (o: Write) => void }).scrollTo = (o) => {
      writes.push({ ...o })
      state.scrollTop = o.top
    }
  } else {
    // The chokepoint falls back to a plain `scrollTop` assignment when the
    // element exposes no scrollTo (older/partial DOM implementations).
    ;(el as unknown as { scrollTo?: unknown }).scrollTo = undefined
  }
  return { el, state, writes }
}

/** Pin an element's rect so the header-offset derivation is deterministic. */
function stubRectTop(node: HTMLElement, top: number) {
  node.getBoundingClientRect = (() => ({
    top, bottom: top, left: 0, right: 0, width: 0, height: 0, x: 0, y: top,
    toJSON: () => ({}),
  })) as HTMLElement['getBoundingClientRect']
}

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

/** Uniform row height — also the estimate, so offsets are exact everywhere. */
const ITEM_H = 100

function mkRow(height: number) {
  const node = document.createElement('div')
  Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => height })
  return node
}

function mountHook(
  sessionId: string,
  items: Item[],
  scroller: { el: HTMLDivElement } | null,
  extra: Partial<UseVirtualChatOptions<Item>> = {},
) {
  const ref: RefObject<HTMLDivElement | null> | undefined =
    scroller ? { current: scroller.el } : undefined
  const initialProps: UseVirtualChatOptions<Item> = {
    items,
    sessionId,
    getKey,
    estimatedHeight: ITEM_H,
    ...(ref ? { externalScrollerRef: ref } : {}),
    ...extra,
  }
  const view = renderHook(
    (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
    { initialProps },
  )
  return { view, initialProps }
}

const mountedIndices = (items: { index: number }[]) => items.map((i) => i.index)

// ---------------------------------------------------------------------------
// scrollToIndex — window jump + aligned scrollTop, deferred into a frame.
// ---------------------------------------------------------------------------
describe('useVirtualChat: scrollToIndex', () => {
  let origRaf: typeof globalThis.requestAnimationFrame

  beforeEach(() => {
    localStorage.clear()
    vi.useFakeTimers()
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
  })
  afterEach(() => {
    globalThis.requestAnimationFrame = origRaf
    vi.useRealTimers()
  })

  it('aligns the target row to the top of the viewport by default', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 3000, clientHeight: 400 })
    const { view } = mountHook('sti-start', mkItems(21), sc)

    act(() => { view.result.current.scrollToIndex(8) })

    // Row 8 starts at 8 * ITEM_H; 'start' puts that offset at the viewport top.
    expect(sc.el.scrollTop).toBe(800)
  })

  it('centres the target row for align:center', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 3000, clientHeight: 400 })
    const { view } = mountHook('sti-center', mkItems(21), sc)

    act(() => { view.result.current.scrollToIndex(8, { align: 'center' }) })

    // 800 - clientHeight/2 + itemHeight/2
    expect(sc.el.scrollTop).toBe(650)
  })

  it('aligns the target row to the bottom of the viewport for align:end', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 3000, clientHeight: 400 })
    const { view } = mountHook('sti-end', mkItems(21), sc)

    act(() => { view.result.current.scrollToIndex(8, { align: 'end' }) })

    // 800 - clientHeight + itemHeight
    expect(sc.el.scrollTop).toBe(500)
  })

  it('clamps the jump to the scrollable range and honours the requested behavior', () => {
    // The last row's offset (2000) sits far beyond this scroller's maximum
    // scrollTop (900 - 400), so the write must be clamped rather than passed
    // through — an unclamped value would leave the scroller wedged.
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 900, clientHeight: 400 })
    const { view } = mountHook('sti-clamp', mkItems(21), sc)
    sc.writes.length = 0

    act(() => { view.result.current.scrollToIndex(20, { behavior: 'smooth' }) })

    expect(sc.el.scrollTop).toBe(500)
    expect(sc.writes.at(-1)).toEqual({ top: 500, behavior: 'smooth' })
  })

  it('mounts the target row so a caller can scroll to an off-window index', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 30000, clientHeight: 400 })
    const { view } = mountHook('sti-mount', mkItems(300), sc)
    expect(mountedIndices(view.result.current.virtualItems)).not.toContain(50)

    act(() => { view.result.current.scrollToIndex(50) })

    expect(mountedIndices(view.result.current.virtualItems)).toContain(50)
  })

  it('releases follow so a later append does not yank the viewport back', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 3000, clientHeight: 400 })
    const { view, initialProps } = mountHook('sti-release', mkItems(21), sc)
    // Slot entry pins to the bottom, so follow starts armed.
    expect(sc.el.scrollTop).toBe(2600)

    act(() => { view.result.current.scrollToIndex(5) })
    expect(sc.el.scrollTop).toBe(500)

    act(() => {
      sc.state.scrollHeight = 3200
      view.rerender({ ...initialProps, items: mkItems(22) })
    })

    expect(sc.el.scrollTop).toBe(500)
  })

  it('no-ops for an empty list and when no scroller is attached', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 0, clientHeight: 400 })
    const empty = mountHook('sti-empty', [], sc)
    sc.writes.length = 0
    act(() => { empty.view.result.current.scrollToIndex(3) })
    expect(sc.writes).toEqual([])

    const detached = mountHook('sti-noscroller', mkItems(10), null)
    expect(() => act(() => { detached.view.result.current.scrollToIndex(3) })).not.toThrow()
  })

  it('writes through plain scrollTop when the scroller exposes no scrollTo', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 3000, clientHeight: 400 }, { withScrollTo: false })
    const { view } = mountHook('sti-no-scrollto', mkItems(21), sc)

    // Slot entry already had to take the fallback path to reach the bottom.
    expect(sc.el.scrollTop).toBe(2600)

    act(() => { view.result.current.scrollToIndex(4) })
    expect(sc.el.scrollTop).toBe(400)
  })
})

// ---------------------------------------------------------------------------
// scrollToIndexSmooth — no pre-mounted window; target derived from a mounted
// row so the caller's header spacer is accounted for.
// ---------------------------------------------------------------------------
describe('useVirtualChat: scrollToIndexSmooth', () => {
  const HEADER_PX = 40
  let origRaf: typeof globalThis.requestAnimationFrame

  beforeEach(() => {
    localStorage.clear()
    vi.useFakeTimers()
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
  })
  afterEach(() => {
    globalThis.requestAnimationFrame = origRaf
    vi.useRealTimers()
  })

  /**
   * Registers two measured rows whose rects imply DIFFERENT header offsets:
   * the lower-index row (2) implies HEADER_PX, the higher-index row (5) — the
   * one registered FIRST, so it comes first in Map insertion order — implies
   * HEADER_PX + 50. The hook must pick the lowest index, not the first entry.
   */
  function setupRows(sc: ReturnType<typeof makeScroller>, view: ReturnType<typeof mountHook>['view']) {
    stubRectTop(sc.el, 0)
    const stale = mkRow(ITEM_H)
    stubRectTop(stale, HEADER_PX + 50 + 5 * ITEM_H - sc.state.scrollTop)
    const lowest = mkRow(ITEM_H)
    stubRectTop(lowest, HEADER_PX + 2 * ITEM_H - sc.state.scrollTop)
    act(() => { view.result.current.measureRef(5)(stale) })
    act(() => { view.result.current.measureRef(2)(lowest) })
  }

  it('derives the header offset from the LOWEST-index mounted row', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 3000, clientHeight: 400 })
    const { view } = mountHook('stis-header', mkItems(21), sc)
    setupRows(sc, view)
    sc.writes.length = 0

    act(() => { view.result.current.scrollToIndexSmooth(10) })

    // headerPx + row offset. Reading the first Map entry (index 5, whose rect
    // is deliberately 50px off) would land at 1090 instead.
    expect(sc.writes.at(-1)).toEqual({ top: HEADER_PX + 1000, behavior: 'smooth' })
  })

  it('centres the target and applies the caller offset', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 3000, clientHeight: 400 })
    const { view } = mountHook('stis-center', mkItems(21), sc)
    setupRows(sc, view)
    sc.writes.length = 0

    act(() => { view.result.current.scrollToIndexSmooth(10, { align: 'center', offset: -25 }) })

    // (40 + 1000) - clientHeight/2 + itemHeight/2 + offset
    expect(sc.el.scrollTop).toBe(865)
  })

  it('treats the header as zero when no row is mounted yet, and clamps to the range', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 900, clientHeight: 400 })
    const { view } = mountHook('stis-clamp', mkItems(21), sc)
    stubRectTop(sc.el, 0)
    sc.writes.length = 0

    act(() => { view.result.current.scrollToIndexSmooth(999) })

    // Index clamped to the last row, target clamped to the scrollable maximum.
    expect(sc.writes.at(-1)).toEqual({ top: 500, behavior: 'smooth' })
  })

  it('releases follow so streaming growth no longer pins the viewport', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 3000, clientHeight: 400 })
    const { view, initialProps } = mountHook('stis-release', mkItems(21), sc)
    stubRectTop(sc.el, 0)

    act(() => { view.result.current.scrollToIndexSmooth(3) })
    const landed = sc.el.scrollTop

    act(() => {
      sc.state.scrollHeight = 3400
      view.rerender({ ...initialProps, items: mkItems(23) })
    })

    expect(sc.el.scrollTop).toBe(landed)
  })

  it('no-ops for an empty list and when no scroller is attached', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 0, clientHeight: 400 })
    const empty = mountHook('stis-empty', [], sc)
    sc.writes.length = 0
    act(() => { empty.view.result.current.scrollToIndexSmooth(0) })
    expect(sc.writes).toEqual([])

    const detached = mountHook('stis-noscroller', mkItems(10), null)
    expect(() => act(() => { detached.view.result.current.scrollToIndexSmooth(3) })).not.toThrow()
  })
})

// ---------------------------------------------------------------------------
// mountIndex — near targets union with the window, far targets replace it and
// report themselves so the caller can teleport instead of gliding.
// ---------------------------------------------------------------------------
describe('useVirtualChat: mountIndex', () => {
  let origRaf: typeof globalThis.requestAnimationFrame

  beforeEach(() => {
    localStorage.clear()
    vi.useFakeTimers()
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
  })
  afterEach(() => {
    globalThis.requestAnimationFrame = origRaf
    vi.useRealTimers()
  })

  it('unions the window for a NEAR target and reports it as near', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 30000, clientHeight: 400 })
    const { view } = mountHook('mi-near', mkItems(300), sc)
    const before = mountedIndices(view.result.current.virtualItems)
    expect(before).toContain(299)

    let far: boolean | undefined
    act(() => { far = view.result.current.mountIndex(280) })

    const after = mountedIndices(view.result.current.virtualItems)
    expect(far).toBe(false)
    expect(after).toContain(280)
    // Union, not replacement: the previous tail stays mounted (no flash).
    expect(after).toContain(299)
  })

  it('replaces the window for a FAR target and reports it as far', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 30000, clientHeight: 400 })
    const { view } = mountHook('mi-far', mkItems(300), sc)
    expect(mountedIndices(view.result.current.virtualItems)).toContain(299)

    let far: boolean | undefined
    act(() => { far = view.result.current.mountIndex(0) })

    const after = mountedIndices(view.result.current.virtualItems)
    expect(far).toBe(true)
    expect(after).toContain(0)
    // Replacement: the thousands of rows in between are NOT mounted.
    expect(after).not.toContain(299)
    expect(after.length).toBeLessThan(20)
  })

  it('clamps an out-of-range index onto the last row', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 3000, clientHeight: 400 })
    const { view } = mountHook('mi-clamp', mkItems(21), sc)

    act(() => { view.result.current.mountIndex(9999) })

    expect(mountedIndices(view.result.current.virtualItems)).toContain(20)
  })

  it('reports near (false) for an empty list', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 0, clientHeight: 400 })
    const { view } = mountHook('mi-empty', [], sc)

    let far: boolean | undefined
    act(() => { far = view.result.current.mountIndex(4) })

    expect(far).toBe(false)
    expect(view.result.current.virtualItems).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// isSticky — the full-scan path: a sticky row renders even off-window.
// ---------------------------------------------------------------------------
describe('useVirtualChat: sticky items', () => {
  let origRaf: typeof globalThis.requestAnimationFrame

  beforeEach(() => {
    localStorage.clear()
    vi.useFakeTimers()
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
  })
  afterEach(() => {
    globalThis.requestAnimationFrame = origRaf
    vi.useRealTimers()
  })

  it('keeps a sticky row mounted while it sits far outside the window', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 30000, clientHeight: 400 })
    const { view } = mountHook('sticky-off-window', mkItems(300), sc, {
      isSticky: (_it, i) => i === 3,
    })

    const indices = mountedIndices(view.result.current.virtualItems)
    expect(indices).toContain(3)
    expect(indices).toContain(299)
    // Non-sticky off-window rows are still omitted (the spacers cover them).
    expect(indices).not.toContain(150)
    // Emitted in index order so React reconciliation stays stable.
    expect([...indices].sort((a, b) => a - b)).toEqual(indices)
  })

  it('marks every emitted row mounted and sizes it from the height cache', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 3000, clientHeight: 400 })
    const { view, initialProps } = mountHook('sticky-heights', mkItems(21), sc, {
      isSticky: (_it, i) => i === 0,
    })
    // Measured off-window: the sticky row is mounted, so it gets a real height
    // instead of the estimate the placeholder path would use.
    act(() => { view.result.current.measureRef(0)(mkRow(137)) })
    act(() => { view.rerender({ ...initialProps, items: mkItems(22), isSticky: (_it, i) => i === 0 }) })

    const sticky = view.result.current.virtualItems.find((v) => v.index === 0)
    expect(sticky?.mounted).toBe(true)
    expect(sticky?.height).toBe(137)
    expect(view.result.current.virtualItems.every((v) => v.mounted)).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// scrollToBottom — pins next frame, then re-pins over a few settle frames so a
// late measurement cannot leave the jump short of the true bottom.
// ---------------------------------------------------------------------------
describe('useVirtualChat: scrollToBottom settle', () => {
  let origRaf: typeof globalThis.requestAnimationFrame
  let frames: FrameRequestCallback[]

  beforeEach(() => {
    localStorage.clear()
    vi.useFakeTimers()
    frames = []
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
      frames.push(cb)
      return frames.length
    }) as typeof requestAnimationFrame
  })
  afterEach(() => {
    globalThis.requestAnimationFrame = origRaf
    vi.useRealTimers()
  })

  const runFrame = () => {
    const cb = frames.shift()
    if (cb) act(() => { cb(0) })
    return cb !== undefined
  }

  function mountAttached(sessionId: string) {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 2000, clientHeight: 400 })
    // The settle frames guard on el.isConnected, so the scroller must be live.
    document.body.appendChild(sc.el)
    const mounted = mountHook(sessionId, mkItems(21), sc)
    frames.length = 0
    return { sc, ...mounted }
  }

  it('re-pins across settle frames so late growth still lands exactly at the bottom', () => {
    const { sc, view } = mountAttached('stb-settle')
    act(() => { view.result.current.scrollToBottom() })

    // Frame 1 is the initial pin against the geometry known right now.
    runFrame()
    expect(sc.el.scrollTop).toBe(1600)

    // Rows measured after the tail window committed keep moving the bottom
    // down. Each settle frame re-targets it.
    for (const h of [2400, 2600, 2800]) {
      sc.state.scrollHeight = h
      runFrame()
      expect(sc.el.scrollTop).toBe(h - 400)
    }

    // Bounded: the settle stops after three frames rather than looping.
    expect(frames.length).toBe(0)
    sc.el.remove()
  })

  it('skips the settle frames for a smooth scroll so the glide is not cut short', () => {
    const { sc, view } = mountAttached('stb-smooth')
    act(() => { view.result.current.scrollToBottom('smooth') })
    sc.writes.length = 0

    runFrame()

    expect(sc.writes).toEqual([{ top: 1600, behavior: 'smooth' }])
    // No settle frame was queued — an instant re-pin would abort the glide.
    expect(frames.length).toBe(0)
    sc.el.remove()
  })

  it('abandons the settle when the user scrolls away mid-sequence', () => {
    const { sc, view } = mountAttached('stb-settle-abort')
    act(() => { view.result.current.scrollToBottom() })
    runFrame()
    expect(sc.el.scrollTop).toBe(1600)

    // The user grabs the page before the settle frame runs; the scroll handler
    // releases follow, so the pending frame must not yank them back.
    act(() => { sc.state.scrollTop = 400; sc.el.dispatchEvent(new Event('scroll')) })
    sc.state.scrollHeight = 2600
    while (runFrame()) { /* drain every queued frame */ }

    expect(sc.el.scrollTop).toBe(400)
    sc.el.remove()
  })

  it('no-ops for an empty list', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 0, clientHeight: 400 })
    const { view } = mountHook('stb-empty', [], sc)
    frames.length = 0
    sc.writes.length = 0

    act(() => { view.result.current.scrollToBottom() })
    while (runFrame()) { /* drain */ }

    expect(sc.writes).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// Post-stream settle grace — a row that keeps resizing as its turn closes stays
// on the immediate height-sync path for a FIXED window, then reverts.
// ---------------------------------------------------------------------------
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

function resizeTo(target: HTMLElement, height: number): Partial<ResizeObserverEntry> {
  Object.defineProperty(target, 'offsetHeight', { configurable: true, get: () => height })
  return { target }
}

const latestRo = () => FakeResizeObserver.instances[FakeResizeObserver.instances.length - 1]

describe('useVirtualChat: streaming settle grace lifecycle', () => {
  let origRO: typeof ResizeObserver | undefined
  let origRaf: typeof globalThis.requestAnimationFrame

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

  /** Mounts 21 rows with the tail row streaming, all heights settled. */
  function mountStreaming(sessionId: string) {
    const sc = makeScroller({ scrollTop: 500, scrollHeight: 3000, clientHeight: 400 })
    const items = mkItems(21)
    const lastIdx = items.length - 1
    const { view, initialProps } = mountHook(sessionId, items, sc, { streamingIndex: lastIdx })
    const rows: HTMLElement[] = []
    for (let i = 0; i <= lastIdx; i++) {
      const node = mkRow(ITEM_H)
      rows.push(node)
      act(() => { view.result.current.measureRef(i)(node) })
    }
    act(() => { vi.advanceTimersByTime(HEIGHT_SYNC_MS) })
    return { sc, view, initialProps, rows, lastIdx }
  }

  const HEIGHT_SYNC_MS = 120
  const GRACE_MS = 400

  it('reverts the just-ended streaming row to the debounced path once the grace expires', () => {
    const { view, initialProps, rows, lastIdx } = mountStreaming('grace-expiry')
    const baseline = view.result.current.totalHeight

    // Turn closes: the caller stops naming the row.
    act(() => { view.rerender({ ...initialProps, streamingIndex: undefined }) })
    // Inside the grace, a trailing resize still tracks immediately.
    act(() => { latestRo().fire([resizeTo(rows[lastIdx], ITEM_H + 20)]) })
    act(() => { vi.advanceTimersByTime(16) })
    expect(view.result.current.totalHeight).toBe(baseline + 20)

    // The grace is a FIXED window from the transition, never re-armed by a
    // resize — so an oscillating widget cannot hold the row on the immediate
    // path forever. Past it, the row is debounced again.
    act(() => { vi.advanceTimersByTime(GRACE_MS) })
    act(() => { latestRo().fire([resizeTo(rows[lastIdx], ITEM_H + 60)]) })
    act(() => { vi.advanceTimersByTime(16) })
    expect(view.result.current.totalHeight).toBe(baseline + 20)

    // …and lands once the debounce elapses.
    act(() => { vi.advanceTimersByTime(HEIGHT_SYNC_MS) })
    expect(view.result.current.totalHeight).toBe(baseline + 60)
  })

  it('cancels a pending grace when a new turn starts streaming', () => {
    const { view, initialProps, rows, lastIdx } = mountStreaming('grace-cancel')
    const baseline = view.result.current.totalHeight

    // Turn closes (grace armed for the tail row), then a NEW turn immediately
    // starts streaming a different row — the pending grace must be dropped.
    act(() => { view.rerender({ ...initialProps, streamingIndex: undefined }) })
    act(() => { view.rerender({ ...initialProps, streamingIndex: lastIdx - 1 }) })

    // Well inside the old grace window: with it cancelled, a resize on the
    // previously-streaming row takes the debounced path.
    act(() => { vi.advanceTimersByTime(100) })
    act(() => { latestRo().fire([resizeTo(rows[lastIdx], ITEM_H + 30)]) })
    act(() => { vi.advanceTimersByTime(16) })
    expect(view.result.current.totalHeight).toBe(baseline)

    act(() => { vi.advanceTimersByTime(HEIGHT_SYNC_MS) })
    expect(view.result.current.totalHeight).toBe(baseline + 30)
  })
})

// ---------------------------------------------------------------------------
// ResizeObserver first mount — a row measured for the first time must follow
// only while the viewport is already pinned to the bottom.
// ---------------------------------------------------------------------------
describe('useVirtualChat: first-mount measurement', () => {
  let origRO: typeof ResizeObserver | undefined
  let origRaf: typeof globalThis.requestAnimationFrame

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

  /**
   * Registers a row whose height is 0 at ref time, so measureRef does NOT seed
   * the cache — the first real height then arrives via the observer, which is
   * the genuine first-mount signal.
   */
  function registerUnmeasured(view: ReturnType<typeof mountHook>['view'], index: number) {
    const node = mkRow(0)
    act(() => { view.result.current.measureRef(index)(node) })
    return node
  }

  it('follows a freshly measured row while the viewport is pinned to the bottom', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 2000, clientHeight: 400 })
    const { view } = mountHook('fm-follow', mkItems(21), sc)
    expect(sc.el.scrollTop).toBe(1600)

    const node = registerUnmeasured(view, 20)
    sc.state.scrollHeight = 2500

    act(() => { latestRo().fire([resizeTo(node, 260)]) })

    // New content at the bottom of a followed transcript — pin to the new end.
    expect(sc.el.scrollTop).toBe(2100)
  })

  it('does not yank a scrolled-up reader when a row above them is measured', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 2000, clientHeight: 400 })
    const { view } = mountHook('fm-no-yank', mkItems(21), sc)

    // User scrolls up; let the post-scroll settle window elapse so the result
    // reflects the follow decision rather than the anti-fling gate.
    act(() => { sc.state.scrollTop = 300; sc.el.dispatchEvent(new Event('scroll')) })
    act(() => { vi.advanceTimersByTime(300) })

    const node = registerUnmeasured(view, 18)
    sc.state.scrollHeight = 2400
    act(() => { latestRo().fire([resizeTo(node, 300)]) })

    expect(sc.el.scrollTop).toBe(300)
  })

  it('ignores resize entries for nodes it never registered', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 2000, clientHeight: 400 })
    const { view } = mountHook('fm-unknown', mkItems(21), sc)
    const before = view.result.current.totalHeight

    act(() => { latestRo().fire([resizeTo(mkRow(999), 999)]) })
    act(() => { vi.advanceTimersByTime(200) })

    expect(view.result.current.totalHeight).toBe(before)
  })
})

// ---------------------------------------------------------------------------
// Append settle frame — the pin runs synchronously (pre-paint, no flicker) and
// again next frame, once the new row's real height is known.
// ---------------------------------------------------------------------------
describe('useVirtualChat: append settle frame', () => {
  let origRaf: typeof globalThis.requestAnimationFrame
  let frames: FrameRequestCallback[]

  beforeEach(() => {
    localStorage.clear()
    vi.useFakeTimers()
    frames = []
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
      frames.push(cb)
      return frames.length
    }) as typeof requestAnimationFrame
  })
  afterEach(() => {
    globalThis.requestAnimationFrame = origRaf
    vi.useRealTimers()
  })

  const drain = () => {
    let guard = 0
    while (frames.length > 0 && guard++ < 10) {
      const batch = frames.splice(0, frames.length)
      act(() => { batch.forEach((cb) => cb(0)) })
    }
  }

  function mountAttached(sessionId: string, count: number) {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: count * ITEM_H, clientHeight: 400 })
    document.body.appendChild(sc.el)
    const mounted = mountHook(sessionId, mkItems(count), sc)
    drain()
    return { sc, ...mounted }
  }

  it('re-pins next frame once the appended row has been measured', () => {
    const { sc, view, initialProps } = mountAttached('append-settle', 21)
    expect(sc.el.scrollTop).toBe(1700)

    // One new message: the synchronous pin uses the geometry available before
    // paint, which still excludes the new row's real height.
    act(() => {
      sc.state.scrollHeight = 2200
      view.rerender({ ...initialProps, items: mkItems(22) })
    })
    expect(sc.el.scrollTop).toBe(1800)

    // The row measures taller than the estimate, moving the bottom again.
    sc.state.scrollHeight = 2400
    drain()

    expect(sc.el.scrollTop).toBe(2000)
    sc.el.remove()
  })

  it('re-pins next frame after a bulk history hydration', () => {
    const { sc, view, initialProps } = mountAttached('append-bulk-settle', 2)

    act(() => {
      sc.state.scrollHeight = 20000
      view.rerender({ ...initialProps, items: mkItems(200) })
    })
    expect(sc.el.scrollTop).toBe(19600)

    // Late measurement of the hydrated tail grows the content once more.
    sc.state.scrollHeight = 20400
    drain()

    expect(sc.el.scrollTop).toBe(20000)
    sc.el.remove()
  })
})

// ---------------------------------------------------------------------------
// window.__vcSnapshot — the diagnostic probe used to debug scroll geometry.
// ---------------------------------------------------------------------------
interface VcSnapshot {
  sessionId: string
  count: number
  measured: number
  estimated: number
  estimatedHeight: number
  windowRange: { start: number; end: number }
  endIsCount: boolean
  offsetBefore: number
  offsetAfter: number
  totalHeight: number
  geom: { scrollTop: number; scrollHeight: number; clientHeight: number; distanceFromBottom: number } | null
  children: { tag: string; aria: string | null; h: number; cls: string }[]
  mountedRows: { index: number; cached: number | undefined; dom: number; delta: number }[]
  stick: boolean
  lastWriteTop: number
}

const readProbe = () =>
  (window as unknown as { __vcSnapshot?: () => VcSnapshot }).__vcSnapshot

describe('useVirtualChat: __vcSnapshot debug probe', () => {
  let origRaf: typeof globalThis.requestAnimationFrame

  beforeEach(() => {
    localStorage.clear()
    vi.useFakeTimers()
    vi.spyOn(console, 'log').mockImplementation(() => {})
    vi.spyOn(console, 'table').mockImplementation(() => {})
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
  })
  afterEach(() => {
    vi.restoreAllMocks()
    globalThis.requestAnimationFrame = origRaf
    vi.useRealTimers()
  })

  it('reports live geometry, the mounted window, and cached-vs-DOM heights', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 3000, clientHeight: 400 })
    const spacer = document.createElement('div')
    spacer.setAttribute('aria-hidden', 'true')
    spacer.className = 'vc-spacer'
    sc.el.appendChild(spacer)
    const { view } = mountHook('probe-full', mkItems(21), sc)

    // One row measured, then silently grown in the DOM: the probe exists to
    // surface exactly this drift between the cache and the live element.
    const row = mkRow(ITEM_H)
    act(() => { view.result.current.measureRef(20)(row) })
    Object.defineProperty(row, 'offsetHeight', { configurable: true, get: () => 220 })

    const snap = readProbe()?.()
    expect(snap).toBeDefined()
    expect(snap?.sessionId).toBe('probe-full')
    expect(snap?.count).toBe(21)
    expect(snap?.measured).toBe(1)
    expect(snap?.estimated).toBe(20)
    expect(snap?.endIsCount).toBe(true)
    expect(snap?.geom).toEqual({
      scrollTop: 2600, scrollHeight: 3000, clientHeight: 400, distanceFromBottom: 0,
    })
    expect(snap?.mountedRows).toEqual([{ index: 20, cached: ITEM_H, dom: 220, delta: 120 }])
    expect(snap?.children).toEqual([
      { tag: 'div', aria: 'true', h: 0, cls: 'vc-spacer' },
    ])
    expect(snap?.stick).toBe(true)
    // Every row now resolves to ITEM_H: the one measurement matches the
    // estimate, so the running mean it feeds leaves the unmeasured rows alone.
    expect(snap?.totalHeight).toBe(21 * ITEM_H)
    expect(snap?.offsetBefore).toBe(snap!.windowRange.start * ITEM_H)
  })

  it('reports null geometry and no rows when the scroller is not attached', () => {
    mountHook('probe-detached', mkItems(5), null)

    const snap = readProbe()?.()
    expect(snap?.geom).toBeNull()
    expect(snap?.children).toEqual([])
    expect(snap?.mountedRows).toEqual([])
    expect(snap?.measured).toBe(0)
    expect(snap?.lastWriteTop).toBe(-1)
  })

  it('removes its own probe on unmount', () => {
    const sc = makeScroller({ scrollTop: 0, scrollHeight: 3000, clientHeight: 400 })
    const { view } = mountHook('probe-cleanup', mkItems(5), sc)
    expect(readProbe()).toBeTypeOf('function')

    act(() => { view.unmount() })

    expect(readProbe()).toBeUndefined()
  })
})
