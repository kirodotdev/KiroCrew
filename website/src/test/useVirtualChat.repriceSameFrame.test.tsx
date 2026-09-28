/**
 * WHEN a reprice above the reader is compensated — the same frame, or 120ms later.
 *
 * A mounted row's real height changes in the commit that renders it. The height
 * INDEX learns about it only when the debounced sync runs
 * (`HEIGHT_SYNC_DEBOUNCE_MS`), and the released-reader correction is keyed on
 * the index's version. So growth ABOVE a mid-transcript reader displaces them
 * immediately and is undone one debounce later: a visible excursion that
 * returns to where it started.
 *
 * Measured on the device rather than reasoned about: a 10fps frame walk of a
 * 2.1s capture is perfectly static except for ONE +324 device-px step (108 CSS
 * px at 3x) and an exact −324 step in the very next sample — one displacement,
 * one cancellation, ~100ms apart, which is the debounce.
 *
 * Pin: the correction lands in the SAME observer fire as the growth, so no
 * frame exists in which the reader has moved. The debounced index sync then has
 * nothing left to correct.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { RefObject } from 'react'
import { useVirtualChat, type UseVirtualChatOptions } from '../hooks/virtualizer/useVirtualChat'

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
  el.getBoundingClientRect = () =>
    ({ top: 0, bottom: 400, left: 0, right: 390, width: 390, height: 400, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
  return { el, state }
}

/**
 * A row whose height AND viewport position are both mutable: a row above the
 * fold has a negative `top`, which is how the hook tells "above the reader"
 * from "below" it.
 */
function makeRow(box: { top: number; h: number }) {
  const node = document.createElement('div')
  Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => box.h })
  node.getBoundingClientRect = () =>
    ({
      top: box.top, bottom: box.top + box.h, left: 0, right: 390,
      width: 390, height: box.h, x: 0, y: box.top, toJSON: () => ({}),
    }) as DOMRect
  return node
}

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

describe('useVirtualChat: a reprice above a released reader is corrected in the same frame', () => {
  let origRaf: typeof requestAnimationFrame
  let origRO: typeof ResizeObserver | undefined
  let fire: ((entries: { target: Element }[]) => void) | undefined

  beforeEach(() => {
    localStorage.clear()
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
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    globalThis.requestAnimationFrame = origRaf
    if (origRO) globalThis.ResizeObserver = origRO
    fire = undefined
  })

  /**
   * Park mid-transcript with follow RELEASED, one measured row ABOVE the fold
   * and one measured row inside the viewport.
   */
  function setup() {
    const { el, state } = makeScroller({ scrollTop: 2000, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const items = mkItems(30)
    const view = renderHook(
      (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
      { initialProps: { items, sessionId: 'reprice-frame', getKey, externalScrollerRef: ref, followOutput: true } },
    )
    // Mount places a followed reader AT THE BOTTOM, so the release has to
    // happen after it: move up and let the scroll handler see an upward move
    // from the bottom, which is what actually releases follow.
    act(() => {
      state.scrollTop = 2000
      el.dispatchEvent(new Event('scroll'))
    })

    const above = { top: -900, h: 250 }
    const visible = { top: 40, h: 250 }
    const aboveRow = makeRow(above)
    const visibleRow = makeRow(visible)
    act(() => {
      view.result.current.measureRef(3)(aboveRow)
      view.result.current.measureRef(12)(visibleRow)
    })
    return { el, state, view, aboveRow, above, visibleRow, visible }
  }

  it('holds the reader when a row ABOVE the fold grows, without waiting for the height-sync debounce', () => {
    const { state, aboveRow, above, visible } = setup()
    const startedAt = state.scrollTop

    // Row 3 grows by 108 CSS px — the amplitude measured off the device. In a
    // real browser the growth extends the content above the reader, so with
    // scrollTop untouched everything they are reading slides DOWN by 108.
    const growth = 108
    above.h += growth
    state.scrollHeight += growth
    visible.top += growth

    // Exactly one observer fire, NO timers advanced: the frame the growth
    // happened in. The correction must already be here.
    act(() => { fire?.([{ target: aboveRow }]) })

    expect(state.scrollTop).toBe(startedAt + growth)
  })

  it('leaves the reader alone when the growth is BELOW them', () => {
    const { state, view, visible } = setup()
    const startedAt = state.scrollTop
    // A row below the fold: growth there extends the content after the reader,
    // which moves nothing they can see, so any scrollTop write would itself be
    // the bug.
    const below = { top: 900, h: 250 }
    const belowRow = makeRow(below)
    act(() => { view.result.current.measureRef(20)(belowRow) })
    below.h += 400
    state.scrollHeight += 400
    void visible
    act(() => { fire?.([{ target: belowRow }]) })
    expect(state.scrollTop).toBe(startedAt)
  })

  it('does not double-correct once the debounced height sync lands', () => {
    const { state, aboveRow, above, visible } = setup()
    const startedAt = state.scrollTop
    const growth = 108
    above.h += growth
    state.scrollHeight += growth
    visible.top += growth
    act(() => { fire?.([{ target: aboveRow }]) })
    const afterFrame = state.scrollTop
    // The index catches up 120ms later. The row is already where it belongs, so
    // the delta it computes is zero and nothing further may move -- otherwise
    // the reader takes the excursion in the other direction.
    act(() => { vi.advanceTimersByTime(400) })
    expect(state.scrollTop).toBe(afterFrame)
    expect(state.scrollTop).toBe(startedAt + growth)
  })
})
