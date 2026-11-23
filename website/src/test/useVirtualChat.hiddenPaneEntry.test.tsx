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

  /** The pane is shown again, as Electron did it: the document comes back
   *  shorter, the scroller lands at its end, and that scroll fires BEFORE the
   *  ResizeObserver reports the box. */
  function revealClamped(el: HTMLElement, state: Geom, scrollHeight: number) {
    act(() => {
      state.clientHeight = 400
      state.scrollHeight = scrollHeight
      state.scrollTop = scrollHeight - 400
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => { fire?.([{ target: el }]) })
  }

  function mountVisible(sessionId: string, n: number) {
    const { el, state } = makeScroller({ scrollTop: 0, scrollHeight: n * 100, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    // `restoreGate` of every render, so a gate raised and dropped within one act still shows.
    const gates: boolean[] = []
    const view = renderHook(
      (props: UseVirtualChatOptions<Item>) => {
        const chat = useVirtualChat<Item>(props)
        gates.push(chat.restoreGate)
        return chat
      },
      { initialProps: { items: mkItems(n), sessionId, getKey, externalScrollerRef: ref } },
    )
    return { el, state, view, gates, ref }
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

  it('returns a reader scrolled up to their row when the pane is hidden and shown again', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    // The reader's row is saved (jsdom has no rects to capture one from).
    seedHeights('sess-pane-back', 50, 100)
    saveScrollAnchor('sess-pane-back', { key: 'm20', top: -30 })
    const { el, state, view, gates } = mountVisible('sess-pane-back', 50)
    expect(state.scrollTop).toBe(2030)
    collapse(el, state)
    const shownAt = gates.length
    revealClamped(el, state, 3000)
    // The rows are already mounted: no skeleton over them while the row lands.
    expect(gates.slice(shownAt)).not.toContain(true)
    act(() => { vi.advanceTimersByTime(1000) })
    expect(gates.slice(shownAt)).not.toContain(true)
    expect(view.result.current.restoreGate).toBe(false)
    expect(state.scrollTop).toBe(2030)
    expect(view.result.current.getFollow()).toBe(false)
  })

  it('returns a reader to the row they jumped to when the pane is hidden and shown again', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    // Row A is the one the reader scrolled to by hand earlier, so it is stored.
    seedHeights('sess-pane-jump', 50, 100)
    saveScrollAnchor('sess-pane-jump', { key: 'm20', top: -30 })
    const { el, state, view, gates } = mountVisible('sess-pane-jump', 50)
    expect(state.scrollTop).toBe(2030)
    // Mounted rows with rects from the live scrollTop, so a settle can read
    // which row is on screen (jsdom lays nothing out).
    for (let i = 8; i <= 24; i++) {
      const node = document.createElement('div')
      Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => 100 })
      node.getBoundingClientRect = () =>
        ({ top: i * 100 - state.scrollTop, bottom: i * 100 - state.scrollTop + 100, height: 100 } as DOMRect)
      act(() => { view.result.current.measureRef(i)(node) })
    }
    // A jump to row B (a search hit, a jump-to-turn control): a scroller write
    // with no wheel, touch, key or scrollbar input before it.
    act(() => {
      el.scrollTop = 1000
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => { vi.advanceTimersByTime(ANCHOR_SAVE_DEBOUNCE_MS + 1) })
    // The intent gate did not persist B: the stored row is still A.
    expect(loadScrollAnchor('sess-pane-jump')).toEqual({ key: 'm20', top: -30 })
    collapse(el, state)
    const writes: number[] = []
    const setTop = Object.getOwnPropertyDescriptor(el, 'scrollTop')!.set!
    Object.defineProperty(el, 'scrollTop', {
      configurable: true,
      get: () => state.scrollTop,
      set: (v: number) => { writes.push(v); setTop(v) },
    })
    const shownAt = gates.length
    revealClamped(el, state, 3000)
    act(() => { vi.advanceTimersByTime(1000) })
    // Back on B, not on the stored A.
    expect(state.scrollTop).toBe(1000)
    expect(writes).not.toContain(2030)
    expect(gates.slice(shownAt)).not.toContain(true)
    expect(view.result.current.restoreGate).toBe(false)
    expect(view.result.current.getFollow()).toBe(false)
  })

  it('returns a reader to their row when their offset still fits the shorter reshown document', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    seedHeights('sess-pane-fits', 50, 100)
    saveScrollAnchor('sess-pane-fits', { key: 'm20', top: -30 })
    const { el, state, view, gates } = mountVisible('sess-pane-fits', 50)
    expect(state.scrollTop).toBe(2030)
    collapse(el, state)
    // Log the hook's writes from here on: in Electron the surviving offset shows
    // another row (rows above re-laid out from estimates), so only a write puts
    // the reader back, even when it lands on the same number.
    const writes: number[] = []
    const setTop = Object.getOwnPropertyDescriptor(el, 'scrollTop')!.set!
    Object.defineProperty(el, 'scrollTop', {
      configurable: true,
      get: () => state.scrollTop,
      set: (v: number) => { writes.push(v); setTop(v) },
    })
    const shownAt = gates.length
    // Shown again shorter, but the old offset fits, so nothing clamps it to the end.
    act(() => {
      state.clientHeight = 400
      state.scrollHeight = 4000
      state.scrollTop = 2030
      el.dispatchEvent(new Event('scroll'))
    })
    act(() => { fire?.([{ target: el }]) })
    act(() => { vi.advanceTimersByTime(1000) })
    // The restore re-placed the reader at their row's offset against the reshown heights.
    expect(writes).toContain(2030)
    expect(state.scrollTop).toBe(2030)
    expect(gates.slice(shownAt)).not.toContain(true)
    expect(view.result.current.restoreGate).toBe(false)
    expect(view.result.current.getFollow()).toBe(false)
  })

  it('keeps a reader at the newest message when the pane is hidden and shown again', () => {
    seedHeights('sess-pane-tail', 50, 100)
    const { el, state, view } = mountVisible('sess-pane-tail', 50)
    expect(state.scrollTop).toBe(4600)
    // A stale row stored for the session: only follow keeps it unused.
    saveScrollAnchor('sess-pane-tail', { key: 'm20', top: -30 })
    collapse(el, state)
    revealClamped(el, state, 5000)
    expect(state.scrollTop).toBe(4600)
    expect(view.result.current.getFollow()).toBe(true)
  })

  it('keeps the skeleton up when the pane is hidden and shown while an entry restore waits for its row', () => {
    // The hold reads performance.now(): fake it with the timers so
    // RESTORE_HYDRATE_WAIT_MS cannot run out between the steps below.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] })
    seedHeights('sess-pane-hold', 50, 100)
    // The saved row comes with a later hydration chunk: only the first 20 rows
    // are on hand, so the entry restore holds the position.
    saveScrollAnchor('sess-pane-hold', { key: 'm40', top: -30 })
    const { el, state, view, gates, ref } = mountVisible('sess-pane-hold', 20)
    expect(view.result.current.restoreGate).toBe(true)
    collapse(el, state)
    const shownAt = gates.length
    revealClamped(el, state, 2000)
    act(() => { vi.advanceTimersByTime(500) })
    // The entry still owns the position: the partly loaded, unplaced
    // transcript stays behind the skeleton while its row is missing.
    expect(gates.length).toBeGreaterThan(shownAt)
    expect(gates.slice(shownAt)).not.toContain(false)
    // The rest of the transcript arrives, with the saved row in it.
    act(() => {
      state.scrollHeight = 5000
      view.rerender({ items: mkItems(50), sessionId: 'sess-pane-hold', getKey, externalScrollerRef: ref })
    })
    act(() => { vi.advanceTimersByTime(1000) })
    // Row 40 starts at 4000px; it sat 30px above the viewport top.
    expect(state.scrollTop).toBe(4030)
    expect(view.result.current.restoreGate).toBe(false)
    expect(view.result.current.getFollow()).toBe(false)
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
