// Feature: chat-virtualizer — a session ENTERED while its scroller has no box
// (a Remote Crew pane warming behind another tab: the pane's <iframe> is
// display:none, so every element inside it lays out at zero height) is placed
// once the box first gets a height.
//
// The slot-entry placement (restore the saved anchor, else pin to the bottom)
// runs once per entry. Run against a zero-height box it writes nothing that
// survives: a restore clamps to 0, a bottom pin targets 0. The re-placement
// for a collapsed box was wired only to `visibilitychange`, but a display:none
// iframe never fires one -- its document stays `visible` because the TOP
// window is visible. So the pane opened at the top of the transcript, both for
// a saved reading position and for a session with none.
//
// The browser's only signal is the scroller's own box resizing, so these tests
// grow the box and fire the ResizeObserver for the scroller, and fire no
// visibilitychange at all.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { type RefObject } from 'react'

import { useVirtualChat, type UseVirtualChatOptions } from '../hooks/virtualizer/useVirtualChat'
import { HEIGHT_SCHEMA_VERSION, SCHEMA_VERSION_KEY } from '../hooks/virtualizer/HeightCache'
import { loadScrollAnchor, saveScrollAnchor } from '../hooks/virtualizer/ScrollAnchorCache'
import { __resetRailWidth } from '../hooks/useRailWidth'
import { ANCHOR_SAVE_DEBOUNCE_MS } from '../hooks/virtualizer/readingPosition'

interface Geom { scrollTop: number; scrollHeight: number; clientHeight: number }

function makeScroller(initial: Geom) {
  const el = document.createElement('div')
  const state: Geom = { ...initial }
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => state.scrollTop,
    // A real engine clamps to the live maximum: in a collapsed box that is 0.
    set: (v: number) => { state.scrollTop = Math.max(0, Math.min(v, state.scrollHeight - state.clientHeight)) },
  })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => state.scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => state.clientHeight })
  ;(el as unknown as { scrollTo: (o: { top: number }) => void }).scrollTo = (o) => { el.scrollTop = o.top }
  return { el, state }
}

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

function seedHeights(sessionId: string, n: number, h: number) {
  const blob: Record<string, number | string> = { [SCHEMA_VERSION_KEY]: HEIGHT_SCHEMA_VERSION }
  for (let i = 0; i < n; i++) blob[`m${i}`] = h
  localStorage.setItem(`vc_heights_${sessionId}`, JSON.stringify(blob))
}

