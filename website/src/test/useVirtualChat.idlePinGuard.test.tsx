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
 * Two rules share the work. POSITION: a reader resting on follow's own last
 * write is still at the end and is carried (a wheel that moved nothing is not
 * a move); a reader whose scrollTop LEFT that write while nothing runs is one
 * who left, and idle + above the bottom releases follow instead of pinning.
 * INTENT IN FLIGHT: an UPWARD input stamps before the scroll event that moves
 * the reader, so for one frame the position still reads "resting"; the pin is
 * held until that scroll event lands (`upwardIntentPending`). Downward and
 * directionless input is not held -- it moves nothing at the end. Explicit
 * intent -- slot entry, the jump-to-bottom pill, sending -- goes through
 * `forcePin` and is deliberately unaffected.
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
  it('idle: a wheel that moved nothing does not stop a still reader being carried back', () => {
    const { view, el, state, writes, bottom } = mountGrownBelow(false, 120)
    // The reader wheeled DOWN at the end (there was nothing below, so nothing
    // moved) and a directionless grab landed too. Neither is a departure: they
    // still rest on our last write to the pixel, and the gap under them is
    // content's. Reading those stamps as "the reader left" is how a reply
    // landing in an idle DM stopped following its reader.
    act(() => { el.dispatchEvent(new WheelEvent('wheel', { deltaY: 120 })) })
    act(() => { el.dispatchEvent(new Event('wheel')) })
    // Step the hardware clock past the gesture-settle window, or the RO path
    // declines to evaluate at all (a gesture in flight outranks any pin) and the
    // resting rule is never reached.
    const afterGesture = performance.now() + 1000
    const nowSpy = vi.spyOn(performance, 'now').mockReturnValue(afterGesture)
    const ro = FakeRO.instances[FakeRO.instances.length - 1]
    act(() => { ro.fire([{ target: el }]) })
    act(() => { vi.advanceTimersByTime(600) })
    nowSpy.mockRestore()

    expect(state.scrollTop).toBe(bottom())
    expect(writes.filter((w) => Math.abs(w - bottom()) < 2).length).toBeGreaterThan(0)
    expect(view.result.current.getFollow()).toBe(true)
  })

  it('idle: a reader whose scroll-up has LANDED is released, not sprung back', () => {
    const { view, el, state, writes, bottom } = mountGrownBelow(false, 120)
    // The whole gesture: upward input, then its scroll event moving the reader.
    act(() => { el.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 })) })
    const parked = state.scrollTop - 100
    state.scrollTop = parked
    act(() => { el.dispatchEvent(new Event('scroll')) })
    expect(view.result.current.getFollow()).toBe(false)
    const afterGesture = performance.now() + 1000
    const nowSpy = vi.spyOn(performance, 'now').mockReturnValue(afterGesture)
    const ro = FakeRO.instances[FakeRO.instances.length - 1]
    act(() => { ro.fire([{ target: el }]) })
    act(() => { vi.advanceTimersByTime(600) })
    nowSpy.mockRestore()

    expect(state.scrollTop).toBe(parked)
    expect(writes.filter((w) => Math.abs(w - bottom()) < 2)).toEqual([])
    expect(view.result.current.getFollow()).toBe(false)
  })

  describe('scroll intent whose scroll event has not dispatched yet (the sub-frame race)', () => {
    // Input lands BEFORE the scroll it causes. Between the two the reader still
    // sits on our last write to the pixel, so position alone reads "resting",
    // and an append committing in that same frame -- the growth layout effect,
    // the one pin path with no settle gate -- would pin them to the bottom
    // against the scroll they have just begun. An UPWARD input and a SCROLLBAR
    // grab are held until their scroll event arrives; that event then decides
    // (release if they moved up, re-baseline if they did not). Intent that never
    // scrolls expires with the settle window and the held pin is retried then.
    /** Glued to the bottom of a settled idle transcript with a scrollbar band
     *  the pointer can land on: 400px wide box, 385px client width. */
    function mountFresh(key: string) {
      const { el, state, writes } = makeScroller({ scrollTop: 5000 - 700, scrollHeight: 5000, clientHeight: 700 })
      Object.defineProperty(el, 'clientWidth', { configurable: true, get: () => 385 })
      el.getBoundingClientRect = (() => ({ top: 0, left: 0, right: 400, bottom: 700, width: 400, height: 700, x: 0, y: 0, toJSON: () => ({}) })) as unknown as typeof el.getBoundingClientRect
      const ref: RefObject<HTMLDivElement | null> = { current: el }
      const props = { items: mkItems(60), sessionId: `idle-pin-${key}`, getKey, externalScrollerRef: ref, followOutput: true, runActive: false }
      const view = renderHook((p: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(p), { initialProps: props })
      act(() => { vi.advanceTimersByTime(200) })
      writes.length = 0
      const bottom = () => state.scrollHeight - state.clientHeight
      /** A reply lands: the tail grows and a row is appended in one commit. */
      const append = (count: number) => {
        state.scrollHeight += 120
        act(() => { view.rerender({ ...props, items: mkItems(count) }) })
      }
      return { el, state, writes, view, bottom, append }
    }

    it('an UPWARD wheel followed by an append in the same frame is not pinned over', () => {
      const t = mountFresh('upward-race')
      const resting = t.state.scrollTop
      // The wheel-up: stamped now; its scroll event is still in flight.
      act(() => { t.el.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 })) })
      t.append(61)
      expect(t.state.scrollTop).toBe(resting)
      expect(t.writes.filter((w) => Math.abs(w - t.bottom()) < 2)).toEqual([])
      // Follow is HELD, not released: the scroll event owns that decision.
      expect(t.view.result.current.getFollow()).toBe(true)
      // ...and here it is: the reader moved up. Released, and the next append
      // leaves them where they are -- the expiry retry included.
      const parked = resting - 100
      t.state.scrollTop = parked
      act(() => { t.el.dispatchEvent(new Event('scroll')) })
      expect(t.view.result.current.getFollow()).toBe(false)
      act(() => { vi.advanceTimersByTime(300) })
      t.append(62)
      act(() => { vi.advanceTimersByTime(300) })
      expect(t.state.scrollTop).toBe(parked)
    })

    it('the same frame after a DOWNWARD wheel is pinned: a no-op input at the end is ignored', () => {
      const t = mountFresh('downward-noop')
      act(() => { t.el.dispatchEvent(new WheelEvent('wheel', { deltaY: 120 })) })
      t.append(61)
      expect(t.state.scrollTop).toBe(t.bottom())
      expect(t.view.result.current.getFollow()).toBe(true)
    })

    it('upward intent that never scrolls expires with the settle window and the held pin is retried', () => {
      // A transcript shorter than its viewport cannot scroll: the wheel-up
      // produces no scroll event. The hold must not park follow, and the
      // append it held must not be lost: at expiry the pin runs again.
      const t = mountFresh('upward-expiry')
      act(() => { t.el.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 })) })
      t.append(61)
      expect(t.state.scrollTop).not.toBe(t.bottom())
      act(() => { vi.advanceTimersByTime(300) })
      expect(t.state.scrollTop).toBe(t.bottom())
      expect(t.view.result.current.getFollow()).toBe(true)
    })

    it('a SCROLLBAR grab is held like an upward input until its first scroll event', () => {
      // pointerdown on the scrollbar band (x=392 of a 400px box whose client
      // width is 385) stamps a directionless grab: the drag that follows scrolls
      // with no delta or key to name the direction, and its first movement may
      // be upward. An append in the gap must not pin over it.
      const t = mountFresh('grab-race')
      const resting = t.state.scrollTop
      act(() => { t.el.dispatchEvent(new MouseEvent('pointerdown', { clientX: 392, clientY: 300 })) })
      t.append(61)
      expect(t.state.scrollTop).toBe(resting)
      expect(t.view.result.current.getFollow()).toBe(true)
      // The drag's first scroll event: upward. Released, and the retry that
      // fires at expiry leaves the reader where the drag put them.
      const parked = resting - 150
      t.state.scrollTop = parked
      act(() => { t.el.dispatchEvent(new Event('scroll')) })
      expect(t.view.result.current.getFollow()).toBe(false)
      act(() => { vi.advanceTimersByTime(300) })
      expect(t.state.scrollTop).toBe(parked)
    })

    it('a scrollbar grab that never drags releases the hold: the append is pinned at expiry', () => {
      const t = mountFresh('grab-click')
      act(() => { t.el.dispatchEvent(new MouseEvent('pointerdown', { clientX: 392, clientY: 300 })) })
      t.append(61)
      expect(t.state.scrollTop).not.toBe(t.bottom())
      act(() => { vi.advanceTimersByTime(300) })
      expect(t.state.scrollTop).toBe(t.bottom())
      expect(t.view.result.current.getFollow()).toBe(true)
    })

    it('a pointer on the transcript itself (a click, a text selection) holds nothing', () => {
      // Same frame, but the pointer landed on a message, not the scrollbar: it
      // moves nothing, so the append is followed at once.
      const t = mountFresh('click-noop')
      act(() => { t.el.dispatchEvent(new MouseEvent('pointerdown', { clientX: 120, clientY: 300 })) })
      t.append(61)
      expect(t.state.scrollTop).toBe(t.bottom())
      expect(t.view.result.current.getFollow()).toBe(true)
    })
  })

  it('idle: the same growth under a reader who has NOT moved is carried back', () => {
    // No input since the entry pin, resting on it to the pixel: every pixel of
    // the gap is content settling, and the reader is kept at the end. This is
    // what native scroll anchoring did silently on Chromium; WebKit has none,
    // and releasing here is how a phone opened idle sessions above their end.
    const { view, el, state, bottom } = mountGrownBelow(false, 120)
    const ro = FakeRO.instances[FakeRO.instances.length - 1]
    act(() => { ro.fire([{ target: el }]) })
    act(() => { vi.advanceTimersByTime(600) })

    expect(state.scrollTop).toBe(bottom())
    expect(view.result.current.getFollow()).toBe(true)
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
