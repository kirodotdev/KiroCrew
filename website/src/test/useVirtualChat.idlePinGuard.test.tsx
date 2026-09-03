import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { RefObject } from 'react'
import { useVirtualChat, type UseVirtualChatOptions } from '../hooks/virtualizer/useVirtualChat'

/**
 * REGRESSION GUARD — with nothing running, an automatic pin must not move a
 * reader who has scrolled up.
 *
 * Reported from a phone: scrolling up a bit over a hundred pixels sprang the
 * transcript back to the bottom even with no turn in flight. Every automatic pin
 * is gated on `stick`, and `stick` is meant to be released by a scroll-up — but
 * that leaves the whole guarantee resting on one event's bookkeeping, and a
 * geometry commit landing at the wrong moment (a height settle, a viewport
 * resize, an iOS momentum tail) can find follow still armed.
 *
 * `runActive` removes the class rather than one path: follow means "keep me at
 * the end of a LIVE turn", so idle + above the bottom releases follow instead of
 * pinning. Explicit intent — slot entry, the jump-to-bottom pill, sending — goes
 * through `forcePin` and is deliberately unaffected.
 */

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

interface Geom { scrollTop: number; scrollHeight: number; clientHeight: number }

function makeScroller(initial: Geom) {
  const el = document.createElement('div')
  const state = { ...initial }
  const writes: number[] = []
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => state.scrollTop,
    set: (v: number) => { state.scrollTop = v; writes.push(v) },
  })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => state.scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => state.clientHeight })
  ;(el as unknown as { scrollTo: (o: { top: number }) => void }).scrollTo = (o) => {
    state.scrollTop = o.top
    writes.push(o.top)
  }
  return { el, state, writes }
}

class FakeRO {
  static instances: FakeRO[] = []
  constructor(readonly cb: ResizeObserverCallback) { FakeRO.instances.push(this) }
  observe() {}
  unobserve() {}
  disconnect() {}
  fire(entries: Partial<ResizeObserverEntry>[]) {
    this.cb(entries as ResizeObserverEntry[], this as unknown as ResizeObserver)
  }
}

const origRO = globalThis.ResizeObserver
const origRaf = globalThis.requestAnimationFrame

beforeEach(() => {
  vi.useFakeTimers()
  FakeRO.instances = []
  globalThis.ResizeObserver = FakeRO as unknown as typeof ResizeObserver
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
    cb(0)
    return 0
  }) as typeof requestAnimationFrame
})

afterEach(() => {
  vi.useRealTimers()
  globalThis.ResizeObserver = origRO as typeof ResizeObserver
  globalThis.requestAnimationFrame = origRaf
})

/** Mounts glued to the bottom, then grows content BELOW the fold by `growPx`.
 *
 *  No user scroll: a scroll-up releases follow on its own (correctly), which is
 *  why driving this with a gesture would pass whether or not the idle rule
 *  exists. Growth is the state where follow is still armed and the reader is no
 *  longer at the bottom — the one the idle rule alone decides. */
function mountGrownBelow(runActive: boolean, growPx: number) {
  const { el, state, writes } = makeScroller({ scrollTop: 5000 - 700, scrollHeight: 5000, clientHeight: 700 })
  const ref: RefObject<HTMLDivElement | null> = { current: el }
  const view = renderHook(
    (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
    {
      initialProps: {
        items: mkItems(60),
        sessionId: `idle-pin-${runActive}-${growPx}`,
        getKey,
        externalScrollerRef: ref,
        followOutput: true,
        runActive,
      },
    },
  )
  act(() => { vi.advanceTimersByTime(200) })
  writes.length = 0
  state.scrollHeight += growPx
  return { view, el, state, writes, bottom: () => state.scrollHeight - state.clientHeight }
}

describe('automatic pin requires a live run', () => {
  it('idle: content growing below the fold does not spring the reader down to it', () => {
    const { view, el, state, writes, bottom } = mountGrownBelow(false, 120)
    const parked = state.scrollTop
    const ro = FakeRO.instances[FakeRO.instances.length - 1]
    act(() => { ro.fire([{ target: el }]) })
    act(() => { vi.advanceTimersByTime(600) })

    expect(state.scrollTop).toBe(parked)
    expect(writes.filter((w) => Math.abs(w - bottom()) < 2)).toEqual([])
    // Released, not merely skipped — leaving follow armed would hand the same
    // yank to whichever turn starts next.
    expect(view.result.current.getFollow()).toBe(false)
  })

  it('running: the same growth IS followed', () => {
    const { view, el, state } = mountGrownBelow(true, 120)
    const parked = state.scrollTop
    const ro = FakeRO.instances[FakeRO.instances.length - 1]
    act(() => { ro.fire([{ target: el }]) })
    act(() => { vi.advanceTimersByTime(600) })

    // Mid-turn a gap must be closed, or a burst of output strands the reader.
    expect(state.scrollTop).not.toBe(parked)
    expect(view.result.current.getFollow()).toBe(true)
  })
})
