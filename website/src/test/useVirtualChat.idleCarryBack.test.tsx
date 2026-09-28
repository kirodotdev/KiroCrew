import { renderHook, act } from '@testing-library/react'
import type { RefObject } from 'react'
import { useVirtualChat, type UseVirtualChatOptions } from '../hooks/virtualizer/useVirtualChat'

/**
 * REGRESSION GUARD — a still reader at the bottom is carried through content
 * settling under them, even with nothing running.
 *
 * On entry the transcript is pinned to the bottom, and then its rows keep
 * settling: a code-block stand-in swaps for the highlighted block, the top
 * spacer reprices as rows above the window measure. When content ABOVE the
 * viewport shrinks, the browser clamps scrollTop and the reader stays at the
 * bottom; when it grows back, nothing moves scrollTop -- on Chromium native
 * scroll anchoring quietly carries the reader, on WebKit nothing does. The idle
 * rule then read the gap as the reader having scrolled up and RELEASED follow,
 * so an iPhone opened every idle session a viewport or more above its end
 * (measured 1007-1119px on the phone rig with anchoring disabled).
 *
 * The discriminator is not distance but INPUT plus position: a reader who left
 * the bottom has touched the scroller since we last placed them, or is no
 * longer resting where our last write put them. These cases pin the three ways
 * that signal is fed.
 */

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number, p = 'm'): Item[] => Array.from({ length: n }, (_, i) => ({ id: `${p}${i}` }))

function mkRowNode(h: number): HTMLDivElement {
  const node = document.createElement('div')
  Object.defineProperty(node, 'offsetHeight', { configurable: true, get: () => h })
  return node
}

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
  el.getBoundingClientRect = (() => ({ top: 0, bottom: state.clientHeight, height: state.clientHeight })) as unknown as typeof el.getBoundingClientRect
  return { el, state, writes }
}

class FakeRO {
  static instances: FakeRO[] = []
  constructor(readonly cb: ResizeObserverCallback) { FakeRO.instances.push(this) }
  observe() {}
  unobserve() {}
  disconnect() {}
}

const origRO = globalThis.ResizeObserver
const origRaf = globalThis.requestAnimationFrame

beforeEach(() => {
  vi.useFakeTimers()
  FakeRO.instances = []
  globalThis.ResizeObserver = FakeRO as unknown as typeof ResizeObserver
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
})

afterEach(() => {
  vi.useRealTimers()
  globalThis.ResizeObserver = origRO as typeof ResizeObserver
  globalThis.requestAnimationFrame = origRaf
})

/** Mounts an IDLE session glued to the bottom of a settled transcript. */
function mountIdleAtBottom(key: string) {
  const { el, state, writes } = makeScroller({ scrollTop: 5000 - 700, scrollHeight: 5000, clientHeight: 700 })
  const ref: RefObject<HTMLDivElement | null> = { current: el }
  const view = renderHook(
    (props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props),
    {
      initialProps: {
        items: mkItems(60),
        sessionId: `carry-${key}`,
        getKey,
        externalScrollerRef: ref,
        followOutput: true,
        runActive: false,
      },
    },
  )
  act(() => { vi.advanceTimersByTime(300) })
  writes.length = 0
  return { view, el, ref, state, writes, bottom: () => state.scrollHeight - state.clientHeight }
}

/** A height commit: rows measure, then the debounced sync announces it. */
function landHeightCommit(view: ReturnType<typeof mountIdleAtBottom>['view'], rows = 60) {
  act(() => {
    for (let i = 0; i < rows; i++) view.result.current.measureRef(i)(mkRowNode(95))
  })
  act(() => { vi.advanceTimersByTime(400) })
}

/** Content above the viewport shrinks by `px`: the engine clamps scrollTop to
 *  the new maximum and fires a scroll event nobody wrote. */
function layoutClamp(t: ReturnType<typeof mountIdleAtBottom>, px: number) {
  t.state.scrollHeight -= px
  t.state.scrollTop = t.bottom()
  act(() => { t.el.dispatchEvent(new Event('scroll')) })
}

