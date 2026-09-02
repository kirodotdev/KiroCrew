// Feature: chat-virtualizer — reading-position save/restore (issue #2774).
//
// Switching away from a session and back used to always pin to the bottom,
// losing the user's place in a long transcript. The fix persists a scroll
// ANCHOR (topmost visible row's key + viewport offset — never a raw
// scrollTop, which is meaningless before rows are measured) on scroll-settle,
// and restores it on slot entry instead of the unconditional bottom pin.
//
// Harness matches useVirtualChat.integration.test.tsx: a detached scroller
// with controllable geometry, layout-effect-driven assertions. The restore's
// initial write is offset math (synchronous, pre-paint), so it is
// deterministic in jsdom; the DOM settle frames guard on degenerate rects and
// a disconnected scroller, so they self-disable here.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { type RefObject } from 'react'

import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import type { UseVirtualChatOptions } from '../hooks/virtualizer/types'
import {
  ANCHOR_KEY_PREFIX,
  saveScrollAnchor,
  loadScrollAnchor,
} from '../hooks/virtualizer/ScrollAnchorCache'

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

/** Pre-measure every row at `h` px via the persisted HeightCache blob, so the
 *  restore's offset math is exact (100px * index) rather than estimate-driven. */
function seedHeights(sessionId: string, n: number, h: number) {
  const blob: Record<string, number> = {}
  for (let i = 0; i < n; i++) blob[`m${i}`] = h
  localStorage.setItem(`vc_heights_${sessionId}`, JSON.stringify(blob))
}

