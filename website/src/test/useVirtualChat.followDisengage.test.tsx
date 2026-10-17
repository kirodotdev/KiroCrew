// Feature: chat-virtualizer — sticky follow with manual disengage (#7256).
//
// Pins the three behaviours the direction-aware follow decision adds:
//   1. an upward user scroll INSIDE the 100px at-bottom band releases follow
//      (observable via getFollow — this is what gates ChatPage's timer-driven
//      scrollToBottom calls, the "streaming yanked me back" sources),
//   2. a downward return re-engages only within the tight FOLLOW_REENGAGE_PX
//      band, not anywhere inside the pill's 100px band,
//   3. the ResizeObserver follow pin respects the settle gate while follow is
//      armed: user input suppresses RO pins for SCROLL_SETTLE_MS even though
//      stick is still true (the old bypass pinned against an active gesture,
//      fighting the user frame by frame).
//
// jsdom has no layout engine: geometry is faked on a detached scroller via
// `externalScrollerRef` (the layoutShrink suite's technique), and the RO path
// is driven through a stubbed ResizeObserver (the viewportResize suite's).

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { type RefObject } from 'react'

import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import type { UseVirtualChatOptions } from '../hooks/virtualizer/types'
import { FOLLOW_REENGAGE_PX } from '../hooks/virtualizer/FollowController'

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
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => state.scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => state.clientHeight })
  ;(el as unknown as { scrollTo: (o: { top: number }) => void }).scrollTo = (o) => {
    writes.n++
    state.scrollTop = o.top
  }
  return { el, state, writes }
}

class FakeResizeObserver {
  static instances: FakeResizeObserver[] = []
  cb: ResizeObserverCallback
  observed = new Set<Element>()
  constructor(cb: ResizeObserverCallback) { this.cb = cb; FakeResizeObserver.instances.push(this) }
  observe(el: Element) { this.observed.add(el) }
  unobserve(el: Element) { this.observed.delete(el) }
  disconnect() { this.observed.clear() }
  fire(entries: Partial<ResizeObserverEntry>[] = []) {
    this.cb(entries as ResizeObserverEntry[], this as unknown as ResizeObserver)
  }
}

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

const CH = 400
const SH = 1000
const BOTTOM = SH - CH // 600

function mount(items: Item[], sessionId: string) {
  const { el, state, writes } = makeScroller({ scrollTop: 0, scrollHeight: SH, clientHeight: CH })
  const ref: RefObject<HTMLDivElement | null> = { current: el }
  const initialProps: UseVirtualChatOptions<Item> = { items, sessionId, getKey, externalScrollerRef: ref }
  const view = renderHook((p: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(p), { initialProps })
  return { el, state, writes, view, ref }
}

describe('useVirtualChat: direction-aware follow disengage (#7256)', () => {
  beforeEach(() => localStorage.clear())

  it('exposes follow armed at the bottom after slot entry', () => {
    const { el, view } = mount(mkItems(5), 'follow-armed')
    expect(el.scrollTop).toBe(BOTTOM)
    expect(view.result.current.getFollow()).toBe(true)
  })

  it('releases follow on an upward scroll INSIDE the 100px band, and a later append does not yank', () => {
    const { el, state, view, ref } = mount(mkItems(5), 'inband-release')
    expect(view.result.current.getFollow()).toBe(true)

    // 40px up: inside the pill band, previously kept stick armed.
    act(() => { state.scrollTop = BOTTOM - 40; el.dispatchEvent(new Event('scroll')) })
    expect(view.result.current.getFollow()).toBe(false)

    // Content grows: the reader's position must belong to them now.
    act(() => {
      state.scrollHeight = SH + 300
      view.rerender({ items: mkItems(6), sessionId: 'inband-release', getKey, externalScrollerRef: ref })
    })
    expect(state.scrollTop).toBe(BOTTOM - 40)
    expect(view.result.current.getFollow()).toBe(false)
  })

  it('re-engages only within FOLLOW_REENGAGE_PX of the bottom, not merely inside the pill band', () => {
    const { el, state, view } = mount(mkItems(5), 'reengage-band')
    // Scroll far up (releases), then back DOWN to 60px above the bottom —
    // inside the 100px pill band, outside the tight re-engage band.
    act(() => { state.scrollTop = 200; el.dispatchEvent(new Event('scroll')) })
    expect(view.result.current.getFollow()).toBe(false)
    act(() => { state.scrollTop = BOTTOM - 60; el.dispatchEvent(new Event('scroll')) })
    expect(view.result.current.getFollow()).toBe(false)

    // Continue down into the tight band → re-engaged.
    act(() => { state.scrollTop = BOTTOM - (FOLLOW_REENGAGE_PX - 1); el.dispatchEvent(new Event('scroll')) })
    expect(view.result.current.getFollow()).toBe(true)
  })

  it('keeps following across a layout clamp that lands at the true bottom', () => {
    const { el, state, view } = mount(mkItems(5), 'clamp-keep')
    expect(view.result.current.getFollow()).toBe(true)
    // Content shrinks 200px; the browser clamps scrollTop to the new bottom.
    act(() => {
      state.scrollHeight = SH - 200
      state.scrollTop = BOTTOM - 200
      el.dispatchEvent(new Event('scroll'))
    })
    expect(view.result.current.getFollow()).toBe(true)
  })
})

describe('useVirtualChat: RO follow pin respects the settle gate while armed (#7256)', () => {
  let origRO: typeof ResizeObserver | undefined
  let nowSpy: ReturnType<typeof vi.spyOn> | undefined
  let now = 100_000

  beforeEach(() => {
    localStorage.clear()
    FakeResizeObserver.instances = []
    origRO = globalThis.ResizeObserver
    globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver
    now = 100_000
    nowSpy = vi.spyOn(performance, 'now').mockImplementation(() => now)
  })

  afterEach(() => {
    globalThis.ResizeObserver = origRO as typeof ResizeObserver
    nowSpy?.mockRestore()
  })

  /** The observer watching the scroller element (rows + viewport share it). */
  function viewportRO(el: HTMLElement): FakeResizeObserver {
    const inst = FakeResizeObserver.instances.find((i) => i.observed.has(el))
    expect(inst).toBeDefined()
    return inst!
  }

  it('holds RO pins off within SCROLL_SETTLE_MS of user input, then resumes', () => {
    const { el, state, writes } = mount(mkItems(5), 'settle-gate')
    expect(state.scrollTop).toBe(BOTTOM)
    const ro = viewportRO(el)

    // Wheel input lands (the intent listener bumps the settle timestamp before
    // any scroll event dispatches). Content then grows, so a follow pin is due
    // — but the RO tick arrives inside the settle window and must hold off
    // instead of fighting the gesture (the old stick-armed bypass pinned here).
    act(() => { el.dispatchEvent(new Event('wheel')) })
    state.scrollHeight = SH + 100
    const before = writes.n
    act(() => { ro.fire([{ target: el } as Partial<ResizeObserverEntry>]) })
    expect(writes.n).toBe(before) // no pin write during the settle window
    expect(state.scrollTop).toBe(BOTTOM)

    // Past SCROLL_SETTLE_MS with follow still armed and the user stationary,
    // the same tick now pins to the new bottom.
    now += 200
    act(() => { ro.fire([{ target: el } as Partial<ResizeObserverEntry>]) })
    expect(state.scrollTop).toBe(SH + 100 - CH)
  })
})