describe('useVirtualChat: entry into a collapsed (display:none pane) scroller', () => {
  let origRaf: typeof requestAnimationFrame
  let origRO: typeof ResizeObserver | undefined
  let fire: ((entries: { target: Element }[]) => void) | undefined

  beforeEach(() => {
    localStorage.clear()
    __resetRailWidth()
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
    origRO = globalThis.ResizeObserver
    globalThis.ResizeObserver = class {
      constructor(cb: ResizeObserverCallback) {
        fire = (entries) => cb(entries as unknown as ResizeObserverEntry[], this as unknown as ResizeObserver)
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
    Object.defineProperty(document, 'hidden', { value: false, configurable: true })
  })
  afterEach(() => {
    vi.useRealTimers()
    __resetRailWidth()
    globalThis.requestAnimationFrame = origRaf
    if (origRO) globalThis.ResizeObserver = origRO
    fire = undefined
  })

  function mountCollapsed(sessionId: string, n: number) {
    const { el, state } = makeScroller({ scrollTop: 0, scrollHeight: 0, clientHeight: 0 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const view = renderHook(
      (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
      { initialProps: { items: mkItems(n), sessionId, getKey, externalScrollerRef: ref } },
    )
    return { el, state, view, ref }
  }

  /** The pane is shown: the box gets its real height and the ResizeObserver
   *  reports the scroller. No visibilitychange -- an iframe never gets one. */
  function reveal(el: HTMLElement, state: Geom, scrollHeight: number) {
    act(() => {
      state.clientHeight = 400
      state.scrollHeight = scrollHeight
      fire?.([{ target: el }])
    })
  }

  /** The pane is hidden behind another tab: display:none collapses the box and
   *  the engine clamps scrollTop to 0, with the scroll event that clamp fires. */
  function collapse(el: HTMLElement, state: Geom) {
    act(() => {
      state.clientHeight = 0
      state.scrollHeight = 0
      state.scrollTop = 0
      el.dispatchEvent(new Event('scroll'))
      fire?.([{ target: el }])
    })
  }

  function mountVisible(sessionId: string, n: number) {
    const { el, state } = makeScroller({ scrollTop: 0, scrollHeight: n * 100, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const view = renderHook(
      (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
      { initialProps: { items: mkItems(n), sessionId, getKey, externalScrollerRef: ref } },
    )
    return { el, state, view }
  }

  it('keeps the saved reading position when a pending save fires after the pane is hidden', () => {
    // Fake only the timer the debounced save uses: the rAF stub above stays
    // synchronous, and the save's intent window reads the real clock.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    seedHeights('sess-pane-keep', 50, 100)
    saveScrollAnchor('sess-pane-keep', { key: 'm20', top: -30 })
    const { el, state } = mountVisible('sess-pane-keep', 50)
    expect(state.scrollTop).toBe(2030)
    // The reader nudges the transcript by hand, which arms the debounced save...
    act(() => {
      el.dispatchEvent(new Event('wheel'))
      el.scrollTop = 2000
      el.dispatchEvent(new Event('scroll'))
    })
    // ...and switches to another tab before it fires: the box collapses, so
    // the save reads a zero-height box, which looks like "at the bottom".
    collapse(el, state)
    act(() => { vi.advanceTimersByTime(ANCHOR_SAVE_DEBOUNCE_MS + 1) })
    expect(loadScrollAnchor('sess-pane-keep')).not.toBeNull()
  })

  it('lands a session with no saved position at the newest message', () => {
    const { el, state, view } = mountCollapsed('sess-pane-fresh', 50)
    expect(state.scrollTop).toBe(0)
    reveal(el, state, 5000)
    expect(state.scrollTop).toBe(4600)
    expect(view.result.current.getFollow()).toBe(true)
  })

  it('lands at the newest message when the transcript arrives while the box is collapsed', () => {
    const { el, state, view, ref } = mountCollapsed('sess-pane-late', 0)
    act(() => {
      view.rerender({ items: mkItems(50), sessionId: 'sess-pane-late', getKey, externalScrollerRef: ref })
    })
    expect(state.scrollTop).toBe(0)
    reveal(el, state, 5000)
    expect(state.scrollTop).toBe(4600)
    expect(view.result.current.getFollow()).toBe(true)
  })

  it('restores the saved reading position', () => {
    seedHeights('sess-pane-anchor', 50, 100)
    saveScrollAnchor('sess-pane-anchor', { key: 'm20', top: -30 })
    const { el, state, view } = mountCollapsed('sess-pane-anchor', 50)
    // Nothing a zero-height box can hold.
    expect(state.scrollTop).toBe(0)
    reveal(el, state, 5000)
    // Row 20 starts at 2000px; it sat 30px above the viewport top.
    expect(state.scrollTop).toBe(2030)
    expect(view.result.current.getFollow()).toBe(false)
  })

  it('DISCRIMINATOR: a box that already has a height is not re-placed by a later resize', () => {
    // A normal dashboard entry: placed at entry. A later viewport grow (the
    // composer collapsing) must not re-run entry placement over a reader who
    // has since moved.
    const { el, state } = makeScroller({ scrollTop: 0, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    renderHook(
      (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
      { initialProps: { items: mkItems(50), sessionId: 'sess-dash', getKey, externalScrollerRef: ref } },
    )
    expect(state.scrollTop).toBe(4600)
    act(() => {
      el.dispatchEvent(new Event('wheel'))
      el.scrollTop = 1000
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => {
      state.clientHeight = 500
      fire?.([{ target: el }])
    })
    expect(state.scrollTop).toBe(1000)
  })
})