describe('re-engaging follow by hand', () => {
  it('scrolling back down onto the exact pixel of our last pin re-arms follow', () => {
    // The bottom the reader returns to IS the bottom we last pinned -- maximum
    // scrollTop has not moved -- so the arrival lands on our own last write to
    // the pixel. It must still read as the reader's return, not as our scroll:
    // follow re-arms, and the next turn is followed.
    const t = mountIdleAtBottom('re-engage')
    const bottom = t.bottom()
    expect(t.view.result.current.getFollow()).toBe(true)
    act(() => { t.el.dispatchEvent(new Event('wheel')) })
    t.state.scrollTop = bottom - 500
    act(() => { t.el.dispatchEvent(new Event('scroll')) })
    expect(t.view.result.current.getFollow()).toBe(false)
    act(() => { t.el.dispatchEvent(new Event('wheel')) })
    t.state.scrollTop = bottom - 250
    act(() => { t.el.dispatchEvent(new Event('scroll')) })
    t.state.scrollTop = bottom
    act(() => { t.el.dispatchEvent(new Event('scroll')) })
    expect(t.view.result.current.getFollow()).toBe(true)
  })
})

describe('idle carry-back: a still reader stays at the bottom through content settling', () => {
  it('clamp then regrow with nobody touching the scroller: carried back to the bottom', () => {
    // The entry shape measured on the phone rig: a row above the fold shrinks
    // (clamp keeps us at the bottom), then grows back (nothing moves scrollTop,
    // the reader is now the regrowth short of the end).
    const t = mountIdleAtBottom('clamp-regrow')
    layoutClamp(t, 300)
    expect(t.state.scrollTop).toBe(t.bottom())
    t.state.scrollHeight += 300
    landHeightCommit(t.view)

    expect(t.state.scrollTop).toBe(t.bottom())
    expect(t.writes).toContain(t.bottom())
  })

  it('a wheel at the bottom BEFORE the clamp does not count: the clamp re-established the bottom', () => {
    // The reader wheeled but never left the bottom (a wheel-down at the end moves
    // nothing). The clamp that follows is our re-placement, later than that
    // input, so the regrowth is still ours to close.
    const t = mountIdleAtBottom('wheel-then-clamp')
    act(() => { t.el.dispatchEvent(new Event('wheel')) })
    layoutClamp(t, 300)
    t.state.scrollHeight += 300
    landHeightCommit(t.view)

    expect(t.state.scrollTop).toBe(t.bottom())
  })

  it('a programmatic reveal whose scroll event is still pending is not dragged back', () => {
    // A search hit / pinned-prompt jump writes the scroller through its own
    // scrollTo, never through the virtualizer, and the click that asked for it
    // landed outside the scroller -- so there is no hardware input on record.
    // Its scroll event dispatches a frame later; a height commit landing in
    // that window sees follow still armed, no input, and the reader away from
    // the bottom. The position has LEFT our last write, and that is what keeps
    // this from being read as content settling: the jump must stand.
    const t = mountIdleAtBottom('reveal-race')
    const revealed = t.bottom() - 500
    t.state.scrollTop = revealed // no scroll event yet
    landHeightCommit(t.view)

    expect(t.state.scrollTop).toBe(revealed)
    expect(t.writes.filter((w) => w > revealed)).toEqual([])
  })

  it('input in the OUTGOING session does not disown the incoming session\'s entry pin', () => {
    // Scrolling around in session A, then switching to B: B's entry pin is our
    // placement and comes after that input, so a reprice settling under B's
    // bottom is still carried -- A's gesture must not outrank B's pin, or B
    // would open short of its end, which is the "every session I enter" report.
    const t = mountIdleAtBottom('switch')
    act(() => { t.el.dispatchEvent(new Event('wheel')) })
    t.state.scrollTop = t.bottom() - 800
    act(() => { t.el.dispatchEvent(new Event('scroll')) })
    // Switch sessions: the new transcript is shorter, the engine clamps, the
    // entry pin lands at the bottom.
    t.state.scrollHeight = 3000
    t.state.scrollTop = t.bottom()
    act(() => {
      t.view.rerender({
        items: mkItems(40, 'b'),
        sessionId: 'carry-switch-B',
        getKey,
        externalScrollerRef: t.ref,
        followOutput: true,
        runActive: false,
      })
    })
    act(() => { vi.advanceTimersByTime(300) })
    expect(t.state.scrollTop).toBe(t.bottom())
    t.writes.length = 0
    t.state.scrollHeight += 300
    landHeightCommit(t.view, 40)

    expect(t.state.scrollTop).toBe(t.bottom())
  })
})