function mount(sessionId: string, geom: Geom, items: Item[]) {
  const { el, state } = makeScroller(geom)
  const ref: RefObject<HTMLDivElement | null> = { current: el }
  const view = renderHook(
    (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
    { initialProps: { items, sessionId, getKey, externalScrollerRef: ref } },
  )
  return { el, state, view, ref }
}

describe('useVirtualChat: reading-position restore on slot entry', () => {
  let origRaf: typeof requestAnimationFrame
  beforeEach(() => {
    localStorage.clear()
    // Synchronous rAF: the bottom-pin settle and the restore settle frames run
    // inline (the latter self-disable on the detached scroller — isConnected
    // is false — so the offset-math write is what the assertions see).
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
  })
  afterEach(() => {
    globalThis.requestAnimationFrame = origRaf
  })

  it('restores the saved anchor instead of pinning to the bottom (first mount)', () => {
    seedHeights('sess-r', 50, 100)
    saveScrollAnchor('sess-r', { key: 'm10', top: 24 })
    const { el, view } = mount(
      'sess-r',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    // offsetOf(m10) = 10 * 100 = 1000; the row's top sits 24px below the
    // viewport top → scrollTop = 976. The bottom pin would be 4600.
    expect(el.scrollTop).toBe(976)
    // The mounted window is around the anchor, not the tail.
    const indices = view.result.current.virtualItems.map((v) => v.index)
    expect(indices).toContain(10)
    expect(indices).not.toContain(49)
  })

  it('restores on a slot SWITCH into a session with a saved anchor', () => {
    seedHeights('sess-b', 50, 100)
    saveScrollAnchor('sess-b', { key: 'm20', top: -30 })
    const { el, state, view } = mount(
      'sess-a',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    // Session A has no anchor: slot entry pinned to the bottom.
    expect(el.scrollTop).toBe(4600)

    act(() => {
      state.scrollTop = 4600
      view.rerender({ items: mkItems(50), sessionId: 'sess-b', getKey, externalScrollerRef: { current: el } })
    })
    // offsetOf(m20) = 2000; row top 30px ABOVE the viewport top → 2030.
    expect(el.scrollTop).toBe(2030)
  })

  it('a streaming append while restored does NOT yank to the bottom', () => {
    seedHeights('sess-y', 50, 100)
    saveScrollAnchor('sess-y', { key: 'm10', top: 24 })
    const { el, state, view } = mount(
      'sess-y',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    expect(el.scrollTop).toBe(976)

    act(() => {
      state.scrollHeight = 5100
      view.rerender({ items: mkItems(51), sessionId: 'sess-y', getKey, externalScrollerRef: { current: el } })
    })
    // Follow is released while restored mid-history: no pull to the bottom.
    expect(el.scrollTop).toBe(976)
    expect(view.result.current.isAtBottom).toBe(false)
  })

  it('falls back to the bottom pin when the anchored row no longer exists', () => {
    seedHeights('sess-gone', 50, 100)
    saveScrollAnchor('sess-gone', { key: 'deleted-row', top: 0 })
    const { el, state, view } = mount(
      'sess-gone',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    expect(el.scrollTop).toBe(4600)
    // ...and follow was re-armed: an append pins to the new bottom.
    act(() => {
      state.scrollHeight = 5100
      view.rerender({ items: mkItems(51), sessionId: 'sess-gone', getKey, externalScrollerRef: { current: el } })
    })
    expect(el.scrollTop).toBe(4700)
  })

  it('pins to the bottom as before when no anchor is saved', () => {
    const { el } = mount(
      'sess-none',
      { scrollTop: 0, scrollHeight: 2000, clientHeight: 400 },
      mkItems(5),
    )
    expect(el.scrollTop).toBe(1600)
  })
})

describe('useVirtualChat: reading-position save on scroll settle', () => {
  let origRaf: typeof requestAnimationFrame
  beforeEach(() => {
    localStorage.clear()
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    globalThis.requestAnimationFrame = origRaf
  })

  /** Register a DOM node for row `i` whose viewport rect is derived from the
   *  live scrollTop, mimicking real layout: rowTop(i) = i*100 - scrollTop. */
  function attachRows(
    view: { result: { current: { measureRef: (i: number) => (el: HTMLElement | null) => void } } },
    state: Geom,
    indices: number[],
  ) {
    for (const i of indices) {
      const node = document.createElement('div')
      Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => 100 })
      node.getBoundingClientRect = () =>
        ({ top: i * 100 - state.scrollTop, bottom: i * 100 - state.scrollTop + 100, height: 100 } as DOMRect)
      act(() => { view.result.current.measureRef(i)(node) })
    }
  }

  it('persists the topmost visible row + offset after a user scroll settles', () => {
    const { el, state, view } = mount(
      'sess-save',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    attachRows(view, state, [4, 5, 6, 7, 8])

    // User scrolls up to read history: row 5 (top = 500-590 = -90) is the
    // topmost row still intersecting the viewport; row 4 ends above it.
    act(() => {
      state.scrollTop = 590
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => { vi.advanceTimersByTime(250) })

    expect(loadScrollAnchor('sess-save')).toEqual({ key: 'm5', top: -90 })
  })

  it('clears the anchor once the user returns to the bottom', () => {
    const { el, state, view } = mount(
      'sess-clear',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    attachRows(view, state, [4, 5, 6])
    act(() => {
      state.scrollTop = 590
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => { vi.advanceTimersByTime(250) })
    expect(localStorage.getItem(`${ANCHOR_KEY_PREFIX}sess-clear`)).not.toBeNull()

    act(() => {
      state.scrollTop = 4600 // exactly at the bottom
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => { vi.advanceTimersByTime(250) })
    expect(localStorage.getItem(`${ANCHOR_KEY_PREFIX}sess-clear`)).toBeNull()
  })

  it('flushes a pending save when the slot switches inside the debounce window', () => {
    const { el, state, view } = mount(
      'sess-flush',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    attachRows(view, state, [4, 5, 6, 7, 8])

    // Scroll up, then switch sessions BEFORE the 200ms save timer fires.
    act(() => {
      state.scrollTop = 590
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => {
      view.rerender({ items: mkItems(10), sessionId: 'sess-other', getKey, externalScrollerRef: { current: el } })
    })

    // The switch flushed the outgoing session's position synchronously.
    expect(loadScrollAnchor('sess-flush')).toEqual({ key: 'm5', top: -90 })
    // The cancelled timer must not fire later against the new session.
    act(() => { vi.advanceTimersByTime(500) })
    expect(loadScrollAnchor('sess-flush')).toEqual({ key: 'm5', top: -90 })
  })

  it('flush at switch clears the outgoing anchor when leaving at the bottom', () => {
    const { el, state, view } = mount(
      'sess-flush-bottom',
      { scrollTop: 0, scrollHeight: 5000, clientHeight: 400 },
      mkItems(50),
    )
    attachRows(view, state, [4, 5, 6])
    // A stale anchor exists from an earlier visit (written after mount so the
    // entry latch did not consume it).
    act(() => { saveScrollAnchor('sess-flush-bottom', { key: 'm5', top: -90 }) })

    // User scrolls (lands within the bottom threshold), then switches before
    // the timer fires: the flush must CLEAR the stale anchor, because the
    // user left this session at the bottom.
    act(() => {
      state.scrollTop = 4550
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => {
      view.rerender({ items: mkItems(10), sessionId: 'sess-other-2', getKey, externalScrollerRef: { current: el } })
    })
    expect(loadScrollAnchor('sess-flush-bottom')).toBeNull()
  })
})
