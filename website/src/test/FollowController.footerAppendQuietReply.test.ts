// REGRESSION GUARD — kirodotdev/KiroCrew#17320.
//
// A chat reply's per-message footer (model name, clock, duration — ~108px) is
// gated OFF while the reply streams (ChatPage `showFooter`: `if (isStreaming)
// return false`) and flips ON the instant the turn closes. So it appends BELOW
// the last assistant row AFTER the run ends. In the same beat the composer dock
// shrinks (its running chrome — progress bars, the stop affordance — leaves),
// dropping the scroller's `paddingBottom` reserve; the engine clamps scrollTop
// DOWN to the smaller max. That clamp lands the reader a few px below follow's
// last write, and the footer-grow ResizeObserver fire beats the clamp's scroll
// event, so evaluateAutoPin's resting rule misses and the idle-release branch
// dropped follow — the footer stranded under the dock, nothing to re-pin it.
//
// Pin: a tail APPEND under a still, followed reader clamped at/below our write
// re-baselines onto the clamped position (the "resting on a re-baselined write"
// contract) so the append is carried. A reader who actually scrolled is held
// out by the pinSuppressedNow gate (their input stamped lastUserScrollAtRef).
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { RefObject } from 'react'
import { useVirtualChat, type UseVirtualChatOptions } from '../hooks/virtualizer/useVirtualChat'

interface Geom { scrollTop: number; scrollHeight: number; clientHeight: number }
function makeScroller(initial: Geom) {
  const el = document.createElement('div')
  const state: Geom = { ...initial }
  const writes: number[] = []
  Object.defineProperty(el, 'scrollTop', { configurable: true, get: () => state.scrollTop, set: (v: number) => { state.scrollTop = v; writes.push(v) } })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => state.scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => state.clientHeight })
  ;(el as unknown as { scrollTo: (o: { top: number }) => void }).scrollTo = (o) => { state.scrollTop = o.top; writes.push(o.top) }
  el.getBoundingClientRect = () => ({ top: 0, bottom: 400, left: 0, right: 390, width: 390, height: 400, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
  return { el, state, writes }
}
function makeRow(h: { v: number }) {
  const node = document.createElement('div')
  Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => h.v })
  node.getBoundingClientRect = () => ({ top: 0, bottom: h.v, left: 0, right: 390, width: 390, height: h.v, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
  return node
}
interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

describe('useVirtualChat: #17320 — footer appended after a reply goes quiet is followed', () => {
  let fire: ((e: { target: Element }[]) => void) | undefined
  let origRaf: typeof requestAnimationFrame
  let origRO: typeof ResizeObserver | undefined
  beforeEach(() => {
    localStorage.clear()
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
    origRO = globalThis.ResizeObserver
    globalThis.ResizeObserver = class { constructor(cb: ResizeObserverCallback) { fire = (e) => cb(e as unknown as ResizeObserverEntry[], this as unknown as ResizeObserver) } observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver
    vi.useFakeTimers()
  })
  afterEach(() => { vi.useRealTimers(); globalThis.requestAnimationFrame = origRaf; if (origRO) globalThis.ResizeObserver = origRO; fire = undefined })

  function mountQuietAfterStream(sessionId: string) {
    const { el, state, writes } = makeScroller({ scrollTop: 4600, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const items = mkItems(30)
    const lastIdx = items.length - 1
    const view = renderHook(
      (p: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(p),
      { initialProps: { items, sessionId, getKey, externalScrollerRef: ref, followOutput: true, streamingIndex: lastIdx, running: true } as UseVirtualChatOptions<Item> },
    )
    const tailH = { v: 250 }
    const tailRow = makeRow(tailH)
    act(() => { view.result.current.measureRef(3)(makeRow({ v: 250 })); view.result.current.measureRef(lastIdx)(tailRow) })
    state.scrollTop = 4600
    // Turn closes: streamingIndex -> undefined, run no longer active.
    act(() => { view.rerender({ items, sessionId, getKey, externalScrollerRef: ref, followOutput: true, streamingIndex: undefined, running: false } as UseVirtualChatOptions<Item>) })
    act(() => { vi.advanceTimersByTime(500) })
    return { el, state, writes, view, tailH, tailRow }
  }

  it('a tail footer appended after the dock-shrink clamp carries the reader to the new bottom', () => {
    const { state, writes, view, tailH, tailRow } = mountQuietAfterStream('iss-17320-fix')
    // Dock shrinks (padding reserve drops ~60): engine clamps scrollTop 4600->4540.
    state.scrollHeight = 4940
    state.scrollTop = 4540
    writes.length = 0
    // Footer renders: tail row grows +108, bottom is now 5048-400 = 4648.
    tailH.v = 358
    state.scrollHeight = 5048
    act(() => { fire?.([{ target: tailRow }]) })
    act(() => { vi.advanceTimersByTime(600) })
    const bottom = state.scrollHeight - state.clientHeight
    expect(state.scrollTop).toBe(bottom)
    expect(view.result.current.getFollow()).toBe(true)
  })

  it('a reader who scrolled UP before the footer is NOT yanked to it (contract preserved)', () => {
    const { el, state, view, tailH, tailRow } = mountQuietAfterStream('iss-17320-scrollup')
    // The reader wheels up and the scroll lands well above the bottom.
    act(() => { el.dispatchEvent(new WheelEvent('wheel', { deltaY: -300 })) })
    state.scrollTop = 4100
    act(() => { el.dispatchEvent(new Event('scroll')) })
    // Footer renders.
    tailH.v = 358
    state.scrollHeight = 5108
    act(() => { fire?.([{ target: tailRow }]) })
    act(() => { vi.advanceTimersByTime(600) })
    // They keep their place; the footer is theirs to scroll to.
    expect(state.scrollTop).toBe(4100)
    expect(view.result.current.getFollow()).toBe(false)
  })
})
