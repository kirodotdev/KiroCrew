// Feature: chat-virtualizer — viewport-box resize re-pin.
//
// The row ResizeObserver tracks CONTENT heights; the viewport observer under
// test here tracks the SCROLLER's own box. Chrome around the transcript
// (composer autosize on draft restore, attachment strips, banners, a window
// resize) shrinks the scroller with no scroll event and no row resize; while
// pinned to the bottom that used to strand the view slightly ABOVE the new
// bottom target ("switching sessions doesn't land at the bottom"). These tests
// pin the re-pin, its follow-guard (a reading user is never yanked), and the
// rail-collapse deferral (no per-frame scrollTop writes during the shell grid
// animation).

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { type RefObject } from 'react'
import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import type { UseVirtualChatOptions } from '../hooks/virtualizer/types'
import { setRailWidth, railWidthFor, RAIL_SETTLE_MS, __resetRailWidth } from '../hooks/useRailWidth'

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
  fire(entries: Partial<ResizeObserverEntry>[] = []) {
    this.cb(entries as ResizeObserverEntry[], this as unknown as ResizeObserver)
  }
}

describe('useVirtualChat: viewport-box resize re-pin', () => {
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

  /** The shared observer (it watches the scroller element alongside rows). */
  function viewportRO(el: HTMLElement): FakeResizeObserver {
    const inst = FakeResizeObserver.instances.find((i) => i.observed.has(el))
    expect(inst).toBeDefined()
    return inst!
  }

  /** Deliver a viewport-box resize: an entry whose target is the scroller. */
  function fireViewport(el: HTMLElement) {
    viewportRO(el).fire([{ target: el } as Partial<ResizeObserverEntry>])
  }

  function mount(sessionId: string, geom: Geom, items: Item[]) {
    const { el, state, writes } = makeScroller(geom)
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const baseProps: UseVirtualChatOptions<Item> = { items, sessionId, getKey, externalScrollerRef: ref }
    const view = renderHook((p: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(p), { initialProps: baseProps })
    act(() => { vi.advanceTimersByTime(250) }) // settle mount timers
    return { el, state, view, writes }
  }

  it('re-pins to the new bottom when the viewport shrinks while followed', () => {
    // Pinned at the bottom: 2000 - 400 = 1600 (slot-entry forcePin).
    const { el, state } = mount('viewport-shrink', { scrollTop: 0, scrollHeight: 2000, clientHeight: 400 }, mkItems(10))
    expect(el.scrollTop).toBe(1600)

    // The composer grows (draft restored / attachment strip mounts): the
    // scroller's box shrinks by 60px. No scroll event, no row resize — only
    // the viewport observer sees it. Old scrollTop is now 60px short.
    act(() => {
      state.clientHeight = 340
      fireViewport(el)
    })
    expect(el.scrollTop).toBe(2000 - 340)
  })

  it('does NOT move a user who scrolled up when the viewport shrinks', () => {
    const { el, state } = mount('viewport-noyank', { scrollTop: 0, scrollHeight: 2000, clientHeight: 400 }, mkItems(10))
    expect(el.scrollTop).toBe(1600)

    // User scrolls up to read history — the scroll handler releases follow.
    act(() => { state.scrollTop = 600; el.dispatchEvent(new Event('scroll')) })

    act(() => {
      state.clientHeight = 340
      fireViewport(el)
    })
    expect(el.scrollTop).toBe(600)
  })

  it('defers per-frame writes during the rail collapse and re-pins once at settle', () => {
    const { el, state, writes } = mount('viewport-rail', { scrollTop: 0, scrollHeight: 2000, clientHeight: 400 }, mkItems(10))
    expect(el.scrollTop).toBe(1600)
    const before = writes.n

    // Rail collapse arms the settle window; the shell grid animation resizes
    // the scroller's box every frame. None of those frames may write scrollTop.
    act(() => { setRailWidth(railWidthFor({ isMobile: false, collapsed: true })) })
    act(() => {
      for (let i = 0; i < 8; i++) {
        state.clientHeight = 400 - i // width-driven reflow jitters the box
        fireViewport(el)
      }
    })
    expect(writes.n).toBe(before)

    // One re-pin when the settle window closes (we were following).
    act(() => { state.clientHeight = 340; vi.advanceTimersByTime(RAIL_SETTLE_MS + 1) })
    expect(el.scrollTop).toBe(2000 - 340)
  })
})
